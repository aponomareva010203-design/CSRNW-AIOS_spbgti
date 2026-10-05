"""Скрытый блок «Контекст о пользователе»: сбор материала и генерация LLM.

Формируется языковой моделью по расписанию (не чаще раза в сутки на
пользователя) на основе двух источников, видимых платформе:

  1. сессии пользователя с нейросетью (сессии, привязанные к учётной записи
     по Bearer-токену в POST /api/chat);
  2. упоминания пользователя — сообщения в сессиях других пользователей,
     где встречаются его ФИО/фамилия с инициалами/логин.

Правила блока:
  • не дублирует анкетные данные из блока «Данные пользователя» — анкета
    передаётся модели отдельной секцией с запретом на пересказ;
  • не более 1000 символов (CONTEXT_MAX_CHARS в runtime/cabinet.py);
  • перегенерируется, только если появился новый материал: sha256-дайджест
    собранного материала сравнивается с сохранённым в файле блока;
    без изменений планировщик лишь обновляет отметку checked_at.

Планировщик — фоновая asyncio-задача (запускается lifespan-ом приложения):
один проход в сутки по московскому времени в AIOS_USER_CONTEXT_RUN_AT
(по умолчанию «23:00»). Без настроенной LLM проходы пропускаются.
При нескольких воркерах uvicorn задача дублируется, но атомарная запись
файлов делает конкуренцию безвредной.
"""

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import time
import zoneinfo

from .cabinet import CONTEXT_MAX_CHARS, get_store
from .config import get_settings
from .llm_settings import effective_config, effective_config_for
from .providers import build_provider
from .sessions import SessionStore
from .usage import SYSTEM_USER_ID, get_store as get_usage_store
from .user_llm import any_personal, personal_llm
from .users import UserStore

logger = logging.getLogger("aios.user_context")

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
STARTUP_DELAY = 30.0  # первый проход планировщика после старта, сек

# бюджеты материала, чтобы промпт не разрастался
OWN_SESSIONS_LIMIT = 20  # сколько последних своих сессий берём
OWN_MESSAGES_PER_SESSION = 12  # сообщений на сессию
MESSAGE_EXCERPT_CHARS = 400  # обрезка одного сообщения
MENTIONS_LIMIT = 30  # сколько упоминаний берём

SYSTEM_PROMPT = (
    "Ты — служебный модуль платформы AIOS. Твоя задача — составить компактный "
    "контекст о пользователе для персонализации ассистента.\n"
    "Правила:\n"
    "1. Опирайся только на приведённые материалы: сессии пользователя "
    "с нейросетью и упоминания о нём в сессиях платформы.\n"
    "2. Не дублируй анкетные данные (ФИО, должности, учёную степень, научные "
    "интересы) — они переданы отдельно и уже известны.\n"
    "3. Пиши о содержательном: типичные задачи и запросы, рабочий стиль, "
    "упоминания в рабочих обсуждениях, роли в проектах, чего ожидает от платформы.\n"
    "4. Не выдумывай фактов; если материала мало — скажи об этом коротко.\n"
    "5. Ответ — связный текст на русском без markdown-разметки, списков и "
    "заголовков, не более 1000 символов. Никаких пояснений о задании — только "
    "сам контекст."
)


def _mention_needles(user: dict, profile: dict) -> list[str]:
    """Варианты написания, по которым ищем упоминания пользователя."""
    needles: set[str] = set()
    last = (profile.get("last_name") or "").strip()
    first = (profile.get("first_name") or "").strip()
    patronymic = (profile.get("patronymic") or "").strip()
    if last and first:
        needles.add(f"{last} {first}")
        initial = first[0]
        if patronymic:
            needles.add(f"{last} {initial}.{patronymic[0]}.")
            needles.add(f"{last} {first} {patronymic}")
        needles.add(f"{last} {initial}.")
    if user.get("name"):
        needles.add(str(user["name"]).strip())
    if user.get("login"):
        needles.add(str(user["login"]).strip().lower())
    return sorted(n for n in needles if len(n) >= 5)


def _iter_dialog_messages(session: dict) -> list[str]:
    """Текстовые сообщения сессии (без вызовов инструментов и их результатов)."""
    out = []
    for msg in session.get("messages") or []:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            out.append(str(msg["content"]))
    return out



