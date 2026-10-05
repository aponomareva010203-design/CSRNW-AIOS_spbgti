"""Настройки LLM: хранилище, приоритет admin→env, админ-эндпоинты, чат без настроек."""

import pytest

from api.runtime.config import get_settings
from api.runtime.llm_settings import (
    LLMSettingsError,
    LLMSettingsStore,
    effective_config,
    mask_key,
)

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Чистое окружение: без LLM из .env, свежий runtime-каталог."""
    monkeypatch.delenv("AIOS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIOS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_LLM_MODEL", raising=False)
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # без SMTP регистрация идёт в dev-режиме: без MX-проверки домена
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_llm_store_validation_and_mask(tmp_path):
    store = LLMSettingsStore(tmp_path / "rt")

    with pytest.raises(LLMSettingsError):
        store.save("not-a-url", "key", "m1")
    with pytest.raises(LLMSettingsError):
        store.save("https://api.example.com/v1", "key", "")

    store.save("  https://api.example.com/v1/  ", "sk-abcdefgh1234", "gpt-test")
    saved = store.load()
    assert saved["base_url"] == "https://api.example.com/v1"
    assert saved["model"] == "gpt-test"

    assert mask_key("sk-abcdefgh1234") == "sk-" + "•" * 6 + "1234"
    assert mask_key("") == ""
    assert mask_key("short") == "•" * 5


def test_effective_config_priority(tmp_path, monkeypatch):
    # не настроено
    assert effective_config().source == ""

    # .env (окружение)
    monkeypatch.setenv("AIOS_LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("AIOS_LLM_API_KEY", "env-key")
    monkeypatch.setenv("AIOS_LLM_MODEL", "env-model")
    get_settings.cache_clear()
    cfg = effective_config()
    assert (cfg.source, cfg.model) == ("env", "env-model")

    # настройки админа сильнее .env
    LLMSettingsStore(tmp_path / "rt").save(
        "https://admin.example.com/v1", "admin-key", "admin-model"
    )
    cfg = effective_config()
    assert (cfg.source, cfg.model) == ("admin", "admin-model")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _reset_clients(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import auth as auth_router
    from api.routers import chat as chat_router

    for fn in (
        auth_router.get_user_store,
        auth_router.get_token_store,
        auth_router.get_llm_store,
        chat_router.get_store,
        chat_router.get_registry,
        chat_router.get_provider,
    ):
        fn.cache_clear()
    return TestClient(app), auth_router, chat_router


def test_admin_llm_endpoints(tmp_path, monkeypatch):
    client, auth_router, chat_router = _reset_clients(monkeypatch)

    # до настройки: рантайм не настроен, чат отвечает 503 без mock-ответов
    assert client.get("/api/runtime").json()["mode"] == "not_configured"
    resp = client.post("/api/chat", json={"message": "привет"})
    assert resp.status_code == 503
    assert "не настроена" in resp.json()["detail"]

    # обычному пользователю LLM-раздел закрыт
    client.post(
        "/api/auth/register",
        json={"login": "u@ex.com", "password": "pass1234", "name": "Юзер Ю.", "consent_pdn": True, "consent_policy": True},
    )
    # регистрация требует подтверждения e-mail — подтверждаем учётку напрямую
    auth_router.get_user_store().confirm(
        auth_router.get_user_store().get_by_login("u@ex.com")
    )
    utoken = client.post(
        "/api/auth/login", json={"login": "u@ex.com", "password": "pass1234"}
    ).json()["token"]
    calls = (
        ("get", "/api/admin/llm", None),
        ("put", "/api/admin/llm", {}),
        ("post", "/api/admin/llm/test", {}),
        ("post", "/api/admin/llm/models", {"base_url": "http://127.0.0.1:9/v1"}),
    )
    for method, path, body in calls:
        if body is None:
            r = client.get(path, headers=_auth(utoken))
        else:
            r = getattr(client, method)(path, json=body, headers=_auth(utoken))
        assert r.status_code == 403, (method, path)

    # админ: текущая конфигурация не настроена
    atoken = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    view = client.get("/api/admin/llm", headers=_auth(atoken)).json()
    assert view["configured"] is False

    # некорректный URL → 422
    bad = client.put(
        "/api/admin/llm",
        json={"base_url": "not-a-url", "api_key": "k", "model": "m"},
        headers=_auth(atoken),
    )
    assert bad.status_code == 422

    # сохранение: применяется на лету, ключ отдаётся только маской
    ok = client.put(
        "/api/admin/llm",
        json={"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-secret-key-000111", "model": "test-model"},
        headers=_auth(atoken),
    )
    assert ok.status_code == 200
    view = ok.json()
    assert view["configured"] is True and view["source"] == "admin"
    assert view["api_key_masked"] == "sk-" + "•" * 6 + "0111"

    runtime = client.get("/api/runtime").json()
    assert runtime["mode"] == "live"
    assert runtime["default_model"] == "test-model"

    # смена модели с пустым ключом сохраняет прежний ключ
    client.put(
        "/api/admin/llm",
        json={"base_url": "http://127.0.0.1:9/v1", "api_key": "", "model": "other"},
        headers=_auth(atoken),
    )
    saved = auth_router.get_llm_store().load()
    assert saved["api_key"] == "sk-secret-key-000111"
    assert saved["model"] == "other"

    # проверка соединения с недоступным эндпоинтом — ошибка транспорта, не 500
    res = client.post("/api/admin/llm/test", json={}, headers=_auth(atoken)).json()
    assert res["ok"] is False and res.get("error")

    # список моделей с недоступного эндпоинта → 502
    assert (
        client.post(
            "/api/admin/llm/models",
            json={"base_url": "http://127.0.0.1:9/v1", "api_key": "k"},
            headers=_auth(atoken),
        ).status_code
        == 502
    )

    chat_router.get_provider.cache_clear()
