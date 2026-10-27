"""Обратная связь: отправка предложений + админ-эндпоинты списка/отметки выполнения.

 proposals.json пишется во временный каталог (monkeypatch _FEEDBACK_DIR),
боевой feedback/proposals.json тестами не трогается.
"""

import pytest

from api.runtime.config import get_settings

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Чистое окружение: свежий runtime-каталог и отдельный каталог feedback."""
    from api.routers import feedback as feedback_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # письма в тестах не отправляются (AIOS_SMTP_HOST из .env отключаем)
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    monkeypatch.setenv("AIOS_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(feedback_router, "_FEEDBACK_DIR", tmp_path / "feedback")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _reset_clients():
    """Сброс lru_cache хранилищ (тесты создают свежие каталоги)."""
    from api.routers import auth as auth_router

    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    _reset_clients()
    return TestClient(app)


def _register_confirmed(client, login="user@ex.com", password="pass1234"):
    """Зарегистрированная и подтверждённая учётка, возвращает токен."""
    from api.routers import auth as auth_router

    client.post(
        "/api/auth/register",
        json={
            "login": login,
            "password": password,
            "consent_pdn": True,
            "consent_policy": True,
        },
    )
    auth_router.get_user_store().confirm(
        auth_router.get_user_store().get_by_login(login)
    )
    return client.post("/api/auth/login", json={"login": login, "password": password}).json()["token"]


def test_suggestions_submit_and_admin_flow():
    client = _client()

    # гость отправляет предложение
    assert client.post(
        "/api/feedback/suggestions", json={"message": "Первое предложение"}
    ).status_code == 200

    # авторизованный пользователь: логин подставляется из сессии
    utoken = _register_confirmed(client)
    assert client.post(
        "/api/feedback/suggestions",
        json={"message": "Второе предложение", "login": "подделка"},
        headers=_auth(utoken),
    ).status_code == 200

    # список закрыт для гостей и пользователей, открыт админу
    assert client.get("/api/feedback/suggestions").status_code == 401
    assert client.get("/api/feedback/suggestions", headers=_auth(utoken)).status_code == 403

    atoken = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    data = client.get("/api/feedback/suggestions", headers=_auth(atoken)).json()
    items = data["suggestions"]
    assert len(items) == 2
    assert items[0]["done"] is False and items[0]["done_at"] is None
    assert items[0]["login"] is None
    assert items[1]["login"] == "user@ex.com"  # из сессии, не из тела запроса

    # отметка выполнения: запись остаётся в списке, меняется только флаг
    sid = items[0]["id"]
    patched = client.patch(
        f"/api/feedback/suggestions/{sid}", json={"done": True}, headers=_auth(atoken)
    )
    assert patched.status_code == 200
    assert patched.json()["done"] is True and patched.json()["done_at"] is not None

    items = client.get("/api/feedback/suggestions", headers=_auth(atoken)).json()["suggestions"]
    assert len(items) == 2  # выполненное не удаляется
    assert [i["done"] for i in items] == [True, False]

    # возврат в работу
    patched = client.patch(
        f"/api/feedback/suggestions/{sid}", json={"done": False}, headers=_auth(atoken)
    )
    assert patched.json()["done"] is False and patched.json()["done_at"] is None

    # чужой id → 404
    assert (
        client.patch(
            "/api/feedback/suggestions/unknown-id", json={"done": True}, headers=_auth(atoken)
        ).status_code
        == 404
    )
    # PATCH недоступен обычному пользователю
    assert (
        client.patch(
            f"/api/feedback/suggestions/{sid}", json={"done": True}, headers=_auth(utoken)
        ).status_code
        == 403
    )


def test_suggestions_old_format_records_read_as_open(tmp_path):
    """Записи старого формата (без done/done_at) читаются как невыполненные."""
    import json

    from api.routers import feedback as feedback_router

    feedback_router._FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    old = [
        {
            "id": "abc",
            "created_at": "2026-09-10T10:57:40.569926+00:00",
            "login": None,
            "message": "Старая запись",
        }
    ]
    (feedback_router._FEEDBACK_DIR / "proposals.json").write_text(
        json.dumps(old, ensure_ascii=False), encoding="utf-8"
    )

    client = _client()
    atoken = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    items = client.get("/api/feedback/suggestions", headers=_auth(atoken)).json()["suggestions"]
    assert items == [
        {
            "id": "abc",
            "created_at": "2026-09-10T10:57:40.569926+00:00",
            "login": None,
            "message": "Старая запись",
        }
    ]

    # старую запись можно пометить выполненной — формат дозаписывается
    patched = client.patch(
        "/api/feedback/suggestions/abc", json={"done": True}, headers=_auth(atoken)
    )
    assert patched.status_code == 200 and patched.json()["done"] is True


def test_suggestions_mine_statuses():
    """«Статусы предложений» в модалке: только предложения этого аккаунта,
    свежие сверху; done=True (отметка в «Предложениях пользователей»)
    превращает статус «в работе» в «выполнено»."""
    mine_token = _register_confirmed(client=_client(), login="mine@ex.com")
    other_token = _register_confirmed(client=_client(), login="other@ex.com")
    atoken = _client().post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]
    mine_headers = _auth(mine_token)

    # без токена — 401; у новых аккаунтов предложений нет
    assert _client().get("/api/feedback/suggestions/mine").status_code == 401
    items = _client().get("/api/feedback/suggestions/mine", headers=mine_headers).json()["items"]
    assert items == []

    # два предложения от моего аккаунта и одно от чужого
    client = _client()
    client.post("/api/feedback/suggestions", json={"message": "Моё первое"}, headers=mine_headers)
    client.post("/api/feedback/suggestions", json={"message": "Чужое"}, headers=_auth(other_token))
    client.post("/api/feedback/suggestions", json={"message": "Моё второе"}, headers=mine_headers)

    resp = client.get("/api/feedback/suggestions/mine", headers=mine_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [r["message"] for r in items] == ["Моё второе", "Моё первое"]  # свежие сверху
    assert all(r["done"] is False and r["done_at"] is None for r in items)

    # анонимные предложения не привязаны к аккаунтам — чужие не видны
    assert [r["message"] for r in client.get(
        "/api/feedback/suggestions/mine", headers=_auth(other_token)
    ).json()["items"]] == ["Чужое"]

    # админ отмечает первое выполненным → у автора статус меняется
    target = items[0]["id"]
    assert client.patch(
        f"/api/feedback/suggestions/{target}", json={"done": True}, headers=_auth(atoken)
    ).status_code == 200
    mine = client.get("/api/feedback/suggestions/mine", headers=mine_headers).json()["items"]
    by_id = {r["id"]: r for r in mine}
    assert by_id[target]["done"] is True and by_id[target]["done_at"]
    assert by_id[[r for r in mine if r["id"] != target][0]["id"]]["done"] is False
