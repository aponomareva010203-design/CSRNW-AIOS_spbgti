"""Смоук-тесты вкладки «Настройки»: личные настройки внешнего API (LLM).

Хранилище (UserLLMStore, user/{id}-llm.json): валидация base_url,
сохранение прежнего ключа при api_key=None, очистка. effective_config_for:
личная конфигурация перекрывает общую (модель остаётся общей, source
"user"), без личных — общая. API: GET/PUT/DELETE /api/settings/llm — ключ
никогда не отдаётся целиком (только маска), несовпадение ключа и повтора —
422, пустой base_url — возврат к общим. Лимиты: при личных настройках
usage/me отдаёт limit: null (llm_mode «личные»), агентный цикл не
блокируется суточным лимитом; в /api/admin/users присутствует признак
personal_llm; при удалении учётки личные настройки стираются.
"""

import asyncio

import pytest

from api.runtime.config import get_settings
from api.runtime.llm_settings import (
    LLMSettingsStore,
    effective_config,
    effective_config_for,
)
from api.runtime.loop import _token_limit_block
from api.runtime.providers import ProviderError
from api.runtime.usage import get_store as get_usage_store
from api.runtime.usage_limits import get_store as get_limits_store
from api.runtime.user_llm import UserLLMError, UserLLMStore, get_store as get_user_llm_store
from api.runtime.users import UserStore

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime, свежие кэши, без env-LLM."""
    from api.routers import auth as auth_router
    from api.routers import chat as chat_router
    from api.runtime import projects as projects_runtime
    from api.runtime import user_llm as user_llm_runtime
    from api.runtime import user_context as user_context_runtime

    for name in ("AIOS_LLM_BASE_URL", "AIOS_LLM_API_KEY", "AIOS_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    auth_router.get_llm_store.cache_clear()
    chat_router.get_provider.cache_clear()
    projects_runtime.get_store.cache_clear()
    user_llm_runtime.get_store.cache_clear()
    user_context_runtime.get_store.cache_clear()
    yield
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    auth_router.get_llm_store.cache_clear()
    chat_router.get_provider.cache_clear()
    projects_runtime.get_store.cache_clear()
    user_llm_runtime.get_store.cache_clear()
    user_context_runtime.get_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _mk_user(client, login="user@ex.com", name="Тестов Тест Тестович"):
    """Учётка с подтверждением и Bearer-токен (без e-mail-потока)."""
    from api.routers.auth import get_token_store

    users = UserStore(get_settings().runtime_dir / "users")
    user = users.create(login=login, password="pass1234", name=name, confirmed=True)
    token = get_token_store().issue(user["id"])
    return user, token


# ----- хранилище личных настроек -----


def test_user_llm_store_roundtrip(tmp_path):
    store = UserLLMStore(tmp_path / "user")
    assert store.get("abc123def456") is None

    saved = store.set(
        "abc123def456",
        provider="vLLM", base_url="https://llm.local/v1/", api_key="sk-1", model="qwen2.5:14b",
    )
    assert saved["base_url"] == "https://llm.local/v1"  # слэш на конце срезается
    assert saved["provider"] == "vLLM"
    assert saved["api_key"] == "sk-1"
    assert saved["model"] == "qwen2.5:14b"

    # api_key/model=None — прежние значения сохраняются; пустая строка — стираются
    saved = store.set(
        "abc123def456", base_url="https://llm2.local/v1", api_key=None, model=None
    )
    assert saved["api_key"] == "sk-1"
    assert saved["model"] == "qwen2.5:14b"
    saved = store.set("abc123def456", base_url="https://llm2.local/v1", api_key="", model="")
    assert saved["api_key"] == ""
    assert saved["model"] == ""  # пустая модель — используется общая платформы

    store.clear("abc123def456")
    assert store.get("abc123def456") is None
    with pytest.raises(UserLLMError):
        store.set("abc123def456", base_url="не url")
    with pytest.raises(UserLLMError):
        store.set("короткий", base_url="https://x.local")


def test_effective_config_for_prefers_personal():
    settings = get_settings()
    # общая конфигурация — через админский файл llm.json
    LLMSettingsStore(settings.runtime_dir).save(
        base_url="https://shared.local/v1", api_key="shared-key", model="shared-model"
    )
    base = effective_config()
    assert base.source == "admin"

    uid = "abc123def456"
    assert effective_config_for(uid).base_url == "https://shared.local/v1"

    # личные настройки: endpoint/ключ пользователя; модель пустая — общая
    get_user_llm_store().set(uid, provider="OpenAI", base_url="https://personal.local/v1", api_key="own-key")
    personal = effective_config_for(uid)
    assert personal.source == "user"
    assert personal.base_url == "https://personal.local/v1"
    assert personal.api_key == "own-key"
    assert personal.model == base.model

    # личная модель указана — используется она
    get_user_llm_store().set(uid, base_url="https://personal.local/v1", api_key="own-key", model="own-model")
    personal = effective_config_for(uid)
    assert personal.model == "own-model"
    # пользователь без личных настроек — общая конфигурация
    other = effective_config_for("fff000fff000")
    assert other.source == "admin"


# ----- API /api/settings/llm -----


def test_settings_llm_requires_auth():
    client = _client()
    assert client.get("/api/settings/llm").status_code == 401
    assert client.put("/api/settings/llm", json={"base_url": "https://x.local"}).status_code == 401
    assert client.delete("/api/settings/llm").status_code == 401


def test_settings_llm_roundtrip_and_masking():
    client = _client()
    user, token = _mk_user(client)

    # пусто: общие настройки
    resp = client.get("/api/settings/llm", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "provider": "", "base_url": "", "api_key_masked": "",
        "model": "", "active": False, "llm_mode": "общие",
    }

    # сохранение: ключ отдаётся только маской; модель сохраняется
    resp = client.put(
        "/api/settings/llm",
        json={"provider": "Ollama", "base_url": "http://127.0.0.1:11434/v1", "api_key": "", "api_key2": "", "model": "qwen2.5:14b"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["active"] is True
    assert data["llm_mode"] == "личные"
    assert data["base_url"] == "http://127.0.0.1:11434/v1"
    assert data["model"] == "qwen2.5:14b"
    assert "api_key" not in data

    resp = client.put(
        "/api/settings/llm",
        json={"provider": "OpenAI", "base_url": "https://api.openai.com/v1", "api_key": "sk-secret-12345", "api_key2": "sk-secret-12345"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key_masked"].startswith("sk-")
    assert "secret" not in resp.json()["api_key_masked"]

    # несовпадение ключа и повтора — 422; кривой адрес — 422
    assert (
        client.put(
            "/api/settings/llm",
            json={"base_url": "https://x.local", "api_key": "a", "api_key2": "b"},
            headers=auth(token),
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/settings/llm",
            json={"base_url": "совсем не url", "api_key": "", "api_key2": ""},
            headers=auth(token),
        ).status_code
        == 422
    )

    # пустой ключ при сохранении — прежний ключ остаётся (маска та же)
    masked_before = client.get("/api/settings/llm", headers=auth(token)).json()["api_key_masked"]
    client.put(
        "/api/settings/llm",
        json={"base_url": "https://api.openai.com/v1", "api_key": "", "api_key2": ""},
        headers=auth(token),
    )
    assert client.get("/api/settings/llm", headers=auth(token)).json()["api_key_masked"] == masked_before

    # пустой base_url — возврат к общим настройкам (личные стираются)
    resp = client.put("/api/settings/llm", json={"base_url": ""}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False
    # DELETE — то же самое явно
    client.put(
        "/api/settings/llm",
        json={"base_url": "https://api.openai.com/v1", "api_key": "k1", "api_key2": "k1"},
        headers=auth(token),
    )
    resp = client.delete("/api/settings/llm", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["llm_mode"] == "общие"
    assert get_user_llm_store().get(user["id"]) is None


# ----- лимиты и чат при личных настройках -----


def test_usage_me_limit_null_with_personal():
    client = _client()
    user, token = _mk_user(client)
    get_limits_store().set_global(10_000)

    resp = client.get("/api/usage/me", headers=auth(token))
    assert resp.json()["limit"] == 10_000
    assert resp.json()["llm_mode"] == "общие"

    get_user_llm_store().set(user["id"], base_url="https://own.local/v1", api_key="k")
    resp = client.get("/api/usage/me", headers=auth(token))
    assert resp.json()["limit"] is None
    assert resp.json()["llm_mode"] == "личные"


def test_token_limit_bypassed_with_personal():
    user_id = "abc123def456"
    get_limits_store().set_global(10)
    get_usage_store().record(user_id, 500, 500)  # перерасход
    assert _token_limit_block(user_id) is not None  # общий случай: блок

    get_user_llm_store().set(user_id, base_url="https://own.local/v1", api_key="k")
    assert _token_limit_block(user_id) is None  # личный ключ — без лимита
    assert _token_limit_block(None) is None  # анонимная сессия не ограничивается


def test_chat_provider_uses_personal_config():
    from api.routers.chat import get_provider
    from api.runtime.providers import OpenAICompatProvider

    LLMSettingsStore(get_settings().runtime_dir).save(
        base_url="https://shared.local/v1", api_key="shared", model="shared-model"
    )
    uid = "abc123def456"
    shared = get_provider("https://shared.local/v1", "shared", "shared-model")
    assert isinstance(shared, OpenAICompatProvider)

    get_user_llm_store().set(uid, base_url="https://own.local/v1", api_key="own")
    own = get_provider("https://own.local/v1", "own", "shared-model")
    assert isinstance(own, OpenAICompatProvider)
    assert own is not shared
    # кэш по значениям: та же тройка — тот же провайдер
    assert get_provider("https://own.local/v1", "own", "shared-model") is own


# ----- админ-таблица и удаление учётки -----


def test_admin_users_flag_and_delete_cleanup():
    client = _client()
    user, token = _mk_user(client, login="member@ex.com")
    other, other_token = _mk_user(client, login="plain@ex.com", name="Обычный Ю. Ю.")
    get_user_llm_store().set(user["id"], base_url="https://own.local/v1", api_key="k")

    admin_token = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    resp = client.get("/api/admin/users", headers=auth(admin_token))
    assert resp.status_code == 200, resp.text
    by_id = {u["id"]: u for u in resp.json()["items"]}
    assert by_id[user["id"]]["personal_llm"] is True
    assert by_id[other["id"]]["personal_llm"] is False

    # удаление учётки стирает и личные настройки внешнего API
    assert get_user_llm_store().get(user["id"]) is not None
    resp = client.delete(f"/api/admin/users/{user['id']}", headers=auth(admin_token))
    assert resp.status_code == 200, resp.text
    assert get_user_llm_store().get(user["id"]) is None


# ----- проверка соединения (POST /api/settings/llm/test) -----


def test_settings_llm_test_endpoint():
    client = _client()
    user, token = _mk_user(client)
    get_user_llm_store().set(user["id"], base_url="https://own.local/v1", api_key="k")
    # общая модель не настроена (ни env, ни админский файл) — проверка невозможна
    resp = client.post("/api/settings/llm/test", json={}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert "модель" in resp.json()["error"]

    # задаём общую модель админским файлом — теперь проверка работает
    LLMSettingsStore(get_settings().runtime_dir).save(
        base_url="https://shared.local/v1", api_key="shared", model="shared-model"
    )

    # у пользователя без личных настроек и без черновика нечего проверять — 422
    _, clean_token = _mk_user(client, login="clean@ex.com", name="Чистый Ч. Ч.")
    resp = client.post("/api/settings/llm/test", json={}, headers=auth(clean_token))
    assert resp.status_code == 422, resp.text

    # недоступный endpoint из черновика формы: отчёт об ошибке, настройки не меняются
    resp = client.post(
        "/api/settings/llm/test",
        json={"base_url": "http://127.0.0.1:9/v1", "api_key": "k"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False
    assert "Не удалось связаться" in data["error"]
    # конфигурация не тронута
    assert get_user_llm_store().get(user["id"])["base_url"] == "https://own.local/v1"


# ----- FallbackProvider: личный endpoint с откатом к общим настройкам -----


class _FakeProvider:
    """Минимальный провайдер для проверки обёртки (тот же протокол событий)."""

    def __init__(self, events, error_before=None):
        self.events = events  # список ("text", …) / ("usage", …)
        self.error_before = error_before  # выбросить ProviderError перед N-м событием

    async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
        produced = 0
        for event in self.events:
            if self.error_before is not None and produced == self.error_before:
                raise ProviderError("Личный endpoint недоступен", status_code=None)
            produced += 1
            yield event


def test_fallback_provider():
    from api.runtime.providers import FallbackProvider, ProviderError

    async def collect(provider):
        out = []
        async for event in provider.stream_chat([{"role": "user", "content": "привет"}]):
            out.append(event)
        return out

    shared = _FakeProvider([("text", "общий ответ")])

    # личный работает — события без пометки об откате
    personal_ok = _FakeProvider([("text", "личный ответ")])
    out = asyncio.run(collect(FallbackProvider(personal_ok, shared)))
    assert out == [("text", "личный ответ")]

    # личный упал до первого события — пометка + ответ через общие
    personal_broken = _FakeProvider([("text", "не будет выдано")], error_before=0)
    out = asyncio.run(collect(FallbackProvider(personal_broken, shared)))
    assert len(out) == 2
    assert out[0][0] == "text" and "общие настройки" in out[0][1]
    assert out[1] == ("text", "общий ответ")

    # личный упал после первого события — откат невозможен, ошибка наверх
    personal_mid = _FakeProvider([("text", "начало…"), ("text", "дальше")], error_before=1)
    with pytest.raises(ProviderError):
        asyncio.run(collect(FallbackProvider(personal_mid, shared)))

    # без общего фолбэка ошибка личного провайдера уходит как есть
    with pytest.raises(ProviderError):
        asyncio.run(collect(FallbackProvider(_FakeProvider([("text", "x")], error_before=0), None)))
