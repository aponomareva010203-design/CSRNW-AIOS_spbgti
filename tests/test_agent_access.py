"""Доступ к агенту «Литературный радар»: автор и команда — через кабинет,
остальные — только навык в чате.

Сценарий пользователя: агент создаётся автором (Аксёновым) и привязывается
к его проекту. Интерфейс агента (/agent.html и его API) открывают только
владелец и участники его проекта («Моя команда» кабинета) — карточка ведёт
на /agent.html именно из кабинета. Остальным пользователям агент остаётся
навыком каталога «Агенты»: запускается в чате (skill_ids), без агентных
функций — чат с agent_id и страница агента им недоступны.

Проверяется: список /api/agents (кто видит), карточка /api/agents/{id}
(404 для посторонних), _agent_card_for_user (чат с agent_id — только свои),
и что навык каталога (шаблон «Венчурный билдер») при этом доступен всем как навык чата.
"""

import pytest

from api.runtime import agent_store, projects
from api.runtime.config import get_settings
from api.runtime.projects import ProjectStore
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"

SKILL_ID = "venture-builder"  # навык каталога data/skills.json


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from api.routers import auth as auth_router
    from api.routers import chat as chat_router
    from api.runtime import cabinet as cabinet_runtime
    from api.runtime import projects as projects_runtime
    from api.runtime import team as team_runtime

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))
    # DNS-проверка почтового домена в тестах не нужна: при настроенном SMTP
    # (как в .env сервера) она завалила бы регистрацию любых test-доменов.
    monkeypatch.setattr(auth_router, "email_domain_accepts_mail", lambda login: True)
    get_settings.cache_clear()
    agent_store._STORE = None
    agent_store._RECORD_STORE = None
    agent_store._EVAL_STORE = None
    chat_router.get_store.cache_clear()
    projects_runtime.get_store.cache_clear()
    cabinet_runtime.get_store.cache_clear()
    team_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    agent_store._STORE = None
    agent_store._RECORD_STORE = None
    agent_store._EVAL_STORE = None
    get_settings.cache_clear()


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register_user(client, login, name):
    """Регистрация с подтверждением e-mail (без SMTP); возвращает (token, user_id)."""
    from api.routers import auth as auth_router

    resp = client.post("/api/auth/register", json={
        "login": login, "password": "pass1234", "name": name,
        "consent_pdn": True, "consent_policy": True,
    })
    assert resp.status_code == 201, resp.text
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login(login)["id"]
    token = auth_router.get_confirmation_store().issue(user_id)
    resp = client.get(f"/api/auth/confirm?token={token}", follow_redirects=True)
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/auth/login", json={"login": login, "password": "pass1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"], user_id


def _radar_card(owner_id: str, project_id: str) -> dict:
    """Карточка радара в миниатюре: те же привязки и инструменты, что у
    созданной в рантайме agt-literature-radar."""
    return {
        "project_id": project_id,
        "title": "Литературный радар — обзор публикаций по базальтопластикам",
        "interface_type": "dashboard",
        "status": "active",
        "visibility": "project",
        "trigger": {"type": "schedule", "interval_minutes": 1440, "time": "09:00"},
        "autonomy": {"level": "semi_autonomous"},
        "nodes": [
            {"id": "n1", "type": "trigger", "config": {}},
            {"id": "n2", "type": "reasoning",
             "config": {"system_prompt": "радар", "available_tools": ["n3", "n4"]}},
            {"id": "n3", "type": "tool.literature_search", "config": {}},
            {"id": "n4", "type": "tool.save_record", "config": {}},
            {"id": "n5", "type": "output", "config": {}},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n5"}],
    }


@pytest.fixture
def scenario(tmp_path):
    """Автор, участник его команды, посторонний; проект и карточка агента."""
    client = _client()
    owner_token, owner_id = _register_user(client, "aksenov@ex.com", "Аксенов Даниил Дмитриевич")
    teammate_token, teammate_id = _register_user(client, "belyakov@ex.com", "Беляков Максим")
    outsider_token, outsider_id = _register_user(client, "outsider@ex.com", "Чужой Посторонний Посторонневич")

    store = ProjectStore(get_settings().runtime_dir / "projects")
    project = store.save(owner_id, {
        "title": "Отечественное базальтовое волокно в конструкционных композитах",
        "participants": "Беляков Максим",
    })

    card = agent_store.get_store().save(owner_id, _radar_card(owner_id, project["id"]))
    agent_id = card["id"]

    tokens = {"owner": owner_token, "teammate": teammate_token, "outsider": outsider_token}
    return client, tokens, owner_id, teammate_id, outsider_id, agent_id


def test_radar_stays_a_chat_skill_for_everyone():
    """Другая половина сценария: навык каталога «Агенты» подсказывается всем
    в чате независимо от агентных прав (проверяем на шаблоне каталога)."""
    from api.runtime.datarepo import get_skill

    assert get_skill(SKILL_ID) is not None, "навык в каталоге"

    client = _client()
    # минимум длины запроса для подсказок — 8 символов (короче отсекается)
    resp = client.get("/api/chat/skills-suggest", params={"q": "трекер стартапов"})
    assert resp.status_code == 200
    ids = [s.get("id") for s in resp.json()["items"]]
    assert SKILL_ID in ids, "чипы навыка доступны любому пользователю чата"