"""Смоук-тесты навыка «Конструктор онтологий»: хранилище, публикация, инструменты, API.

Хранилище (OntologyStore): upsert по ontology_id, статусная модель draft →
active (публикация — только после явного подтверждения и полной валидации
графа: ≥2 классов, ≥1 связи, ≥1 компетентностного вопроса), публикация
карточки в каталог data/ontologies.json и графа в objects/. Инструменты
get_ontologies / save_ontology: анонимная сессия получает ok:false,
авторизованная — сохраняет и читает онтологии. API: GET — только
собственные онтологии по Bearer-токену; publish — владелец; DELETE —
только роль «Администратор» (вместе с публикацией).
"""

import asyncio
import json

import pytest

from api.runtime import ontologies
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"
USER_ID = "abc123def456"

ONTO = {
    "name": "Онтология полимерных композитов",
    "domain": "Композиты",
    "description": "Мини-онтология для тестов: компаунд и компоненты.",
    "author": "Тестов Т. Т.",
    "tags": ["композиты", "тест"],
    "competency_questions": [
        {
            "id": "cq-1",
            "question": "Из каких компонентов состоит компаунд?",
            "coverage": ["cls-compound →rel-consists-of→ cls-component"],
        }
    ],
    "classes": [
        {
            "id": "cls-compound",
            "name": "Компаунд",
            "description": "Смесь компонентов для заливки.",
            "parent": None,
            "attributes": [
                {"id": "attr-viscosity", "name": "Вязкость", "type": "float", "unit": "Па·с"}
            ],
            "synonyms": ["смола"],
            "keywords": ["эпоксид"],
        },
        {"id": "cls-component", "name": "Компонент", "description": "Часть компаунда.", "parent": None},
        {"id": "cls-filler", "name": "Наполнитель", "description": "Твёрдая фаза.", "parent": "cls-component"},
    ],
    "relations": [
        {
            "id": "rel-consists-of",
            "name": "состоит из",
            "domain_class": "cls-compound",
            "range_class": "cls-component",
            "cardinality": "1..N",
        }
    ],
    "rag_hints": {
        "extraction_targets": ["cls-compound", "cls-component"],
        "retrieval_paths": ["cls-compound →rel-consists-of→ cls-component"],
    },
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированные runtime/данные, свежие кэши хранилищ."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_ONTOLOGY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    ontologies.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    ontologies.get_store.cache_clear()
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


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


def test_store_draft_upsert_and_merge(tmp_path):
    store = ontologies.OntologyStore(tmp_path / "ontologies", tmp_path / "data")
    saved = store.save(USER_ID, dict(ONTO))
    assert saved["id"].startswith("onto-")
    assert saved["status"] == ontologies.STATUS_DRAFT
    assert saved["version"] == 1
    assert saved["classes_count"] == 3
    assert saved["relations_count"] == 1

    # сохранение без ontology_id дописывает существующий черновик с тем же
    # названием и проектом, а не создаёт дубликат
    merged = store.save(USER_ID, {"name": ONTO["name"]})
    assert merged["id"] == saved["id"]
    assert len(store.list(USER_ID)) == 1

    # другой проект — заведомо новая онтология
    other = store.save(USER_ID, {"name": ONTO["name"], "project_id": "prj-aaaaaaaa"})
    assert other["id"] != saved["id"]
    assert len(store.list(USER_ID)) == 2

    # upsert тем же id не создаёт дубль и не теряет граф
    again = store.save(USER_ID, {"ontology_id": saved["id"], "name": "Новое название"})
    assert again["id"] == saved["id"]
    assert again["name"] == "Новое название"
    assert again["domain"] == ONTO["domain"]
    assert len(again["classes"]) == 3
    assert again["version"] == 3  # merged + again — два сохранения в черновик
    assert len(store.list(USER_ID)) == 2  # черновик + онтология другого проекта

    # новая онтология не может создаться сразу активной
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"name": "Сразу актив", "status": "active"})

    # валидация: битый parent
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": saved["id"], "classes": [
            {"id": "cls-a", "name": "A", "parent": "cls-nope"}
        ]})
    # дубли id классов
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": saved["id"], "classes": [
            {"id": "cls-a", "name": "A"},
            {"id": "cls-a", "name": "A2"},
        ]})
    # связь на неизвестный класс
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": saved["id"],
                             "classes": [{"id": "cls-a", "name": "A"}, {"id": "cls-b", "name": "B"}],
                             "relations": [{"id": "rel-x", "name": "x",
                                            "domain_class": "cls-a", "range_class": "cls-ghost"}]})
    # цикл в иерархии
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": saved["id"], "classes": [
            {"id": "cls-a", "name": "A", "parent": "cls-b"},
            {"id": "cls-b", "name": "B", "parent": "cls-a"},
        ]})
    # кривой project_id
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": saved["id"], "project_id": "не-prj-id"})

    # изоляция пользователей
    assert store.get("f" * 12, saved["id"]) is None
    assert len(store.list("f" * 12)) == 0


