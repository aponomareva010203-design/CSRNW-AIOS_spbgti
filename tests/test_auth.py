"""Смоук-тесты серверной авторизации: хранилища, токены, подтверждение e-mail, эндпоинты, админ-раздел."""

import logging

import pytest

from api.runtime.config import get_settings
from api.runtime.confirmations import ConfirmationStore
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
    assert user["confirmed"] is False  # новая регистрация ждёт подтверждения e-mail
    # в публичном представлении нет секретов
    assert set(store.public(user)) == {
        "id", "login", "name", "role", "created_at", "last_login", "confirmed"
    }

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

    # имя опционально: пустое значение допустимо
    anon = store.create(login="d@e.io", password="12345", name="")
    assert anon["name"] == ""
    assert anon["confirmed"] is False


def test_users_store_seeds_single_admin(tmp_path):
    root = tmp_path / "users"
    store = UserStore(root)
    admin = store.get_by_login(ADMIN_LOGIN)
    assert admin is not None and admin["role"] == "Администратор"
    assert store.verify_password(admin, ADMIN_PASSWORD) is True
    assert store.count_admins() == 1
    # seed-админ подтверждён (создан до введения подтверждения e-mail)
    assert admin["confirmed"] is True

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


def test_confirmations_store(tmp_path):
    store = ConfirmationStore(tmp_path / "confirm")

    token = store.issue("u1")
    assert len(token) == 43
    assert store.resolve(token) == "u1"

    # повторный выпуск переиспользует живой токен: ссылка из прежнего письма работает
    assert store.issue("u1") == token

    # просроченный токен удаляется, выдаётся новый
    expired = store.issue("u2", ttl=-1)
    assert store.resolve(expired) is None
    fresh = store.issue("u2")
    assert fresh != expired
    assert store.resolve(fresh) == "u2"

    # некорректные значения отбрасываются (path traversal)
    assert store.resolve("../etc/passwd") is None
    assert store.resolve("z" * 64) is None

    assert store.revoke(fresh) is True
    assert store.resolve(fresh) is None
    assert store.revoke(fresh) is False

    t3 = store.issue("u3")
    assert store.issue("u3") == t3  # дубль не создаётся
    assert store.revoke_all_for_user("u3") == 1


