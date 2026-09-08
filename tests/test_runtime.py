"""Смоук-тесты агентного рантайма: песочница, сессии, цикл, SSE-эндпоинт."""

import asyncio

import pytest

from api.runtime.config import get_settings
from api.runtime.loop import run_agent_loop
from api.runtime.providers import MockProvider
from api.runtime.sessions import SessionError, SessionStore
from api.runtime.tools import build_default_registry


def test_tools_sandbox_roundtrip(tmp_path):
    registry = build_default_registry(tmp_path / "tools")

    written = asyncio.run(registry.call("write_file", {"path": "notes/x.md", "content": "привет"}))
    assert written["ok"] is True and written["created"] is True

    read = asyncio.run(registry.call("read_file", {"path": "notes/x.md"}))
    assert read["ok"] is True and read["content"] == "привет"

    escape = asyncio.run(registry.call("read_file", {"path": "../outside.txt"}))
    assert escape["ok"] is False and "песочницы" in escape["error"]

    unknown = asyncio.run(registry.call("nope", {}))
    assert unknown["ok"] is False


def test_sessions_store(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(title="Тест", model="m1", skill_ids=["s1"])
    store.append_message(session, {"role": "user", "content": "вопрос"})
    store.append_message(session, {"role": "assistant", "content": "ответ"})

    loaded = store.get(session["id"])
    assert [m["content"] for m in loaded["messages"]] == ["вопрос", "ответ"]
    assert store.summaries()[0]["message_count"] == 2
    assert store.delete(session["id"]) is True
    assert store.get(session["id"]) is None

    with pytest.raises(SessionError):
        store._path("../etc/passwd")
    assert store.get("../etc/passwd") is None


def test_build_system_prompt_from_files():
    """Промпты собираются из api/runtime/prompts/*.md — файлы на месте."""
    from api.runtime.prompts import build_system_prompt

    base = build_system_prompt()
    assert "Рабочего пространства" in base

    with_agent = build_system_prompt(agent_id="chemcrow")
    assert "Роль агента" in with_agent
    assert len(with_agent) > len(base)

    with_skill = build_system_prompt(skill_ids=["scimlchem-architect"])
    assert "Навык:" in with_skill
    assert len(with_skill) > len(base)


def test_mock_loop_calls_tool_and_finishes(tmp_path):
    settings = get_settings()
    store = SessionStore(tmp_path / "sessions")
    registry = build_default_registry(tmp_path / "tools")
    session = store.create(title="t")

    async def run():
        events = []
        async for event in run_agent_loop(
            settings=settings,
            provider=MockProvider(),
            registry=registry,
            store=store,
            session=session,
            user_text="найди знания про катализ полимеров",
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "done"

    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["name"] == "search_knowledge"

    roles = [m["role"] for m in store.get(session["id"])["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_chat_endpoint_sse(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import chat as chat_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()
    chat_router.get_provider.cache_clear()
    # LLM не настроена → провайдер None; в тесте подключаем MockProvider напрямую
    monkeypatch.setattr(chat_router, "get_provider", lambda: MockProvider())

    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "найди знания про ALD-процессы"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert '"type":"session"' in resp.text.replace(" ", "")
    assert "search_knowledge" in resp.text
    assert "[DONE]" in resp.text

    # Сессия сохранилась и читается через API
    sessions = client.get("/api/sessions").json()["items"]
    assert sessions and sessions[0]["message_count"] == 4
    sid = sessions[0]["id"]
    full = client.get(f"/api/sessions/{sid}").json()
    assert full["title"].startswith("найди знания")
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True

    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()
    try:
        chat_router.get_provider.cache_clear()
    except AttributeError:
        pass  # get_provider подменён на lambda-заглушку с MockProvider
