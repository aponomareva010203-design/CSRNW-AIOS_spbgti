"""HTTP-слой харнесса: SSE-чат, сессии, статус рантайма.

POST /api/chat          — сообщение ассистенту; ответ — поток SSE-событий;
                          необязательный заголовок Authorization: Bearer
                          привязывает сессию к учётной записи (анализ сессий
                          для скрытого «Контекста о пользователе» в «Кабинете»);
POST /api/chat/attachments — загрузка вложения к сообщению (multipart,
                          только по Bearer); текст pdf/txt/md подмешивается
                          в сообщение пользователя при отправке;
GET  /api/chat/attachments/{id} — скачивание вложения владельцем;
GET  /api/sessions      — список сессий (сводки);
GET  /api/sessions/{id} — полная сессия (транскрипт);
DELETE /api/sessions/{id};
GET  /api/runtime       — статус рантайма: провайдер, инструменты.

Формат SSE: строки «data: {json}\n\n», события типов session/delta/tool_call/
tool_result/done/error, затем «data: [DONE]\n\n». Пока модель думает (до первых
токенов может пройти минута и больше), каждые HEARTBEAT_INTERVAL секунд уходит
SSE-комментарий «: keep-alive\n\n»: клиент его не разбирает, но байты в канале
не дают браузеру (WebKit рвёт «немой» стрим ~60 c — у пользователя это выглядит
как «Load failed») и промежуточным прокси считать соединение зависшим.

Обрыв соединения (панель чата свернули, ушли на другую страницу) не убивает
turn: генерация дорабатывает в фоне (detach_background_turn) и дописывает
финальный ответ в транскрипт сессии.
"""

import asyncio
import contextlib
import json
from functools import lru_cache

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..runtime import uploads
from ..runtime.config import get_settings
from ..runtime.datarepo import get_agent, get_skill
from ..runtime.llm_settings import effective_config
from ..runtime.loop import run_agent_loop
from ..runtime.providers import ProviderError, build_provider
from ..runtime.sessions import SessionError, SessionStore
from ..runtime.tools import build_default_registry
from .auth import get_current_user

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    """Тело запроса POST /api/chat. Модель задаёт администратор централизованно."""

    message: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = None
    agent_id: str | None = Field(default=None, max_length=64)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    rag_ids: list[str] = Field(default_factory=list, max_length=3)


@lru_cache(maxsize=1)
def get_store() -> SessionStore:
    """Синглтоны рантайма; в тестах сбрасываются cache_clear()."""
    return SessionStore(get_settings().runtime_dir / "sessions")


