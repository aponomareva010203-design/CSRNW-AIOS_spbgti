"""Смоук-тесты страницы «Мои проекты» и инструментов карточек проектов.

Хранилище (ProjectStore): upsert по project_id, статусная модель
draft → active (только после явного подтверждения), изоляция
пользователей. API: GET — только собственные карточки по Bearer-токену;
PATCH — правка руководителем проекта (владельцем токена); DELETE —
только роль «Администратор» (любая карточка по id). Инструменты
get_projects / save_project_card: анонимная сессия получает ok:false;
авторизованная — сохраняет и читает карточки.
"""

import asyncio

import pytest

from api.runtime import projects
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"

CARD = {
    "title": "Синтез полимерных мембран",
    "description": "Изучение селективности мембран при вариации давления.",
    "goal": "Повысить выход готового продукта с 85% до 92% к 30 ноября 2026 года.",
    "team_size": "3–5",
    "participants": "Иванов И.И., Петров П.П.",
    "departments": "Лаборатория полимерных мембран",
    "budget": "грант РНФ, до 500 тыс. руб.",
    "groundwork": "черновик обзора литературы, 12 страниц",
    "groundwork_links": ["https://example.tld/paper"],
    "presentation": True,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime-каталог, свежие кэши хранилищ."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _admin_id():
    users = UserStore(get_settings().runtime_dir / "users")
    return users.get_by_login(ADMIN_LOGIN)["id"]


def test_store_upsert_and_status_flow(tmp_path):
    store = projects.ProjectStore(tmp_path / "projects")
    saved = store.save("abc123def456", dict(CARD))
    assert saved["id"].startswith("prj-")
    assert saved["status"] == projects.STATUS_DRAFT

    # upsert тем же project_id не создаёт дубль и не теряет поля
    again = store.save(
        "abc123def456",
        {"project_id": saved["id"], "goal": "Снизить простои на 15% к 31.12.2026"},
    )
    assert again["id"] == saved["id"]
    assert again["goal"] == "Снизить простои на 15% к 31.12.2026"
    assert again["title"] == CARD["title"]
    assert again["groundwork_links"] == CARD["groundwork_links"]
    assert len(store.list("abc123def456")) == 1

    # новая карточка не может появиться сразу активной — нужно подтверждение
    with pytest.raises(projects.ProjectError):
        store.save("abc123def456", dict(CARD, status="active"))

    # явное подтверждение переводит карточку в active
    done = store.save("abc123def456", {"project_id": saved["id"], "status": "active"})
    assert done["status"] == projects.STATUS_ACTIVE

    # чужому пользователю карточка не видна; неизвестный id — ошибка
    assert store.get("fff000fff000", saved["id"]) is None
    with pytest.raises(projects.ProjectError):
        store.save("abc123def456", {"project_id": "prj-deadbeef", "title": "x"})


def test_store_validation_and_isolation(tmp_path):
    store = projects.ProjectStore(tmp_path / "projects")
    with pytest.raises(projects.ProjectError):
        store.save("abc123def456", {"groundwork_links": 42})  # не список и не строка
    with pytest.raises(projects.ProjectError):
        store.save("abc123def456", {"status": "archive"})
    with pytest.raises(projects.ProjectError):
        store.save("bad-id!", {"title": "x"})

    first = store.save("abc123def456", {"title": "Проект А"})
    second = store.save("fff000fff000", {"title": "Проект Б"})
    assert first["id"] != second["id"]
    assert [c["id"] for c in store.list("abc123def456")] == [first["id"]]

    # лимиты полей: строка обрезается молча, а не ломает сохранение
    long_title = store.save("abc123def456", {"title": "ж" * 500})
    assert len(long_title["title"]) == projects.TITLE_MAX_CHARS

    # списковые поля: многострочная строка → список без пустых элементов
    card = store.save("abc123def456", {"groundwork_files": "a.pdf\n\nb.pdf\n"})
    assert card["groundwork_files"] == ["a.pdf", "b.pdf"]


def test_api_projects_requires_auth_and_scopes():
    client = _client()
    assert client.get("/api/projects").status_code == 401

    token = _login_admin(client)
    assert client.get("/api/projects", headers=auth(token)).json() == {"items": []}

    # карточку сохраняет инструмент из чата — в тесте пишем напрямую в хранилище
    uid = _admin_id()
    saved = projects.get_store().save(uid, dict(CARD))

    items = client.get("/api/projects", headers=auth(token)).json()["items"]
    assert len(items) == 1 and items[0]["title"] == CARD["title"]
    assert "user_id" not in items[0]  # служебное поле наружу не отдаётся

    one = client.get(f"/api/projects/{saved['id']}", headers=auth(token))
    assert one.status_code == 200 and one.json()["id"] == saved["id"]
    assert client.get("/api/projects/prj-deadbeef", headers=auth(token)).status_code == 404


def test_tools_projects_flow():
    registry = build_default_registry(get_settings().tools_root)
    assert "get_projects" in registry.names()
    assert "save_project_card" in registry.names()

    # анонимная сессия — мягкий отказ, цикл не падает
    anon = asyncio.run(registry.call("get_projects", {}, ctx={"session": {}}))
    assert anon["ok"] is False and "войти" in anon["error"]

    session = {"user_id": "abc123def456"}
    saved = asyncio.run(
        registry.call("save_project_card", {"card": dict(CARD)}, ctx={"session": session})
    )
    assert saved["ok"] is True and saved["project_id"].startswith("prj-")
    assert saved["status"] == "draft"

    upd = asyncio.run(
        registry.call(
            "save_project_card",
            {
                "card": {
                    "project_id": saved["project_id"],
                    "goal": "Снизить простои на 15% к 31.12.2026",
                }
            },
            ctx={"session": session},
        )
    )
    assert upd["ok"] is True and upd["card"]["goal"].startswith("Снизить простои")

    listed = asyncio.run(registry.call("get_projects", {}, ctx={"session": session}))
    assert listed["ok"] is True and listed["count"] == 1

    # невалидная карточка — ok:false, а не исключение в цикле
    bad = asyncio.run(
        registry.call("save_project_card", {"card": {"status": "мусор"}}, ctx={"session": session})
    )
    assert bad["ok"] is False


def test_api_patch_owner_and_delete_admin():
    """PATCH — только руководитель проекта; DELETE — только администратор."""
    client = _client()
    token = _login_admin(client)
    uid = _admin_id()
    saved = projects.get_store().save(uid, dict(CARD))
    url = f"/api/projects/{saved['id']}"

    # правка без токена — 401
    assert client.patch(url, json={"goal": "x"}).status_code == 401

    # руководитель проекта (владелец токена) правит карточку частичным patch
    resp = client.patch(
        url,
        headers=auth(token),
        json={"goal": "Снизить простои на 15% к 31.12.2026", "groundwork_links": ["https://ex.tld/a", ""]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["goal"].startswith("Снизить простои")
    assert body["title"] == CARD["title"]  # непереданные поля целы
    assert body["groundwork_links"] == ["https://ex.tld/a"]  # пустые отфильтрованы

    # второй пользователь: чужую карточку не правит (404) и не удаляет (403)
    users = UserStore(get_settings().runtime_dir / "users")
    users.create("petrov@csr-nw.ru", "pass1234", name="Петров П.", confirmed=True)
    other_token = client.post(
        "/api/auth/login", json={"login": "petrov@csr-nw.ru", "password": "pass1234"}
    ).json()["token"]
    assert client.patch(url, headers=auth(other_token), json={"title": "взлом"}).status_code == 404
    assert client.delete(url, headers=auth(other_token)).status_code == 403

    # администратор удаляет карточку по id (у любого владельца)
    resp = client.delete(url, headers=auth(token))
    assert resp.status_code == 200 and resp.json() == {"ok": True, "deleted": saved["id"]}
    assert client.get(url, headers=auth(token)).status_code == 404
    # повторное удаление и неизвестный id — 404
    assert client.delete(url, headers=auth(token)).status_code == 404
    assert client.delete("/api/projects/prj-deadbeef", headers=auth(token)).status_code == 404
