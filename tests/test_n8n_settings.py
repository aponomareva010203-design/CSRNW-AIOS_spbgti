"""Настройки n8n: хранилище, приоритет admin→env, админ-эндпоинты, маска ключа."""

import pytest

from api.runtime.config import get_settings
from api.runtime.llm_settings import mask_key
from api.runtime.n8n_settings import (
    N8nSettingsError,
    N8nSettingsStore,
    effective_config,
)
from api.runtime.users import UserStore

ADMIN_LOGIN = "admin@ex.com"
ADMIN_PASSWORD = "pass-1234"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("AIOS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIOS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_LLM_MODEL", raising=False)
    monkeypatch.delenv("AIOS_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("AIOS_N8N_API_KEY", raising=False)
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_n8n_store_validation_and_service_key(tmp_path):
    store = N8nSettingsStore(tmp_path / "rt")

    with pytest.raises(N8nSettingsError):
        store.save("not-a-url", "key")
    with pytest.raises(N8nSettingsError):
        store.save("http://127.0.0.1:5678", "")

    saved = store.save("  http://127.0.0.1:5678/  ", "n8n-key-123456")
    assert saved["base_url"] == "http://127.0.0.1:5678"
    assert saved["api_key"] == "n8n-key-123456"
    assert saved["service_key"].startswith("aios-svc-")

    # service_key не пересоздаётся при смене адреса/ключа: ноды n8n
    # уже несут старый секрет
    again = store.save("http://127.0.0.1:5789", "n8n-key-999999")
    assert again["service_key"] == saved["service_key"]

    assert mask_key("n8n-key-123456") == "n8n" + "•" * 6 + "3456"


def test_effective_config_priority(tmp_path, monkeypatch):
    # не настроено
    assert effective_config().source == ""
    assert effective_config().configured is False

    # .env
    monkeypatch.setenv("AIOS_N8N_BASE_URL", "http://env-n8n:5678")
    monkeypatch.setenv("AIOS_N8N_API_KEY", "env-key")
    get_settings.cache_clear()
    cfg = effective_config()
    assert (cfg.source, cfg.base_url, cfg.api_key) == ("env", "http://env-n8n:5678", "env-key")
    assert cfg.service_key  # ленивый секрет для env-конфигурации

    # настройки админа сильнее .env
    N8nSettingsStore(tmp_path / "rt").save("http://admin-n8n:5678", "admin-key")
    cfg = effective_config()
    assert (cfg.source, cfg.base_url, cfg.api_key) == ("admin", "http://admin-n8n:5678", "admin-key")


def _reset(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import auth as auth_router
    from api.routers import automations as automations_router

    for fn in (
        auth_router.get_user_store,
        auth_router.get_token_store,
        auth_router.get_confirmation_store,
        auth_router.get_llm_store,
        automations_router.get_n8n_store,
    ):
        fn.cache_clear()
    return TestClient(app), automations_router


def test_admin_n8n_endpoints(tmp_path, monkeypatch):
    client, automations_router = _reset(monkeypatch)

    from api.routers.auth import get_token_store

    # обычный пользователь напрямую через хранилище: регистрация зависит
    # от SMTP/блоклиста доменов из .env, а здесь проверяются только права
    users = UserStore(get_settings().runtime_dir / "users")
    u = users.create(login="u@ex.com", password="pass1234", name="Юзер Ю.", confirmed=True)
    utoken = get_token_store().issue(u["id"])
    user_auth = {"Authorization": f"Bearer {utoken}"}

    atoken = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    admin_auth = {"Authorization": f"Bearer {atoken}"}

    # обычному пользователю раздел закрыт
    for method, path, body in (
        ("get", "/api/admin/n8n", None),
        ("put", "/api/admin/n8n", {}),
        ("post", "/api/admin/n8n/test", {}),
    ):
        if body is None:
            r = client.get(path, headers=user_auth)
        else:
            r = getattr(client, method)(path, json=body, headers=user_auth)
        assert r.status_code == 403, (method, path)

    # не настроено
    view = client.get("/api/admin/n8n", headers=admin_auth).json()
    assert view["configured"] is False

    # некорректный URL → 422
    bad = client.put("/api/admin/n8n", json={"base_url": "not-a-url", "api_key": "k"}, headers=admin_auth)
    assert bad.status_code == 422

    # сохранение: ключ отдаётся только маской
    ok = client.put(
        "/api/admin/n8n",
        json={"base_url": "http://127.0.0.1:9", "api_key": "n8n-key-123456"},
        headers=admin_auth,
    )
    assert ok.status_code == 200
    view = ok.json()
    assert view["configured"] is True and view["source"] == "admin"
    assert view["api_key_masked"] == "n8n" + "•" * 6 + "3456"

    # пустой ключ = прежний
    client.put("/api/admin/n8n", json={"base_url": "http://127.0.0.1:5678", "api_key": ""}, headers=admin_auth)
    saved = automations_router.get_n8n_store().load()
    assert saved["api_key"] == "n8n-key-123456"

    # проверка связи с закрытым портом — отчёт, не 500
    res = client.post("/api/admin/n8n/test", json={}, headers=admin_auth).json()
    assert res["ok"] is False and res.get("error")
