"""Смоук-тесты обратной связи каталога агентов (страница «Агенты»).

Хранилище (runtime/agent_feedback.py): звёзды (переключение и счётчик),
комментарии (добавление, удаление автором/администратором, чужой — 404),
учёт применения в проектах (дедупликация по проекту, слияние артефактов)
и record_usage_from_session — глотает ошибки и пишет только агентов из
data/skills.json.

API (routers/catalog.py): счётчики overview, публичный просмотр feedback
(имя автора вместо логина, названия проектов без персональных полей),
звезда и комментарий требуют входа, удаление — автор или администратор,
посторонний agent_id — 404.

Автотрекинг: save_project_card с сессией навыка помечает применение
агента в проекте — эндпоинт feedback отдаёт название проекта.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.auth import get_token_store, get_user_store
from api.runtime import agent_feedback, cabinet, projects
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"

ONBOARDING = "project-onboarding"  # агент каталога (data/skills.json)
ARCHITECT = "portfolio-analyst"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Клиент с изолированным runtime-каталогом (сброс синглтонов-хранилищ)."""
    # пароль сид-админа из .env не совпадает с тестовым — задаём свой
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    _clear_caches()
    yield TestClient(app)
    _clear_caches()


def _clear_caches():
    get_settings.cache_clear()
    agent_feedback.get_store.cache_clear()
    projects.get_store.cache_clear()
    cabinet.get_store.cache_clear()
    get_user_store.cache_clear()
    get_token_store.cache_clear()


def _make_user(name: str = "Иван Иванов") -> tuple[dict, str]:
    """Подтверждённый пользователь + его токен (мимо регистрации: без писем)."""
    login = f"u{uuid.uuid4().hex[:10]}@example-test.io"
    user = get_user_store().create(login, "1234", name=name, confirmed=True)
    token = get_token_store().issue(user["id"])
    return user, token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----- хранилище -----


def test_star_toggle_and_counts(tmp_path):
    store = agent_feedback.AgentFeedbackStore(tmp_path / "fb")
    assert store.counts(ARCHITECT) == {"stars": 0, "comments": 0, "projects": 0}

    first = store.toggle_star(ARCHITECT, "u1")
    assert first == {"starred": True, "stars": 1}
    store.toggle_star(ARCHITECT, "u2")
    assert store.counts(ARCHITECT)["stars"] == 2

    again = store.toggle_star(ARCHITECT, "u1")  # повторная звезда снимается
    assert again == {"starred": False, "stars": 1}


def test_comments_add_delete_permissions(tmp_path):
    store = agent_feedback.AgentFeedbackStore(tmp_path / "fb")
    comment = store.add_comment(ARCHITECT, "u1", "Полезный агент")
    assert store.feedback(ARCHITECT)["comments"][0]["text"] == "Полезный агент"

    assert store.delete_comment(ARCHITECT, comment["id"], "u2", False) is False
    assert store.delete_comment(ARCHITECT, comment["id"], "u1", False) is True
    store.add_comment(ARCHITECT, "u1", "Ещё раз")
    other = store.add_comment(ARCHITECT, "u2", "Чужой")
    assert store.delete_comment(ARCHITECT, other["id"], "u1", True) is True  # админ


def test_usage_dedupes_by_project(tmp_path):
    store = agent_feedback.AgentFeedbackStore(tmp_path / "fb")
    store.record_usage(ARCHITECT, "prj-aaaaaaaa", "ontology")
    store.record_usage(ARCHITECT, "prj-aaaaaaaa", "rag")
    usage = store.feedback(ARCHITECT)["usage"]
    assert len(usage) == 1
    assert usage[0]["runs"] == 2
    assert sorted(usage[0]["artifacts"]) == ["ontology", "rag"]


def test_record_usage_from_session_filters_catalog(tmp_path, monkeypatch):
    """Пишутся только агенты из data/skills.json; любые сбои глотаются."""
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    agent_feedback.get_store.cache_clear()
    store = agent_feedback.get_store()

    session = {"skill_ids": [ONBOARDING, "no-such-agent"], "user_id": "u1"}
    agent_feedback.record_usage_from_session(session, "prj-bbbbbbbb", "project")
    assert store.counts(ONBOARDING)["projects"] == 1
    assert store.feedback("no-such-agent") == store._empty()  # файл не создан

    # битые входы не ломают вызов
    agent_feedback.record_usage_from_session(None, "prj-bbbbbbbb")
    agent_feedback.record_usage_from_session({"skill_ids": "not-a-list"}, "prj-bbbbbbbb")
    agent_feedback.record_usage_from_session(session, "")


