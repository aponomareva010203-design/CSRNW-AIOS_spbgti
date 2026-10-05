"""Approval gate: подтверждение действий агента человеком и журнал решений."""

import asyncio
import dataclasses

from fastapi.testclient import TestClient

from api.runtime import approvals
from api.runtime.config import get_settings
from api.runtime.loop import run_agent_loop
from api.runtime.providers import ToolCall
from api.runtime.sessions import SessionStore
from api.runtime.tools import build_default_registry


class ScriptedCallsProvider:
    """Провайдер с фиксированным сценарием и фиксированными id вызовов
    (id нужен заранее: брокер подтверждений ждёт решения по нему)."""

    def __init__(self, script):
        self.script = list(script)

    async def stream_chat(self, messages, tools=None, model=None):
        kind, payload = self.script.pop(0)
        if kind == "tool_calls":
            calls = [
                ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments") or {})
                for c in payload
            ]
            yield ("tool_calls", calls)
            return
        yield (kind, payload)


def _approval_script():
    """Сценарий: модель вызывает write_file (требует подтверждения),
    получает результат и отвечает текстом."""
    return [
        (
            "tool_calls",
            [{"id": "call-appr-1", "name": "write_file",
              "arguments": {"path": "notes/a.md", "content": "текст"}}],
        ),
        ("text", "Файл записан."),
    ]


def _run_loop(tmp_path, provider, settings=None):
    store = SessionStore(tmp_path / "sessions")
    registry = build_default_registry(tmp_path / "tools")
    session = store.create(title="t")

    async def run():
        events = []
        async for event in run_agent_loop(
            settings=settings or get_settings(),
            provider=provider,
            registry=registry,
            store=store,
            session=session,
            user_text="запиши файл",
        ):
            events.append(event)
        return events, store.get(session["id"])

    return asyncio.run(run())


def _resolve_when_pending(approval_id, decision):
    """Фоновая задача: как только запрос появился в брокере — решить."""
    async def resolver():
        for _ in range(2000):  # до ~10 секунд ожидания
            if approvals.get(approval_id) and approvals.get(approval_id).get("session_id"):
                approvals.resolve(approval_id, decision)
                return
            await asyncio.sleep(0.005)

    return asyncio.create_task(resolver())


def test_broker_approve_flow():
    async def t():
        approvals.create("b1", "s1", "write_file", {"path": "x"})
        task = asyncio.create_task(approvals.wait("b1", 5.0))
        await asyncio.sleep(0.01)
        assert approvals.resolve("b1", "approve") is True
        assert await task == "approve"
        assert approvals.resolve("b1", "decline") is False  # дважды не решается

    asyncio.run(t())


def test_broker_timeout_declines():
    async def t():
        approvals.create("b2", "s1", "notify", {})
        decision = await approvals.wait("b2", 0.02)
        assert decision == "declined_by_timeout"
        # поздний клик уже не «оживляет» turn
        assert approvals.resolve("b2", "approve") is False

    asyncio.run(t())


def _enable_gate(monkeypatch, tools="write_file"):
    """Включает approval gate в тесте (по умолчанию он выключен)."""
    monkeypatch.setenv("AIOS_APPROVAL_TOOLS", tools)
    get_settings.cache_clear()


def test_loop_approval_approved_executes_tool(tmp_path, monkeypatch):
    """Утверждение: инструмент исполняется, события approval_request/result
    приходят, журнал решений сессии содержит запрос и решение."""
    _enable_gate(monkeypatch)

    async def run():
        provider = ScriptedCallsProvider(_approval_script())
        store = SessionStore(tmp_path / "sessions")
        registry = build_default_registry(tmp_path / "tools")
        session = store.create(title="t")
        resolver = _resolve_when_pending("call-appr-1", "approve")
        events = []
        async for event in run_agent_loop(
            settings=get_settings(), provider=provider, registry=registry,
            store=store, session=session, user_text="запиши файл",
        ):
            events.append(event)
        await resolver
        return events, store.get(session["id"])

    events, saved = asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert "approval_request" in kinds and "approval_result" in kinds
    result = next(e for e in events if e["type"] == "approval_result")
    assert result["decision"] == "approve"

    tool = next(e for e in events if e["type"] == "tool_result")
    assert tool["ok"] is True  # файл реально записан
    assert (tmp_path / "tools" / "notes" / "a.md").read_text(encoding="utf-8") == "текст"

    log = saved.get("decision_log") or []
    kinds_log = [e["kind"] for e in log]
    assert "approval_request" in kinds_log and "approval_decision" in kinds_log
    decision = next(e for e in log if e["kind"] == "approval_decision")
    assert decision["decision"] == "approve"
    assert kinds[-1] == "done"


