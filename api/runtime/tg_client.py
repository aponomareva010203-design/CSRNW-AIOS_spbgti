"""Минимальный клиент Telegram Bot API — httpx без SDK (в духе платформы:
провайдер LLM тоже написан руками, api/runtime/providers.py).

Бот API — обычный JSON-RPC поверх HTTPS: POST
https://api.telegram.org/bot{token}/{метод} → {"ok": true, "result": …}.
Нужны всего несколько методов, поэтому SDK (aiogram) не тянем.

Разметка ответа: модель пишет GFM-маркдаун, Telegram понимает узкое
подмножество HTML. md_to_telegram_html() конвертирует осторожно (только
то, что есть в Telegram: <b> <i> <code> <pre> <a>); если разметку не
приняли (400 — почти всегда из-за незакрытого тега на обрезке),
send_message повторяет отправку чистым текстом — ответ не теряется.
"""

import asyncio
import html
import re

import httpx

from .config import get_settings

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096  # предел Telegram на одно сообщение
CHUNK_SEPARATOR = "\n\n"

# DPI-фильтры хостингов к Telegram «плавают»: TCP открывается, TLS до API
# периодически буксует. Повторяем только НЕОТКРЫВШЕЕСЯ соединение — такой
# запрос гарантированно не ушёл, дубли исключены даже для sendMessage.
CONNECT_RETRY_DELAYS = (0.0, 1.0, 3.0)


class TelegramError(RuntimeError):
    """Ошибка вызова Bot API: неверный токен, отклонённый запрос, сеть."""

    def __init__(self, method: str, description: str, status_code: int = 0,
                 connect_failed: bool = False):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.status_code = status_code
        self.connect_failed = connect_failed


def split_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Жёсткая резка плоского текста на куски ≤ limit: по абзацам, затем по
    строкам, затем посимвольно. Для ответов с разметкой используйте
    split_markdown_chunks — она режет по блокам и сохраняет разметку."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        window = text[:limit]
        cut = window.rfind(CHUNK_SEPARATOR)
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n").lstrip()
    return chunks or [""]


# Запас под разрастание разметки при конвертации (<b> и <blockquote> добавляют
# символы к маркдауну): кусок маркдауна режется меньше лимита Telegram, чтобы
# сконвертированный HTML гарантированно влезал в 4096.
MD_CHUNK_LIMIT = 3600

_FENCE_SPLIT_RE = re.compile(r"(```[^\S\n]*[^\n`]*\n.*?```)", re.DOTALL)


def _markdown_blocks(text: str) -> list[str]:
    """Блоки маркдауна в порядке следования: ограждённые блоки кода целиком
    (с ограждениями) и абзацы (текст между ними, разбитый по пустым строкам)."""
    blocks: list[str] = []
    pos = 0
    for match in _FENCE_SPLIT_RE.finditer(text):
        between = text[pos : match.start()]
        blocks.extend(p for p in re.split(r"\n\s*\n", between) if p.strip())
        blocks.append(match.group(1))
        pos = match.end()
    blocks.extend(p for p in re.split(r"\n\s*\n", text[pos:]) if p.strip())
    return blocks