def test_store_publish_flow(tmp_path):
    store = ontologies.OntologyStore(tmp_path / "ontologies", tmp_path / "data")
    saved = store.save(USER_ID, dict(ONTO))

    # активация публикует карточку в каталог и граф в objects/
    active = store.save(USER_ID, {"ontology_id": saved["id"], "status": "active"})
    assert active["status"] == ontologies.STATUS_ACTIVE

    catalog = json.loads((tmp_path / "data" / "ontologies.json").read_text(encoding="utf-8"))
    entries = [i for i in catalog["items"] if i["id"] == saved["id"]]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["classes_count"] == 3
    assert entry["relations_count"] == 1
    assert entry["author"] == ONTO["author"]
    assert entry["file_url"] == f"/data/ontologies/objects/{saved['id']}.json"

    obj = tmp_path / "data" / "ontologies" / "objects" / f"{saved['id']}.json"
    graph = json.loads(obj.read_text(encoding="utf-8"))
    assert len(graph["classes"]) == 3
    assert "user_id" not in graph

    # повторная активация (обновление) не плодит дубль в каталоге
    store.save(USER_ID, {"ontology_id": saved["id"], "name": "Обновлённая", "status": "active"})
    catalog = json.loads((tmp_path / "data" / "ontologies.json").read_text(encoding="utf-8"))
    entries = [i for i in catalog["items"] if i["id"] == saved["id"]]
    assert len(entries) == 1
    assert entries[0]["name"] == "Обновлённая"

    # неполный граф (без CQ) не публикуется
    weak = store.save(USER_ID, {"name": "Слабая онтология",
                                "classes": [{"id": "cls-a", "name": "A"}, {"id": "cls-b", "name": "B"}],
                                "relations": [{"id": "rel-x", "name": "x",
                                               "domain_class": "cls-a", "range_class": "cls-b"}]})
    with pytest.raises(ontologies.OntologyError):
        store.save(USER_ID, {"ontology_id": weak["id"], "status": "active"})

    # удаление сносит и черновик, и публикацию
    assert store.delete(USER_ID, saved["id"]) is True
    catalog = json.loads((tmp_path / "data" / "ontologies.json").read_text(encoding="utf-8"))
    assert not [i for i in catalog["items"] if i["id"] == saved["id"]]
    assert not obj.is_file()
    assert store.get(USER_ID, saved["id"]) is None


# ---------------------------------------------------------------------------
# Инструменты рантайма
# ---------------------------------------------------------------------------


