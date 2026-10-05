"""Telegram-бот платформы: привязка учётной записи, состояния личных чатов,
ход через run_agent_loop (тот же конвейер, что и веб-чат), вебхук.

Сеть не трогается: FakeBot подменяет TelegramBot в роутере и пишет все
отправки в память; LLM — MockProvider (как в тестах веб-чата).
"""

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.runtime import uploads
from api.runtime.config import get_settings
from api.runtime.providers import MockProvider
from api.runtime.tokens import TokenStore
from api.runtime.users import UserStore

TG_TOKEN = "123:TEST-TOKEN"
TG_SECRET = "test-webhook-secret"
TG_CHAT = 4242
TG_USER = 777


class FakeBot:
    """Запись вызовов Bot API вместо сети."""

    CALLS: list[tuple] = []  # classvar: все вызовы call() всех экземпляров

    def __init__(self) -> None:
        self.messages: list[dict] = []  # отправленные сообщения
        self.typing: list = []
        self.callbacks: list[tuple] = []
        self.edits: list[tuple] = []
        self.deletes: list[tuple] = []
        self.markup_edits: list[tuple] = []
        self.calls: list[tuple] = []
        self.photos: list[dict] = []  # отправленные фото (графики, файлы-картинки)
        self.documents: list[dict] = []  # отправленные документы (pdf/docx/…)

    async def send_message(self, chat_id, text, reply_markup=None, markdown=False):
        self.messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"message_id": len(self.messages)}

    async def send_typing(self, chat_id):
        self.typing.append(chat_id)

    async def send_photo(self, chat_id, png, caption=""):
        self.photos.append({"chat_id": chat_id, "png": png, "caption": caption})
        return {"message_id": len(self.photos)}

    async def send_document(self, chat_id, content, filename, caption=""):
        self.documents.append({"chat_id": chat_id, "content": content,
                               "filename": filename, "caption": caption})
        return {"message_id": len(self.documents)}

    async def answer_callback(self, callback_query_id, text=""):
        self.callbacks.append((callback_query_id, text))

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text))

    async def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.markup_edits.append((chat_id, message_id, reply_markup))

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))

    async def call(self, method, **params):
        self.calls.append((method, params))
        FakeBot.CALLS.append((method, params))
        return {}

    # тесты дергают handle_update(update) без явного бота — подменяем from_settings
    @classmethod
    def from_settings(cls):
        return cls()

    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]


def _message(text: str, chat_id=TG_CHAT, sender=TG_USER) -> dict:
    return {
        "message_id": 1,
        "from": {"id": sender, "username": "tg_user", "first_name": "Тест"},
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }


@pytest.fixture()
def tg_env(tmp_path, monkeypatch):
    """Изолированный рантайм, настроенный бот, свежие кэши синглтонов."""
    from api.routers import auth as auth_router
    from api.routers import chat as chat_router
    from api.routers import telegram as tg_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_TG_BOT_TOKEN", TG_TOKEN)
    monkeypatch.setenv("AIOS_TG_WEBHOOK_SECRET", TG_SECRET)
    # пустое значение вместо delenv: мини-парсер .env реального проекта
    # восстанавливает переменные через setdefault
    monkeypatch.setenv("AIOS_PUBLIC_BASE_URL", "")
    for name in ("AIOS_LLM_BASE_URL", "AIOS_LLM_API_KEY", "AIOS_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    for module, cache in (
        (tg_router, tg_router.get_telegram_store),
        (chat_router, chat_router.get_store),
        (chat_router, chat_router.get_registry),
        (chat_router, chat_router.get_provider),
        (auth_router, auth_router.get_user_store),
        (auth_router, auth_router.get_token_store),
        (uploads, uploads.get_store),
    ):
        cache.cache_clear()

    # пользователь платформы + Bearer-токен (регистрация не нужна)
    user = UserStore(get_settings().runtime_dir / "users").create(
        login="tg-user@test.io", password="secret", name="Тест Тестов", confirmed=True
    )
    token = TokenStore(get_settings().runtime_dir / "auth").issue(user["id"])

    # сеть не трогаем: FakeBot вместо TelegramBot, MockProvider вместо LLM
    monkeypatch.setattr(tg_router, "TelegramBot", FakeBot)
    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: MockProvider())

    yield tg_router, user, token, FakeBot

    get_settings.cache_clear()
    for module, cache in (
        (tg_router, tg_router.get_telegram_store),
        (chat_router, chat_router.get_store),
        (chat_router, chat_router.get_registry),
        (chat_router, chat_router.get_provider),
        (auth_router, auth_router.get_user_store),
        (auth_router, auth_router.get_token_store),
        (uploads, uploads.get_store),
    ):
        cache.cache_clear()


def _handle(tg_router, update: dict, bot: FakeBot | None = None) -> FakeBot:
    """handle_update синхронно; создаёт и возвращает бота с записью."""
    if bot is None:
        bot = FakeBot()
    asyncio.run(tg_router.handle_update(update, bot))
    return bot


def _link(tg_router, tg_user_id: int, user_id: str) -> None:
    store = tg_router.get_telegram_store()
    code = store.issue_code(tg_user_id, username="tg_user", first_name="Тест")
    store.bind(tg_user_id, user_id, username="tg_user", first_name="Тест")
    assert store.consume_code(code) is not None  # код снимается привязкой вручную