def split_markdown_chunks(text: str, limit: int = MD_CHUNK_LIMIT) -> list[str]:
    """Режет GFM-ответ на самостоятельные куски ≤ limit ДО конвертации в HTML.

    Каждый кусок конвертируется отдельно (md_to_telegram_html), поэтому
    разрез, попавший внутрь тега, давал бы незакрытый HTML — Telegram
    отвечал бы 400, и ответ уходил бы чистым текстом со «звёздочками».
    Здесь абзацы и блоки кода неделимы: блок кода больше лимита делится
    на несколько законченных <pre>-блоков, огромный абзац — жёсткой резкой.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    for block in _markdown_blocks(text):
        if block.startswith("```") and len(block) > limit:
            # длинный блок кода: тело режем по строкам на законченные блоки
            flush()
            lines = block.split("\n")
            fence_open, fence_close = lines[0], lines[-1]
            body_lines = lines[1:-1]
            piece = ""
            for line in body_lines:
                candidate = f"{piece}\n{line}" if piece else line
                if len(candidate) + len(fence_open) + len(fence_close) + 2 > limit and piece:
                    chunks.append(f"{fence_open}\n{piece}\n{fence_close}")
                    piece = line[: limit - len(fence_open) - len(fence_close) - 2]
                else:
                    piece = candidate
            if piece.strip():
                chunks.append(f"{fence_open}\n{piece}\n{fence_close}")
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        flush()
        if len(block) <= limit:
            current = block
        else:
            # огромный абзац без пустых строк — жёсткая резка (без разметки,
            # каждая часть конвертируется независимо и остаётся сбалансированной)
            chunks.extend(split_text(block, limit))
    flush()
    return chunks or [""]


def telegram_chunks(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Куски маркдауна, чья конвертация в HTML гарантированно влезает в лимит
    Telegram. Разметка раздувает текст (<b>, <blockquote>, длинные href в
    <a>), поэтому кусок из split_markdown_chunks мог стать длиннее 4096 уже
    после конвертации — такой кусок делим мельче и проверяем снова.
    Дополнительная резка идёт через _piece_chunks без обратной склейки:
    split_markdown_chunks здесь свёл бы разрезанное обратно в один блок."""
    pieces: list[str] = []
    for chunk in split_markdown_chunks(text):
        if len(md_to_telegram_html(chunk)) <= limit:
            pieces.append(chunk)
        else:
            pieces.extend(_piece_chunks(chunk, limit))
    return pieces or [""]