def test_tools_ontology_flow():
    registry = build_default_registry(get_settings().tools_root)
    assert "get_ontologies" in registry.names()
    assert "save_ontology" in registry.names()

    # анонимная сессия — мягкий отказ, цикл не падает
    anon = asyncio.run(registry.call("get_ontologies", {}, ctx={"session": {}}))
    assert anon["ok"] is False and "войти" in anon["error"]

    session = {"user_id": USER_ID}
    saved = asyncio.run(
        registry.call("save_ontology", {"card": dict(ONTO)}, ctx={"session": session})
    )
    assert saved["ok"] is True and saved["ontology_id"].startswith("onto-")
    assert saved["status"] == "draft"
    assert saved["published"] is False
    assert saved["classes_count"] == 3 and saved["relations_count"] == 1

    # публикация через статус active (карточка уже существует)
    pub = asyncio.run(
        registry.call(
            "save_ontology",
            {"card": {"ontology_id": saved["ontology_id"], "status": "active"}},
            ctx={"session": session},
        )
    )
    assert pub["ok"] is True and pub["published"] is True
    assert pub["file_url"].endswith(f"/data/ontologies/objects/{saved['ontology_id']}.json")
    assert pub["viewer_url"] == f"/ontology.html?id={saved['ontology_id']}"

    # список своих: сводки без графа, но с формулировками CQ
    listed = asyncio.run(registry.call("get_ontologies", {}, ctx={"session": session}))
    assert listed["ok"] is True and listed["count"] == 1
    assert "classes" not in listed["items"][0]
    assert listed["items"][0]["competency_questions"][0]["question"].startswith("Из каких")

    # полный граф по ontology_id
    one = asyncio.run(
        registry.call(
            "get_ontologies",
            {"ontology_id": saved["ontology_id"]},
            ctx={"session": session},
        )
    )
    assert one["ok"] is True and len(one["ontology"]["classes"]) == 3

    # каталог опубликованных
    catalog = asyncio.run(registry.call("get_ontologies", {"scope": "catalog"}, ctx={"session": session}))
    assert catalog["ok"] is True and catalog["count"] >= 1

    # невалидный граф — ok:false, а не исключение в цикле
    bad = asyncio.run(
        registry.call(
            "save_ontology",
            {"card": {"ontology_id": saved["ontology_id"], "relations": [
                {"id": "rel-x", "name": "x", "domain_class": "cls-a", "range_class": "cls-ghost"}
            ]}},
            ctx={"session": session},
        )
    )
    assert bad["ok"] is False and "range_class" in bad["error"]


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def test_api_ontologies_owner_publish_delete():
    client = _client()
    token = _login_admin(client)
    uid = _admin_id()
    store = ontologies.get_store()
    saved = store.save(uid, dict(ONTO))
    url = f"/api/ontologies/{saved['id']}"

    # без токена — 401; владелец видит полный граф
    assert client.get("/api/ontologies").status_code == 401
    resp = client.get("/api/ontologies", headers=auth(token))
    assert resp.status_code == 200 and len(resp.json()["items"]) == 1
    assert len(resp.json()["items"][0]["classes"]) == 3

    resp = client.get(url, headers=auth(token))
    assert resp.status_code == 200 and resp.json()["id"] == saved["id"]
    assert client.get("/api/ontologies/onto-deadbeef", headers=auth(token)).status_code == 404

    # правка владельцем; невалидный граф — 422
    resp = client.patch(url, headers=auth(token), json={"name": "Правленая онтология"})
    assert resp.status_code == 200 and resp.json()["name"] == "Правленая онтология"
    resp = client.patch(url, headers=auth(token), json={"classes": [
        {"id": "cls-a", "name": "A", "parent": "cls-ghost"}
    ]})
    assert resp.status_code == 422

    # публикация: полный граф — ок, неполный — 422
    resp = client.post(f"{url}/publish", headers=auth(token))
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert resp.json()["catalog_entry"]["file_url"].endswith(".json")

    weak = store.save(uid, {"name": "Без вопросов",
                            "classes": [{"id": "cls-a", "name": "A"}, {"id": "cls-b", "name": "B"}],
                            "relations": [{"id": "rel-x", "name": "x",
                                           "domain_class": "cls-a", "range_class": "cls-b"}]})
    resp = client.post(f"/api/ontologies/{weak['id']}/publish", headers=auth(token))
    assert resp.status_code == 422

    # второй пользователь: чужие онтологии недостижимы (404), удаление — 403
    users = UserStore(get_settings().runtime_dir / "users")
    users.create("petrov@csr-nw.ru", "pass1234", name="Петров П.", confirmed=True)
    other_token = client.post(
        "/api/auth/login", json={"login": "petrov@csr-nw.ru", "password": "pass1234"}
    ).json()["token"]
    assert client.get(url, headers=auth(other_token)).status_code == 404
    assert client.post(f"{url}/publish", headers=auth(other_token)).status_code == 404
    assert client.delete(url, headers=auth(other_token)).status_code == 403

    # администратор удаляет онтологию вместе с публикацией
    obj = store.objects_dir / f"{saved['id']}.json"
    assert obj.is_file()
    resp = client.delete(url, headers=auth(token))
    assert resp.status_code == 200 and resp.json() == {"ok": True, "deleted": saved["id"]}
    assert client.get(url, headers=auth(token)).status_code == 404
    assert not obj.is_file()
    assert not [i for i in store._load_catalog()["items"] if i["id"] == saved["id"]]
    assert client.delete(url, headers=auth(token)).status_code == 404