# ----- API -----


def test_overview_lists_catalog_agents(client):
    resp = client.get("/api/catalog/agents/overview")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert set(items) >= {ONBOARDING, ARCHITECT, "ecosystem-analyst"}
    assert items[ONBOARDING] == {"stars": 0, "comments": 0, "projects": 0}


def test_feedback_requires_existing_agent(client):
    assert client.get("/api/catalog/agents/nope/feedback").status_code == 404
    assert client.post("/api/catalog/agents/nope/star").status_code == 401


def test_star_flow(client):
    _, token = _make_user()
    assert client.post(f"/api/catalog/agents/{ARCHITECT}/star").status_code == 401

    on = client.post(f"/api/catalog/agents/{ARCHITECT}/star", headers=_auth(token))
    assert on.json() == {"stars": 1, "stars_by_me": True}

    view = client.get(f"/api/catalog/agents/{ARCHITECT}/feedback", headers=_auth(token))
    assert view.json()["starred_by_me"] is True
    guest = client.get(f"/api/catalog/agents/{ARCHITECT}/feedback")
    assert guest.json()["starred_by_me"] is False
    assert guest.json()["stars"] == 1

    off = client.post(f"/api/catalog/agents/{ARCHITECT}/star", headers=_auth(token))
    assert off.json()["stars_by_me"] is False


def test_comments_flow_author_hidden_login(client):
    user, token = _make_user(name="Мария Петрова")
    posted = client.post(
        f"/api/catalog/agents/{ARCHITECT}/comments",
        json={"text": "Собрал модель за вечер"},
        headers=_auth(token),
    )
    assert posted.status_code == 200
    body = posted.json()
    assert body["author"] == "Мария Петрова"
    assert body["mine"] is True
    assert "login" not in body and "user_id" not in body

    view = client.get(f"/api/catalog/agents/{ARCHITECT}/feedback").json()
    assert view["comments"][0]["text"] == "Собрал модель за вечер"
    assert view["comments"][0]["author"] == "Мария Петрова"

    # пустой текст — 422, без входа — 401
    assert client.post(
        f"/api/catalog/agents/{ARCHITECT}/comments", json={"text": "  "}
    ).status_code == 401

    # чужой не удаляет (404), автор удаляет
    _, other = _make_user(name="Другой")
    cid = body["id"]
    assert client.delete(
        f"/api/catalog/agents/{ARCHITECT}/comments/{cid}", headers=_auth(other)
    ).status_code == 404
    assert client.delete(
        f"/api/catalog/agents/{ARCHITECT}/comments/{cid}", headers=_auth(token)
    ).status_code == 200


def test_admin_deletes_any_comment(client):
    admin = client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()
    _, user_token = _make_user()
    cid = client.post(
        f"/api/catalog/agents/{ARCHITECT}/comments",
        json={"text": "комментарий пользователя"},
        headers=_auth(user_token),
    ).json()["id"]
    resp = client.delete(
        f"/api/catalog/agents/{ARCHITECT}/comments/{cid}",
        headers=_auth(admin["token"]),
    )
    assert resp.status_code == 200


def test_usage_recorded_from_project_tool(client, tmp_path):
    """save_project_card в сессии агента → проект в блоке применения."""
    _, token = _make_user()
    session_user = get_token_store().resolve(token)

    registry = build_default_registry(tmp_path / "tools")
    session = {"skill_ids": [ONBOARDING], "user_id": session_user}
    result = asyncio.run(
        registry.call(
            "save_project_card",
            {"card": {"title": "Катализаторы будущего"}},
            ctx={"session": dict(session)},
        )
    )
    assert result["ok"] is True

    view = client.get(f"/api/catalog/agents/{ONBOARDING}/feedback").json()
    assert view["projects"], "проект должен попасть в блок применения"
    entry = view["projects"][0]
    assert entry["title"] == "Катализаторы будущего"
    assert entry["runs"] == 1
    assert "карточка проекта" in entry["artifacts"]

    # счётчик виден и в overview
    overview = client.get("/api/catalog/agents/overview").json()["items"]
    assert overview[ONBOARDING]["projects"] == 1
