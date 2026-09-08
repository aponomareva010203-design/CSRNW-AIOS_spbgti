"""HTTP-слой харнесса: SSE-чат, сессии, статус рантайма.

POST /api/chat          — сообщение ассистенту; ответ — поток SSE-событий;
GET  /api/sessions      — список сессий (сводки);
GET  /api/sessions/{id} — полная сессия (транскрипт);
DELETE /api/sessions/{id};
GET  /api/runtime       — статус рантайма: провайдер, инструменты.

Формат SSE: строки «data: {json}\n\n», события типов session/delta/tool_call/
tool_result/done/error, затем «data: [DONE]\n\n».
"""

import json
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..runtime.config import get_settings
from ..runtime.datarepo import get_agent, get_skill
from ..runtime.llm_settings import effective_config
from ..runtime.loop import run_agent_loop
from ..runtime.providers import ProviderError, build_provider
from ..runtime.sessions import SessionError, SessionStore
from ..runtime.tools import build_default_registry

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    """Тело запроса POST /api/chat. Модель задаёт администратор централизованно."""

    message: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = None
    agent_id: str | None = Field(default=None, max_length=64)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)


@lru_cache(maxsize=1)
def get_store() -> SessionStore:
    """Синглтоны рантайма; в тестах сбрасываются cache_clear()."""
    return SessionStore(get_settings().runtime_dir / "sessions")


@lru_cache(maxsize=1)
def get_registry():
    return build_default_registry(get_settings().tools_root)


@lru_cache(maxsize=1)
def get_provider():
    return build_provider(effective_config(), read_timeout=get_settings().request_timeout)


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
async def chat(req: ChatRequest):
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
    if req.session_id:
        session = store.get(req.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
    else:
        session = store.create(
            title=req.message[:80],
            model=cfg.model,
            agent_id=req.agent_id,
            skill_ids=req.skill_ids,
        )
    session["model"] = cfg.model
    if req.agent_id:
        session["agent_id"] = req.agent_id
    if req.skill_ids:
        session["skill_ids"] = list(req.skill_ids)
    store.save(session)

    async def event_stream():
        yield _sse({"type": "session", "id": session["id"], "title": session["title"]})
        try:
            async for event in run_agent_loop(
                settings=get_settings(),
                provider=get_provider(),
                registry=get_registry(),
                store=store,
                session=session,
                user_text=req.message,
            ):
                yield _sse(event)
        except ProviderError as exc:
            yield _sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — ошибка уходит клиенту событием
            yield _sse({"type": "error", "message": f"Внутренняя ошибка: {type(exc).__name__}: {exc}"})
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