# ---------------------------------------------------------------------------
# Клиент и разметка


def test_split_text_chunks():
    from api.runtime.tg_client import split_text

    assert split_text("короткий") == ["короткий"]
    # по абзацам, не посимвольно
    long = "\n\n".join(f"абзац {i} " + "т" * 200 for i in range(30))
    chunks = split_text(long, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n\n".join(chunks) == long  # ничего не потеряно
    # совсем без разделителей — режется по лимиту
    solid = "ж" * 2500
    assert [len(c) for c in split_text(solid, limit=1000)] == [1000, 1000, 500]


def test_split_markdown_chunks_keeps_markup_valid():
    """Куски для Telegram: блоки кода неделимы, каждый кусок конвертируется
    в сбалансированный HTML — иначе Telegram отвечал 400 и ответ уходил
    чистым текстом со звёздочками."""
    from api.runtime.tg_client import md_to_telegram_html, split_markdown_chunks

    md = (
        "Вводный абзац.\n\n"
        "**Заголовок вывода**\n\n"
        "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```\n\n"
        "Заключение с `кодом` и [ссылкой](https://example.com)."
    )
    chunks = split_markdown_chunks(md, limit=300)
    assert len(chunks) > 1
    for chunk in chunks:
        html_out = md_to_telegram_html(chunk)
        assert len(html_out) <= 4096
        # сбалансированность: конвертер открывает тег только с парой
        for tag in ("b", "code", "pre", "a", "blockquote"):
            opens = html_out.count(f"<{tag}>") + html_out.count(f"<{tag} ")
            closes = html_out.count(f"</{tag}>")
            assert opens == closes, (tag, chunk[:80])
    # блок кода не разорван: где-то есть целый блок с телом
    assert any("```python" in c and "line_39" in c for c in chunks) or all(
        "```" not in c or "line_" in c for c in chunks
    )
    # ограждение целиком в одном куске (не разрезано между кусками)
    fence_chunks = [c for c in chunks if c.startswith("```")]
    assert all(c.rstrip().endswith("```") for c in fence_chunks)


def test_split_markdown_chunks_oversize_fence():
    """Блок кода длиннее лимита — несколько законченных <pre>-блоков."""
    from api.runtime.tg_client import split_markdown_chunks

    body = "\n".join(f"строка {i} " + "х" * 40 for i in range(50))
    md = f"```text\n{body}\n```"
    chunks = split_markdown_chunks(md, limit=600)
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith("```text") and c.rstrip().endswith("```")
        assert len(c) <= 600


def test_bot_retries_failed_connects():
    """DPI к Telegram «плавает»: неоткрывшееся соединение повторяется с
    паузой (1/3 с) — запрос при неоткрывшемся канале гарантированно не ушёл,
    дубли исключены; ошибка API без повторов уходит сразу."""
    import httpx as httpx_mod

    from api.runtime.tg_client import TelegramError, TelegramBot as RealBot

    class FlakyTransport(httpx_mod.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                raise httpx_mod.ConnectTimeout("DPI буксует", request=request)
            return httpx_mod.Response(
                200, json={"ok": True, "result": {"done": True}}, request=request
            )

    bot = RealBot("123:TEST", transport=FlakyTransport())
    result = asyncio.run(bot.call("getMe"))
    assert result == {"done": True}

    # ошибка API (не сеть) — без повторов
    class RejectingTransport(httpx_mod.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx_mod.Response(400, json={"ok": False, "description": "bad"}, request=request)

    bot2 = RealBot("123:TEST", transport=RejectingTransport())
    with pytest.raises(TelegramError) as exc:
        asyncio.run(bot2.call("sendMessage", chat_id=1, text="x"))
    assert exc.value.status_code == 400


def test_md_to_telegram_html():
    from api.runtime.tg_client import md_to_telegram_html

    out = md_to_telegram_html("Заголовок\n\n**жирный** и `код` <script>x</script>")
    assert "<b>жирный</b>" in out
    assert "<code>код</code>" in out
    assert "&lt;script&gt;" in out  # невалидная разметка из ответа модели безопасна
    fence = md_to_telegram_html("до\n```python\nprint(1 < 2)\n```\nпосле")
    assert "<pre>" in fence and "print(1 &lt; 2)" in fence
    link = md_to_telegram_html("[текст](https://example.com/a?b=1)")
    assert '<a href="https://example.com/a?b=1">текст</a>' in link
    # цитаты источников (RAG-ответы) — в <blockquote> Telegram
    quote = md_to_telegram_html("Вывод:\n> Источник А\n> Источник Б\nДальше текст.")
    assert "<blockquote>Источник А\nИсточник Б</blockquote>" in quote
    assert "Дальше текст." in quote


def test_telegram_chunks_fit_after_conversion():
    """Куски ответа: конвертированный HTML каждого влезает в 4096 —
    иначе Telegram отвечал 400 и сообщение уходило плоским текстом."""
    from api.runtime.tg_client import MAX_MESSAGE_LEN, md_to_telegram_html, telegram_chunks

    para = (
        "Абзац про **полимерные композиты** с `кодом` и ссылками "
        "[Source One](https://doi.org/10.1000/very-long-doi-suffix-000000000001) и "
        "[Source Two](https://onlinelibrary.wiley.com/doi/full/10.1002/000000000000002) "
        "плюс *термины* и «кавычки».\n\n"
    )
    md = para * 30
    chunks = telegram_chunks(md)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(md_to_telegram_html(chunk)) <= MAX_MESSAGE_LEN


def test_extract_choices():
    """```aios-choices-блок убирается из ответа, варианты идут в кнопки."""
    from api.routers.telegram import _extract_choices

    text = (
        "Остался вопрос — сколько работ показать?\n\n"
        "```aios-choices\n10 работ\n- 15 работ\n• 25 работ\n```\n\n"
        "После блока."
    )
    clean, options = _extract_choices(text)
    assert options == ["10 работ", "15 работ", "25 работ"]  # маркеры списков сняты
    assert "aios-choices" not in clean
    assert "сколько работ показать?" in clean and "После блока." in clean


async def _noop_async():
    return None


def test_turn_sends_choices_keyboard_and_callback_answers(tg_env, monkeypatch):
    """Ответ с aios-choices: клавиатура на последнем куске, клик отправляет
    вариант как сообщение пользователя."""
    tg_router, user, _token, FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])

    # MockProvider не знает aios-choices — подменяем ход фиксированным ответом
    class ChoicesProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            if any(m.get("role") == "tool" for m in messages):
                yield ("text", "Готово.")
                return
            yield ("text", "Вопрос?\n\n```aios-choices\n10 работ\n25 работ\n```\n")

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: ChoicesProvider())
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Начнём обзор")})
    assert "aios-choices" not in "".join(bot.texts())  # блок убран из текста
    keyboard = bot.messages[-1]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard] == ["10 работ", "25 работ"]
    assert [row[0]["callback_data"] for row in keyboard] == ["ch:0", "ch:1"]
    state = tg_router.get_telegram_store().chat_state(TG_CHAT)
    assert state["choices"] == ["10 работ", "25 работ"]

    # клик по варианту — новый ход с текстом варианта
    bot = _handle(tg_router, _callback("ch:1"))
    assert ("cb-1", "25 работ") in bot.callbacks
    session = tg_router.get_store().get(
        tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"]
    )
    user_texts = [m["content"] for m in session["messages"] if m["role"] == "user"]
    assert user_texts[-1] == "25 работ"