def collect_material(user: dict, profile: dict, sessions) -> dict:
    """Собирает материал о пользователе из всех сессий платформы.

    Возвращает {"own_sessions": [...], "mentions": [...]}; sessions —
    итератор полных сессий (SessionStore.iter_all()).
    """
    uid = user.get("id")
    needles = [n.lower() for n in _mention_needles(user, profile)]
    own: list[dict] = []
    mentions: list[dict] = []

    for session in sessions:
        messages = _iter_dialog_messages(session)
        if not messages:
            continue
        if session.get("user_id") == uid:
            own.append(
                {
                    "id": session.get("id"),
                    "title": session.get("title") or "",
                    "updated_at": session.get("updated_at"),
                    "messages": [m[:MESSAGE_EXCERPT_CHARS] for m in messages[-OWN_MESSAGES_PER_SESSION:]],
                }
            )
            continue
        if not needles:
            continue
        for message in messages:
            lowered = message.lower()
            if any(n in lowered for n in needles):
                mentions.append(
                    {
                        "session_id": session.get("id"),
                        "title": session.get("title") or "",
                        "excerpt": message[:MESSAGE_EXCERPT_CHARS],
                    }
                )
                break  # одно упоминание на сессию достаточно

    own.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return {"own_sessions": own[:OWN_SESSIONS_LIMIT], "mentions": mentions[:MENTIONS_LIMIT]}