def test_loop_approval_declined_skips_tool(tmp_path, monkeypatch):
    """Отказ: инструмент не исполняется, модель получает честный ok:false
    и продолжает диалог (цикл доходит до done)."""
    _enable_gate(monkeypatch)

    async def run():
        provider = ScriptedCallsProvider(_approval_script())
        store = SessionStore(tmp_path / "sessions")
        registry = build_default_registry(tmp_path / "tools")
        session = store.create(title="t")
        resolver = _resolve_when_pending("call-appr-1", "decline")
        events = []
        async for event in run_agent_loop(
            settings=get_settings(), provider=provider, registry=registry,
            store=store, session=session, user_text="запиши файл",
        ):
            events.append(event)
        await resolver
        return events, store.get(session["id"])

    events, saved = asyncio.run(run())
    tool = next(e for e in events if e["type"] == "tool_result")
    assert tool["ok"] is False
    assert "отклонил" in tool["result"]["error"]
    assert not (tmp_path / "tools" / "notes" / "a.md").exists()  # файл не писался
    assert [e["type"] for e in events][-1] == "done"

    decision = next(
        e for e in saved.get("decision_log") or [] if e["kind"] == "approval_decision"
    )
    assert decision["decision"] == "decline"


def test_loop_approval_timeout_declines(tmp_path, monkeypatch):
    """Тишина = отказ: без решения за AIOS_APPROVAL_TIMEOUT инструмент
    не исполняется, цикл продолжается."""
    _enable_gate(monkeypatch)

    async def run():
        provider = ScriptedCallsProvider(_approval_script())
        store = SessionStore(tmp_path / "sessions")
        registry = build_default_registry(tmp_path / "tools")
        session = store.create(title="t")
        settings = dataclasses.replace(get_settings(), approval_timeout=0.05)
        events = []
        async for event in run_agent_loop(
            settings=settings, provider=provider, registry=registry,
            store=store, session=session, user_text="запиши файл",
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    result = next(e for e in events if e["type"] == "approval_result")
    assert result["decision"] == "declined_by_timeout"
    assert not (tmp_path / "tools" / "notes" / "a.md").exists()


def test_approvals_endpoint_resolution_and_ownership(tmp_path, monkeypatch):
    """POST /api/chat/approvals: решение применяется; повтор — 409; у сессии
    с владельцем анонимный запрос отклоняется (404, как «не найдено»)."""
    from api.main import app
    from api.routers import chat as chat_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_provider.cache_clear()

    client = TestClient(app)
    store = chat_router.get_store()

    # анонимная сессия: держатель идентификаторов решает (как и сам чат)
    session = store.create(title="t")
    approvals.create("call-x1", session["id"], "write_file", {"path": "x.md"})

    bad = client.post(
        "/api/chat/approvals",
        json={"session_id": session["id"], "approval_id": "call-other", "decision": "approve"},
    )
    assert bad.status_code == 404

    invalid = client.post(
        "/api/chat/approvals",
        json={"session_id": session["id"], "approval_id": "call-x1", "decision": "later"},
    )
    assert invalid.status_code == 422

    ok = client.post(
        "/api/chat/approvals",
        json={"session_id": session["id"], "approval_id": "call-x1", "decision": "approve"},
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True

    again = client.post(
        "/api/chat/approvals",
        json={"session_id": session["id"], "approval_id": "call-x1", "decision": "approve"},
    )
    assert again.status_code == 409

    # сессия с владельцем: аноним (без токена) решение принять не может
    owned = store.create(title="own")
    owned["user_id"] = "u-owner"
    store.save(owned)
    approvals.create("call-x2", owned["id"], "write_file", {"path": "y.md"})
    stranger = client.post(
        "/api/chat/approvals",
        json={"session_id": owned["id"], "approval_id": "call-x2", "decision": "approve"},
    )
    assert stranger.status_code == 404
    assert approvals.get("call-x2").get("session_id") == owned["id"]  # запрос жив

    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()
    chat_router.get_provider.cache_clear()


def test_approval_rules_only_in_prompt_when_gate_on(tmp_path, monkeypatch):
    """Правила подтверждений попадают в системный промпт только при
    включённом gate: выключенный gate в промпте не упоминается —
    модель не должна ждать карточек, которые никогда не появятся."""
    from pathlib import Path

    from api.runtime.prompts import build_system_prompt

    assert "правила подтверждений" not in build_system_prompt().lower()

    seen_prompt = {}

    class ProbeProvider:
        async def stream_chat(self, messages, tools=None, model=None):
            seen_prompt["system"] = messages[0]["content"]
            yield ("text", "ок")

    async def run():
        store = SessionStore(Path(tmp_path) / "sessions")
        session = store.create(title="t")
        async for _ in run_agent_loop(
            settings=get_settings(), provider=ProbeProvider(),
            registry=build_default_registry(Path(tmp_path) / "tools"),
            store=store, session=session, user_text="вопрос",
        ):
            pass

    monkeypatch.setenv("AIOS_APPROVAL_TOOLS", "write_file")
    get_settings.cache_clear()
    asyncio.run(run())
    assert "правила подтверждений" in seen_prompt["system"].lower()

    monkeypatch.setenv("AIOS_APPROVAL_TOOLS", "")
    get_settings.cache_clear()
    asyncio.run(run())
    assert "правила подтверждений" not in seen_prompt["system"].lower()

    monkeypatch.delenv("AIOS_APPROVAL_TOOLS", raising=False)
    get_settings.cache_clear()
