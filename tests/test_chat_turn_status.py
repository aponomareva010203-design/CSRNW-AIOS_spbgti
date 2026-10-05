"""Статус turn'а в сессии (session["turn"]) — контракт восстановления ответа
после обрыва стрима (api/routers/chat.py, recoverReply в frontend/js/api.js).

Клиент, чей SSE-канал умер до конца генерации, опрашивает GET /api/sessions/{id}:

  running — фоновая генерация ещё дорабатывает, ответ допишется — ждать;
  done    — ответ дописан в транскрипт;
  failed  — ответа не будет, в turn["error"] причина (провайдер, лимит, отмена).
"""

import asyncio
import json

import pytest

from api.runtime.config import get_settings
from api.runtime.providers import MockProvider, ProviderError


@pytest.fixture()
def chat_env(monkeypatch, tmp_path):
    """Изолированный рантайм, свежие кэши и подменяемый провайдер/цикл."""
    from api.routers import chat as chat_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()

    def use_provider(provider):
        monkeypatch.setattr(chat_router, "get_provider", lambda *a, **k: provider)

    yield chat_router, use_provider

    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()


def _session_event_id(body: str) -> str:
    first = body.split("\n\n")[0].removeprefix("data: ").strip()
    return json.loads(first)["id"]


def test_turn_status_done_after_stream(chat_env):
    """Обычный ход: после конца стрима turn = done, ответ в транскрипте."""
    from fastapi.testclient import TestClient

    from api.main import app

    chat_router, use_provider = chat_env
    use_provider(MockProvider())

    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "найди знания про ALD-процессы"})
    assert resp.status_code == 200
    assert resp.text.rstrip().endswith("[DONE]")

    sid = _session_event_id(resp.text)
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["turn"]["state"] == "done"
    assert "error" not in session["turn"]
    # ответ ассистента дописан не позже статуса done — опрашивающий клиент,
    # увидев done, берёт последний ответ из транскрипта
    assert session["messages"][-1]["role"] == "assistant"
    assert session["messages"][-1]["content"]


def test_turn_status_failed_on_provider_error(chat_env):
    """Провайдер упал: клиенту — событие error, сессии — turn failed с причиной."""

    class BrokenProvider:
        async def stream_chat(self, messages, tools=None, model=None):
            raise ProviderError("LLM HTTP 503: перегрузка")
            yield  # pragma: no cover — делает функцию генератором

    from fastapi.testclient import TestClient

    from api.main import app

    chat_router, use_provider = chat_env
    use_provider(BrokenProvider())

    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "привет"})
    assert resp.status_code == 200
    assert '"type":"error"' in resp.text.replace(" ", "")
    assert "LLM HTTP 503" in resp.text

    sid = _session_event_id(resp.text)
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["turn"]["state"] == "failed"
    assert "LLM HTTP 503" in session["turn"]["error"]
    # ответа ассистента нет — опрашивающий клиент не должен ждать его впустую
    assert not [m for m in session["messages"] if m["role"] == "assistant"]


def test_turn_status_failed_on_loop_error_event(chat_env, monkeypatch):
    """Ошибка, которую цикл отдаёт событием (дневной лимит), тоже видна в turn."""

    async def fake_loop(**kwargs):
        yield {"type": "delta", "text": "начал…"}
        yield {"type": "error", "message": "Дневной лимит токенов исчерпан"}

    from fastapi.testclient import TestClient

    from api.main import app

    chat_router, use_provider = chat_env
    use_provider(MockProvider())
    monkeypatch.setattr(chat_router, "run_agent_loop", fake_loop)

    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "привет"})
    assert resp.status_code == 200

    sid = _session_event_id(resp.text)
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["turn"]["state"] == "failed"
    assert "Дневной лимит" in session["turn"]["error"]


def test_turn_status_after_client_disconnect(chat_env):
    """Обрыв соединения: turn остаётся running, пока фон дорабатывает, затем done.

    Сценарий «чат перестал генерировать, а пайплайн ещё жив»: клиент опрашивает
    сессию и по turn.state понимает, ждать ли ответ.
    """
    from api.routers.chat import ChatRequest

    class SlowProvider:
        async def stream_chat(self, messages, tools=None, model=None):
            await asyncio.sleep(0.2)
            yield ("text", "дописал в фоне")

    chat_router, use_provider = chat_env
    use_provider(SlowProvider())

    async def scenario():
        response = await chat_router.chat(ChatRequest(message="привет"), authorization=None)
        stream = response.body_iterator
        first = await stream.__anext__()  # session-событие
        sid = json.loads(first.removeprefix("data: ").strip())["id"]

        second = await stream.__anext__()  # delta
        assert json.loads(second.removeprefix("data: ").strip())["type"] == "delta"
        await stream.aclose()  # имитация обрыва соединения

        store = chat_router.get_store()
        # фон ещё дорабатывает — статус running (или уже done на быстрой машине)
        early = store.get(sid)
        assert early["turn"]["state"] in ("running", "done")

        for _ in range(100):
            loaded = store.get(sid)
            if loaded and loaded["turn"]["state"] == "done":
                break
            await asyncio.sleep(0.05)
        loaded = store.get(sid)
        assert loaded["turn"]["state"] == "done"
        assert loaded["messages"][-1]["content"] == "дописал в фоне"

    asyncio.run(scenario())


def test_turn_status_failed_when_background_cancelled(chat_env, monkeypatch):
    """Отмена фоновой задачи (watchdog/лимит): turn = failed, ждать нечего."""

    class HangingProvider:
        async def stream_chat(self, messages, tools=None, model=None):
            await asyncio.sleep(30)
            yield ("text", "не дождётесь")  # pragma: no cover

    chat_router, use_provider = chat_env
    use_provider(HangingProvider())
    monkeypatch.setattr(chat_router, "HEARTBEAT_INTERVAL", 0.05)

    from api.routers.chat import ChatRequest

    async def scenario():
        response = await chat_router.chat(ChatRequest(message="привет"), authorization=None)
        stream = response.body_iterator
        first = await stream.__anext__()
        sid = json.loads(first.removeprefix("data: ").strip())["id"]
        await stream.__anext__()  # заходим внутрь try продюсера
        await stream.aclose()  # обрыв → turn уходит в фон

        # среди фоновых задач находим сам продюсер (watchdog — отдельная задача)
        producer = next(
            t for t in list(chat_router._BACKGROUND_TURNS)
            if not t.done() and t.get_coro().__qualname__.endswith(".produce")
        )
        producer.cancel()  # то, что сделал бы watchdog по таймауту
        with pytest.raises(asyncio.CancelledError):
            await producer

        loaded = chat_router.get_store().get(sid)
        assert loaded["turn"]["state"] == "failed"
        assert "таймаут" in loaded["turn"]["error"]

    asyncio.run(scenario())
