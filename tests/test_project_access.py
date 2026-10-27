"""Смоук-тесты уровней доступа участников проекта (названия — как на GitHub).

Хранилище: clean_acl (валидация блока acl карточки), access_level —
приоритеты «руководитель > явный уровень из acl > автопросмотр по ФИО
из participants», set/remove_member_level, artifact_level (уровень на
проекте, к которому привязан артефакт). API: GET/PUT/DELETE
/api/projects/{id}/members — список участников с уровнями, выдача/смена/
снятие уровня (руководитель, участник с «Полным доступом», администратор
роли; только пользователям из состава команды). Доступ участника к
карточке, черновикам и файлам по уровню: Просмотр — чтение,
Редактирование — правка карточки и гипотез, Полный доступ — управление
участниками. Онтологии и RAG-базы, привязанные к проекту: Просмотр+ —
чтение, Редактирование+ — правка, Полный доступ — публикация.
"""

import pytest

from api.runtime import projects
from api.runtime.config import get_settings
from api.runtime.projects import ProjectStore, clean_acl
from api.runtime.users import UserStore

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"

OWNER_LOGIN = "lead@ex.com"
OWNER_NAME = "Руководитель Руковод Руководович"
PART_LOGIN = "member@ex.com"
PART_NAME = "Участников Участник Участникович"
PART2_LOGIN = "writer@ex.com"
PART2_NAME = "Писателей Писатель Писателейич"
OUT_LOGIN = "out@ex.com"
OUT_NAME = "Сторонний Чужой Чужович"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime/данные, свежие кэши хранилищ."""
    from api.routers import auth as auth_router
    from api.runtime import cabinet as cabinet_runtime
    from api.runtime import ontologies as ontologies_runtime
    from api.runtime import projects as projects_runtime
    from api.runtime import rag as rag_runtime
    from api.runtime import team as team_runtime

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # без SMTP регистрация идёт в dev-режиме: без MX-проверки домена
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    monkeypatch.setenv("AIOS_ONTOLOGY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOS_RAG_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    projects_runtime.get_store.cache_clear()
    ontologies_runtime.get_store.cache_clear()
    rag_runtime.get_store.cache_clear()
    cabinet_runtime.get_store.cache_clear()
    team_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    projects_runtime.get_store.cache_clear()
    ontologies_runtime.get_store.cache_clear()
    rag_runtime.get_store.cache_clear()
    cabinet_runtime.get_store.cache_clear()
    team_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _register_user(client, login, name):
    """Регистрация с подтверждением e-mail (без SMTP); возвращает токен."""
    from api.routers import auth as auth_router

    resp = client.post(
        "/api/auth/register",
        json={
            "login": login,
            "password": "pass1234",
            "name": name,
            "consent_pdn": True,
            "consent_policy": True,
        },
    )
    assert resp.status_code == 201, resp.text
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login(login)["id"]
    token = auth_router.get_confirmation_store().issue(user_id)
    resp = client.get(f"/api/auth/confirm?token={token}", follow_redirects=True)
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/auth/login", json={"login": login, "password": "pass1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _mk_project(store: ProjectStore, user_id: str, **fields) -> dict:
    saved = store.save(user_id, dict(fields))
    return store.save(user_id, {"project_id": saved["id"], "status": "active"})


def _team(*extra_participants):
    """Руководитель + два участника в составе команды + посторонний.

    Возвращает (client, owner_token, part_token, part2_token, out_token,
    ids, card). ids — словарь owner/part/part2/out."""
    client = _client()
    owner_token = _register_user(client, OWNER_LOGIN, OWNER_NAME)
    part_token = _register_user(client, PART_LOGIN, PART_NAME)
    part2_token = _register_user(client, PART2_LOGIN, PART2_NAME)
    out_token = _register_user(client, OUT_LOGIN, OUT_NAME)
    users = UserStore(get_settings().runtime_dir / "users")
    ids = {
        "owner": users.get_by_login(OWNER_LOGIN)["id"],
        "part": users.get_by_login(PART_LOGIN)["id"],
        "part2": users.get_by_login(PART2_LOGIN)["id"],
        "out": users.get_by_login(OUT_LOGIN)["id"],
    }
    store = ProjectStore(get_settings().runtime_dir / "projects")
    participants = ", ".join([PART_NAME, PART2_NAME] + list(extra_participants))
    card = _mk_project(store, ids["owner"], title="Общий проект", participants=participants)
    return client, owner_token, part_token, part2_token, out_token, ids, card


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ----- хранилище: clean_acl / access_level / управление участниками -----


def test_clean_acl_validation():
    assert clean_acl(None) == {}
    assert clean_acl({"abc123def456": "write"}) == {"abc123def456": "write"}
    with pytest.raises(projects.ProjectError):
        clean_acl("нет")
    with pytest.raises(projects.ProjectError):
        clean_acl({"короткий": "read"})
    with pytest.raises(projects.ProjectError):
        clean_acl({"abc123def456": "delete"})  # уровень вне read/write/admin


def test_acl_persists_in_card(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    saved = store.save("abc123def456", {"title": "Проект", "acl": {"abc123def456": "read"}})
    assert saved["acl"] == {"abc123def456": "read"}
    # acl дозаписывается частично: передан пустой блок — поле убирается
    again = store.save("abc123def456", {"project_id": saved["id"], "acl": {}})
    assert "acl" not in again
    with pytest.raises(projects.ProjectError):
        store.save("abc123def456", {"project_id": saved["id"], "acl": {"bad": "read"}})


def test_access_level_precedence(tmp_path):
    from api.runtime import cabinet

    store = ProjectStore(tmp_path / "projects")
    owner_id, out_id = "aaa111aaa111", "ccc333ccc333"
    card = _mk_project(store, owner_id, title="Проект", participants=PART_NAME)

    users = UserStore(tmp_path / "users")
    part_user = users.create(
        login=PART_LOGIN, password="pass1234", name=PART_NAME, confirmed=True
    )
    # анкета кабинета важнее имени учётной записи (participant_needles)
    cabinet.CabinetStore(tmp_path / "user").save_profile(
        part_user["id"],
        last_name="Участников",
        first_name="Участник",
        patronymic="Участникович",
    )

    # руководитель — owner; посторонний — доступа нет
    assert projects.access_level(card, {"id": owner_id}) == projects.LEVEL_OWNER
    assert projects.access_level(card, {"id": out_id, "name": OUT_NAME}) is None

    # зарегистрированный участник по ФИО получает автопросмотр
    assert projects.access_level(card, part_user) == projects.LEVEL_READ

    # явный уровень из acl перекрывает автопросмотр
    saved = store.save(
        owner_id, {"project_id": card["id"], "acl": {part_user["id"]: "write"}}
    )
    assert projects.access_level(saved, {"id": part_user["id"]}) == projects.LEVEL_WRITE
    # иерархия уровней: read < write < admin, owner покрывает всё
    assert projects.level_allows("read", "read")
    assert not projects.level_allows("read", "write")
    assert projects.level_allows("write", "read")
    assert projects.level_allows("owner", "admin")


def test_set_and_remove_member_level():
    store = ProjectStore(get_settings().runtime_dir / "projects")
    card = _mk_project(store, "aaa111aaa111", title="Проект")
    saved = projects.set_member_level(card["id"], "bbb222bbb222", projects.LEVEL_ADMIN)
    assert saved["acl"] == {"bbb222bbb222": projects.LEVEL_ADMIN}
    saved = projects.set_member_level(card["id"], "bbb222bbb222", projects.LEVEL_WRITE)
    assert saved["acl"] == {"bbb222bbb222": projects.LEVEL_WRITE}
    with pytest.raises(projects.ProjectError):
        projects.set_member_level(card["id"], "bbb222bbb222", "delete")
    with pytest.raises(projects.ProjectError):
        projects.set_member_level("prj-deadbeef", "bbb222bbb222", "read")
    removed = projects.remove_member_level(card["id"], "bbb222bbb222")
    assert "acl" not in removed


def test_artifact_level_follows_project():
    store = ProjectStore(get_settings().runtime_dir / "projects")
    card = _mk_project(store, "aaa111aaa111", title="Проект")
    projects.set_member_level(card["id"], "bbb222bbb222", projects.LEVEL_WRITE)
    assert projects.artifact_level(card["id"], {"id": "bbb222bbb222"}) == projects.LEVEL_WRITE
    assert projects.artifact_level(card["id"], {"id": "ccc333ccc333"}) is None
    assert projects.artifact_level("", {"id": "bbb222bbb222"}) is None
    assert projects.artifact_level("prj-deadbeef", {"id": "bbb222bbb222"}) is None


# ----- API: участники и права -----


def test_members_requires_auth():
    client = _client()
    assert client.get("/api/projects/prj-07622a46d6/members").status_code == 401
    assert (
        client.put(
            "/api/projects/prj-07622a46d6/members/000000000000", json={"level": "read"}
        ).status_code
        == 401
    )


def test_members_listing_matches_and_unregistered():
    client, owner_token, part_token, part2_token, out_token, ids, card = _team(
        "Безымянный Без Учетки"
    )
    resp = client.get(f"/api/projects/{card['id']}/members", headers=auth(owner_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    by_user = {r["user_id"]: r for r in data["items"]}
    assert by_user[ids["owner"]]["level"] == "owner"
    assert by_user[ids["part"]]["level"] == "read"
    assert by_user[ids["part"]]["source"] == "matched"
    assert ids["part2"] in by_user
    assert data["unregistered"] == ["Безымянный Без Учетки"]
    # посторонний в списке участников отсутствует
    assert ids["out"] not in by_user
    # посторонний без доступа получает 404 (как и для несуществующего проекта)
    assert (
        client.get(f"/api/projects/{card['id']}/members", headers=auth(out_token)).status_code
        == 404
    )


def test_member_level_lifecycle():
    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    base = f"/api/projects/{pid}/members/{ids['part']}"

    # некорректный уровень → 422; себе → 400; не в составе команды → 404
    assert client.put(base, json={"level": "delete"}, headers=auth(owner_token)).status_code == 422
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['owner']}",
            json={"level": "read"},
            headers=auth(owner_token),
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['out']}",
            json={"level": "read"},
            headers=auth(owner_token),
        ).status_code
        == 404
    )
    # участник без прав управления не меняет уровни (404 — неотличимо)
    assert client.put(base, json={"level": "admin"}, headers=auth(part_token)).status_code == 404

    # руководитель выдаёт «Редактирование»; уровень сохраняется в карточке
    resp = client.put(base, json={"level": "write"}, headers=auth(owner_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["level"] == "write"
    saved = ProjectStore(get_settings().runtime_dir / "projects").find(pid)
    assert saved["acl"] == {ids["part"]: "write"}

    # администратор роли тоже может менять уровни
    seed_admin = _login_admin(client)
    assert (
        client.put(base, json={"level": "admin"}, headers=auth(seed_admin)).status_code == 200
    )

    # снятие уровня: остаётся автопросмотр (человек в составе команды)
    resp = client.delete(base, headers=auth(owner_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["level"] == "read"
    # повторное снятие (явного уровня нет, но в команде есть) — тоже ок
    assert client.delete(base, headers=auth(owner_token)).status_code == 200


def test_admin_level_member_manages_team():
    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['part']}",
            json={"level": "admin"},
            headers=auth(owner_token),
        ).status_code
        == 200
    )

    # «Полный доступ» позволяет участнику выдавать и снимать уровни
    resp = client.put(
        f"/api/projects/{pid}/members/{ids['part2']}",
        json={"level": "write"},
        headers=auth(part_token),
    )
    assert resp.status_code == 200, resp.text
    # ...но не пользователю вне состава команды
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['out']}",
            json={"level": "read"},
            headers=auth(part_token),
        ).status_code
        == 404
    )
    # себе уровень сменить нельзя
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['part']}",
            json={"level": "read"},
            headers=auth(part_token),
        ).status_code
        == 400
    )

    # снятие уровня у себя: остаётся автопросмотр, правка карточки недоступна
    resp = client.delete(
        f"/api/projects/{pid}/members/{ids['part']}", headers=auth(part_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["level"] == "read"
    assert (
        client.patch(
            f"/api/projects/{pid}", json={"goal": "цель"}, headers=auth(part_token)
        ).status_code
        == 404
    )


def test_member_read_only_access():
    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    # просмотр: карточка видна с уровнем, правка — нет
    resp = client.get(f"/api/projects/{pid}", headers=auth(part_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["my_level"] == "read"
    assert (
        client.patch(
            f"/api/projects/{pid}", json={"goal": "новая цель"}, headers=auth(part_token)
        ).status_code
        == 404
    )
    # посторонний карточку по id не получает
    assert (
        client.get(f"/api/projects/{pid}", headers=auth(out_token)).status_code == 404
    )

    # черновик чужого проекта виден участнику (доступ к черновикам проекта)
    store = ProjectStore(get_settings().runtime_dir / "projects")
    draft = store.save(ids["owner"], {"title": "Черновик", "participants": PART_NAME})
    items = client.get("/api/projects", headers=auth(part_token)).json()["items"]
    mine = {c["id"]: c for c in items}
    assert draft["id"] in mine
    assert mine[draft["id"]]["my_level"] == "read"
    assert (
        client.get(f"/api/projects/{draft['id']}", headers=auth(part_token)).status_code == 200
    )
    # чужой черновик постороннему не виден
    items = client.get("/api/projects", headers=auth(out_token)).json()["items"]
    assert draft["id"] not in {c["id"] for c in items}


def test_member_write_edits_card_but_not_acl():
    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    assert (
        client.put(
            f"/api/projects/{pid}/members/{ids['part']}",
            json={"level": "write"},
            headers=auth(owner_token),
        ).status_code
        == 200
    )

    # правка карточки по уровню «Редактирование»
    resp = client.patch(f"/api/projects/{pid}", json={"goal": "новая цель"}, headers=auth(part_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal"] == "новая цель"

    # эскалация закрыта: acl из PATCH игнорируется, уровень остаётся write
    resp = client.patch(
        f"/api/projects/{pid}",
        json={"acl": {ids["part"]: "admin"}},
        headers=auth(part_token),
    )
    assert resp.status_code == 200, resp.text
    store = ProjectStore(get_settings().runtime_dir / "projects")
    assert store.find(pid)["acl"] == {ids["part"]: "write"}

    # статус и флаг тестовой карточки участнику недоступны
    client.patch(f"/api/projects/{pid}", json={"admin_only": True}, headers=auth(part_token))
    assert not store.find(pid).get("admin_only")
    client.patch(f"/api/projects/{pid}", json={"status": "draft"}, headers=auth(part_token))
    assert store.find(pid)["status"] == "active"

    # участник с автопросмотром (без явного уровня) править не может
    assert (
        client.patch(
            f"/api/projects/{pid}", json={"budget": "х3"}, headers=auth(part2_token)
        ).status_code
        == 404
    )
    # и посторонний — тем более
    assert (
        client.patch(
            f"/api/projects/{pid}", json={"budget": "х3"}, headers=auth(out_token)
        ).status_code
        == 404
    )


# ----- API: общие онтологии и RAG-базы проекта -----


def _ontology_graph():
    """Минимальный полный граф (проходит require_complete при публикации)."""
    return {
        "name": "Онтология общая",
        "domain": "Тесты",
        "competency_questions": [{"id": "cq-1", "question": "Из чего состоит объект?"}],
        "classes": [
            {"id": "cls-a", "name": "Объект", "description": "Корневой класс."},
            {"id": "cls-b", "name": "Часть", "description": "Составная часть.", "parent": None},
        ],
        "relations": [
            {
                "id": "rel-has-part",
                "name": "имеет часть",
                "domain_class": "cls-a",
                "range_class": "cls-b",
                "cardinality": "1..N",
            }
        ],
    }


def test_shared_ontology_access():
    from api.runtime import ontologies as ontologies_runtime

    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    store = ontologies_runtime.get_store()
    onto = store.save(ids["owner"], dict(_ontology_graph(), project_id=pid))

    # автопросмотр: участник видит онтологию проекта (список и карточку)
    resp = client.get(f"/api/ontologies/{onto['id']}", headers=auth(part_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared"] is True
    assert resp.json()["my_level"] == "read"
    items = client.get("/api/ontologies", headers=auth(part_token)).json()["items"]
    assert onto["id"] in {c["id"] for c in items}
    assert client.get(f"/api/ontologies/{onto['id']}", headers=auth(out_token)).status_code == 404

    # правка — только с «Редактированием»
    assert (
        client.patch(
            f"/api/ontologies/{onto['id']}",
            json={"description": "правка"},
            headers=auth(part_token),
        ).status_code
        == 404
    )
    projects.set_member_level(pid, ids["part"], projects.LEVEL_WRITE)
    resp = client.patch(
        f"/api/ontologies/{onto['id']}",
        json={"description": "правка участником"},
        headers=auth(part_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "правка участником"

    # публикация — только с «Полным доступом»
    assert (
        client.post(f"/api/ontologies/{onto['id']}/publish", headers=auth(part_token)).status_code
        == 404
    )
    projects.set_member_level(pid, ids["part"], projects.LEVEL_ADMIN)
    resp = client.post(f"/api/ontologies/{onto['id']}/publish", headers=auth(part_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_shared_rag_access():
    from api.runtime import rag as rag_runtime

    client, owner_token, part_token, part2_token, out_token, ids, card = _team()
    pid = card["id"]
    store = rag_runtime.get_store()
    rag_card = store.save(ids["owner"], {"name": "База проекта", "project_id": pid})

    # автопросмотр: чтение базы, списка и статусов документов
    resp = client.get(f"/api/rag/{rag_card['id']}", headers=auth(part_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared"] is True
    assert resp.json()["my_level"] == "read"
    items = client.get("/api/rag", headers=auth(part_token)).json()["items"]
    assert rag_card["id"] in {c["id"] for c in items}
    resp = client.get(f"/api/rag/{rag_card['id']}/documents", headers=auth(part_token))
    assert resp.status_code == 200, resp.text

    # загрузка документа — правка и выше; на Просмотре отказ (404)
    files = {"file": ("doc.txt", "Текст документа для индексации".encode("utf-8"), "text/plain")}
    assert (
        client.post(
            f"/api/rag/{rag_card['id']}/documents", files=files, headers=auth(part_token)
        ).status_code
        == 404
    )
    projects.set_member_level(pid, ids["part"], projects.LEVEL_WRITE)
    resp = client.patch(
        f"/api/rag/{rag_card['id']}", json={"name": "Обновлённая база"}, headers=auth(part_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Обновлённая база"

    # публикация — только с «Полным доступом»; постороннему отказ (404)
    projects.set_member_level(pid, ids["part"], projects.LEVEL_ADMIN)
    # полный доступ есть, но база без готовых документов — содержательный отказ
    resp = client.post(f"/api/rag/{rag_card['id']}/publish", headers=auth(part_token))
    assert resp.status_code == 422
    assert (
        client.post(f"/api/rag/{rag_card['id']}/publish", headers=auth(out_token)).status_code
        == 404
    )
