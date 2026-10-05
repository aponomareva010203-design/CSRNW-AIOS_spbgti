"""Тесты суточного учёта и лимитов токенов LLM.

UsageStore: пожизненные агрегаты (tokens.json) плюс расход за текущие
сутки (daily.json — при смене московских суток старые записи
выбрасываются, счётчик обнуляется). UsageLimitsStore: общий суточный
лимит и индивидуальные оверрайды. Агентный цикл: шаг к модели не
выполняется, когда лимит исчерпан. API: /api/usage/me, /api/admin/limits,
/api/admin/users/{id}/token-limit и поля tokens_today/daily_token_limit
в /api/admin/users.
"""

import asyncio

import pytest

from api.runtime import projects as projects_module
from api.runtime import usage as usage_module
from api.runtime.config import get_settings
from api.runtime.loop import run_agent_loop
from api.runtime.providers import MockProvider
from api.runtime.sessions import SessionStore
from api.runtime.tools import build_default_registry
from api.runtime.usage import UsageStore, get_store as get_usage_store
from api.runtime.usage_limits import (
    DEFAULT_DAILY_LIMIT,
    UsageLimitsError,
    UsageLimitsStore,
    get_store as get_limits_store,
)

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Изолированный runtime-каталог и свежие кэши всех синглтонов."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    get_usage_store.cache_clear()
    get_limits_store.cache_clear()
    projects_module.get_store.cache_clear()
    yield
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    get_usage_store.cache_clear()
    get_limits_store.cache_clear()
    projects_module.get_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- UsageStore: суточный учёт ----------


def test_daily_usage_and_reset(tmp_path, monkeypatch):
    store = UsageStore(tmp_path)
    store.record("u1", 100, 50)
    store.record("u1", 10, 5)
    store.record("u2", 7, 0)

    today = store.used_today("u1")
    assert today["prompt"] == 110 and today["completion"] == 55
    assert today["total"] == 165 and today["requests"] == 2
    assert store.used_today("u2")["total"] == 7
    assert store.used_today("ghost")["total"] == 0
    # пожизненный агрегат при этом копится как раньше
    assert store.get("u1")["prompt"] == 110

    # смена московских суток: «сегодня» обнулено, новая запись попадает
    # в новые сутки, а вчерашние записи выбрасываются
    monkeypatch.setattr(usage_module, "today_key", lambda: "2000-01-02")
    assert store.used_today("u1")["total"] == 0
    store.record("u1", 3, 0)
    daily = store._load_daily()
    assert list(daily.keys()) == ["2000-01-02"]
    assert daily["2000-01-02"]["u1"]["prompt"] == 3

    # delete_user чистит и пожизненные, и суточные записи
    assert store.delete_user("u1") is True
    assert store.get("u1")["prompt"] == 0
    assert store.used_today("u1")["total"] == 0


# ---------- UsageLimitsStore: общий лимит и оверрайды ----------


def test_limits_store(tmp_path):
    store = UsageLimitsStore(tmp_path)
    assert store.effective_limit("u1") == DEFAULT_DAILY_LIMIT
    assert store.override("u1") is None

    store.set_global(1_000_000)
    assert store.effective_limit("u1") == 1_000_000
    assert store.config()["daily_limit"] == 1_000_000

    store.set_override("u1", 8_000_000)
    assert store.effective_limit("u1") == 8_000_000
    assert store.effective_limit("u2") == 1_000_000

    with pytest.raises(UsageLimitsError):
        store.set_global(0)
    with pytest.raises(UsageLimitsError):
        store.set_override("u1", -5)
    with pytest.raises(UsageLimitsError):
        store.set_override("", 100)

    store.set_override("u1", None)
    assert store.override("u1") is None
    assert store.effective_limit("u1") == 1_000_000


# ---------- Агентный цикл: остановка при исчерпанном лимите ----------


def _run_loop(settings, store, registry, session, text):
    async def run():
        events = []
        async for event in run_agent_loop(
            settings=settings,
            provider=MockProvider(),
            registry=registry,
            store=store,
            session=session,
            user_text=text,
        ):
            events.append(event)
        return events

    return asyncio.run(run())


def test_loop_blocks_when_limit_exceeded(tmp_path):
    settings = get_settings()
    store = SessionStore(tmp_path / "sessions")
    registry = build_default_registry(tmp_path / "tools")
    session = store.create(title="t")
    session["user_id"] = "u-limit"
    store.save(session)

    # расход ровно в лимит — следующий шаг к модели запрещён
    get_usage_store().record("u-limit", DEFAULT_DAILY_LIMIT, 0)

    events = _run_loop(settings, store, registry, session, "найди знания про катализ полимеров")
    assert [e["type"] for e in events] == ["error"]
    assert "лимит" in events[0]["message"].lower()
    # сообщение пользователя сохранено, ответа модели нет
    roles = [m["role"] for m in store.get(session["id"])["messages"]]
    assert roles == ["user"]


def test_loop_allows_below_limit(tmp_path):
    settings = get_settings()
    store = SessionStore(tmp_path / "sessions")
    registry = build_default_registry(tmp_path / "tools")
    session = store.create(title="t")
    session["user_id"] = "u-ok"
    store.save(session)

    get_usage_store().record("u-ok", 100, 50)

    events = _run_loop(settings, store, registry, session, "найди знания про катализ полимеров")
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done"
    assert "error" not in kinds


# ---------- API ----------


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def test_usage_api(monkeypatch):
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()

    client = _client()
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    user_id = auth_router.get_user_store().get_by_login(ADMIN_LOGIN)["id"]

    # расход за сутки 0, лимит — общий по умолчанию (3,5 млн)
    resp = client.get("/api/usage/me", headers=auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["used_today"] == 0
    assert data["limit"] == DEFAULT_DAILY_LIMIT
    assert data["is_override"] is False

    # админ меняет общий лимит — сразу действует
    resp = client.put("/api/admin/limits", json={"daily_limit": 1_000_000}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["daily_limit"] == 1_000_000
    assert client.get("/api/usage/me", headers=auth(token)).json()["limit"] == 1_000_000

    # невалидный лимит — 422
    assert client.put(
        "/api/admin/limits", json={"daily_limit": 0}, headers=auth(token)
    ).status_code == 422

    # индивидуальный лимит перекрывает общий
    resp = client.put(
        f"/api/admin/users/{user_id}/token-limit",
        json={"daily_token_limit": 8_000_000},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    me = client.get("/api/usage/me", headers=auth(token)).json()
    assert me["limit"] == 8_000_000 and me["is_override"] is True

    # в админ-таблице видны расход за сутки и оверрайд
    resp = client.get("/api/admin/users", headers=auth(token))
    assert resp.status_code == 200
    item = next(u for u in resp.json()["items"] if u["id"] == user_id)
    assert item["tokens_today"] == 0
    assert item["daily_token_limit"] == 8_000_000

    # null возвращает общий лимит
    resp = client.put(
        f"/api/admin/users/{user_id}/token-limit",
        json={"daily_token_limit": None},
        headers=auth(token),
    )
    assert resp.status_code == 200
    me = client.get("/api/usage/me", headers=auth(token)).json()
    assert me["limit"] == 1_000_000 and me["is_override"] is False

    # чужой пользователь — 404
    assert client.put(
        "/api/admin/users/no-such-id/token-limit",
        json={"daily_token_limit": 100},
        headers=auth(token),
    ).status_code == 404

    # без токена — 401
    assert client.get("/api/admin/limits").status_code == 401
    assert client.get("/api/usage/me").status_code == 401
