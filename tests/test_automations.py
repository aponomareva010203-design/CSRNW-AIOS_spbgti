"""Автоматизации: транслятор блоков, клиент n8n, права, запуск, агентный шаг."""

import httpx
import pytest

from api.runtime import automations_store as automations_store_module
from api.runtime.config import get_settings
from api.runtime.n8n_blocks import BlockError, translate
from api.runtime.n8n_client import N8nApiError, N8nClient
from api.runtime.n8n_settings import N8nSettingsStore
from api.runtime.users import UserStore

ADMIN_LOGIN = "admin@ex.com"
ADMIN_PASSWORD = "pass-1234"


# ---------------------------------------------------------------------------
# Изоляция окружения


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    for name in (
        "AIOS_LLM_BASE_URL", "AIOS_LLM_API_KEY", "AIOS_LLM_MODEL",
        "AIOS_N8N_BASE_URL", "AIOS_N8N_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()

    from api.routers import auth as auth_router
    from api.routers import chat as chat_router
    from api.routers import automations as automations_router

    for fn in (
        auth_router.get_user_store,
        auth_router.get_token_store,
        auth_router.get_confirmation_store,
        auth_router.get_llm_store,
        automations_router.get_n8n_store,
        chat_router.get_provider,
    ):
        fn.cache_clear()
    automations_store_module._STORE = None
    yield
    get_settings.cache_clear()
    automations_store_module._STORE = None


def _mk_user(login="user@ex.com", role=None):
    from api.routers.auth import get_token_store

    users = UserStore(get_settings().runtime_dir / "users")
    user = users.create(login=login, password="pass1234", name="Тестов Тест Тестович", confirmed=True)
    if role:
        user["role"] = role
        users.save(user)
    token = get_token_store().issue(user["id"])
    return user, token


def _configure_n8n(service_key="aios-svc-test"):
    N8nSettingsStore(get_settings().runtime_dir).save("http://n8n.test", "n8n-key")
    store = N8nSettingsStore(get_settings().runtime_dir)
    data = store.load()
    data["service_key"] = service_key
    store.path.write_text(__import__("json").dumps(data), encoding="utf-8")
    return service_key


class FakeN8n:
    """Заглушка public API n8n: пишет вызовы, отвечает заготовками."""

    def __init__(self):
        self.calls = []

    async def create_workflow(self, *, name, nodes, connections, settings):
        self.calls.append(("create", name, [n["name"] for n in nodes]))
        return {"id": "wf-123"}

    async def update_workflow(self, workflow_id, *, name, nodes, connections, settings):
        self.calls.append(("update", workflow_id, name))
        return {}

    async def publish_workflow(self, workflow_id):
        self.calls.append(("publish", workflow_id))
        return {}

    async def unpublish_workflow(self, workflow_id):
        self.calls.append(("unpublish", workflow_id))
        return {}

    async def delete_workflow(self, workflow_id):
        self.calls.append(("delete", workflow_id))
        return {}

    async def list_executions(self, *, workflow_id=None, limit=50, cursor=None):
        self.calls.append(("executions", workflow_id))
        return {
            "data": [
                {"id": "ex-1", "status": "success", "mode": "webhook",
                 "startedAt": "2026-01-01T10:00:00.000Z", "stoppedAt": "2026-01-01T10:00:05.000Z"},
                {"id": "ex-2", "status": "error", "mode": "webhook",
                 "startedAt": "2026-01-02T10:00:00.000Z", "stoppedAt": "2026-01-02T10:00:05.000Z"},
            ]
        }

    async def retry_execution(self, execution_id):
        self.calls.append(("retry", execution_id))
        return {}

    async def call_webhook(self, path, payload=None):
        class R:
            status_code = 200
            text = '{"text": "готово"}'

            def json(self):
                return {"text": "готово"}

        self.calls.append(("webhook", path))
        return R()


def _patch_n8n(monkeypatch):
    fake = FakeN8n()

    def _builder(*, timeout=None):
        return fake

    from api.routers import automations as automations_router

    monkeypatch.setattr(automations_router, "build_client", _builder)
    return fake


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Транслятор блоков


def _translate(trigger, steps, **kw):
    return translate(
        trigger=trigger,
        steps=steps,
        webhook_path="aios-abc123",
        agent_step_url="http://host.docker.internal:8000/api/internal/agent-step",
        service_key="svc",
        owner_id="usr-1",
        **kw,
    )


def test_translate_manual_agent_http():
    nodes, connections = _translate(
        {"type": "manual"},
        [
            {"type": "agent", "prompt": "Сводка: {{prev}}"},
            {"type": "http", "method": "POST", "url": "https://api.example.com/x", "body": '{"k": 1}'},
        ],
    )
    assert [n["name"] for n in nodes] == ["Триггер", "Агент AIOS 1", "HTTP-запрос 2"]
    trig, agent, http_node = nodes

    assert trig["type"] == "n8n-nodes-base.webhook"
    assert trig["parameters"]["path"] == "aios-abc123"
    assert trig["parameters"]["responseMode"] == "lastNode"

    assert agent["parameters"]["url"].endswith("/api/internal/agent-step")
    header = next(
        h for h in agent["parameters"]["headerParameters"]["parameters"]
        if h["name"] == "X-AIOS-Service-Key"
    )
    assert header["value"] == "svc"
    assert '"Сводка: " + JSON.stringify($json)' in agent["parameters"]["jsonBody"]
    assert '"usr-1"' in agent["parameters"]["jsonBody"]

    assert http_node["parameters"]["method"] == "POST"
    assert http_node["parameters"]["jsonBody"] == '{"k": 1}'

    # линейная цепочка: триггер → агент → http
    assert connections["Триггер"]["main"][0][0]["node"] == "Агент AIOS 1"
    assert connections["Агент AIOS 1"]["main"][0][0]["node"] == "HTTP-запрос 2"
    assert "HTTP-запрос 2" not in connections


def test_translate_schedule_and_email():
    nodes, _ = _translate(
        {"type": "schedule", "interval_minutes": 30},
        [
            {"type": "email", "to": ["a@ex.com"], "subject": "Сводка", "text": "Итоги: {{prev}}"},
        ],
        email_credential_id="cred-7",
        email_from="noreply@ institute.example",
    )
    trig, mail = nodes
    assert trig["type"] == "n8n-nodes-base.scheduleTrigger"
    assert trig["parameters"]["rule"]["interval"][0] == {"field": "minutes", "minutesInterval": 30}
    assert mail["type"] == "n8n-nodes-base.emailSend"
    assert mail["credentials"]["n8n-nodes-base/emailSend"]["id"] == "cred-7"
    assert mail["parameters"]["text"].startswith("=")

    # email без credential (SMTP не настроен) — понятная ошибка
    with pytest.raises(BlockError):
        _translate({"type": "manual"}, [{"type": "email", "to": ["a@ex.com"], "subject": "s", "text": "t"}])


# ---------------------------------------------------------------------------
# Клиент n8n (httpx.MockTransport)


def test_n8n_client_request_and_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-N8N-API-KEY")
        if request.url.path == "/api/v1/workflows":
            return httpx.Response(200, json={"data": [], "nextCursor": None})
        return httpx.Response(403, json={"message": "forbidden", "reason": "insufficient_permissions"})

    client = N8nClient("http://n8n.test", "k", transport=httpx.MockTransport(handler))
    import asyncio

    data = asyncio.run(client.list_workflows(limit=5))
    assert data == {"data": [], "nextCursor": None}
    assert seen["url"].endswith("/api/v1/workflows?limit=5")
    assert seen["key"] == "k"

    with pytest.raises(N8nApiError) as exc:
        asyncio.run(client.get_workflow("wf-1"))
    assert exc.value.status_code == 403
    assert "insufficient_permissions" in exc.value.message

    # транспортная ошибка → 502 с читаемым текстом
    down = N8nClient("http://127.0.0.1:9", "k", timeout=httpx.Timeout(2.0))
    with pytest.raises(N8nApiError) as exc2:
        asyncio.run(down.list_workflows())
    assert exc2.value.status_code == 502


# ---------------------------------------------------------------------------
# Роутер: права, CRUD, запуск


PAYLOAD = {
    "name": "Утренняя сводка",
    "description": "тест",
    "trigger": {"type": "manual"},
    "steps": [{"type": "agent", "prompt": "Сделай сводку"}],
}


def test_automations_require_configured_n8n(monkeypatch):
    client = _client()
    _, token = _mk_user()
    r = client.post("/api/automations", json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503


def test_create_run_executions_delete(monkeypatch):
    _configure_n8n()
    fake = _patch_n8n(monkeypatch)
    client = _client()
    user, token = _mk_user()

    # гость — 401
    assert client.get("/api/automations").status_code == 401

    created = client.post("/api/automations", json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    assert created.status_code == 200, created.text
    card = created.json()
    auto_id = card["id"]
    assert card["n8n_workflow_id"] == "wf-123"
    assert card["active"] is False
    assert fake.calls[0][0] == "create"
    assert fake.calls[0][2] == ["Триггер", "Агент AIOS 1"]

    # запуск выключенной: публикует, затем дергает webhook
    run = client.post(f"/api/automations/{auto_id}/run", headers={"Authorization": f"Bearer {token}"})
    assert run.status_code == 200
    body = run.json()
    assert body["ok"] is True and body["activated"] is True
    assert body["result"] == {"text": "готово"}
    ops = [c[0] for c in fake.calls]
    assert "publish" in ops and "webhook" in ops
    assert fake.calls[-1] == ("webhook", card["webhook_path"])

    # история
    hist = client.get(f"/api/automations/{auto_id}/executions", headers={"Authorization": f"Bearer {token}"})
    assert hist.status_code == 200
    items = hist.json()["items"]
    assert [i["id"] for i in items] == ["ex-1", "ex-2"]

    retry = client.post(
        f"/api/automations/{auto_id}/executions/ex-2/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry.status_code == 200
    assert ("retry", "ex-2") in fake.calls

    # удаление чистит n8n (выключение + удаление воркфлоу)
    deleted = client.delete(f"/api/automations/{auto_id}", headers={"Authorization": f"Bearer {token}"})
    assert deleted.status_code == 200
    ops = [c[0] for c in fake.calls]
    assert "unpublish" in ops and "delete" in ops


def test_update_translates_blocks(monkeypatch):
    _configure_n8n()
    fake = _patch_n8n(monkeypatch)
    client = _client()
    _, token = _mk_user()
    card = client.post("/api/automations", json=PAYLOAD, headers={"Authorization": f"Bearer {token}"}).json()

    changed = dict(PAYLOAD)
    changed["steps"] = [{"type": "http", "method": "GET", "url": "https://example.com"}]
    r = client.put(f"/api/automations/{card['id']}", json=changed, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert ("update", "wf-123", "Утренняя сводка") in fake.calls


def test_permissions_owner_admin_and_stranger(monkeypatch):
    _configure_n8n()
    _patch_n8n(monkeypatch)
    client = _client()
    _, owner_token = _mk_user("owner@ex.com")
    _, stranger_token = _mk_user("stranger@ex.com")
    _, admin_token = _mk_user("boss@ex.com", role="Администратор")

    card = client.post("/api/automations", json=PAYLOAD, headers={"Authorization": f"Bearer {owner_token}"}).json()
    auto_id = card["id"]

    # чужая автоматизация неотличима от несуществующей
    for method, path in (
        ("get", f"/api/automations/{auto_id}"),
        ("put", f"/api/automations/{auto_id}"),
        ("delete", f"/api/automations/{auto_id}"),
        ("post", f"/api/automations/{auto_id}/run"),
    ):
        kwargs = {"json": {}} if method in ("put", "post") else {}
        r = getattr(client, method)(path, headers={"Authorization": f"Bearer {stranger_token}"}, **kwargs)
        assert r.status_code == 404, (method, r.status_code)

    # список чужому не показывает
    mine = client.get("/api/automations", headers={"Authorization": f"Bearer {stranger_token}"}).json()
    assert mine["total"] == 0

    # админ видит и в общем списке, и по id
    assert client.get(f"/api/automations/{auto_id}", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200
    all_view = client.get("/api/automations?all=1", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert all_view["total"] == 1

    # обычному пользователю ?all=1 недоступен смыслально: вернутся только свои
    owner_all = client.get("/api/automations?all=1", headers={"Authorization": f"Bearer {owner_token}"}).json()
    assert owner_all["total"] == 1  # его единственная


def test_block_validation(monkeypatch):
    _configure_n8n()
    _patch_n8n(monkeypatch)
    client = _client()
    _, token = _mk_user()

    def post(payload):
        return client.post("/api/automations", json=payload, headers={"Authorization": f"Bearer {token}"})

    # пустое имя
    assert post({**PAYLOAD, "name": ""}).status_code == 422
    # неизвестный триггер
    assert post({**PAYLOAD, "trigger": {"type": "magic"}}).status_code == 422
    # расписание без числа
    assert post({**PAYLOAD, "trigger": {"type": "schedule"}}).status_code == 422
    # ни одного шага
    assert post({**PAYLOAD, "steps": []}).status_code == 422
    # http-шаг с невалидным JSON в теле
    bad_http = {**PAYLOAD, "steps": [{"type": "http", "method": "POST", "url": "https://x.test", "body": "{oops"}]}
    assert post(bad_http).status_code == 422
    # письмо без получателя
    bad_mail = {**PAYLOAD, "steps": [{"type": "email", "to": "", "subject": "s", "text": "t"}]}
    assert post(bad_mail).status_code == 422


# ---------------------------------------------------------------------------
# Агентный шаг


def test_agent_step_service_key(monkeypatch):
    import asyncio

    service_key = _configure_n8n()
    client = _client()
    user, _ = _mk_user("owner@ex.com")

    from api.routers import chat as chat_router
    from api.runtime.providers import MockProvider

    monkeypatch.setattr(chat_router, "get_provider", lambda *a, **k: MockProvider())

    # без ключа — 401
    r = client.post("/api/internal/agent-step", json={"owner_id": user["id"], "prompt": "привет"})
    assert r.status_code == 401
    # с неверным ключом — 401
    r = client.post(
        "/api/internal/agent-step",
        json={"owner_id": user["id"], "prompt": "привет"},
        headers={"X-AIOS-Service-Key": "wrong"},
    )
    assert r.status_code == 401
    # несуществующий владелец — 404
    r = client.post(
        "/api/internal/agent-step",
        json={"owner_id": "usr-nope", "prompt": "привет"},
        headers={"X-AIOS-Service-Key": service_key},
    )
    assert r.status_code == 404

    # корректный вызов: агентный цикл от имени владельца, ответ — текст
    r = client.post(
        "/api/internal/agent-step",
        json={"owner_id": user["id"], "prompt": "Сделай сводку"},
        headers={"X-AIOS-Service-Key": service_key},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"]

    # одноразовая сессия не остаётся в списке диалогов владельца
    from api.routers.chat import get_store as get_session_store

    sessions = get_session_store().summaries(user_id=user["id"])
    assert sessions == []
    # asyncio импортирован для симметрии с клиентскими тестами выше
    assert asyncio is not None