def material_digest(material: dict) -> str:
    """Отпечаток материала: меняется только при новых сессиях/упоминаниях.

    Для своих сессий достаточно идентификатора, времени обновления и числа
    сообщений — сообщения в сессии только дописываются; упоминания входят
    цитатами, так как набор строк зависит от содержимого.
    """
    own = [
        [s.get("id"), s.get("updated_at"), len(s.get("messages") or [])]
        for s in material.get("own_sessions") or []
    ]
    mentions = [
        [m.get("session_id"), m.get("excerpt")] for m in material.get("mentions") or []
    ]
    payload = json.dumps({"own": own, "mentions": mentions}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> str:
    """Обрезка по границе предложения — чтобы текст не обрывался на полуслове
    и на середине мысли. Если конец последнего целого предложения найден и не
    укорачивает текст более чем на 40% от лимита — режем по нему. Иначе —
    запасной вариант: по границе слова (как раньше)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]

    sentence_end = -1
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] in ".!?" and (i + 1 == len(cut) or cut[i + 1] in " \n"):
            sentence_end = i
            break

    if sentence_end != -1 and sentence_end >= limit * 0.6:
        return cut[: sentence_end + 1].strip()

    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-") or text[:limit]


def _render_task(user: dict, profile: dict, material: dict) -> str:
    """Пользовательское сообщение для модели: анкета + материал анализа."""
    from .cabinet import full_name  # локальный импорт — избежать цикла на уровне модуля

    lines: list[str] = []
    name = full_name(profile)
    lines.append(f"Пользователь: {name or (user.get('login') or '')}")
    lines.append(
        "Анкетные данные (НЕ дублировать в ответе): "
        f"должности: {'; '.join(profile.get('positions') or []) or '—'}; "
        f"учёная степень: {profile.get('degree') or '—'}; "
        f"научные интересы: {profile.get('interests') or '—'}"
    )
    lines.append("")

    lines.append("=== Сессии пользователя с нейросетью ===")
    if material.get("own_sessions"):
        for session in material["own_sessions"]:
            lines.append(f"Сессия «{session['title']}»:")
            for message in session["messages"]:
                lines.append(f"  • {message}")
    else:
        lines.append("(сессий нет)")
    lines.append("")

    lines.append("=== Упоминания пользователя в сессиях платформы ===")
    if material.get("mentions"):
        for mention in material["mentions"]:
            lines.append(f"Сессия «{mention['title']}»: {mention['excerpt']}")
    else:
        lines.append("(упоминаний нет)")

    lines.append("")
    lines.append(f"Составь контекст о пользователе (до {CONTEXT_MAX_CHARS} символов).")
    return "\n".join(lines)


async def generate_context(provider, user: dict, profile: dict, material: dict) -> str:
    """Один вызов LLM: контекст по материалу, обрезанный до лимита блока.

    Расход токенов записывается на фонового агента платформы (SYSTEM_USER_ID),
    а не на пользователя: контекст формирует сама платформа.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _render_task(user, profile, material)},
    ]
    chunks: list[str] = []
    usage: dict | None = None
    settings = get_settings()
    # отдельная (обычно более дешёвая/быстрая) модель для фонового вызова,
    # если администратор задал AIOS_USER_CONTEXT_MODEL; иначе — основная
    model = settings.user_context_model or None
    # запас над CONTEXT_MAX_CHARS: кириллица у многих моделей — не более
    # ~2 символов на токен, плюс модель не обязана уложиться в лимит сама —
    # раньше это ловилось только клиентской обрезкой текста постфактум,
    # теперь ограничиваем и сам ответ API, чтобы не платить за лишнее
    max_tokens = max(300, CONTEXT_MAX_CHARS // 2 + 100)
    async for kind, payload in provider.stream_chat(messages, tools=None, model=model, max_tokens=max_tokens):
        if kind == "text":
            chunks.append(payload)
        elif kind == "usage" and isinstance(payload, dict):
            usage = payload
    text = _truncate("".join(chunks), CONTEXT_MAX_CHARS)

    if usage:
        get_usage_store().record(
            SYSTEM_USER_ID,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
    else:
        # провайдер usage не прислал — оценка по длине (~4 символа на токен)
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        get_usage_store().record(
            SYSTEM_USER_ID,
            max(1, prompt_chars // 4),
            max(1, len("".join(chunks)) // 4),
        )
    return text


def _current_provider():
    """Провайдер из действующих настроек LLM; None — LLM не настроена."""
    settings = get_settings()
    return build_provider(effective_config(), read_timeout=settings.request_timeout)


async def run_user_context_job(provider=None) -> dict:
    """Один проход по всем пользователям (вызывается планировщиком и тестами).

    Для каждого пользователя: не чаще раза в сутки; при неизменном дайджесте
    материала контекст не трогаем (только отметка checked_at); без материала
    файл контекста не создаём. Возвращает счётчики прохода.

    Явно переданный провайдер (тесты) используется для всех; иначе у
    пользователя с личными настройками внешнего API — его endpoint и ключ,
    у остальных — общая конфигурация платформы.
    """
    explicit = provider is not None
    stats = {"users": 0, "generated": 0, "unchanged": 0, "skipped_fresh": 0, "no_material": 0}
    if not explicit and effective_config().base_url == "" and not any_personal():
        stats["skipped"] = "llm_not_configured"
        return stats

    settings = get_settings()
    users = UserStore(settings.runtime_dir / "users")
    sessions = SessionStore(settings.runtime_dir / "sessions")
    cabinet = get_store()
    now = time.time()

    for user in users.list_all():  # публичное представление: id, login, name, …
        uid = user.get("id")
        if not uid:
            continue
        stats["users"] += 1

        ctx = cabinet.get_context(uid)
        checked = float(ctx.get("checked_at") or ctx.get("generated_at") or 0) if ctx else 0.0
        if ctx is not None and _msk_date(checked) == _msk_date(now):
            stats["skipped_fresh"] += 1
            continue  # расписание: этому пользователю сегодня уже проверяли

        profile = cabinet.get_profile(uid)
        material = collect_material(user, profile, sessions.iter_all())
        if not material["own_sessions"] and not material["mentions"]:
            stats["no_material"] += 1
            continue

        digest = material_digest(material)
        if ctx is not None and ctx.get("source_digest") == digest and ctx.get("context"):
            cabinet.touch_checked(uid)  # нового материала нет — контекст не меняем
            stats["unchanged"] += 1
            continue

        # провайдер этого пользователя: общий или личный (Настройки → Внешний API)
        user_provider = provider
        if not explicit and personal_llm(uid):
            user_provider = build_provider(
                effective_config_for(uid), read_timeout=settings.request_timeout
            )
        if user_provider is None:
            continue  # ни общая, ни личная LLM не настроены

        try:
            text = await generate_context(user_provider, user, profile, material)
        except Exception:  # noqa: BLE001 — сбой одного пользователя не валит проход
            logger.exception("Не удалось сформировать контекст для %s", uid)
            continue
        if not text:
            continue
        cabinet.save_context(uid, text, digest)
        stats["generated"] += 1
        logger.info("Контекст о пользователе %s обновлён (%d символов)", uid, len(text))

    return stats


def _msk_date(ts: float) -> dt.date:
    """Календарная дата отметки времени по московскому времени."""
    return dt.datetime.fromtimestamp(ts, tz=MSK).date()


def _next_run_delay() -> float:
    """Секунды до ближайшего запуска по расписанию (МСК, из AIOS_USER_CONTEXT_RUN_AT)."""
    hh, _, mm = get_settings().user_context_run_at.partition(":")
    hour = min(23, max(0, int(hh or 0)))
    minute = min(59, max(0, int(mm or 0)))
    now_msk = dt.datetime.now(MSK)
    run = now_msk.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now_msk:
        run += dt.timedelta(days=1)
    return (run - now_msk).total_seconds()


async def scheduler_loop() -> None:
    """Фоновый цикл приложения: ежедневный проход run_user_context_job в 23:00 МСК.

    Первый проход — по ближайшему срабатыванию расписания после старта
    (через STARTUP_DELAY даём приложению доугрузиться).
    """
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        delay = _next_run_delay()
        logger.info(
            "Следующее обновление контекстов пользователей — %s МСК (через %.0f с)",
            get_settings().user_context_run_at,
            delay,
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
        try:
            await run_user_context_job()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — планировщик живёт дальше любого сбоя
            logger.exception("Сбой прохода формирования контекстов")