def _piece_chunks(piece: str, limit: int) -> list[str]:
    if len(md_to_telegram_html(piece)) <= limit or len(piece) <= 100:
        # мелкий, но раздутый кусок оставляем: send_message при отказе
        # Telegram уйдёт на отправку без разметки — ответ не потеряется
        return [piece]
    out: list[str] = []
    for part in split_text(piece, len(piece) // 2):
        out.extend(_piece_chunks(part, limit))
    return out


_FENCE_RE = re.compile(r"```[^\S\n]*([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _blockquote_sub(text: str) -> str:
    """Строки «&gt; …» (уже экранированные) — в <blockquote> Telegram
    (Bot API 7.0): цитаты источников из RAG-ответов читаются как цитаты."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("<blockquote>" + "\n".join(buf) + "</blockquote>")
            buf.clear()

    for line in text.split("\n"):
        if line.startswith("&gt; "):
            buf.append(line[5:])
        elif line == "&gt;":
            buf.append("")
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=\S)([^*\n<>]+?)(?<=\S)\*\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?=[^\s*])([^*\n<>]+?)(?<=[^\s*])\*(?![*\w])")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_HRULE_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _tables_to_cards(text: str) -> str:
    """GFM-таблица -> карточки строк: первая колонка жирным, остальные
    «*Колонка:* значение». Широкая таблица в Telegram разъезжается, карточки
    читаются на телефоне."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (i + 1 < len(lines) and "|" in lines[i] and "|" in lines[i + 1]
                and _TABLE_SEP_RE.match(lines[i + 1])):
            header = _split_row(lines[i])
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row = _split_row(lines[i])
                row += [""] * (len(header) - len(row))
                title = row[0].replace("**", "") or "-"
                out.append(f"**{title}**")
                for label, cell in zip(header[1:], row[1:]):
                    if cell:
                        out.append(f"*{label.replace('*', '')}:* {cell}")
                out.append("")
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def md_to_telegram_html(text: str) -> str:
    """GFM-маркдаун -> подмножество HTML Telegram (parse_mode="HTML").

    Блоки и inline-код прячутся до экранирования (внутри них разметка не
    действует), таблицы становятся карточками, затем жирный, курсив, заголовки,
    ссылки, цитаты, маркеры списков. Всё экранируется: невалидные теги из
    ответа модели безопасны."""
    blocks: list[str] = []

    def stash(fragment: str) -> str:
        blocks.append(fragment)
        return f"\x00{len(blocks) - 1}\x00"

    def stash_fence(match: re.Match) -> str:
        lang = (match.group(1) or "").strip()
        body = html.escape(match.group(2).rstrip("\n"))
        if lang:
            return stash(f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>')
        return stash(f"<pre>{body}</pre>")

    text = _FENCE_RE.sub(stash_fence, text)
    text = _tables_to_cards(text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = html.escape(text)
    text = _HRULE_RE.sub("──────────", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_ITALIC_RE.sub(lambda m: f"<b><i>{m.group(1)}</i></b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _HEADER_RE.sub(lambda m: "<b>" + re.sub(r"</?b>", "", m.group(2)) + "</b>", text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _blockquote_sub(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


class TelegramBot:
    """Асинхронный клиент Bot API одного бота (по токену из @BotFather)."""

    def __init__(self, token: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 proxy: str | None = None):
        self.token = token
        self._transport = transport  # для тестов (httpx.MockTransport)
        self._proxy = proxy  # хостинги, где api.telegram.org недоступен напрямую
        self.timeout = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)

    @classmethod
    def from_settings(cls) -> "TelegramBot | None":
        """Бот текущего процесса или None, если токен не задан (бот выключен)."""
        settings = get_settings()
        if not settings.tg_bot_token:
            return None
        return cls(settings.tg_bot_token, proxy=settings.tg_proxy or None)

    @property
    def base_url(self) -> str:
        return f"{TELEGRAM_API}/bot{self.token}"

    async def _retry_connect(self, make_call):
        """Повторы с паузой 1/3 с — только когда соединение не открылось."""
        for attempt, delay in enumerate(CONNECT_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await make_call()
            except TelegramError as exc:
                if not exc.connect_failed or attempt == len(CONNECT_RETRY_DELAYS) - 1:
                    raise

    async def call(self, method: str, **params) -> dict:
        """Вызов метода Bot API; не-2xx или ok=false — TelegramError."""
        return await self._retry_connect(lambda: self._call_once(method, params))

    async def _call_once(self, method: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                proxy=self._proxy,
            ) as client:
                resp = await client.post(method, json=params)
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            raise TelegramError(
                method, f"сеть: {type(exc).__name__}: {exc}", connect_failed=True
            ) from exc
        except httpx.HTTPError as exc:
            raise TelegramError(method, f"сеть: {type(exc).__name__}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(method, f"не-JSON ответ (HTTP {resp.status_code})") from exc
        if resp.status_code >= 400 or not data.get("ok"):
            raise TelegramError(
                method, str(data.get("description") or f"HTTP {resp.status_code}"), resp.status_code
            )
        return data.get("result", {})

    async def send_message(self, chat_id: int | str, text: str,
                           reply_markup: dict | None = None, markdown: bool = False) -> dict:
        """markdown=True: текст - маркдаун модели, уходит конвертированным в
        HTML; при отказе Telegram (400) - повтор исходным текстом без разметки.
        markdown=False: прежнее поведение (текст уже готов)."""
        if not markdown:
            return await self._send_raw(chat_id, text, reply_markup)
        params: dict = {"chat_id": chat_id, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            return await self.call("sendMessage", **params, text=md_to_telegram_html(text),
                                   parse_mode="HTML")
        except TelegramError as exc:
            if exc.status_code != 400:
                raise
            import logging
            logging.getLogger(__name__).warning("Telegram отклонил HTML-разметку, шлю текстом: %s", exc)
        return await self.call("sendMessage", **params, text=text)

    async def _send_raw(self, chat_id: int | str, text: str,
                        reply_markup: dict | None = None) -> dict:
        """Отправка сообщения: сначала с HTML-разметкой, при отказе API —
        повтор без неё (кривая разметка не должна терять ответ)."""
        params: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            return await self.call("sendMessage", **params, parse_mode="HTML")
        except TelegramError as exc:
            if exc.status_code != 400:
                raise
            params.pop("parse_mode", None)
            return await self.call("sendMessage", **params)

    async def send_typing(self, chat_id: int | str) -> None:
        """«Печатает…» (живёт ~5 с); фоновый ход обновляет его по кругу."""
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass  # индикатор не критичен

    async def send_photo(self, chat_id: int | str, png: bytes, caption: str = "") -> None:
        """Фото multipart-загрузкой (графики chart_generate); ошибки — наружу."""
        params: dict = {"chat_id": str(chat_id)}
        if caption:
            params["caption"] = caption[:1000]

        async def once() -> dict:
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout,
                    transport=self._transport, proxy=self._proxy,
                ) as client:
                    resp = await client.post(
                        "sendPhoto",
                        data=params,
                        files={"photo": ("chart.png", png, "image/png")},
                    )
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                raise TelegramError(
                    "sendPhoto", f"сеть: {type(exc).__name__}: {exc}", connect_failed=True
                ) from exc
            except httpx.HTTPError as exc:
                raise TelegramError("sendPhoto", f"сеть: {type(exc).__name__}: {exc}") from exc
            try:
                result = resp.json()
            except ValueError as exc:
                raise TelegramError("sendPhoto", f"не-JSON ответ (HTTP {resp.status_code})") from exc
            if resp.status_code >= 400 or not result.get("ok"):
                raise TelegramError(
                    "sendPhoto",
                    str(result.get("description") or f"HTTP {resp.status_code}"),
                    resp.status_code,
                )
            return result.get("result", {})

        return await self._retry_connect(once)

    async def send_document(self, chat_id: int | str, content: bytes, filename: str,
                            caption: str = "") -> None:
        """Файл документом (pdf/docx/… — то, что нельзя показать картинкой)."""
        params: dict = {"chat_id": str(chat_id)}
        if caption:
            params["caption"] = caption[:1000]

        async def once() -> dict:
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout,
                    transport=self._transport, proxy=self._proxy,
                ) as client:
                    resp = await client.post(
                        "sendDocument",
                        data=params,
                        files={"document": (filename, content,
                                            "application/octet-stream")},
                    )
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                raise TelegramError(
                    "sendDocument", f"сеть: {type(exc).__name__}: {exc}", connect_failed=True
                ) from exc
            except httpx.HTTPError as exc:
                raise TelegramError("sendDocument", f"сеть: {type(exc).__name__}: {exc}") from exc
            try:
                result = resp.json()
            except ValueError as exc:
                raise TelegramError("sendDocument", f"не-JSON ответ (HTTP {resp.status_code})") from exc
            if resp.status_code >= 400 or not result.get("ok"):
                raise TelegramError(
                    "sendDocument",
                    str(result.get("description") or f"HTTP {resp.status_code}"),
                    resp.status_code,
                )
            return result.get("result", {})

        return await self._retry_connect(once)

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        try:
            await self.call(
                "answerCallbackQuery",
                callback_query_id=callback_query_id,
                **({"text": text} if text else {}),
            )
        except TelegramError:
            pass  # подтверждение нажатия не критично

    async def edit_message(self, chat_id: int | str, message_id: int, text: str,
                           reply_markup: dict | None = None) -> None:
        """Правка сообщения (например, отметка решения по подтверждению)."""
        params: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            await self.call("editMessageText", **params, parse_mode="HTML")
        except TelegramError as exc:
            if exc.status_code == 400:
                params.pop("parse_mode", None)
                await self.call("editMessageText", **params)
            elif "message is not modified" in str(exc).lower():
                return
            else:
                raise

    async def edit_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict) -> None:
        """Замена клавиатуры под сообщением, текст не трогается."""
        try:
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                            reply_markup=reply_markup)
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        """Удаление сообщения (например, статуса прогресса); ошибки Telegram не критичны."""
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass
