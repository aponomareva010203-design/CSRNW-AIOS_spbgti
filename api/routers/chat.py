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

Состояние текущего turn'а пишется в саму сессию (session["turn"] = {"state":
"running"|"done"|"failed", ...}, видно в GET /api/sessions/{id}): клиент, чей
стрим оборвался, опрашивает транскрипт — "running" значит, генерация ещё
дорабатывает в фоне и ответ допишется; "failed" — ответа не будет, в error
причина. Без этого клиент не отличал «ещё работает» от «уже упало» и либо ждал
впустую, либо зависал с мигающим курсором.
"""

import asyncio
import contextlib
import json
import time
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..runtime import agent_store, approvals, datarepo, skill_drafts, uploads, users
from ..runtime.config import get_settings
from ..runtime.datarepo import get_skill
from ..runtime.llm_settings import LLMConfig, effective_config, effective_config_for
from ..runtime.loop import run_agent_loop
from ..runtime.providers import FallbackProvider, ProviderError, build_provider
from ..runtime.sessions import SessionError, SessionStore
from ..runtime.tools import build_default_registry
from .auth import get_current_user, get_optional_user

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    """Тело запроса POST /api/chat. Модель задаёт администратор централизованно."""

    message: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = None
    agent_id: str | None = Field(default=None, max_length=64)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    rag_ids: list[str] = Field(default_factory=list, max_length=1)


class ApprovalDecision(BaseModel):
    """Решение человека по карточке подтверждения действия агента."""

    session_id: str = Field(min_length=1, max_length=64)
    approval_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "decline"]


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


@lru_cache(maxsize=64)
def get_provider(base_url: str = "", api_key: str = "", model: str = ""):
    """Провайдер LLM по тройке конфигурации (кэш по значениям).

    Без аргументов — действующая общая конфигурация (effective_config());
    с аргументами — конфигурация конкретного пользователя (см.
    effective_config_for: личные настройки из «Настроек»). Провайдер
    зависит только от значений конфигурации, поэтому смена настроек
    администратором не требует сброса кэша: новая тройка даст новый
    провайдер. В тестах сбрасывается cache_clear().
    """
    if not (base_url and model):
        cfg = effective_config()
        base_url, api_key, model = cfg.base_url, cfg.api_key, cfg.model
    return build_provider(
        LLMConfig(base_url=base_url, api_key=api_key, model=model, source=""),
        read_timeout=get_settings().request_timeout,
    )


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


@router.get("/chat/skills-suggest")
def skills_suggest(
    q: str = "",
    exclude: str = "",
    user: dict | None = Depends(get_optional_user),
):
    """Подсказки навыков по тексту сообщения (фразы-триггеры из frontmatter).

    Лёгкий эндпоинт для чипов над полем ввода рабочего чата: клиент вызывает
    с debounce, короткие запросы (<8 символов) отбрасываются и без обращения
    к каталогу. exclude — id активных навыков через запятую (переход к новому
    навыку всё равно означает новую сессию). Неавторизованный вызов работает
    как вызов обычного пользователя: admin-only навыки не подсказываются.
    """
    exclude_ids = [x.strip() for x in exclude.split(",") if x.strip()]
    is_admin = bool(user) and user.get("role") == users.ROLE_ADMIN
    return {
        "items": datarepo.suggest_skills(q, exclude=exclude_ids, is_admin=is_admin)
    }


@router.post("/chat/approvals")
async def resolve_approval(
    req: ApprovalDecision, authorization: str | None = Header(default=None)
):
    """Решение человека по карточке подтверждения действия агента.

    Идентификаторы приходят из SSE-события approval_request. Чужая сессия
    неотличима от несуществующей (как везде в чате); уже решённый или
    истёкший запрос — 409: карточку в этом случае закрывает само событие
    approval_result ( declined_by_timeout ).
    """
    user_id = _token_user_id(authorization)
    record = approvals.get(req.approval_id)
    if record is None or record.get("session_id") != req.session_id:
        raise HTTPException(status_code=404, detail="Запрос подтверждения не найден")
    session = get_store().get(req.session_id)
    owner = session.get("user_id") if session else None
    if owner and user_id != owner:
        # у сессии есть владелец — решать может только он; анонимная сессия
        # (как и сам чат) доверяет держателю идентификаторов
        raise HTTPException(status_code=404, detail="Запрос подтверждения не найден")
    if not approvals.resolve(req.approval_id, req.decision):
        raise HTTPException(status_code=409, detail="Запрос уже решён")
    return {"ok": True, "decision": req.decision}


@router.get("/stage-map")
def get_stage_map():
    """Карта этапов конвейера (публичная): названия, навыки, следующий этап.

    Нужна фронтенду для чипа «следующий этап конвейера» после завершения
    навыка и для бейджа этапа у сессии; правится в data/stage-map.json
    без перезапуска сервиса.
    """
    from ..runtime import datarepo

    return datarepo.stage_map()


@router.post("/chat")
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    # необязательная привязка диалога к учётной записи (для «Кабинета») —
    # нужна до создания провайдера: у пользователя с личными настройками
    # внешнего API свой endpoint и ключ
    user_id = _token_user_id(authorization)
    cfg = effective_config_for(user_id)
    provider = get_provider(cfg.base_url, cfg.api_key, cfg.model)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM не настроена администратором (админ-панель → Настройки внешних API)",
        )
    if cfg.source == "user":
        # личный endpoint с откатом: если он не отвечает (неверный ключ,
        # нет модели, лежит сервер), ответ уходит через общие настройки —
        # пользователь не остаётся без чата из-за своих настроек
        shared_cfg = effective_config()
        same = (cfg.base_url, cfg.api_key, cfg.model) == (
            shared_cfg.base_url, shared_cfg.api_key, shared_cfg.model
        )
        shared = None if same else get_provider(
            shared_cfg.base_url, shared_cfg.api_key, shared_cfg.model
        )
        if shared is not None:
            provider = FallbackProvider(provider, shared)
    # agent_id прежних версий (страница агента убрана) игнорируем: такая сессия
    # продолжается как обычный чат
    req.agent_id = None
    is_admin = users.is_admin_user(user_id)
    for skill_id in req.skill_ids:
        # навык каталога либо личный черновик владельца (draft:…, проверка из
        # кабинета); admin-only агенты каталога — только роли «Администратор»
        if not skill_drafts.skill_available(skill_id, user_id, is_admin=is_admin):
            raise HTTPException(status_code=400, detail=f"Неизвестный навык: {skill_id}")

    store = get_store()

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
    # Этап конвейера запущенного навыка (карта data/stage-map.json): фронту
    # нужен для чипа «следующий этап» после завершения навыка и бейджа этапа;
    # навыки вне линейного конвейера (онбординг, гипотезы) этап не ставят.
    stage = datarepo.stage_of_skill(req.skill_ids[0]) if req.skill_ids else None
    if stage:
        if session.get("stage") != stage["key"]:
            session["stage"] = stage["key"]
            session.setdefault("decision_log", []).append(
                {"kind": "stage_set", "stage": stage["key"], "ts": time.time()}
            )
    else:
        session.pop("stage", None)
    # RAG-базы, подключённые кнопкой в чате: не влияют на решение о новой
    # сессии (в отличие от skill_ids) — подключение не должно сбрасывать
    # текущий диалог; сохраняем как есть, включая пустой список (отключение).
    session["rag_ids"] = list(req.rag_ids)
    if not session["rag_ids"]:
        # у агента может быть привязана база знаний: подключаем её сами (выбор в меню «+» её перекрывает)
        try:
            bound = skill_drafts.bound_rag_id(session.get("skill_ids"), user_id)
        except Exception:  # noqa: BLE001 — привязка не должна ронять чат
            bound = ""
        if bound:
            session["rag_ids"] = [bound]
    # модели коллектива, привязанные к агенту (model_ids во frontmatter):
    # пересчитываются на каждом сообщении — отозванная или снятая с
    # публикации модель пропадает из промпта сразу
    try:
        session["model_ids"] = skill_drafts.bound_model_ids(session.get("skill_ids"), user_id)
    except Exception:  # noqa: BLE001 — привязка не должна ронять чат
        session["model_ids"] = []
    store.save(session)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def produce() -> None:
            """Перекладывает события цикла в очередь; ошибка — событием error."""
            turn_error: str | None = None
            turn_final_written = False  # терминальный статус пишется один раз

            def set_turn_state(state: str, error: str | None = None) -> None:
                """Отметка о состоянии turn'а — в сессию (см. докстринг модуля).

                Отдельного эндпоинта не нужно: GET /api/sessions/{id} и так
                возвращает сессию целиком, а клиент после обрыва стрима всё
                равно опрашивает транскрипт в ожидании фонового ответа.
                """
                turn = {"state": state, "at": time.time()}
                if error:
                    turn["error"] = error[:500]
                session["turn"] = turn
                store.save(session)

            def finish_turn(state: str, error: str | None = None) -> None:
                """Терминальный статус: ветка отмены пишет его раньше finally,
                и finally не должен затирать «failed» обратно в «done»."""
                nonlocal turn_final_written
                if turn_final_written:
                    return
                turn_final_written = True
                set_turn_state(state, error)

            try:
                set_turn_state("running")
                async for event in run_agent_loop(
                    settings=get_settings(),
                    provider=provider,  # конфигурация владельца сессии (личная или общая)
                    registry=get_registry(),
                    store=store,
                    session=session,
                    user_text=user_text,
                    user_attachments=user_attachments or None,
                ):
                    # цикл некоторые ошибки не поднимает, а отдаёт событием
                    # (дневной лимит токенов) — их тоже видим в статусе turn'а
                    if event.get("type") == "error":
                        turn_error = event.get("message") or turn_error
                    await queue.put(event)
            except ProviderError as exc:
                turn_error = str(exc)
                await queue.put({"type": "error", "message": turn_error})
            except Exception as exc:  # noqa: BLE001 — ошибка уходит клиенту событием
                turn_error = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
                await queue.put({"type": "error", "message": turn_error})
            except asyncio.CancelledError:
                # гасит watchdog фонового turn'а (BACKGROUND_TURN_TIMEOUT) или
                # лимит фоновых задач: ответа в транскрипте не будет — честно
                # отмечаем провал, иначе опрашивающий клиент ждал бы впустую
                finish_turn("failed", "Генерация прервана на сервере (таймаут)")
                raise
            finally:
                # статус turn'а пишется и когда клиент ушёл (фон), и когда читает:
                # по нему опрашивающий транскрипт клиент узнаёт, ждать ли ответ
                finish_turn("failed" if turn_error else "done", turn_error)
                await queue.put(None)  # сигнал конца потока

        # Задача создаётся ДО первого yield: если клиент оборвёт соединение
        # мгновенно (даже на самой первой попытке отдать байты клиенту —
        # например, уход со страницы сразу после отправки сообщения),
        # генерация уже запущена, и try/finally ниже успеет задетачить её
        # в фон вместо того, чтобы потерять turn целиком (правка 16 сентября
        # 2026 — раньше первый yield стоял до создания задачи и до try,
        # поэтому мгновенный обрыв мог вообще не запустить run_agent_loop).
        producer = asyncio.create_task(produce())
        completed = False
        try:
            yield _sse(
                {
                    "type": "session",
                    "id": session["id"],
                    "title": session["title"],
                    "stage": session.get("stage"),
                }
            )
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