def _reset(monkeypatch, tmp_path):
    """Свежие синглтоны на изолированном runtime-каталоге."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # письма в тестах не отправляются (AIOS_SMTP_HOST из .env отключаем),
    # ссылки подтверждения — относительные (без домена)
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    monkeypatch.setenv("AIOS_PUBLIC_BASE_URL", "")
    get_settings.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
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

    ar = _reset(monkeypatch, tmp_path)

    # письмо не отправляется по-настоящему: подменяем отправку фиксацией вызовов
    sent = []
    monkeypatch.setattr(
        ar, "send_confirmation_email", lambda to, link: sent.append((to, link)) or "sent"
    )
    client = TestClient(app)

    # --- регистрация ---
    resp = client.post(
        "/api/auth/register",
        json={"login": "user@ex.com", "password": "pass1234", "name": "Пользователь П.", "consent_pdn": True, "consent_policy": True},
    )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True, "email": "user@ex.com"}
    assert len(sent) == 1 and sent[0][0] == "user@ex.com"
    assert "/api/auth/confirm?token=" in sent[0][1]

    # имя опционально
    assert (
        client.post(
            "/api/auth/register", json={"login": "anon@ex.com", "password": "pass1234", "consent_pdn": True, "consent_policy": True}
        ).status_code
        == 201
    )
    assert len(sent) == 2

    # занятый логин → 409; некорректный e-mail → 422; короткое имя → 422
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "user@ex.com", "password": "pass1234", "name": "Дубль Д.", "consent_pdn": True, "consent_policy": True},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "bad", "password": "pass1234", "name": "Плохой Л.", "consent_pdn": True, "consent_policy": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "e@f.io", "password": "pass1234", "name": "И", "consent_pdn": True, "consent_policy": True},
        ).status_code
        == 422
    )

    # без согласий регистрация отклоняется (422): согласия обязательны
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "noconsent@ex.com", "password": "pass1234"},
        ).status_code
        == 422
    )
    assert len(sent) == 2  # письмо за отклонённую попытку не уходит

    # --- вход до подтверждения e-mail запрещён ---
    assert (
        client.post("/api/auth/login", json={"login": "user@ex.com", "password": "wrong"}).status_code
        == 401
    )
    assert (
        client.post("/api/auth/login", json={"login": "user@ex.com", "password": "pass1234"}).status_code
        == 403
    )

    # --- подтверждение по ссылке из письма ---
    confirm_token = sent[0][1].split("token=")[1]
    resp = client.get(f"/api/auth/confirm?token={confirm_token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("index.html?confirmed=1")

    # токен одноразовый: повторный переход — «ссылка недействительна»
    resp = client.get(f"/api/auth/confirm?token={confirm_token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("index.html?confirmed=0")

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
    assert resp.json()["user"]["confirmed"] is True

    # --- /me ---
    me = client.get("/api/auth/me", headers=auth(user_token))
    assert me.status_code == 200 and me.json()["login"] == "user@ex.com"
    assert me.json()["confirmed"] is True
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=auth("f" * 64)).status_code == 401

    # --- resend: подтверждённому и неизвестному письмо не уходит ---
    sent.clear()
    assert (
        client.post(
            "/api/auth/resend", json={"login": "user@ex.com", "password": "pass1234"}
        ).json()["ok"]
        is True
    )
    assert (
        client.post(
            "/api/auth/resend", json={"login": "ghost@ex.com", "password": "pass1234"}
        ).json()["ok"]
        is True
    )
    assert sent == []

    # resend неподтверждённому: с неверным паролем — тихий отказ,
    # с верным — письмо уходит, токен переиспользуется (ссылка из первого письма жива)
    assert (
        client.post(
            "/api/auth/resend", json={"login": "anon@ex.com", "password": "wrong"}
        ).json()["ok"]
        is True
    )
    assert sent == []
    assert (
        client.post(
            "/api/auth/resend", json={"login": "anon@ex.com", "password": "pass1234"}
        ).json()["ok"]
        is True
    )
    assert len(sent) == 1 and sent[0][0] == "anon@ex.com"
    anon_token = sent[0][1].split("token=")[1]
    assert (
        client.post(
            "/api/auth/resend", json={"login": "anon@ex.com", "password": "pass1234"}
        ).json()["ok"]
        is True
    )
    assert sent[1][1].split("token=")[1] == anon_token

    # подтверждение anon по ссылке и вход (имя пустое)
    resp = client.get(f"/api/auth/confirm?token={anon_token}", follow_redirects=False)
    assert resp.headers["location"].endswith("index.html?confirmed=1")
    resp = client.post("/api/auth/login", json={"login": "anon@ex.com", "password": "pass1234"})
    assert resp.status_code == 200 and resp.json()["user"]["name"] == ""

    # --- админ-раздел: обычному пользователю запрещён ---
    assert client.get("/api/admin/users", headers=auth(user_token)).status_code == 403

    # --- вход стартовым администратором ---
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    admin_token = resp.json()["token"]
    admin_id = resp.json()["user"]["id"]

    items = client.get("/api/admin/users", headers=auth(admin_token)).json()["items"]
    assert [u["login"] for u in items] == [ADMIN_LOGIN, "user@ex.com", "anon@ex.com"]
    assert all("password_hash" not in u and "salt" not in u for u in items)

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
    # --- выход (токен пользователя отзывается на сервере) ---
    assert client.post("/api/auth/logout", headers=auth(user_token2)).json()["ok"] is True
    assert client.get("/api/auth/me", headers=auth(user_token2)).status_code == 401

    get_settings.cache_clear()


def test_register_smtp_failure_rolls_back(tmp_path, monkeypatch):
    """Сбой отправки письма → 502, неподтверждённая учётка откатывается."""
    from fastapi.testclient import TestClient

    from api.main import app
    from api.runtime.mailer import MailError

    ar = _reset(monkeypatch, tmp_path)

    def broken_mailer(to, link):
        raise MailError("SMTPTransactionFailed: соединение отклонено")

    monkeypatch.setattr(ar, "send_confirmation_email", broken_mailer)
    client = TestClient(app)

    resp = client.post(
        "/api/auth/register",
        json={"login": "user@ex.com", "password": "pass1234", "name": "Тестов Т.", "consent_pdn": True, "consent_policy": True},
    )
    assert resp.status_code == 502
    assert "подтверждения" in resp.json()["detail"]

    # учётки нет: вход невозможен (401 «неверный логин/пароль», а не 403)
    assert (
        client.post("/api/auth/login", json={"login": "user@ex.com", "password": "pass1234"}).status_code
        == 401
    )

    get_settings.cache_clear()


def test_register_dev_fallback(tmp_path, monkeypatch, caplog):
    """Без настроенного SMTP регистрация работает — ссылка печатается в лог."""
    from fastapi.testclient import TestClient

    from api.main import app

    _reset(monkeypatch, tmp_path)  # AIOS_SMTP_HOST пуст → dev-фолбэк
    client = TestClient(app)

    with caplog.at_level(logging.WARNING, logger="aios.mailer"):
        resp = client.post(
            "/api/auth/register",
            json={"login": "user@ex.com", "password": "pass1234", "name": "Тестов Т.", "consent_pdn": True, "consent_policy": True},
        )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True, "email": "user@ex.com"}
    assert "подтверждение e-mail" in caplog.text
    assert "/api/auth/confirm?token=" in caplog.text

    # вход всё ещё закрыт до подтверждения по напечатанной ссылке
    assert (
        client.post("/api/auth/login", json={"login": "user@ex.com", "password": "pass1234"}).status_code
        == 403
    )

    get_settings.cache_clear()

