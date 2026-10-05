"""Telegram-бот платформы: тот же чат с агентами и RAG-базами, что и на сайте.

POST /api/telegram/webhook — приём апдейтов; подлинность подтверждается
       секретом из AIOS_TG_WEBHOOK_SECRET (заголовок X-Telegram-Bot-Api-Secret-Token,
       который Telegram присылает только созданному через setWebhook с secret_token);
POST /api/telegram/webhook/setup — [админ] зарегистрировать вебхук по
       AIOS_PUBLIC_BASE_URL (по сути обёртка над setWebhook);
GET  /api/telegram/status — [админ] диагностика: getMe, getWebhookInfo, число привязок.

Личный чат с ботом работает от имени учётной записи платформы после привязки:
/start в боте выдаёт одноразовый код, пользователь подтверждает его на платформе
(«Настройки» → Telegram или deep-link settings.html#tg-link=КОД). Пароль через
бота не вводится никогда. Групповые чаты — следующий шаг (MVP: только личные).

Ход выполняется тем же кодом, что и веб-чат: run_agent_loop с инструментами,
RAG (search_corpus), кабинетным контекстом и учётом токенов; ответ финальным
сообщением (Telegram не нуждается в SSE). Состояние личного чата — закреплённая
AIOS-сессия + выбранный агент (skill_ids) и RAG-база (rag_ids), см.
api/runtime/telegram_store.py. Вебхук отвечает 200 мгновенно, ход дорабатывает
в фоновой задаче (инструменты с RAG-поиском занимают минуты), «печатает…»
поддерживается циклом sendChatAction.

Для локальной разработки без публичного HTTPS — long polling: AIOS_TG_POLLING=1
(взаимоисключимо с вебхуком: Telegram вернёт 409, пока установлен вебхук).
"""

import asyncio
import contextlib
import hmac
import html
import json
import logging
import re
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..runtime import approvals, chart_png, datarepo, skill_drafts, uploads, users
from ..runtime import rag as rag_module
from ..runtime.cabinet import get_store as get_cabinet_store, full_name as cabinet_full_name
from ..runtime.config import get_settings
from ..runtime.llm_settings import effective_config, effective_config_for
from ..runtime.providers import FallbackProvider
from ..runtime.telegram_store import TelegramStore
from ..runtime.tg_client import TelegramBot, TelegramError, telegram_chunks
from .auth import require_admin
from .chat import get_provider, get_registry, get_store

logger = logging.getLogger("aios.telegram")
if not logger.handlers:
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)

router = APIRouter(prefix="/api")

# MVP — личные чаты; групповые (общая сессия, @упоминания) — отдельный шаг
PRIVATE_CHAT = "private"
PAGE_SIZE = 8  # кнопок агентов/баз на страницу inline-клавиатуры
AGENT_NAMES_LIMIT = 48  # кнопка Telegram не длиннее 64 символов
USAGE_TTL = 300.0  # кэш getMe для показа @username бота в «Кабинете»

ALLOWED_UPDATES = ["message", "callback_query"]

# Меню команд Telegram (кнопка «Меню»). /start в меню не выносим; агенты и базы
# — не команды, а инструменты: их выбирают кнопками постоянной клавиатуры ниже
# (сами /agents и /base продолжают работать — на случай старой привычки).
BOT_COMMANDS = [
    {"command": "new", "description": "Начать новый диалог"},
    {"command": "status", "description": "Текущие настройки чата"},
    {"command": "help", "description": "Справка и команды"},
    {"command": "unlink", "description": "Отвязать Telegram от аккаунта"},
]

# Постоянная клавиатура «Меню» — инструменты платформы всегда под рукой;
# нажатие приходит обычным сообщением с этим текстом.
MENU_AGENTS = "🤖 Вызвать агента"
MENU_BASES = "📚 База знаний"
MENU_NEW = "✨ Новый диалог"
MENU_STATUS = "ℹ️ Статус"
MENU_COMMAND_OF = {
    MENU_AGENTS: "agents",
    MENU_BASES: "base",
    MENU_NEW: "new",
    MENU_STATUS: "status",
}


def _menu_reply_keyboard() -> dict:
    """Reply-клавиатура с инструментами: закрепляется внизу чата."""
    return {
        "keyboard": [
            [{"text": MENU_AGENTS}, {"text": MENU_BASES}],
            [{"text": MENU_NEW}, {"text": MENU_STATUS}],
        ],
        "resize_keyboard": True,
    }