def test_file_card_view_sends_photo(tg_env, monkeypatch):
    """file_card из ```aios-view: картинка уходит фото прямо из uploads-
    хранилища, JSON-блок из текста убирается."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    meta = uploads.get_store().save(user["id"], "structure.png", b"\x89PNG-fake-bytes")

    class FileProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text",
                   "Готово — структура отрисована.\n\n"
                   '```aios-view\n{"component": "file_card", "filename": "structure.png", '
                   f'"url": "/api/chat/attachments/{meta["id"]}", "size": "0 КБ"}}\n'
                   "```\n\n"
                   "На изображении фуллерен C60.")

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: FileProvider())
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Нарисуй молекулу")})
    assert "aios-view" not in "".join(bot.texts())  # JSON-блок убран из текста
    assert any("фуллерен" in t for t in bot.texts())
    assert bot.photos and bot.photos[0]["caption"] == "structure.png"
    assert bot.photos[0]["png"] == b"\x89PNG-fake-bytes"


def test_file_card_document_for_non_image(tg_env, monkeypatch):
    """Не-картинка (docx/pdf) уходит документом Telegram."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    meta = uploads.get_store().save(user["id"], "report.docx", b"docx-bytes")

    class FileProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text",
                   f'```aios-view\n{{"component": "file_card", "filename": "report.docx", '
                   f'"url": "/api/chat/attachments/{meta["id"]}"}}\n```')

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: FileProvider())
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Сделай документ")})
    assert bot.documents and bot.documents[0]["filename"] == "report.docx"
    assert bot.photos == []


