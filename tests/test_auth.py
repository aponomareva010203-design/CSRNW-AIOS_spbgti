"""Смоук-тесты серверной авторизации: хранилища, токены, эндпоинты, админ-раздел."""

import pytest

from api.runtime.config import get_settings
from api.runtime.tokens import TokenStore
from api.runtime.users import DuplicateLogin, UserError, UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    """Детерминированный seed-админ для всех тестов модуля.

    Seed создаётся из настроек (AIOS_ADMIN_LOGIN / AIOS_ADMIN_PASSWORD),
    поэтому фиксируем их и сбрасываем кэш настроек до и после каждого теста.
    """
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_users_store(tmp_path):
    store = UserStore(tmp_path / "users")

    user = store.create(login="  User@Example.COM ", password="secret", name="  Тестов Т.  ")
    assert user["login"] == "user@example.com"
    assert user["name"] == "Тестов Т."
    assert user["role"] == "Пользователь"
    assert user["last_login"] is None
    # в публичном представлении нет секретов
    assert set(store.public(user)) == {"id", "login", "name", "role", "created_at", "last_login"}

    assert store.verify_password(store.get(user["id"]), "secret") is True
    assert store.verify_password(store.get(user["id"]), "wrong") is False

    with pytest.raises(DuplicateLogin):
        store.create(login="user@example.com", password="12345", name="Ещё Один")
    with pytest.raises(UserError):
        store.create(login="не-почта", password="12345", name="Имя И.")
    with pytest.raises(UserError):
        store.create(login="a@b.io", password="123", name="Имя И.")
    with pytest.raises(UserError):
        store.create(login="c@d.io", password="12345", name="И")

    assert store.get_by_login("USER@EXAMPLE.com")["id"] == user["id"]
    assert [u["login"] for u in store.list_all()] == [ADMIN_LOGIN, "user@example.com"]
    assert store.delete(user["id"]) is True
    assert store.get(user["id"]) is None
    assert store.get("../etc/passwd") is None


def test_users_store_seeds_single_admin(tmp_path):
    root = tmp_path / "users"
    store = UserStore(root)
    admin = store.get_by_login(ADMIN_LOGIN)
    assert admin is not None and admin["role"] == "Администратор"
    assert store.verify_password(admin, ADMIN_PASSWORD) is True
    assert store.count_admins() == 1

    # повторная инстанцирация того же каталога не плодит дубликаты
    again = UserStore(root)
    assert [u["login"] for u in again.list_all()] == [ADMIN_LOGIN]


def test_users_store_seeds_admin_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", "boss@corp.io")
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    try:
        store = UserStore(tmp_path / "users")
        admin = store.get_by_login("boss@corp.io")
        assert admin is not None and admin["role"] == "Администратор"
        assert store.verify_password(admin, "s3cret-pass") is True
        assert store.get_by_login(ADMIN_LOGIN) is None
    finally:
        get_settings.cache_clear()


def test_users_store_set_password(tmp_path):
    store = UserStore(tmp_path / "users")
    admin = store.get_by_login(ADMIN_LOGIN)
    old_hash = admin["password_hash"]

    store.set_password(admin, "brand-new-pass")
    assert store.verify_password(admin, ADMIN_PASSWORD) is False
    assert store.verify_password(admin, "brand-new-pass") is True
    # соль перевыпущена — хеш изменился
    assert admin["password_hash"] != old_hash

    with pytest.raises(UserError):
        store.set_password(admin, "123")


def test_tokens_store(tmp_path):
    store = TokenStore(tmp_path / "auth")

    token = store.issue("abc123def456", ttl=3600)
    assert store.resolve(token) == "abc123def456"
    assert len(token) == 64

    expired = store.issue("u1", ttl=-1)
    assert store.resolve(expired) is None

    assert store.resolve("../etc/passwd") is None
    assert store.resolve("z" * 64) is None

    assert store.revoke(token) is True
    assert store.resolve(token) is None
    assert store.revoke(token) is False

    store.issue("u2")
    store.issue("u2")
    assert store.revoke_all_for_user("u2") == 2