@router.post("/chat/attachments", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Загрузка вложения к сообщению чата (только владелец токена).

    Возвращает дескриптор {attachment_id, filename, size}: клиент показывает
    файл чипсами у поля ввода и присылает attachment_id в POST /api/chat
    (attachment_ids). Текст pdf/txt/md при отправке подмешивается в сообщение
    пользователя (attachment_context_block), docx хранится только для
    скачивания. Лимиты и допустимые типы — в runtime/uploads.py.
    """
    store = uploads.get_store()
    content = await file.read()
    try:
        meta = store.save(user["id"], file.filename or "", content)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # дескриптор для клиента: attachment_id уходит в POST /api/chat
    return {
        "attachment_id": meta["id"],
        "filename": meta["filename"],
        "size": meta["size"],
    }


@router.get("/chat/attachments/{attachment_id}")
def download_attachment(attachment_id: str, user: dict = Depends(get_current_user)):
    """Скачивание вложения владельцем токена (чужие — 404, как у карточек)."""
    store = uploads.get_store()
    meta = store.get(user["id"], attachment_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    path = store.content_path(user["id"], meta)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    return FileResponse(path, filename=meta["filename"])


@lru_cache(maxsize=1)
def get_registry():
    return build_default_registry(get_settings().tools_root)


@lru_cache(maxsize=1)
def get_provider():
    return build_provider(effective_config(), read_timeout=get_settings().request_timeout)


# Секунды тишины в SSE-потоке, после которых уходит keep-alive (см. event_stream).
HEARTBEAT_INTERVAL = 15.0

# Фоновые turn'ы: если соединение с клиентом оборвалось (чат свернули, ушли на
# другую страницу, «Load failed» у WebKit), генерация не выбрасывается, а
# дорабатывает в фоне и дописывает финальный ответ в транскрипт сессии — при
# повторном открытии чата восстановление истории показывает ответ целиком.
# Сильные ссылки удерживают задачи от сборки мусора (см. замечание asyncio
# к create_task); watchdog ограничивает время жизни, лимит — количество.
_BACKGROUND_TURNS: set[asyncio.Task] = set()
BACKGROUND_TURN_TIMEOUT = 600.0  # секунд на фоновый turn (watchdog)
MAX_BACKGROUND_TURNS = 8  # максимум одновременно «висящих» фоновых turn'ов


def detach_background_turn(producer: asyncio.Task) -> bool:
    """Оставляет генерацию дорабатывать в фоне после обрыва клиента.

    Возвращает False, если лимит параллельных фоновых turn'ов исчерпан, —
    тогда вызывающий код гасит задачу (прежнее поведение: turn умирает вместе
    с соединением).
    """
    if len(_BACKGROUND_TURNS) >= MAX_BACKGROUND_TURNS:
        return False

    async def watch() -> None:
        """Watchdog: даёт turn'у доработать, но не вечно."""
        try:
            # wait не отменяет задачу по таймауту — гасим вручную
            _, pending = await asyncio.wait({producer}, timeout=BACKGROUND_TURN_TIMEOUT)
            if pending:
                producer.cancel()
        finally:
            _BACKGROUND_TURNS.discard(watcher)

    watcher = asyncio.create_task(watch())
    _BACKGROUND_TURNS.add(producer)
    _BACKGROUND_TURNS.add(watcher)
    producer.add_done_callback(_BACKGROUND_TURNS.discard)
    return True


def _token_user_id(authorization: str | None) -> str | None:
    """Необязательная авторизация чата: id пользователя по Bearer-токену.

    Без заголовка чат работает анонимно (user_id = None). Неверный токен
    приравнивается к анонимному — вход остаётся отдельным шагом интерфейса.
    Привязка нужна, чтобы планировщик «Контекста о пользователе» анализировал
    именно сессии владельца учётной записи.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    # хранилище токенов живёт в роутере auth; импорт здесь — без цикла,
    # так как auth импортирует chat только лениво (в admin_set_llm)
    from .auth import get_token_store

    return get_token_store().resolve(token)



def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/runtime")
def runtime_status():
    """Публичный статус рантайма: настройка LLM и доступные инструменты."""
    provider = get_provider()
    cfg = effective_config()
    return {
        "provider": type(provider).__name__ if provider is not None else "not_configured",
        "mode": "live" if provider is not None else "not_configured",
        "default_model": cfg.model or None,
        "tools": get_registry().names(),
    }


@router.post("/chat")
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    provider = get_provider()
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM не настроена администратором (админ-панель → Настройки внешних API)",
        )
    if req.agent_id and get_agent(req.agent_id) is None:
        raise HTTPException(status_code=400, detail=f"Неизвестный агент: {req.agent_id}")
    for skill_id in req.skill_ids:
        if get_skill(skill_id) is None:
            raise HTTPException(status_code=400, detail=f"Неизвестный навык: {skill_id}")

    cfg = effective_config()
    store = get_store()
    # необязательная привязка диалога к учётной записи (для «Кабинета»)
    user_id = _token_user_id(authorization)

    # Вложения сообщения: резолвим до создания сессии. Чат анонимен без
    # токена, а вложения привязаны к учётной записи — без входа отклоняем.
    user_text = req.message
    user_attachments: list[dict] = []
    if req.attachment_ids:
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="Вложения доступны только авторизованным пользователям",
            )
        try:
            attachment_metas = uploads.get_store().resolve_many(user_id, req.attachment_ids)
        except uploads.UploadError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        contents = []
        for meta in attachment_metas:
            path = uploads.get_store().content_path(user_id, meta)
            try:
                text = uploads.extract_text(meta["filename"], path.read_bytes()) if path.is_file() else ""
            except Exception:  # noqa: BLE001 — битый файл не должен ломать отправку
                text = ""
            contents.append(text)
            user_attachments.append(
                {"attachment_id": meta["id"], "filename": meta["filename"]}
            )
        user_text = f"{req.message}\n\n{uploads.attachment_context_block(attachment_metas, contents)}"

    if req.session_id:
        session = store.get(req.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        owner = session.get("user_id")
        if owner and user_id and owner != user_id:
            # чужая сессия ничем не отличается от несуществующей
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        if not owner and user_id:
            session["user_id"] = user_id  # анонимная сессия усыновляется владельцем токена
        # Запуск навыка всегда начинает новую сессию: пришёл набор навыков,
        # отличный от подключённого в этой, — значит, пользователь запускает
        # другой навык (например, «Формулировку гипотез» после онбординга).
        # Старую сессию не трогаем; навык стартует в свежей с первого шага
        # пайплайна, без чужого контекста и без каких-либо подтверждений.
        if session.get("skill_ids") != list(req.skill_ids):
            owner_id = session.get("user_id")
            session = store.create(
                title=req.message[:80],
                model=cfg.model,
                agent_id=req.agent_id,
                skill_ids=req.skill_ids,
            )
            if owner_id:
                session["user_id"] = owner_id
            store.save(session)
    else:
        session = store.create(
            title=req.message[:80],
            model=cfg.model,
            agent_id=req.agent_id,
            skill_ids=req.skill_ids,
        )
        if user_id:
            session["user_id"] = user_id
    session["model"] = cfg.model
    if req.agent_id:
        session["agent_id"] = req.agent_id
    if req.skill_ids:
        session["skill_ids"] = list(req.skill_ids)
    # RAG-базы, подключённые кнопкой в чате: не влияют на решение о новой
    # сессии (в отличие от skill_ids) — подключение не должно сбрасывать
    # текущий диалог; сохраняем как есть, включая пустой список (отключение).
    session["rag_ids"] = list(req.rag_ids)
    store.save(session)

    async def event_stream():
        yield _sse({"type": "session", "id": session["id"], "title": session["title"]})
        queue: asyncio.Queue = asyncio.Queue()

        async def produce() -> None:
            """Перекладывает события цикла в очередь; ошибка — событием error."""
            try:
                async for event in run_agent_loop(
                    settings=get_settings(),
                    provider=get_provider(),
                    registry=get_registry(),
                    store=store,
                    session=session,
                    user_text=user_text,
                    user_attachments=user_attachments or None,
                ):
                    await queue.put(event)
            except ProviderError as exc:
                await queue.put({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 — ошибка уходит клиенту событием
                await queue.put(
                    {"type": "error", "message": f"Внутренняя ошибка: {type(exc).__name__}: {exc}"}
                )
            finally:
                await queue.put(None)  # сигнал конца потока

        producer = asyncio.create_task(produce())
        completed = False
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    # Модель ещё думает. Комментарий SSE клиент не разбирает,
                    # но байты в канале сбрасывают таймауты простоя браузера
                    # и прокси — соединение живёт, сколько нужно модели.
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    completed = True
                    break
                yield _sse(event)
        finally:
            # Поток дочитан клиентом до конца — продюсер уже завершается, ждём его.
            # Соединение оборвалось — не выбрасываем turn: генерация продолжает
            # работать в фоне и дописывает финальный ответ в транскрипт сессии,
            # чтобы восстановление истории показало его целиком (см. выше про
            # лимит фоновых turn'ов и watchdog).
            if completed or producer.done():
                with contextlib.suppress(Exception):
                    await producer
            elif not detach_background_turn(producer):
                producer.cancel()  # лимит исчерпан — прежнее поведение
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def list_sessions():
    return {"items": get_store().summaries()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    session = get_store().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return session


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    deleted = get_store().delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return {"ok": True}