def test_file_card_foreign_attachment_skipped(tg_env, monkeypatch):
    """Чужое вложение не отправляется, текст при этом доходит целиком."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    other = UserStore(get_settings().runtime_dir / "users").create(
        login="other2@test.io", password="secret", confirmed=True
    )
    meta = uploads.get_store().save(other["id"], "secret.png", b"secret-bytes")

    class FileProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text",
                   f'```aios-view\n{{"component": "file_card", "filename": "secret.png", '
                   f'"url": "/api/chat/attachments/{meta["id"]}"}}\n```')

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: FileProvider())
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Покажи")})
    assert bot.photos == [] and bot.documents == []


def test_chart_generate_result_becomes_photo(tg_env, monkeypatch):
    """График chart_generate (SVG) доходит до Telegram фото-сообщением."""
    asyncio.run(_chart_photo_case(tg_env, monkeypatch))


async def _chart_photo_case(tg_env, monkeypatch):
    tg_router, user, _token, FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])

    class ChartProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text", "Строю график.")

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: ChartProvider())

    # перехватываем событие tool_result: в реальном ходе его рождает реестр,
    # здесь — обёртка run_agent_loop_events с заранее собранным SVG
    registry = tg_router.get_registry()
    result = await registry.call(
        "chart_generate",
        {"style": "line", "labels": ["пн", "вт", "ср"], "values": [1, 5, 3], "title": "Тест графика"},
        ctx={},
    )
    assert result["ok"] and "<polyline" in result["svg"]
    svg = result["svg"]

    from api.runtime import chart_png

    png = chart_png.chart_png(svg)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"  # растеризация работает

    # боту подсовываем ход с tool_result графика через monkeypatch events
    async def fake_events(session, provider, text):
        yield {"type": "tool_result", "id": "x", "name": "chart_generate",
               "ok": True, "result": {"ok": True, "svg": svg}}
        yield {"type": "done", "finish": "stop"}

    monkeypatch.setattr(tg_router, "run_agent_loop_events", fake_events)
    bot = FakeBot()
    await tg_router.handle_update({"update_id": 1, "message": _message("Покажи график")}, bot)
    assert bot.photos, "график должен уйти фото"
    assert bot.photos[0]["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_start_greeting_prefers_cabinet_name(tg_env):
    """Приветствие зовёт по ФИО из анкеты «Кабинета», логин не дублируется."""
    from api.runtime.cabinet import get_store as get_cabinet_store

    tg_router, user, _token, _FakeBot = tg_env
    get_cabinet_store().save_profile(
        user["id"], last_name="Аксёнов", first_name="Пётр", patronymic="Сергеевич"
    )
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/start")})
    text = bot.texts()[0]
    assert "Аксёнов Пётр Сергеевич" in text
    assert text.count("tg-user@test.io") == 1  # логин один раз, в скобках
    assert "<b>Меню — кнопки внизу чата</b>" in text  # разделы приветствия выделены


# ---------------------------------------------------------------------------
# Хранилище: коды, привязки, состояния чатов


def test_store_codes_and_links(tmp_path):
    from api.runtime.telegram_store import TelegramStore

    store = TelegramStore(tmp_path / "telegram")

    code = store.issue_code(1, username="u", first_name="И")
    assert len(code) == 8
    rec = store.consume_code(code)
    assert rec and rec["tg_user_id"] == 1
    assert store.consume_code(code) is None  # одноразовый

    # истёкший код не принимается
    code = store.issue_code(2)
    data = json.loads((tmp_path / "telegram" / "codes.json").read_text())
    data["items"][code]["expires_at"] = 0
    (tmp_path / "telegram" / "codes.json").write_text(json.dumps(data))
    assert store.consume_code(code) is None

    store.bind(1, "aaaaaaaaaaaa")
    assert store.user_of(1) == "aaaaaaaaaaaa"
    # перезапись: Telegram переезжает на другую учётку
    store.bind(1, "bbbbbbbbbbbb")
    assert store.user_of(1) == "bbbbbbbbbbbb"
    assert store.account_of("aaaaaaaaaaaa") is None
    # обратная отвязка со стороны платформы
    assert store.unbind_user("bbbbbbbbbbbb") == 1
    assert store.user_of(1) is None

    # состояния чатов
    store.set_chat_state(42, skill_ids=["a"], rag_ids=["rag-1"], session_id="0123456789ab")
    assert store.chat_state(42)["skill_ids"] == ["a"]
    store.set_chat_state(42, skill_ids=[])
    state = store.chat_state(42)
    assert state["skill_ids"] == [] and state["session_id"] == "0123456789ab"
    store.clear_chat_session(42)
    assert store.chat_state(42)["session_id"] == ""


# ---------------------------------------------------------------------------
# Вебхук


def test_webhook_requires_secret(tg_env, monkeypatch):
    tg_router, _user, _token, _FakeBot = tg_env
    # обработку апдейта подменяем записью: фоновая задача завершается сразу,
    # а мы видим, что апдейт дошёл до диспетчера
    received: list[dict] = []

    async def fake_handle(update, bot=None):
        received.append(update)

    monkeypatch.setattr(tg_router, "handle_update", fake_handle)
    client = TestClient(app)
    update = {"update_id": 1, "message": _message("/start")}

    # бот настроен, но секрет неверный/отсутствует
    resp = client.post("/api/telegram/webhook", json=update)
    assert resp.status_code == 403
    resp = client.post(
        "/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    )
    assert resp.status_code == 403
    assert received == []
    # правильный секрет — апдейт принят (обработка в фоне, ответ мгновенный)
    resp = client.post(
        "/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET}
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert received == [update]


def test_bot_picks_up_proxy_from_settings(tg_env, monkeypatch):
    """Хостинги с блокировкой api.telegram.org: прокси из AIOS_TG_PROXY
    доезжает до клиента Bot API."""
    from api.runtime.tg_client import TelegramBot

    monkeypatch.setenv("AIOS_TG_PROXY", "http://127.0.0.1:3128")
    get_settings.cache_clear()
    try:
        bot = TelegramBot.from_settings()
        assert bot is not None and bot._proxy == "http://127.0.0.1:3128"
    finally:
        get_settings.cache_clear()


def test_webhook_setup_reports_unreachable_api(tg_env, monkeypatch):
    """Недоступный Telegram API — внятный 502 с подсказкой, а не 500."""
    tg_router, _user, _token, _FakeBot = tg_env
    admin = UserStore(get_settings().runtime_dir / "users").create(
        login="adm@test.io", password="secret", role="Администратор", confirmed=True
    )
    admin_token = TokenStore(get_settings().runtime_dir / "auth").issue(admin["id"])
    monkeypatch.setenv("AIOS_PUBLIC_BASE_URL", "https://example.com")
    get_settings.cache_clear()

    class DeadBot(FakeBot):
        async def call(self, method, **params):
            from api.runtime.tg_client import TelegramError

            raise TelegramError(method, "сеть: ConnectTimeout")

    monkeypatch.setattr(tg_router, "TelegramBot", DeadBot)
    client = TestClient(app)
    resp = client.post(
        "/api/telegram/webhook/setup", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 502
    assert "Telegram API недоступен" in resp.json()["detail"]


def test_webhook_setup_admin_only(tg_env, monkeypatch):
    tg_router, user, _token, _FakeBot = tg_env
    users_store = UserStore(get_settings().runtime_dir / "users")
    admin = users_store.create(
        login="adm@test.io", password="secret", role="Администратор", confirmed=True
    )
    admin_token = TokenStore(get_settings().runtime_dir / "auth").issue(admin["id"])
    plain_token = TokenStore(get_settings().runtime_dir / "auth").issue(user["id"])
    client = TestClient(app)

    # не-админ не вызывает вовсе
    resp = client.post(
        "/api/telegram/webhook/setup", headers={"Authorization": f"Bearer {plain_token}"}
    )
    assert resp.status_code == 403
    # админ без AIOS_PUBLIC_BASE_URL — 503
    resp = client.post(
        "/api/telegram/webhook/setup", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 503
    # с настроенным адресом — setWebhook уходит через FakeBot
    monkeypatch.setenv("AIOS_PUBLIC_BASE_URL", "https://example.com")
    get_settings.cache_clear()
    FakeBot.CALLS.clear()
    resp = client.post(
        "/api/telegram/webhook/setup", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://example.com/api/telegram/webhook"
    assert any(m == "setWebhook" for m, _ in FakeBot.CALLS)
    assert resp.json()["webhook_info"] == {}


# ---------------------------------------------------------------------------
# /start и привязка


def test_start_issues_link_code(tg_env):
    tg_router, _user, _token, _FakeBot = tg_env
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/start")})
    text = bot.texts()[0]
    assert "код привязки" in text.lower()
    # код — 8 символов A-F0-9, извлекается из сообщения
    m = re.search(r"<code>([A-F0-9]{8})</code>", text)
    assert m
    store = tg_router.get_telegram_store()
    code_data = store.consume_code(m.group(1))
    assert code_data["tg_user_id"] == TG_USER

    # повторный /start выдаёт новый код, прежний остаётся одноразовым
    bot = _handle(tg_router, {"update_id": 2, "message": _message("/start")})
    assert re.search(r"<code>([A-F0-9]{8})</code>", bot.texts()[0])


def test_start_greets_linked_user(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/start")})
    assert "tg-user@test.io" in bot.texts()[0]


def test_link_via_auth_endpoint(tg_env):
    """Полный сценарий привязки: /start → код → подтверждение на платформе."""
    from api.runtime.telegram_store import TelegramStore

    tg_router, user, token, _FakeBot = tg_env
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/start")})
    code = re.search(r"<code>([A-F0-9]{8})</code>", bot.texts()[0]).group(1)
    store = TelegramStore(get_settings().runtime_dir / "telegram")

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    # до привязки статус пуст
    resp = client.get("/api/auth/telegram/link", headers=headers)
    assert resp.status_code == 200 and resp.json()["linked"] is False

    resp = client.post("/api/auth/telegram/link", json={"code": code}, headers=headers)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert resp.json()["username"] == "tg_user"
    assert store.user_of(TG_USER) == user["id"]

    # код одноразовый
    resp = client.post("/api/auth/telegram/link", json={"code": code}, headers=headers)
    assert resp.status_code == 400

    # статус и отвязка
    resp = client.get("/api/auth/telegram/link", headers=headers)
    assert resp.json()["linked"] is True and resp.json()["username"] == "tg_user"
    resp = client.delete("/api/auth/telegram/link", headers=headers)
    assert resp.status_code == 200
    assert store.user_of(TG_USER) is None


def test_link_rejects_foreign_telegram(tg_env):
    """Соревнование за один Telegram: код уже выдан, но аккаунт успел
    привязаться к другой учётной записи — подтверждение первому отклоняется."""
    tg_router, user, token, _FakeBot = tg_env
    other = UserStore(get_settings().runtime_dir / "users").create(
        login="other@test.io", password="secret", confirmed=True
    )
    store = tg_router.get_telegram_store()
    code = store.issue_code(TG_USER, username="tg_user", first_name="Тест")
    store.bind(TG_USER, other["id"])  # параллельно привязался другой аккаунт

    client = TestClient(app)
    resp = client.post(
        "/api/auth/telegram/link",
        json={"code": code},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "другой учётной записи" in resp.json()["detail"]
    assert store.user_of(TG_USER) == other["id"]  # привязка не перебита


# ---------------------------------------------------------------------------
# Чат: ход через run_agent_loop


def test_turn_runs_as_platform_user(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Привет!")})

    # ответ MockProvider ушёл в Telegram
    assert any("mock-провайдер" in t for t in bot.texts())
    # ход начат и завершён штатно
    store = tg_router.get_store()
    tstore = tg_router.get_telegram_store()
    state = tstore.chat_state(TG_CHAT)
    assert state["session_id"]
    session = store.get(state["session_id"])
    assert session["user_id"] == user["id"]
    assert session["messages"][0]["content"] == "Привет!"
    assert session["messages"][-1]["role"] == "assistant"
    assert session["turn"]["state"] == "done"

    # повторное сообщение продолжит ту же сессию
    sid = state["session_id"]
    _handle(tg_router, {"update_id": 2, "message": _message("Ещё вопрос")})
    assert tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"] == sid


def test_turn_requires_link(tg_env):
    tg_router, _user, _token, _FakeBot = tg_env
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Привет!")})
    assert any("/start" in t for t in bot.texts())
    assert tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"] == ""


def test_dead_link_resets(tg_env):
    """Учётная запись удалена — привязка становится невалидной, бот просит /start."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    UserStore(get_settings().runtime_dir / "users").delete(user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Привет!")})
    assert any("/start" in t for t in bot.texts())
    assert tg_router.get_telegram_store().user_of(TG_USER) is None