def _reset(monkeypatch, tmp_path):
    """Свежие синглтоны на изолированном runtime-каталоге."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    return auth_router


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_change_password_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app

    _reset(monkeypatch, tmp_path)
    client = TestClient(app)

    # две сессии администратора
    t1 = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    t2 = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]

    # неверный текущий пароль
    assert (
        client.patch(
            "/api/auth/password",
            json={"old_password": "wrong", "new_password": "newpass1"},
            headers=auth(t1),
        ).status_code
        == 400
    )

    # смена из сессии t1
    assert (
        client.patch(
            "/api/auth/password",
            json={"old_password": ADMIN_PASSWORD, "new_password": "newpass1"},
            headers=auth(t1),
        ).json()["ok"]
        is True
    )

    # текущая сессия жива, вторая отозвана
    assert client.get("/api/auth/me", headers=auth(t1)).status_code == 200
    assert client.get("/api/auth/me", headers=auth(t2)).status_code == 401

    # старый пароль больше не работает, новый — да
    assert (
        client.post(
            "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
        ).status_code
        == 401
    )
    t3 = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": "newpass1"}
    ).json()["token"]
    assert client.get("/api/auth/me", headers=auth(t3)).status_code == 200

    get_settings.cache_clear()


def test_auth_endpoints_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app

    _reset(monkeypatch, tmp_path)
    client = TestClient(app)

    # --- регистрация ---
    resp = client.post(
        "/api/auth/register",
        json={"login": "user@ex.com", "password": "pass1234", "name": "Пользователь П."},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["role"] == "Пользователь"
    assert "password_hash" not in created and "salt" not in created

    # занятый логин → 409; некорректный e-mail → 422
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "user@ex.com", "password": "pass1234", "name": "Дубль Д."},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "bad", "password": "pass1234", "name": "Плохой Л."},
        ).status_code
        == 422
    )

    # --- вход ---
    assert (
        client.post("/api/auth/login", json={"login": "user@ex.com", "password": "wrong"}).status_code
        == 401
    )
    resp = client.post("/api/auth/login", json={"login": "USER@EX.COM", "password": "pass1234"})
    assert resp.status_code == 200
    user_token = resp.json()["token"]
    user_id = resp.json()["user"]["id"]
    assert resp.json()["user"]["last_login"] is not None

    # --- /me ---
    me = client.get("/api/auth/me", headers=auth(user_token))
    assert me.status_code == 200 and me.json()["login"] == "user@ex.com"
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=auth("f" * 64)).status_code == 401

    # --- админ-раздел: обычному пользователю запрещён ---
    assert client.get("/api/admin/users", headers=auth(user_token)).status_code == 403

    # --- вход стартовым администратором ---
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    admin_token = resp.json()["token"]
    admin_id = resp.json()["user"]["id"]

    items = client.get("/api/admin/users", headers=auth(admin_token)).json()["items"]
    assert [u["login"] for u in items] == [ADMIN_LOGIN, "user@ex.com"]
    assert all("password_hash" not in u for u in items)

    # --- смена роли ---
    resp = client.patch(
        f"/api/admin/users/{user_id}", json={"role": "Администратор"}, headers=auth(admin_token)
    )
    assert resp.status_code == 200 and resp.json()["role"] == "Администратор"
    assert (
        client.patch(
            f"/api/admin/users/{user_id}", json={"role": "НетТакойРоли"}, headers=auth(admin_token)
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/admin/users/000000000000", json={"role": "Пользователь"}, headers=auth(admin_token)
        ).status_code
        == 404
    )

    # теперь два админа: само-понижение разрешено
    assert (
        client.patch(
            f"/api/admin/users/{admin_id}", json={"role": "Пользователь"}, headers=auth(admin_token)
        ).status_code
        == 200
    )
    # пониженному админу раздел уже недоступен
    assert client.get("/api/admin/users", headers=auth(admin_token)).status_code == 403

    # последний админ не может снять свою роль
    resp = client.post("/api/auth/login", json={"login": "user@ex.com", "password": "pass1234"})
    user_token2 = resp.json()["token"]
    assert (
        client.patch(
            f"/api/admin/users/{user_id}", json={"role": "Пользователь"}, headers=auth(user_token2)
        ).status_code
        == 400
    )

    # --- удаление ---
    assert client.delete(f"/api/admin/users/{user_id}", headers=auth(user_token2)).status_code == 400
    assert (
        client.delete(f"/api/admin/users/{admin_id}", headers=auth(user_token2)).status_code == 200
    )
    # токены удалённого пользователя отозваны; чужие — нет
    assert client.get("/api/auth/me", headers=auth(admin_token)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(user_token)).status_code == 200

    # --- выход (токен пользователя отзывается на сервере) ---
    assert client.post("/api/auth/logout", headers=auth(user_token2)).json()["ok"] is True
    assert client.get("/api/auth/me", headers=auth(user_token2)).status_code == 401

    get_settings.cache_clear()

