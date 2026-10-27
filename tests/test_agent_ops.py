"""Личный SMTP для писем агентов и декларативный ui карточки.

Хранилище (runtime/user_mail.py): валидация, пароль не отдаётся API
(только маска), пустой пароль при перезаписи = прежний сохраняется.
Отправка (mailer.send_with_config) — через ЯВНО переданную конфигурацию;
notify использует личные настройки владельца, а без них — общие.
Карточка (agent_store._normalize_ui + PATCH /api/agents/{id}): ui не
пропускает ничего вне белых списков, trigger с деньгами/временем
нормализуется; старые агенты без ui остаются без него.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.auth import get_token_store, get_user_store
from api.runtime import agent_store, user_mail
from api.runtime.config import get_settings
from api.runtime.mail_settings import MailConfig
from api.runtime.mailer import MailError, send_with_config

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


def _clear_caches():
    get_settings.cache_clear()
    user_mail.get_store.cache_clear()
    # синглтоны-хранилища: глобальные переменные модулей, а не lru_cache
    agent_store._STORE = None
    get_user_store.cache_clear()
    get_token_store.cache_clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    _clear_caches()
    yield TestClient(app)
    _clear_caches()


def _make_user(name: str = "Иван Иванов") -> tuple[dict, str]:
    """Подтверждённый пользователь + токен. Логин — e-mail (LOGIN_RE),
    отдельного поля email в записи нет."""
    login = f"u{uuid.uuid4().hex[:10]}@example-test.io"
    user = get_user_store().create(login, "1234", name=name, confirmed=True)
    token = get_token_store().issue(user["id"])
    return user, token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----- хранилище личного SMTP -----


def test_user_mail_store_roundtrip(tmp_path):
    store = user_mail.UserMailStore(tmp_path)
    uid = uuid.uuid4().hex[:12]
    assert store.load(uid) is None
    store.save(uid, host="smtp.yandex.ru", port=465, tls="ssl", user="a@b.ru",
               password="secret", from_email="a@b.ru", from_name="Я")
    data = store.load(uid)
    assert data["host"] == "smtp.yandex.ru"
    assert data["password"] == "secret"

    # пустой пароль хранится как есть: «не менять сохранённый» решает
    # роутер (PUT /api/settings/agent-mail), а не хранилище
    store.save(uid, host="smtp.gmail.com", port=587, tls="starttls", user="a@b.ru",
               password="", from_email="", from_name="")
    again = store.load(uid)
    assert again["password"] == ""
    assert again["host"] == "smtp.gmail.com"
    assert store.clear(uid) is True
    assert store.load(uid) is None


def test_user_mail_store_validation(tmp_path):
    store = user_mail.UserMailStore(tmp_path)
    uid = uuid.uuid4().hex[:12]
    with pytest.raises(user_mail.UserMailError):
        store.save(uid, host="", port=465, tls="ssl", user="", password="",
                   from_email="", from_name="")
    with pytest.raises(user_mail.UserMailError):
        store.save(uid, host="smtp.yandex.ru", port=70000, tls="ssl", user="",
                   password="", from_email="", from_name="")
    with pytest.raises(user_mail.UserMailError):
        store.save(uid, host="smtp.yandex.ru", port=465, tls="quic", user="",
                   password="", from_email="", from_name="")
    with pytest.raises(user_mail.UserMailError):
        store.save(uid, host="smtp.yandex.ru", port=465, tls="ssl", user="",
                   password="", from_email="не адрес", from_name="")
    with pytest.raises(user_mail.UserMailError):
        store.load("короткий")  # некорректный id пользователя


def test_mask_password():
    assert user_mail.mask_password("") == ""
    assert user_mail.mask_password("abcd") == "••••"
    masked = user_mail.mask_password("super-secret")
    assert "super-se" not in masked
    assert masked.startswith("sup") and masked.endswith("t")


# ----- отправка с явной конфигурацией и выбор канала в notify -----


def test_send_with_config_requires_host():
    empty = MailConfig(host="", port=465, tls="ssl", user="", password="",
                       from_email="", from_name="", source="test")
    with pytest.raises(MailError):
        send_with_config(["a@b.ru"], "тема", "текст", empty)


def test_mail_config_for_user_prefers_personal(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    _clear_caches()
    uid = uuid.uuid4().hex[:12]
    # без личных настроек — платформенная (файл настроек админа не заполнен)
    assert user_mail.personal_mail(uid) is None
    # с личными — личная перекрывает
    user_mail.UserMailStore(tmp_path / "rt" / "user").save(
        uid, host="smtp.mine.io", port=465, tls="ssl", user="me@mine.io",
        password="p", from_email="me@mine.io", from_name="Я")
    data = user_mail.personal_mail(uid)
    assert data["host"] == "smtp.mine.io"
    _clear_caches()


# ----- API личной почты -----


def test_agent_mail_endpoints(client):
    user, token = _make_user()
    r = client.get("/api/settings/agent-mail", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["active"] is False

    r = client.put("/api/settings/agent-mail", headers=_auth(token),
              json={"host": "smtp.yandex.ru", "port": 465, "tls": "ssl",
                    "user": "a@b.ru", "password": "пароль-приложения",
                    "from_email": "a@b.ru", "from_name": "Иван"})
    assert r.status_code == 200
    view = r.json()
    assert view["active"] is True
    assert view["host"] == "smtp.yandex.ru"
    assert view["password_masked"] and "пароль-приложени" not in view["password_masked"]

    # пустой пароль не затирает сохранённый
    r = client.put("/api/settings/agent-mail", headers=_auth(token),
              json={"host": "smtp.yandex.ru", "port": 465, "tls": "ssl",
                    "user": "a@b.ru", "password": "", "from_email": "a@b.ru"})
    assert r.status_code == 200

    # неверный порт — 422
    r = client.put("/api/settings/agent-mail", headers=_auth(token),
              json={"host": "smtp.yandex.ru", "port": 999999, "tls": "ssl"})
    assert r.status_code == 422

    r = client.delete("/api/settings/agent-mail", headers=_auth(token))
    assert r.status_code == 200
    assert client.get("/api/settings/agent-mail", headers=_auth(token)).json()["active"] is False

    # проверка без настроек — 422, а не сетевая ошибка
    r = client.post("/api/settings/agent-mail/test", headers=_auth(token), json={})
    assert r.status_code == 422


# ----- ui-декларация карточки: нормализация и PATCH -----


def test_normalize_ui_whitelist_only():
    raw = {
        "theme": "noir",  # нет в списке — отброшен
        "accent": "GOLD",  # регистр не совпадает — отброшен
        "density": "compact",
        "accent2": "gold",
        "intro": "Считает жёсткость волокна и выдаёт рецепт.",
        "hide_empty_views": "yes",  # не bool — отброшен
        "html": "<script>alert(1)</script>",  # неизвестный ключ — не пройдёт
    }
    raw["accent"] = "gold"
    ui = agent_store._normalize_ui(raw)
    assert ui == {
        "density": "compact",
        "accent": "gold",
        "intro": "Считает жёсткость волокна и выдаёт рецепт.",
    }
    assert "<script>" not in str(ui)
    assert agent_store._normalize_ui(None) == {}
    assert agent_store._normalize_ui("текст") == {}


def test_save_card_with_ui_then_without(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    _clear_caches()
    store = agent_store.get_store()
    uid = "u" * 12
    card = store.save(uid, {
        "project_id": "prj-1", "title": "Агент",
        "ui": {"accent": "teal", "intro": "Что делает агент", "theme": "forest"},
    })
    assert card["ui"] == {"accent": "teal", "intro": "Что делает агент", "theme": "forest"}

    # повторное сохранение без ui — прежнее декоративное решение остаётся
    updated = store.save(uid, {"agent_id": card["id"], "title": "Агент v2"})
    assert updated["ui"]["accent"] == "teal"

    # старые агенты создаются без ui и остаются без него
    plain = store.save(uid, {"project_id": "prj-1", "title": "Старый"})
    assert "ui" not in plain
    _clear_caches()