def test_new_command_starts_fresh_session(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    _handle(tg_router, {"update_id": 1, "message": _message("Привет!")})
    old = tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"]
    bot = _handle(tg_router, {"update_id": 2, "message": _message("/new")})
    assert any("новый диалог" in t.lower() for t in bot.texts())
    bot = _handle(tg_router, {"update_id": 3, "message": _message("Привет!")})
    new = tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"]
    assert new and new != old
    # старая сессия не тронута
    assert tg_router.get_store().get(old) is not None


def test_status_and_help(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/status")})
    assert any("не выбран" in t for t in bot.texts())
    bot = _handle(tg_router, {"update_id": 2, "message": _message("/help")})
    assert any("Вызвать агента" in t for t in bot.texts())  # инструменты — кнопками меню


def test_start_greeting_carries_menu_keyboard(tg_env):
    """Привязанному пользователю /start ставит постоянную клавиатуру меню."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/start")})
    kb = (bot.messages[-1]["reply_markup"] or {}).get("keyboard") or []
    labels = [b["text"] for row in kb for b in row]
    assert tg_router.MENU_AGENTS in labels
    assert tg_router.MENU_BASES in labels


def test_menu_buttons_route_like_commands(tg_env):
    """Кнопки постоянной клавиатуры работают как команды: агенты, базы, /new."""
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message(tg_router.MENU_AGENTS)})
    assert any("Вызов агента" in t for t in bot.texts())
    keyboard = bot.messages[-1]["reply_markup"]["inline_keyboard"]
    assert any("Обычный чат" in b["text"] for row in keyboard for b in row)

    bot = _handle(tg_router, {"update_id": 2, "message": _message(tg_router.MENU_BASES)})
    keyboard = bot.messages[-1]["reply_markup"]["inline_keyboard"]
    assert any("Без базы знаний" in b["text"] for row in keyboard for b in row)

    bot = _handle(tg_router, {"update_id": 3, "message": _message(tg_router.MENU_NEW)})
    assert any("новый диалог" in t.lower() for t in bot.texts())


def test_unlink_command(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    _handle(tg_router, {"update_id": 1, "message": _message("/unlink")})
    assert tg_router.get_telegram_store().user_of(TG_USER) is None
    bot = _handle(tg_router, {"update_id": 2, "message": _message("Привет!")})
    assert any("/start" in t for t in bot.texts())


# ---------------------------------------------------------------------------
# Выбор агента и RAG-базы


def _callback(cb_data: str) -> dict:
    return {
        "update_id": 99,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": TG_USER, "username": "tg_user"},
            "message": {"message_id": 10, "chat": {"id": TG_CHAT, "type": "private"}},
            "data": cb_data,
        },
    }


def test_agents_selection(tg_env):
    from api.runtime import datarepo

    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/agents")})
    keyboard = bot.messages[-1]["reply_markup"]["inline_keyboard"]
    assert any("Обычный чат" in b["text"] for row in keyboard for b in row)
    # каждый агент — отдельная строка клавиатуры: в общем ряду Telegram
    # сплющил бы кнопки и обрезал названия
    assert all(len(row) == 1 or any("◀️" in b["text"] or "▶️" in b["text"] for b in row)
               for row in keyboard)

    skill_id = datarepo.skills()[0]["id"]
    bot = _handle(tg_router, _callback(f"ag:{skill_id}"))
    assert any("подключён" in t for t in bot.texts())
    state = tg_router.get_telegram_store().chat_state(TG_CHAT)
    assert state["skill_ids"] == [skill_id]
    assert state["session_id"] == ""  # агент всегда начинает новый диалог

    # следующий ход пойдёт в новую сессию с агентом
    _handle(tg_router, {"update_id": 2, "message": _message("Привет!")})
    session = tg_router.get_store().get(
        tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"]
    )
    assert session["skill_ids"] == [skill_id]
    assert session["turn"]["state"] == "done"


def test_agent_selection_requires_link(tg_env):
    from api.runtime import datarepo

    tg_router, _user, _token, _FakeBot = tg_env
    skill_id = datarepo.skills()[0]["id"]
    bot = _handle(tg_router, _callback(f"ag:{skill_id}"))
    assert ("cb-1", "Сначала привяжите аккаунт: /start") in bot.callbacks
    assert tg_router.get_telegram_store().chat_state(TG_CHAT)["skill_ids"] == []


def test_bases_selection(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])
    bot = _handle(tg_router, {"update_id": 1, "message": _message("/base")})
    keyboard = bot.messages[-1]["reply_markup"]["inline_keyboard"]
    assert any("Без базы знаний" in b["text"] for row in keyboard for b in row)
    # без опубликованных баз каталог показывает только пустой выбор
    bot = _handle(tg_router, _callback("rg:none"))
    state = tg_router.get_telegram_store().chat_state(TG_CHAT)
    assert state["rag_ids"] == []


def test_group_chats_ignored(tg_env):
    tg_router, _user, _token, _FakeBot = tg_env
    message = _message("/start", chat_id=-100500)
    message["chat"]["type"] = "supergroup"
    bot = _handle(tg_router, {"update_id": 1, "message": message})
    assert bot.messages == []


# --- маркдаун ответов модели -> HTML Telegram (правка 30.09.2026) ---

def test_md_italic_bullets_hrule():
    from api.runtime.tg_client import md_to_telegram_html

    out = md_to_telegram_html("*курсив* и ***оба***, 2*3*4\n\n- пункт\n  - вложенный\n\n---")
    assert "<i>курсив</i>" in out
    assert "<b><i>оба</i></b>" in out
    assert "2*3*4" in out  # умножение не курсив
    assert "• пункт" in out and "  • вложенный" in out
    assert "──────────" in out


def test_md_table_becomes_cards():
    from api.runtime.tg_client import md_to_telegram_html

    out = md_to_telegram_html(
        "| Работа | Журнал | DOI |\n| --- | --- | --- |\n| Статья А | Nature | 10.1/a |\n| B & C | Gut | 10.2/b |"
    )
    assert "|" not in out
    assert "<b>Статья А</b>" in out
    assert "<i>Журнал:</i> Nature" in out
    assert "<i>DOI:</i> 10.2/b" in out
    assert "<b>B &amp; C</b>" in out  # экранирование в ячейках


def test_md_code_is_not_formatted_inside():
    from api.runtime.tg_client import md_to_telegram_html

    out = md_to_telegram_html("`**не жирный**` и\n```python\nx = 2 * 3 * 4 < 5\n```")
    assert "<code>**не жирный**</code>" in out
    assert "<b>" not in out
    assert "x = 2 * 3 * 4 &lt; 5" in out
    assert "<i>" not in out


def test_md_output_tags_balanced():
    from html.parser import HTMLParser
    from api.runtime.tg_client import md_to_telegram_html, telegram_chunks

    class Chk(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], False

        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                self.bad = True

    sample = ("## Заголовок с **жирным**\n\n**жирный *вложенный курсив* тут**\n\n"
              "| a | b |\n| - | - |\n| **x** | `y` |\n\n> цитата\n\n```\n<b>не тег</b>\n```\n")
    for chunk in telegram_chunks(sample):
        chk = Chk()
        chk.feed(md_to_telegram_html(chunk))
        assert not chk.bad and not chk.stack


def test_send_message_markdown_converts_and_falls_back():
    import asyncio
    from api.runtime.tg_client import TelegramBot, TelegramError

    sent: list[dict] = []

    async def ok_call(self, method, **params):
        sent.append(params)
        return {"message_id": 1}

    bot = TelegramBot.__new__(TelegramBot)
    TelegramBot.call, orig = ok_call, TelegramBot.call
    try:
        asyncio.run(bot.send_message(1, "**жирный**", markdown=True))
        assert sent[0]["text"] == "<b>жирный</b>" and sent[0]["parse_mode"] == "HTML"

        sent.clear()

        async def reject_html(self, method, **params):
            sent.append(params)
            if params.get("parse_mode"):
                raise TelegramError(method, "can't parse entities", 400)
            return {"message_id": 2}

        TelegramBot.call = reject_html
        asyncio.run(bot.send_message(1, "**жирный** <x", markdown=True))
        assert len(sent) == 2
        assert "parse_mode" not in sent[1]
        assert sent[1]["text"] == "**жирный** <x"  # запасной вариант — исходный текст
    finally:
        TelegramBot.call = orig


def test_register_bot_commands_sends_menu_and_swallows_errors():
    import asyncio
    from api.routers import telegram as tg
    from api.runtime.tg_client import TelegramError

    names = {c["command"] for c in tg.BOT_COMMANDS}
    assert {"new", "status", "help", "unlink"} <= names
    # агенты и базы — не команды, а кнопки меню (сами /agents и /base ещё работают)
    assert "agents" not in names and "base" not in names
    assert "start" not in names
    assert all(1 <= len(c["description"]) <= 256 for c in tg.BOT_COMMANDS)

    class B:
        def __init__(self, fail=False):
            self.calls, self.fail = [], fail

        async def call(self, method, **params):
            self.calls.append((method, params))
            if self.fail:
                raise TelegramError(method, "boom", 500)
            return True

    ok = B()
    asyncio.run(tg.register_bot_commands(ok))
    assert ok.calls == [("setMyCommands", {"commands": tg.BOT_COMMANDS})]

    bad = B(fail=True)
    asyncio.run(tg.register_bot_commands(bad))  # исключение не наружу
    assert len(bad.calls) == 1


def test_turn_logs_one_summary_line(tg_env, monkeypatch, caplog):
    """Каждый ход пишет ровно одну INFO-строку: пользователь, статус, длительность,
    число инструментов, файлов и кусков."""
    import logging

    tg_router, user, _token, FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])

    class PlainProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text", "Короткий ответ.")

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: PlainProvider())
    with caplog.at_level(logging.INFO, logger="aios.telegram"):
        _handle(tg_router, {"update_id": 1, "message": _message("Привет")})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Ход:")]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "status=ok" in line
    assert f"user={user['id']}" in line
    assert "tools=0" in line
    assert "files=0" in line
    assert "chunks=1" in line
    assert "duration=" in line


def test_turn_shows_progress_status_and_removes_it(tg_env, monkeypatch):
    """Вызов инструмента даёт одно сообщение статуса: оно правится при смене
    инструмента и удаляется, ответ приходит отдельным сообщением."""
    tg_router, user, _token, FakeBot = tg_env
    _link(tg_router, TG_USER, user["id"])

    class PlainProvider:
        async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
            yield ("text", "Готово.")

    async def fake_events(session, provider, text):
        yield {"type": "tool_call", "id": "1", "name": "chart_generate", "arguments": {}}
        yield {"type": "tool_result", "id": "1", "name": "chart_generate", "ok": True, "result": {}}
        yield {"type": "tool_call", "id": "2", "name": "other_tool", "arguments": {}}
        yield {"type": "delta", "text": "Готово."}
        yield {"type": "done"}

    monkeypatch.setattr(tg_router, "get_provider", lambda *a, **k: PlainProvider())
    monkeypatch.setattr(tg_router, "run_agent_loop_events", fake_events)
    monkeypatch.setattr(tg_router, "_STATUS_MIN_INTERVAL", 0.0)
    bot = _handle(tg_router, {"update_id": 1, "message": _message("Построй график")})
    assert bot.messages[0]["text"] == "📊 Строю график…"
    assert len(bot.edits) == 1 and bot.edits[0][2] == "⚙️ Работаю…"
    assert len(bot.deletes) == 1 and bot.deletes[0][1] == 1
    assert any("Готово." in m["text"] for m in bot.messages[1:])


def test_tool_status_names():
    """Человекочитаемые статусы: точные имена, префиксы chem_/rxn_, запасной вариант."""
    from api.routers import telegram as tg_router

    assert tg_router._status_for("literature_search") == "🔎 Ищу литературу…"
    assert tg_router._status_for("make_document") == "📄 Собираю документ…"
    assert tg_router._status_for("chem_depict") == "🧪 Работаю с химией…"
    assert tg_router._status_for("rxn_forward") == "🧪 Работаю с химией…"
    assert tg_router._status_for("notify") == tg_router._DEFAULT_STATUS
    assert tg_router._status_for("") == tg_router._DEFAULT_STATUS


def test_telegram_logger_emits_info_without_caplog():
    import logging as _logging
    lg = _logging.getLogger("aios.telegram")
    assert lg.level == _logging.INFO
    assert lg.handlers


def _prep_choices(tg_router, user):
    _link(tg_router, TG_USER, user["id"])
    tg_router._PRESSED_CHOICES.clear()
    tg_router.get_telegram_store().set_chat_state(TG_CHAT, choices=["10 работ", "25 работ"])


def _user_texts(tg_router):
    sid = tg_router.get_telegram_store().chat_state(TG_CHAT)["session_id"]
    session = tg_router.get_store().get(sid)
    return [m["content"] for m in session["messages"] if m["role"] == "user"]


def test_choice_button_replaced_and_not_repeatable(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _prep_choices(tg_router, user)
    bot = _handle(tg_router, _callback("ch:1"))
    assert len(bot.markup_edits) == 1
    button = bot.markup_edits[0][2]["inline_keyboard"][0][0]
    assert button["text"] == "✅ Выбрано: 25 работ"
    assert button["callback_data"] == "noop"
    before = len(_user_texts(tg_router))

    bot2 = _handle(tg_router, _callback("ch:0"))
    assert ("cb-1", "Вариант уже выбран") in bot2.callbacks
    assert not bot2.markup_edits
    assert len(_user_texts(tg_router)) == before

    bot3 = _handle(tg_router, _callback("noop"))
    assert ("cb-1", "") in bot3.callbacks


def test_choice_button_kept_when_chat_busy(tg_env):
    tg_router, user, _token, _FakeBot = tg_env
    _prep_choices(tg_router, user)
    tg_router._RUNNING_CHATS.add(str(TG_CHAT))
    try:
        bot = _handle(tg_router, _callback("ch:1"))
    finally:
        tg_router._RUNNING_CHATS.discard(str(TG_CHAT))
    assert ("cb-1", "Прошлый ответ ещё готовится") in bot.callbacks
    assert not bot.markup_edits
    assert (str(TG_CHAT), 10) not in tg_router._PRESSED_CHOICES

    bot = _handle(tg_router, _callback("ch:1"))
    assert len(bot.markup_edits) == 1