async def register_bot_commands(bot: TelegramBot) -> None:
    """Регистрирует меню команд при старте; сбой не мешает работе бота."""
    try:
        await bot.call("setMyCommands", commands=BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram setMyCommands: %s", exc)


@lru_cache(maxsize=1)
def get_telegram_store() -> TelegramStore:
    """Синглтон хранилища Telegram-интеграции; в тестах сбрасывается cache_clear()."""
    return TelegramStore(get_settings().runtime_dir / "telegram")


def _bot() -> TelegramBot:
    """Клиент бота текущего процесса; без токена — выключен."""
    bot = TelegramBot.from_settings()
    if bot is None:
        raise HTTPException(status_code=503, detail="Бот не настроен (AIOS_TG_BOT_TOKEN)")
    return bot


# Сильные ссылки на задачи обработки апдейтов (см. замечание asyncio к create_task).
_UPDATE_TASKS: set[asyncio.Task] = set()
# Личные чаты, где ход уже выполняется: параллельные сообщения в один чат
# попали бы в одну сессию посреди цикла «модель ↔ инструменты».
_RUNNING_CHATS: set[str] = set()
_PRESSED_CHOICES: set[tuple] = set()  # (chat, message_id) кнопок быстрых ответов, уже нажатых


# ---------------------------------------------------------------------------
# Вебхук
# ---------------------------------------------------------------------------


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Апдейты Telegram. Отвечаем 200 сразу: Telegram ждёт ответ вебхука и
    при таймауте повторяет апдейт — обработка всегда уходит в фоновую задачу."""
    settings = get_settings()
    if not settings.tg_bot_token:
        raise HTTPException(status_code=503, detail="Бот не настроен (AIOS_TG_BOT_TOKEN)")
    secret = settings.tg_webhook_secret
    if not secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, secret
    ):
        # чужой запрос не отличаем от неправильного секрета — оба 403
        raise HTTPException(status_code=403, detail="Неверный секрет вебхука")
    update = await request.json()
    task = asyncio.create_task(handle_update(update))
    _UPDATE_TASKS.add(task)
    task.add_done_callback(_UPDATE_TASKS.discard)
    return {"ok": True}


@router.post("/telegram/webhook/setup")
async def setup_webhook(_: dict = Depends(require_admin)):
    """Регистрация вебхука по AIOS_PUBLIC_BASE_URL (замена ручного setWebhook)."""
    settings = get_settings()
    if not settings.public_base_url:
        raise HTTPException(status_code=503, detail="Не задан AIOS_PUBLIC_BASE_URL")
    if not settings.tg_webhook_secret:
        raise HTTPException(status_code=503, detail="Не задан AIOS_TG_WEBHOOK_SECRET")
    url = f"{settings.public_base_url.rstrip('/')}/api/telegram/webhook"
    try:
        await _bot().call(
            "setWebhook",
            url=url,
            secret_token=settings.tg_webhook_secret,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        info = await _bot().call("getWebhookInfo")
    except TelegramError as exc:
        # чаще всего: хостинг режет исходящие к api.telegram.org — см. AIOS_TG_PROXY
        raise HTTPException(status_code=502, detail=f"Telegram API недоступен: {exc}")
    return {"ok": True, "url": url, "webhook_info": info}


@router.get("/telegram/status")
async def telegram_status(_: dict = Depends(require_admin)):
    """Диагностика для админ-панели: бот, вебхук, привязки."""
    bot = _bot()
    try:
        me = await bot.call("getMe")
        info = await bot.call("getWebhookInfo")
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=f"Telegram API недоступен: {exc}")
    return {
        "me": me,
        "webhook_info": info,
        "links": len(get_telegram_store()._read("links.json").get("items") or {}),
    }


_me_cache: dict = {"username": "", "checked_at": 0.0}


async def bot_username() -> str:
    """@username бота (для «Кабинета»); кэш на 5 минут, недоступен API — ''."""
    if time.time() - _me_cache["checked_at"] > USAGE_TTL:
        try:
            me = await _bot().call("getMe")
            _me_cache["username"] = str(me.get("username") or "")
        except (TelegramError, HTTPException):
            _me_cache["username"] = ""
        _me_cache["checked_at"] = time.time()
    return _me_cache["username"]


# ---------------------------------------------------------------------------
# Разбор апдейтов
# ---------------------------------------------------------------------------


def _command_of(text: str) -> str:
    """/start@BotName → start (в личном чате суффикса не бывает — снимаем на всякий случай)."""
    first = (text or "").split(maxsplit=1)[0] if (text or "").strip() else ""
    return first.lstrip("/").split("@", 1)[0].lower()


async def handle_update(update: dict, bot: TelegramBot | None = None) -> None:
    """Один апдейт Telegram: команда, текст или нажатие inline-кнопки."""
    if bot is None:
        bot = TelegramBot.from_settings()
        if bot is None:
            return
    if update.get("message"):
        await _handle_message(bot, update["message"])
    elif update.get("callback_query"):
        await _handle_callback(bot, update["callback_query"])


async def _handle_message(bot: TelegramBot, message: dict) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != PRIVATE_CHAT:
        return  # групповые чаты — вне MVP
    chat_id = chat.get("id")
    if chat_id is None:
        return
    sender = message.get("from") or {}
    tg_user_id = sender.get("id")
    if tg_user_id is None:
        return

    text = (message.get("text") or "").strip()
    if not text:
        await _safe_send(bot, chat_id, "Пока понимаю только текстовые сообщения.")
        return

    cmd = _command_of(text) if text.startswith("/") else ""
    if not cmd:
        cmd = MENU_COMMAND_OF.get(text, "")  # кнопка постоянной клавиатуры
    try:
        if cmd in ("start", "help"):
            await _cmd_start(bot, chat_id, sender, cmd == "help")
        elif cmd == "new":
            await _cmd_new(bot, chat_id)
        elif cmd == "agents":
            await _send_agents_keyboard(bot, chat_id, tg_user_id, offset=0)
        elif cmd == "base":
            await _send_bases_keyboard(bot, chat_id, tg_user_id, offset=0)
        elif cmd == "status":
            await _cmd_status(bot, chat_id, tg_user_id)
        elif cmd == "unlink":
            await _cmd_unlink(bot, chat_id, tg_user_id)
        elif cmd:
            await _safe_send(bot, chat_id, "Неизвестная команда. Напишите /help.")
        else:
            await _run_turn(bot, chat_id, tg_user_id, text[:8000])
    except TelegramError as exc:
        logger.warning("Telegram API не принял ответ: %s", exc)
    except Exception:  # noqa: BLE001 — апдейт не должен ронять обработку
        logger.exception("Ошибка обработки сообщения чата %s", chat_id)
        with contextlib.suppress(TelegramError):
            await _safe_send(bot, chat_id, "Внутренняя ошибка, попробуйте ещё раз.")


async def _safe_send(bot: TelegramBot, chat_id, text: str, reply_markup: dict | None = None,
                     markdown: bool = False):
    if markdown:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup, markdown=True)
    return await bot.send_message(chat_id, text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def _linked_name(tg_user_id: int) -> tuple[str | None, dict | None]:
    """Учётная запись платформы, к которой привязан Telegram: (user, link)."""
    link = get_telegram_store().link_of(tg_user_id)
    if not link:
        return None, None
    user = users.UserStore(get_settings().runtime_dir / "users").get(link["user_id"])
    return (user, link) if user else (None, link)


def _display_name(user: dict) -> str:
    """Имя для обращений: ФИО из профиля «Кабинета» → имя учётной записи → логин."""
    profile = get_cabinet_store().get_profile(user["id"])
    return (
        cabinet_full_name(profile)
        or str(user.get("name") or "")
        or str(user.get("login") or "")
    )


def _start_greeting(user: dict, _link: dict) -> str:
    login = html.escape(str(user.get("login") or ""))
    name = html.escape(_display_name(user))
    # имя и логин совпадают (анкета не заполнена) — логин второй раз не дублируем
    who = f"<b>{name}</b> ({login})" if name != login else f"<b>{name}</b>"
    return (
        "👋 Здравствуйте!\n\n"
        f"Бот работает от вашей учётной записи AIOS: {who}.\n"
        "Здесь тот же чат, что и на сайте: агенты, базы знаний и инструменты платформы.\n\n"
        "<b>Меню — кнопки внизу чата</b>\n"
        f"{MENU_AGENTS} — выбрать агента (можно и не выбирать: чат подберёт сам)\n"
        f"{MENU_BASES} — подключить базу знаний\n"
        f"{MENU_NEW} — начать новый диалог\n\n"
        "<b>Справка</b>\n"
        f"{MENU_STATUS} — текущие настройки\n"
        "/help — все команды\n\n"
        "Просто напишите вопрос — ответ придёт, когда будет готов."
    )


async def _cmd_start(bot: TelegramBot, chat_id, sender: dict, is_help: bool) -> None:
    tg_user_id = sender.get("id")
    user, _link = await _linked_name(tg_user_id)
    if user:
        await _safe_send(
            bot, chat_id, _start_greeting(user, _link or {}),
            reply_markup=_menu_reply_keyboard(),
        )
        return
    if is_help:
        await _safe_send(
            bot, chat_id, _help_text(), reply_markup=_menu_reply_keyboard()
        )
        return
    # не привязан: одноразовый код + deep-link в «Настройки»
    code = get_telegram_store().issue_code(
        tg_user_id, username=str(sender.get("username") or ""), first_name=str(sender.get("first_name") or "")
    )
    base = get_settings().public_base_url.rstrip("/")
    lines = [
        "👋 Здравствуйте! Это бот платформы <b>AIOS Технологического института</b>: "
        "тот же чат с агентами и базами знаний, что и на сайте.",
        "",
        "Работа начинается с привязки аккаунта:",
        "1️⃣ Нажмите кнопку ниже (вы должны быть залогинены на платформе)",
        "2️⃣ Подтвердите привязку в открывшихся «Настройках»",
        "3️⃣ Вернитесь в этот чат и напишите вопрос",
        "",
        f"Код привязки: <code>{code}</code> — действует 15 минут. "
        "Кнопка не сработала? «Настройки» → Telegram → введите код вручную.",
    ]
    reply_markup = None
    if base:
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🔗 Привязать аккаунт в AIOS", "url": f"{base}/settings.html#tg-link={code}"}]
            ]
        }
    await _safe_send(bot, chat_id, "\n".join(lines), reply_markup=reply_markup)


def _help_text() -> str:
    return (
        "<b>Меню — кнопки внизу чата</b>\n"
        f"{MENU_AGENTS} — вызвать агента (агент стартует в новом диалоге)\n"
        f"{MENU_BASES} — подключить RAG-базу знаний\n"
        f"{MENU_NEW} — начать новый диалог (агент и база сохраняются)\n"
        f"{MENU_STATUS} — текущие привязка, агент и база\n\n"
        "<b>Команды</b>\n"
        "/help — эта справка\n"
        "/unlink — отвязать Telegram от учётной записи\n\n"
        "Любое сообщение уходит агентному циклу платформы: инструменты, "
        "поиск по базам знаний (search_corpus), кабинетный контекст и учёт "
        "токенов — как в веб-чате. Пока агент работает — «печатает…»; "
        "ответ приходит, когда готов."
    )


async def _cmd_new(bot: TelegramBot, chat_id) -> None:
    get_telegram_store().clear_chat_session(chat_id)
    await _safe_send(bot, chat_id, "✨ Начат новый диалог. Агент и база знаний сохранены.")


async def _cmd_status(bot: TelegramBot, chat_id, tg_user_id: int) -> None:
    user, link = await _linked_name(tg_user_id)
    if not user:
        await _safe_send(bot, chat_id, "Привязка не выполнена. Отправьте /start.")
        return
    state = get_telegram_store().chat_state(chat_id)
    agent_name = "не выбран — обычный чат"
    if state["skill_ids"]:
        meta = datarepo.skill_meta(state["skill_ids"][0]) or {}
        agent_name = str(meta.get("name") or state["skill_ids"][0])
    base_name = "не подключена"
    if state["rag_ids"]:
        card = rag_module.get_store().find(state["rag_ids"][0])
        base_name = str((card or {}).get("name") or state["rag_ids"][0])
    session = get_store().get(state["session_id"]) if state["session_id"] else None
    dialogs = f"{len(session.get('messages') or [])} сообщений" if session else "пусто"
    await _safe_send(
        bot,
        chat_id,
        "<b>Текущие настройки чата</b>\n"
        f"👤 Аккаунт: <b>{html.escape(str(user.get('login') or ''))}</b>\n"
        f"✈️ Telegram: @{html.escape(str((link or {}).get('username') or ''))}\n"
        f"🤖 Агент: {html.escape(agent_name)}\n"
        f"📚 База знаний: {html.escape(base_name)}\n"
        f"💬 Диалог: {dialogs}",
    )


async def _cmd_unlink(bot: TelegramBot, chat_id, tg_user_id: int) -> None:
    user_id = get_telegram_store().unbind_tg(tg_user_id)
    if user_id is None:
        await _safe_send(bot, chat_id, "Привязка не найдена.")
        return
    # чат оставляем, но сессия отвязанной учётки продолжаться не должна
    get_telegram_store().clear_chat_session(chat_id)
    await _safe_send(
        bot,
        chat_id,
        "Telegram отвязан от учётной записи. Привязать заново — /start.",
    )


# ---------------------------------------------------------------------------
# Каталоги: агенты и RAG-базы (inline-клавиатуры с пагинацией)
# ---------------------------------------------------------------------------


def _truncate(name: str, limit: int = AGENT_NAMES_LIMIT) -> str:
    name = (name or "").strip()
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _paged_keyboard(
    rows: list[dict], offset: int, nav_prefix: str, extra: list[list[dict]] | None = None
) -> dict:
    """Одна кнопка в строке, навигация ◀️/▶️ и служебные строки снизу."""
    page = rows[offset : offset + PAGE_SIZE]
    # каждая кнопка — своя строка: в общем ряду Telegram сплющит их в
    # квадратики и обрежет названия («Ко…й» вместо «Конструктор онтологий»)
    kb = [[{"text": r["text"], "callback_data": r["callback_data"]}] for r in page]
    nav = []
    if offset > 0:
        nav.append({"text": "◀️ Назад", "callback_data": f"{nav_prefix}:{max(0, offset - PAGE_SIZE)}"})
    if offset + PAGE_SIZE < len(rows):
        nav.append({"text": "Вперёд ▶️", "callback_data": f"{nav_prefix}:{offset + PAGE_SIZE}"})
    if nav:
        kb.append(nav)
    for row in extra or []:
        kb.append(row)
    return {"inline_keyboard": kb}


async def _send_agents_keyboard(bot: TelegramBot, chat_id, tg_user_id: int, offset: int) -> None:
    user, _link = await _linked_name(tg_user_id)
    if not user:
        await _safe_send(bot, chat_id, "Сначала привяжите аккаунт: /start.")
        return
    # admin-only агенты («Конструктор моделей») — только связанному админу
    is_admin = user.get("role") == users.ROLE_ADMIN
    rows = [
        {"text": _truncate(str(skill.get("name") or skill.get("id"))), "callback_data": f"ag:{skill['id']}"}
        for skill in datarepo.skills()
        if skill.get("id") and (is_admin or not datarepo.is_admin_only(skill))
    ]
    keyboard = _paged_keyboard(
        rows,
        offset,
        "agp",
        extra=[[{"text": "💬 Обычный чат (без агента)", "callback_data": "ag:none"}]],
    )
    await _safe_send(
        bot,
        chat_id,
        f"🤖 <b>Вызов агента</b> — доступно: {len(rows)}\n"
        "Выберите агента кнопкой — он начнёт работать в новом диалоге:",
        reply_markup=keyboard,
    )


def _rag_options(user_id: str | None) -> list[dict]:
    """Подключаемые базы: опубликованный каталог + черновики владельца.

    Одиночная база в чате (как в веб-чате, req.rag_ids max 1); admin_only
    видим только администраторы.
    """
    is_admin = users.is_admin_user(user_id)
    items: list[dict] = []
    seen: set[str] = set()
    for entry in datarepo.rag_bases():
        rid = str(entry.get("id") or "")
        if not rid or rid in seen or entry.get("status") != "Активна":
            continue
        if entry.get("admin_only") and not is_admin:
            continue
        seen.add(rid)
        items.append({"id": rid, "name": str(entry.get("name") or rid)})
    if user_id:
        for card in rag_module.get_store().list(user_id):
            rid = str(card.get("id") or "")
            if not rid or rid in seen or card.get("status") != rag_module.STATUS_ACTIVE:
                continue
            seen.add(rid)
            items.append({"id": rid, "name": str(card.get("name") or rid)})
    return items


async def _send_bases_keyboard(bot: TelegramBot, chat_id, tg_user_id: int, offset: int) -> None:
    user, _link = await _linked_name(tg_user_id)
    if not user:
        await _safe_send(bot, chat_id, "Сначала привяжите аккаунт: /start.")
        return
    rows = [
        {"text": _truncate(opt["name"]), "callback_data": f"rg:{opt['id']}"}
        for opt in _rag_options(user["id"])
    ]
    keyboard = _paged_keyboard(
        rows,
        offset,
        "rgp",
        extra=[[{"text": "🚫 Без базы знаний", "callback_data": "rg:none"}]],
    )
    await _safe_send(
        bot,
        chat_id,
        f"📚 <b>База знаний</b> — доступно: {len(rows)}\n"
        "Выберите базу для текущего диалога:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Нажатия inline-кнопок
# ---------------------------------------------------------------------------


async def _handle_callback(bot: TelegramBot, callback: dict) -> None:
    cb_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    sender = callback.get("from") or {}
    tg_user_id = sender.get("id")

    async def answer(text: str = "") -> None:
        await bot.answer_callback(cb_id, text)

    try:
        if data.startswith("ap:"):
            await _callback_approval(bot, chat_id, message, cb_id, tg_user_id, data)
        elif data.startswith("agp:") or data.startswith("rgp:"):
            offset = _int_of(data.split(":", 1)[1])
            if offset is None:
                await answer()
                return
            if data.startswith("agp:"):
                await _send_agents_keyboard(bot, chat_id, tg_user_id, offset)
            else:
                await _send_bases_keyboard(bot, chat_id, tg_user_id, offset)
            await answer()
        elif data.startswith("ag:"):
            await _callback_agent(bot, chat_id, cb_id, tg_user_id, data[3:])
        elif data.startswith("rg:"):
            await _callback_rag(bot, chat_id, cb_id, tg_user_id, data[3:])
        elif data.startswith("ch:"):
            await _callback_choice(bot, chat_id, message, cb_id, tg_user_id, data[3:])
        else:
            await answer()
    except TelegramError as exc:
        logger.warning("Telegram API не принял ответ на кнопку: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка обработки кнопки %r", data)
        await answer("Ошибка, попробуйте ещё раз")


def _int_of(value: str) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


async def _callback_agent(bot: TelegramBot, chat_id, cb_id: str, tg_user_id: int, skill_id: str) -> None:
    """Выбор агента: фиксировался только после привязки, всегда новый диалог
    (паритет с веб-чатом: смена skill_ids — новая сессия)."""
    user, _link = await _linked_name(tg_user_id)
    if not user:
        await bot.answer_callback(cb_id, "Сначала привяжите аккаунт: /start")
        return
    if skill_id == "none":
        get_telegram_store().set_chat_state(chat_id, skill_ids=[], session_id="")
        await bot.answer_callback(cb_id, "Обычный чат")
        await _safe_send(bot, chat_id, "Обычный чат (агент не выбран): начат новый диалог.")
        return
    if not skill_drafts.skill_available(
        skill_id, user["id"], is_admin=user.get("role") == users.ROLE_ADMIN
    ):
        await bot.answer_callback(cb_id, "Агент недоступен")
        return
    meta = datarepo.skill_meta(skill_id) or {}
    name = str(meta.get("name") or skill_id)
    get_telegram_store().set_chat_state(chat_id, skill_ids=[skill_id], session_id="")
    await bot.answer_callback(cb_id, f"Агент: {name}")
    await _safe_send(bot, chat_id, f"🤖 Агент <b>{html.escape(name)}</b> подключён — начат новый диалог.")


async def _callback_rag(bot: TelegramBot, chat_id, cb_id: str, tg_user_id: int, rag_id: str) -> None:
    user, _link = await _linked_name(tg_user_id)
    if not user:
        await bot.answer_callback(cb_id, "Сначала привяжите аккаунт: /start")
        return
    if rag_id == "none":
        get_telegram_store().set_chat_state(chat_id, rag_ids=[])
        await bot.answer_callback(cb_id, "Без базы")
        await _safe_send(bot, chat_id, "База знаний отключена. Привязанная к агенту база подключится сама, если есть.")
        return
    if not any(o["id"] == rag_id for o in _rag_options(user["id"])):
        await bot.answer_callback(cb_id, "База недоступна")
        return
    card = rag_module.get_store().find(rag_id)
    name = str((card or {}).get("name") or rag_id)
    get_telegram_store().set_chat_state(chat_id, rag_ids=[rag_id])
    await bot.answer_callback(cb_id, f"База: {name}")
    await _safe_send(bot, chat_id, f"📚 База знаний <b>{html.escape(name)}</b> подключена к диалогу.")


async def _callback_choice(bot: TelegramBot, chat_id, message: dict, cb_id: str,
                           tg_user_id: int, index_raw: str) -> None:
    """Кнопка быстрых ответов: вариант уходит как обычное сообщение пользователя
    (тот же протокол, что в веб-чате), полный ход агентному циклу."""
    user, _link = await _linked_name(tg_user_id)
    if not user:
        await bot.answer_callback(cb_id, "Сначала привяжите аккаунт: /start")
        return
    index = _int_of(index_raw)
    options = get_telegram_store().chat_state(chat_id)["choices"]
    if index is None or index >= len(options):
        await bot.answer_callback(cb_id, "Варианты устарели — напишите ответ текстом")
        return
    message_id = message.get("message_id")
    pressed = (str(chat_id), message_id)
    if message_id is not None and pressed in _PRESSED_CHOICES:
        await bot.answer_callback(cb_id, "Вариант уже выбран")
        return
    if str(chat_id) in _RUNNING_CHATS:
        await bot.answer_callback(cb_id, "Прошлый ответ ещё готовится")
        return
    if len(_PRESSED_CHOICES) > 500:
        _PRESSED_CHOICES.clear()
    if message_id is not None:
        _PRESSED_CHOICES.add(pressed)
    await bot.answer_callback(cb_id, options[index])
    with contextlib.suppress(Exception):
        await bot.edit_reply_markup(
            chat_id, message_id,
            {"inline_keyboard": [[{"text": _truncate("✅ Выбрано: " + options[index]), "callback_data": "noop"}]]},
        )
    await _run_turn(bot, chat_id, tg_user_id, options[index])


async def _callback_approval(bot: TelegramBot, chat_id, message: dict, cb_id: str,
                             tg_user_id: int, data: str) -> None:
    """Решение по карточке подтверждения — те же записи approvals, что в веб-чате."""
    try:
        _prefix, decision, approval_id = data.split(":", 2)
    except ValueError:
        await bot.answer_callback(cb_id)
        return
    user_id = get_telegram_store().user_of(tg_user_id or 0)
    record = approvals.get(approval_id)
    if record is None:
        await bot.answer_callback(cb_id, "Запрос уже неактуален")
        return
    session = get_store().get(record.get("session_id") or "")
    owner = session.get("user_id") if session else None
    if owner and user_id and owner != user_id:
        await bot.answer_callback(cb_id, "Это не ваш диалог")
        return
    if not approvals.resolve(approval_id, decision):
        await bot.answer_callback(cb_id, "Запрос уже решён")
        return
    mark = "✅ Подтверждено" if decision == approvals.APPROVE else "❌ Отклонено"
    await bot.answer_callback(cb_id, mark)
    with contextlib.suppress(Exception):
        await bot.edit_message(
            chat_id,
            message.get("message_id"),
            f"{mark}: {html.escape(str(record.get('name') or ''))}",
            reply_markup={"inline_keyboard": []},
        )


# ---------------------------------------------------------------------------
# Ход: то же ядро, что у веб-чата (run_agent_loop), ответ — финальным сообщением
# ---------------------------------------------------------------------------


def _build_provider(user_id: str):
    """Провайдер учётной записи: личные настройки с откатом к общим (chat.py)."""
    cfg = effective_config_for(user_id)
    provider = get_provider(cfg.base_url, cfg.api_key, cfg.model)
    if provider is None:
        return None, cfg
    if cfg.source == "user":
        shared_cfg = effective_config()
        same = (cfg.base_url, cfg.api_key, cfg.model) == (
            shared_cfg.base_url, shared_cfg.api_key, shared_cfg.model
        )
        shared = None if same else get_provider(
            shared_cfg.base_url, shared_cfg.api_key, shared_cfg.model
        )
        if shared is not None:
            provider = FallbackProvider(provider, shared)
    return provider, cfg


def _set_turn(session: dict, state: str, error: str | None = None) -> None:
    """Отметка о ходе в сессии — как в веб-чате (GET /api/sessions/{id})."""
    turn = {"state": state, "at": time.time()}
    if error:
        turn["error"] = error[:500]
    session["turn"] = turn
    get_store().save(session)


def _resolve_state(chat_id, user_id: str, cfg, text: str) -> tuple[dict, dict]:
    """Сессия и состояние чата: закреплённая или новая; смена агента — новый
    диалог (как в веб-чате). Валидация skill_ids/rag_ids по правам владельца."""
    tstore = get_telegram_store()
    state = tstore.chat_state(chat_id)
    skill_ids = [s for s in state["skill_ids"] if skill_drafts.skill_available(s, user_id)]
    rag_ids: list[str] = []
    for rid in state["rag_ids"]:
        card = rag_module.get_store().find(rid)
        if card is not None and (card.get("status") == "Активна" or card.get("status") == rag_module.STATUS_ACTIVE):
            rag_ids.append(rid)
    if not rag_ids:
        try:
            bound = skill_drafts.bound_rag_id(skill_ids, user_id)
        except Exception:  # noqa: BLE001 — привязка не должна ронять чат
            bound = ""
        rag_ids = [bound] if bound else []

    store = get_store()
    session = store.get(state["session_id"]) if state["session_id"] else None
    if session is None or session.get("skill_ids") != skill_ids:
        session = store.create(title=text[:80], model=cfg.model, skill_ids=skill_ids)
        tstore.set_chat_state(chat_id, session_id=session["id"])
    session["user_id"] = user_id
    session["model"] = cfg.model
    session["skill_ids"] = skill_ids
    stage = datarepo.stage_of_skill(skill_ids[0]) if skill_ids else None
    if stage:
        if session.get("stage") != stage["key"]:
            session["stage"] = stage["key"]
            session.setdefault("decision_log", []).append(
                {"kind": "stage_set", "stage": stage["key"], "ts": time.time()}
            )
    else:
        session.pop("stage", None)
    # явный выбор в /base перекрывает привязку агента; пустой список — то же
    session["rag_ids"] = rag_ids
    try:
        session["model_ids"] = skill_drafts.bound_model_ids(skill_ids, user_id)
    except Exception:  # noqa: BLE001
        session["model_ids"] = []
    store.save(session)
    return session, state


_TOOL_STATUS = {
    "chart_generate": "📊 Строю график…",
    "format_table": "🗂 Оформляю таблицу…",
    "literature_search": "🔎 Ищу литературу…",
    "search_corpus": "🔎 Ищу в материалах…",
    "search_knowledge": "🔎 Ищу в базе знаний…",
    "make_document": "📄 Собираю документ…",
    "read_document": "📖 Читаю файл…",
    "read_file": "📖 Читаю файл…",
    "read_data_file": "📖 Читаю файл…",
    "list_files": "📖 Смотрю файлы…",
    "write_file": "📝 Пишу файл…",
    "run_calc": "🧮 Считаю…",
    "run_model": "🧮 Считаю…",
    "optimize_model": "🧮 Считаю…",
    "train_model": "🧮 Обучаю модель…",
    "call_agent": "🤝 Привлекаю агента…",
    "use_agent": "🤝 Привлекаю агента…",
    "compound_lookup": "🧪 Работаю с химией…",
}
_TOOL_STATUS_PREFIX = (("chem_", "🧪 Работаю с химией…"), ("rxn_", "🧪 Работаю с химией…"))


def _status_for(tool_name: str) -> str:
    if tool_name in _TOOL_STATUS:
        return _TOOL_STATUS[tool_name]
    for prefix, text in _TOOL_STATUS_PREFIX:
        if tool_name.startswith(prefix):
            return text
    return _DEFAULT_STATUS
_DEFAULT_STATUS = "⚙️ Работаю…"
_STATUS_MIN_INTERVAL = 1.0  # не чаще одной правки в секунду (лимиты Telegram)


class _ProgressStatus:
    """Одно сообщение статуса: создаётся на первом вызове инструмента, правится
    не чаще раза в секунду, в конце хода удаляется."""

    def __init__(self, bot: TelegramBot, chat_id) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = None
        self.last_text = ""
        self.last_edit = 0.0

    async def update(self, tool_name: str) -> None:
        text = _status_for(tool_name)
        if text == self.last_text:
            return
        now = time.monotonic()
        try:
            if self.message_id is None:
                sent = await self.bot.send_message(self.chat_id, text)
                self.message_id = sent.get("message_id") if isinstance(sent, dict) else None
            elif now - self.last_edit >= _STATUS_MIN_INTERVAL:
                await self.bot.edit_message(self.chat_id, self.message_id, text)
            else:
                return
        except TelegramError:
            return
        self.last_text = text
        self.last_edit = now

    async def clear(self) -> None:
        if self.message_id is None:
            return
        message_id, self.message_id = self.message_id, None
        with contextlib.suppress(TelegramError):
            await self.bot.delete_message(self.chat_id, message_id)


async def _typing_loop(bot: TelegramBot, chat_id, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await bot.send_typing(chat_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def _run_turn(bot: TelegramBot, chat_id, tg_user_id: int, text: str) -> None:
    """Полный ход: как produce() в веб-чате, но события собираются в текст."""
    key = str(chat_id)
    user, link = await _linked_name(tg_user_id)
    if not user:
        if link:  # учётная запись удалена — привязка мертва
            get_telegram_store().unbind_tg(tg_user_id)
            get_telegram_store().clear_chat_session(chat_id)
        await _safe_send(
            bot, chat_id, "Аккаунт не привязан. Отправьте /start и подтвердите код на платформе."
        )
        return
    if key in _RUNNING_CHATS:
        await _safe_send(
            bot, chat_id, "Прошлый ответ ещё готовится — дождитесь его или отправьте /new."
        )
        return
    provider, cfg = _build_provider(user["id"])
    if provider is None:
        await _safe_send(
            bot,
            chat_id,
            "LLM не настроена администратором платформы (админ-панель → Настройки внешних API).",
        )
        return

    session, _state = _resolve_state(chat_id, user["id"], cfg, text)
    _set_turn(session, "running")
    _RUNNING_CHATS.add(key)
    chart_svgs: list[str] = []  # графики chart_generate — фото после текста
    turn_started = time.monotonic()
    tool_calls = 0
    files_sent = 0
    chunks_sent = 0
    turn_status = "ok"
    try:
        final_text = ""
        error_msg: str | None = None
        buffer = ""  # дельты текущего шага; вызов инструмента открывает новый шаг
        stop = asyncio.Event()
        status = _ProgressStatus(bot, chat_id)
        typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop))
        try:
            async for event in run_agent_loop_events(session, provider, text):
                etype = event.get("type")
                if etype == "delta":
                    buffer += event.get("text") or ""
                elif etype == "tool_call":
                    tool_calls += 1
                    buffer = ""
                    await status.update(str(event.get("name") or ""))
                elif etype == "approval_request":
                    await _send_approval_request(bot, chat_id, event)
                elif etype == "tool_result":
                    result = event.get("result")
                    if event.get("name") == "chart_generate" and isinstance(result, dict):
                        svg = result.get("svg")
                        if svg and len(chart_svgs) < 3:
                            chart_svgs.append(str(svg))
                elif etype == "error":
                    error_msg = str(event.get("message") or "") or error_msg
                elif etype == "done":
                    final_text = buffer
        finally:
            stop.set()
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task
            await status.clear()
        if error_msg:
            if final_text:
                await _send_long(bot, chat_id, final_text)
            await _send_long(bot, chat_id, f"⚠️ {error_msg}")
            turn_status = "error"
            _set_turn(session, "failed", error_msg)
        elif final_text.strip():
            text, options = _extract_choices(final_text)
            text, views = _extract_views(text)
            chunks_sent = len([c for c in telegram_chunks(text) if c.strip()])
            await _send_answer_text(bot, chat_id, text, options)
            for view in views[:3]:
                await _send_view_file(bot, chat_id, user["id"], view)
            files_sent = len(views[:3])
            _set_turn(session, "done")
        else:
            await _safe_send(bot, chat_id, "Пустой ответ модели — попробуйте переформулировать вопрос.")
            turn_status = "empty"
            _set_turn(session, "done")
        for svg in chart_svgs:
            await _send_chart(bot, chat_id, svg)
    except Exception as exc:  # noqa: BLE001 — ошибка уходит пользователю
        message = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
        logger.exception("Ошибка хода в чате %s", chat_id)
        with contextlib.suppress(TelegramError):
            await _safe_send(bot, chat_id, f"⚠️ {message}")
        turn_status = "exception"
        _set_turn(session, "failed", message)
    finally:
        _RUNNING_CHATS.discard(key)
        logger.info(
            "Ход: chat=%s user=%s agent=%s status=%s duration=%.1fs tools=%d files=%d chunks=%d",
            chat_id,
            user["id"],
            ",".join(str(x) for x in (session.get("skill_ids") or [])) or "-",
            turn_status,
            time.monotonic() - turn_started,
            tool_calls,
            files_sent + len(chart_svgs),
            chunks_sent,
        )


async def run_agent_loop_events(session: dict, provider, text: str):
    """Отложенный импорт run_agent_loop — точка подмены в тестах."""
    from ..runtime.loop import run_agent_loop

    async for event in run_agent_loop(
        settings=get_settings(),
        provider=provider,
        registry=get_registry(),
        store=get_store(),
        session=session,
        user_text=text,
    ):
        yield event


async def _send_long(bot: TelegramBot, chat_id, text: str) -> None:
    """Ответ кусками: резка до конвертации, по блокам разметки — каждый кусок
    конвертируется самостоятельно, теги не рвутся на границе."""
    for chunk in telegram_chunks(text):
        if chunk.strip():
            await _safe_send(bot, chat_id, chunk, markdown=True)


# протокол быстрых ответов агента — как в веб-чате (workspace.js, CHOICES_RE):
# блок ```aios-choices убирается из текста, вариант кликом уходит сообщением
CHOICES_RE = re.compile(r"```aios-choices[ \t]*\r?\n([\s\S]*?)```")


def _extract_choices(text: str) -> tuple[str, list[str]]:
    """Ответ без ```aios-choices-блоков и список вариантов (до 8, как в вебе)."""
    options: list[str] = []

    def grab(match: re.Match) -> str:
        for line in (match.group(1) or "").splitlines():
            line = re.sub(r"^[-*•]\s+", "", line.strip())
            if line:
                options.append(line)
        return ""

    text = CHOICES_RE.sub(grab, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), options[:8]


async def _send_turn_answer(bot: TelegramBot, chat_id, text: str) -> None:
    """Текст ответа + кнопки быстрых ответов (```aios-choices) на последнем
    куске; варианты запоминаются в состоянии чата для callback ch:{i}."""
    text, options = _extract_choices(text)
    await _send_answer_text(bot, chat_id, text, options)


async def _send_answer_text(bot: TelegramBot, chat_id, text: str, options: list[str]) -> None:
    chunks = [c for c in telegram_chunks(text) if c.strip()]
    keyboard = None
    if options:
        keyboard = {
            "inline_keyboard": [
                [{"text": _truncate(opt), "callback_data": f"ch:{i}"}]
                for i, opt in enumerate(options)
            ]
        }
        get_telegram_store().set_chat_state(chat_id, choices=options)
    if chunks:
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            await _safe_send(
                bot, chat_id, chunk,
                reply_markup=keyboard if (keyboard and i == last) else None,
                markdown=True,
            )
    elif keyboard:
        await _safe_send(bot, chat_id, "Выберите вариант:", reply_markup=keyboard)


# компоненты ответа агента (Static Generative UI, agent_views.js): file_card
# отправляем файлом, остальные блоки остаются текстом JSON
VIEW_RE = re.compile(r"```aios-view[ \t]*\r?\n([\s\S]*?)```")
ATTACHMENT_URL_RE = re.compile(r"/api/chat/attachments/([A-Za-z0-9-]+)")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _extract_views(text: str) -> tuple[str, list[dict]]:
    """Убирает ```aios-view-блоки из ответа, возвращает разобранные компоненты."""
    views: list[dict] = []

    def grab(match: re.Match) -> str:
        try:
            view = json.loads(match.group(1))
        except ValueError:
            return ""  # битый JSON — блок-шум, из текста тоже убираем
        if isinstance(view, dict):
            views.append(view)
        return ""

    text = VIEW_RE.sub(grab, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), views


async def _send_view_file(bot: TelegramBot, chat_id, user_id: str, view: dict) -> None:
    """file_card → файл пользователю: картинка фото, остальное документом.
    Вложение берём напрямую из uploads-хранилища (тот же процесс, владелец
    сессии известен), чужие/удалённые молча пропускаем."""
    if view.get("component") != "file_card":
        return
    match = ATTACHMENT_URL_RE.search(str(view.get("url") or ""))
    if not match:
        return
    store = uploads.get_store()
    meta = store.get(user_id, match.group(1))
    if meta is None:
        logger.info("Вложение %s не найдено у пользователя %s", match.group(1), user_id)
        return
    path = store.content_path(user_id, meta)
    if not path.is_file():
        return
    filename = str(meta.get("filename") or "file")
    content = path.read_bytes()
    try:
        if filename.lower().endswith(IMAGE_SUFFIXES):
            await bot.send_photo(chat_id, content, caption=filename)
        else:
            await bot.send_document(chat_id, content, filename, caption=filename)
    except TelegramError as exc:
        logger.warning("Telegram не принял файл %s: %s", filename, exc)


async def _send_chart(bot: TelegramBot, chat_id, svg: str) -> None:
    """График chart_generate фото (SVG растеризуется своим конвертером);
    неудача растеризации/отправки не роняет ход."""
    try:
        png = chart_png.chart_png(svg)
    except Exception:  # noqa: BLE001
        logger.exception("Растеризация графика не удалась")
        return
    if not png:
        return
    try:
        await bot.send_photo(chat_id, png)
    except TelegramError as exc:
        logger.warning("Telegram не принял график: %s", exc)


async def _send_approval_request(bot: TelegramBot, chat_id, event: dict) -> None:
    """Карточка подтверждения с кнопками — те же approvals, что в веб-чате."""
    name = html.escape(str(event.get("name") or ""))
    arguments = json.dumps(event.get("arguments") or {}, ensure_ascii=False)[:600]
    text = (
        f"Агент запрашивает подтверждение: <b>{name}</b>\n"
        f"<pre>{html.escape(arguments)}</pre>"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Подтвердить", "callback_data": f"ap:approve:{event.get('id')}"},
                {"text": "❌ Отклонить", "callback_data": f"ap:decline:{event.get('id')}"},
            ]
        ]
    }
    await _safe_send(bot, chat_id, text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Long polling (разработка без публичного HTTPS)
# ---------------------------------------------------------------------------


async def poll_updates() -> None:
    """Long polling getUpdates вместо вебхука (AIOS_TG_POLLING=1). 409 от
    Telegram означает установленный вебхук — polling бессмыслен, выходим."""
    bot = TelegramBot.from_settings()
    if bot is None:
        return
    await register_bot_commands(bot)
    logger.info("Telegram: long polling запущен")
    offset = 0
    while True:
        try:
            updates = await bot.call(
                "getUpdates", offset=offset, timeout=25, allowed_updates=ALLOWED_UPDATES
            )
        except TelegramError as exc:
            if exc.status_code == 409:
                logger.warning("Telegram: установлен вебхук — long polling остановлен")
                return
            logger.warning("Telegram getUpdates: %s — пауза 3 с", exc)
            await asyncio.sleep(3.0)
            continue
        for update in updates:
            offset = max(offset, int(update.get("update_id") or 0) + 1)
            try:
                await handle_update(update, bot)
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка обработки апдейта %s", update.get("update_id"))
