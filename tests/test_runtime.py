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


# ---------------------------------------------------------------------------
# Навык «Онбординг проекта» и динамический контекст (runtime/skill_context.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _projects_env(monkeypatch, tmp_path):
    """Изолированные каталоги рантайма и организации + свежие кэши."""
    from api.runtime import projects

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_ORGANIZATION_DIR", str(tmp_path / "org"))
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    projects.get_store.cache_clear()


def test_registry_has_project_tools(tmp_path):
    registry = build_default_registry(tmp_path / "tools")
    assert "get_projects" in registry.names()
    assert "save_project_card" in registry.names()


def test_onboarding_skill_body_fits_limit():
    """Навык онбординга читается целиком: файл на месте, лимит не режет."""
    from api.runtime import datarepo
    from api.runtime.prompts import SKILL_BODY_LIMIT, build_system_prompt

    assert SKILL_BODY_LIMIT >= 20_000
    skill = datarepo.get_skill("project-onboarding")
    assert skill, "навык project-onboarding должен быть в data/skills.json"
    body = datarepo.skill_body(skill, limit=SKILL_BODY_LIMIT)
    assert "aios-choices" in body
    assert "Шаг 7" in body
    assert "усечён" not in body

    prompt = build_system_prompt(skill_ids=["project-onboarding"])
    assert "Онбординг проекта" in prompt and "save_project_card" in prompt


def test_skill_context_blocks_by_mentions(_projects_env):
    """Блоки «Об организации» и «Мои проекты» подмешиваются по упоминаниям."""
    from api.runtime import projects, skill_context

    uid = "abc123def456"

    # без упоминаний — блоков нет
    assert skill_context.context_block({"user_id": uid}, "привет") == ""

    # организация упомянута → блок собирается из профиля организации
    from api.runtime import organization

    organization.get_store().save_profile({"name": "ТИ НЦ РАН", "description": "Институт"})
    block = skill_context.context_block({"user_id": uid}, "какие у института показатели?")
    assert "Сведения об организации" in block
    assert "ТИ НЦ РАН" in block

    # активная карточка проекта + упоминание проектов → блок «Мои проекты»
    card = projects.get_store().save(
        uid, {"title": "Мембраны", "goal": "Селективность мембран 99% к 2027 году"}
    )
    projects.get_store().save(uid, {"project_id": card["id"], "status": "active"})
    block = skill_context.context_block({"user_id": uid}, "что у меня с проектами?")
    assert "Мои проекты" in block and "Мембраны" in block

    # черновик в блок не попадает; без пользователя — тоже
    projects.get_store().save(uid, {"title": "Черновик"})
    block = skill_context.context_block({"user_id": uid}, "проект?")
    assert "Черновик" not in block
    assert skill_context.context_block({}, "проект?") == ""


def test_loop_injects_context_block(_projects_env):
    """Системный промпт цикла получает динамические блоки по упоминаниям."""
    from api.runtime import projects
    from api.runtime.loop import run_agent_loop
    from api.runtime.sessions import SessionStore

    settings = get_settings()
    store = SessionStore(_projects_env / "sessions")
    registry = build_default_registry(_projects_env / "tools")
    session = store.create(title="t")
    session["user_id"] = "abc123def456"
    card = projects.get_store().save(
        session["user_id"],
        {"title": "Мембраны", "goal": "Селективность мембран 99% к 2027 году"},
    )
    projects.get_store().save(
        session["user_id"], {"project_id": card["id"], "status": "active"}
    )

    captured = {}

    class CapturingProvider(MockProvider):
        async def stream_chat(self, messages, tools=None, model=None):
            captured["system"] = messages[0]["content"]
            async for kind, payload in super().stream_chat(messages, tools, model):
                yield kind, payload

    async def run():
        async for _ in run_agent_loop(
            settings=settings,
            provider=CapturingProvider(),
            registry=registry,
            store=store,
            session=session,
            user_text="как продвигается мой проект?",
        ):
            pass

    asyncio.run(run())
    assert "Мои проекты" in captured["system"]
    assert "Мембраны" in captured["system"]
