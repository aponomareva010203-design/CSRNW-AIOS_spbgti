"""Смоук-тесты страницы «Кабинет».

Блок «Данные пользователя» (GET|PUT /api/cabinet/profile): только
собственные данные по Bearer-токену, лимиты полей. Скрытый блок
«Контекст о пользователе» через API не отдаётся. Планировщик
(run_user_context_job): генерация по fake-провайдеру, пропуски
по расписанию, дайджесту и отсутствию материала. Удаление учётной
записи администратором чистит блоки кабинета. История сессий
(GET|DELETE /api/cabinet/sessions): сводки только своих сессий, удаление
только своих. «Результаты работы» (GET /api/cabinet/projects): свои
карточки (owner) плюс чужие, где пользователь указан участником по ФИО
(participant, включая черновики — раздел показывает все карточки,
где пользователь упомянут).
"""

import asyncio
import json
import time

import pytest

from api.runtime.cabinet import CabinetError, CabinetStore, full_name, get_store
from api.runtime.config import get_settings
from api.runtime.projects import ProjectStore
from api.runtime.sessions import SessionStore
from api.runtime.team import TeamStore, get_store as get_team_store
from api.runtime.user_context import run_user_context_job
from api.runtime.users import UserStore

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime-каталог, свежие кэши хранилищ."""
    from api.routers import auth as auth_router
    from api.runtime import projects as projects_runtime

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # без SMTP регистрация идёт в dev-режиме: без MX-проверки домена
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    get_settings.cache_clear()
    get_store.cache_clear()
    get_team_store.cache_clear()
    projects_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_store.cache_clear()
    get_team_store.cache_clear()
    projects_runtime.get_store.cache_clear()
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


def _register_user(client, login="user@ex.com", password="pass1234", name="Тестов Тест Тестович"):
    """Регистрация с подтверждением e-mail (без SMTP); возвращает токен."""
    from api.routers import auth as auth_router

    resp = client.post(
        "/api/auth/register", json={"login": login, "password": password, "name": name, "consent_pdn": True, "consent_policy": True}
    )
    assert resp.status_code == 201, resp.text
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login(login)["id"]
    # повторный выпуск переиспользует живой токен подтверждения из регистрации
    token = auth_router.get_confirmation_store().issue(user_id)
    resp = client.get(f"/api/auth/confirm?token={token}", follow_redirects=True)
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/auth/login", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_profile_requires_auth():
    client = _client()
    assert client.get("/api/cabinet/profile").status_code == 401
    assert client.put("/api/cabinet/profile", json={"last_name": "Иванов"}).status_code == 401


def test_profile_roundtrip_and_isolation():
    client = _client()
    admin_token = _login_admin(client)
    user_token = _register_user(client)

    payload = {
        "last_name": "Иванов",
        "first_name": "Иван",
        "patronymic": "Иванович",
        "positions": ["доцент кафедры ИИ", "инженер-исследователь"],
        "degree": "кандидат технических наук",
        "interests": "машинное обучение, обратные задачи",
    }
    resp = client.put("/api/cabinet/profile", json=payload, headers=auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["last_name"] == "Иванов"
    assert data["positions"] == payload["positions"]
    assert data["updated_at"] > 0

    saved = client.get("/api/cabinet/profile", headers=auth(admin_token)).json()
    for key, value in payload.items():
        assert saved[key] == value

    # пользователь видит только собственный блок: у новичка всё пусто
    other = client.get("/api/cabinet/profile", headers=auth(user_token)).json()
    assert other["last_name"] == ""
    assert other["positions"] == []
    assert other["updated_at"] is None

    # перезапись одним пользователем не задевает блок другого
    client.put("/api/cabinet/profile", json={"last_name": "Петров"}, headers=auth(user_token))
    assert (
        client.get("/api/cabinet/profile", headers=auth(admin_token)).json()["last_name"]
        == "Иванов"
    )

    # на диске — по одному файлу профиля на пользователя
    users = UserStore(get_settings().runtime_dir / "users")
    store = get_store()
    for login in (ADMIN_LOGIN, "user@ex.com"):
        user_id = users.get_by_login(login)["id"]
        assert (store.root / f"{user_id}-profile.json").is_file()
def test_profile_validation_limits():
    client = _client()
    token = _login_admin(client)

    ok = {
        "last_name": "И" * 80,
        "first_name": "И" * 80,
        "patronymic": "И" * 80,
        "positions": ["д" * 120] * 10,
        "degree": "к" * 160,
        "interests": "и" * 2000,
    }
    resp = client.put("/api/cabinet/profile", json=ok, headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["positions"] == ok["positions"]

    for field, value in (
        ("last_name", "И" * 81),
        ("first_name", "И" * 81),
        ("patronymic", "И" * 81),
        ("degree", "к" * 161),
        ("interests", "и" * 2001),
    ):
        resp = client.put(
            "/api/cabinet/profile", json=dict(ok, **{field: value}), headers=auth(token)
        )
        assert resp.status_code == 422, f"{field}: {resp.text}"

    # больше 10 должностей; отдельная должность длиннее 120 символов
    assert (
        client.put(
            "/api/cabinet/profile", json=dict(ok, positions=["д"] * 11), headers=auth(token)
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/cabinet/profile", json=dict(ok, positions=["д" * 121]), headers=auth(token)
        ).status_code
        == 422
    )

    # пустые должности отбрасываются, а не ломают сохранение
    resp = client.put(
        "/api/cabinet/profile", json={"positions": ["  ", "доцент"]}, headers=auth(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["positions"] == ["доцент"]
def test_context_block_never_served():
    client = _client()
    token = _login_admin(client)
    users = UserStore(get_settings().runtime_dir / "users")
    admin_id = users.get_by_login(ADMIN_LOGIN)["id"]

    store = get_store()
    # планировщик сохранил скрытый контекст (как после прохода LLM)
    store.save_context(admin_id, "Скрытый контекст о пользователе", "d" * 64)

    data = client.get("/api/cabinet/profile", headers=auth(token)).json()
    assert "context" not in data
    assert "source_digest" not in data

    # набор кабинетных эндпоинтов закрыт (скрытый блок среди них не отдаётся)
    paths = client.get("/openapi.json").json()["paths"]
    assert sorted(p for p in paths if p.startswith("/api/cabinet")) == [
        "/api/cabinet/profile",
        "/api/cabinet/projects",
        "/api/cabinet/sessions",
        "/api/cabinet/sessions/search",
        "/api/cabinet/sessions/{session_id}",
        "/api/cabinet/team",
        "/api/cabinet/team/{user_id}/permissions",
    ]


def test_store_defaults_and_full_name(tmp_path):
    store = CabinetStore(tmp_path / "user")
    uid = "abc123def456"

    assert store.get_profile(uid) == {
        "last_name": "",
        "first_name": "",
        "patronymic": "",
        "positions": [],
        "degree": "",
        "interests": "",
        "updated_at": None,
    }
    # некорректный идентификатор не пробивает путь к файлу
    assert store.get_profile("../../etc/passwd")["last_name"] == ""

    with pytest.raises(CabinetError):
        store.save_profile(uid, last_name="И" * 81)
    with pytest.raises(CabinetError):
        store.save_profile(uid, positions=["д"] * 11)
    with pytest.raises(CabinetError):
        store.save_profile(uid, positions=["д" * 121])
    with pytest.raises(CabinetError):
        store.save_profile(uid, positions="доцент")

    data = store.save_profile(
        uid,
        last_name="Иванов",
        first_name="Иван",
        patronymic="",
        positions=["  доцент  ", ""],
        degree="",
        interests="МО",
    )
    assert full_name(data) == "Иванов Иван"
    assert data["positions"] == ["доцент"]

    assert store.delete_user(uid) is True
    assert store.get_profile(uid)["updated_at"] is None
    assert store.delete_user(uid) is False
class FakeProvider:
    """Подменяет LLM-провайдер: фиксирует промпты, отдаёт готовый текст."""

    def __init__(self, text="Пользователь ведёт расчёты и часто просит подобрать навыки и данные."):
        self.text = text
        self.calls = []

    async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
        self.calls.append(list(messages))
        self.last_model = model
        self.last_max_tokens = max_tokens
        yield "text", self.text


def _make_session(rt, user_id, title, messages):
    sessions = SessionStore(rt / "sessions")
    session = sessions.create(title=title)
    session["user_id"] = user_id  # привязка сессии к учётной записи (как в POST /api/chat)
    for message in messages:
        sessions.append_message(session, message)
    return session


def _set_checked_at(store, user_id):
    """Отмечает «проверка была давно» в файле контекста (не ноль: 0 в цепочке
    `checked_at or generated_at` планировщика отвергается как falsy)."""
    path = store.root / f"{user_id}-context.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checked_at"] = time.time() - 2 * 24 * 60 * 60
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def test_user_context_job_generation_and_skips():
    settings = get_settings()
    users = UserStore(settings.runtime_dir / "users")
    user = users.create(
        login="job-user@ex.io", password="pass1234", name="Задачников Задача Задачевич"
    )
    session = _make_session(
        settings.runtime_dir,
        user["id"],
        "Подбор навыков для расчётов",
        [
            {"role": "user", "content": "Подбери навыки для расчёта фазовых равновесий"},
            {"role": "assistant", "content": "Предлагаю навык thermo-calc"},
        ],
    )

    store = get_store()
    store.save_profile(
        user["id"],
        last_name="Задачников",
        first_name="Задача",
        patronymic="Задачевич",
        positions=["доцент"],
    )

    provider = FakeProvider()
    stats = asyncio.run(run_user_context_job(provider=provider))
    assert stats["generated"] == 1
    assert stats["no_material"] == 1  # seed-админ: ни сессий, ни упоминаний

    ctx = store.get_context(user["id"])
    assert ctx is not None
    assert ctx["context"] == provider.text
    assert len(ctx["source_digest"]) == 64

    # модели передали и анкету, и материал; анкету просили не дублировать
    task = provider.calls[0][1]["content"]
    assert "Задачников" in task
    assert "НЕ дублировать" in task
    assert "Подбери навыки" in task

    # повторный проход в тот же день — пропуск по расписанию
    stats = asyncio.run(run_user_context_job(provider=provider))
    assert stats["skipped_fresh"] == 1
    assert stats["generated"] == 0
    assert len(provider.calls) == 1

    # проверка была давно, материал не менялся → контекст не трогаем
    _set_checked_at(store, user["id"])
    stats = asyncio.run(run_user_context_job(provider=provider))
    assert stats["unchanged"] == 1
    assert store.get_context(user["id"])["context"] == provider.text
    assert len(provider.calls) == 1

    # новый материал → перегенерация
    sessions = SessionStore(settings.runtime_dir / "sessions")
    sessions.append_message(session, {"role": "user", "content": "Ещё вопрос про онтологии"})
    _set_checked_at(store, user["id"])
    stats = asyncio.run(run_user_context_job(provider=provider))
    assert stats["generated"] == 1
    assert len(provider.calls) == 2

    # пользователь без материала — файл контекста не создаётся
    other = users.create(login="quiet@ex.io", password="pass1234", name="Тихонов Тихон Тихонович")
    stats = asyncio.run(run_user_context_job(provider=provider))
    assert stats["no_material"] == 2  # seed-админ и новый пользователь
    assert stats["skipped_fresh"] == 1
    assert store.get_context(other["id"]) is None

    # удаление учётной записи чистит оба блока
    assert store.delete_user(user["id"]) is True
    assert store.get_context(user["id"]) is None
    assert store.delete_user(user["id"]) is False
def test_admin_delete_user_cleans_cabinet():
    client = _client()
    admin_token = _login_admin(client)
    user_token = _register_user(client)

    resp = client.put(
        "/api/cabinet/profile",
        json={
            "last_name": "Сидоров",
            "first_name": "Сидор",
            "patronymic": "Сидорович",
            "positions": ["инженер"],
        },
        headers=auth(user_token),
    )
    assert resp.status_code == 200, resp.text

    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login("user@ex.com")["id"]
    store = get_store()
    assert (store.root / f"{user_id}-profile.json").is_file()

    # удаление учётной записи администратором убирает и блоки кабинета
    resp = client.delete(f"/api/admin/users/{user_id}", headers=auth(admin_token))
    assert resp.status_code == 200, resp.text
    assert not (store.root / f"{user_id}-profile.json").exists()
    assert not (store.root / f"{user_id}-context.json").exists()


def test_prompt_block_combines_blocks(tmp_path):
    store = CabinetStore(tmp_path / "user")
    uid = "abc123def456"

    # о пользователе ничего не известно — блок пуст
    assert store.prompt_block(uid) == ""
    assert store.prompt_block("bad-id!") == ""

    store.save_profile(
        uid,
        last_name="Иванов",
        first_name="Иван",
        patronymic="Иванович",
        positions=["доцент"],
        degree="к.т.н.",
        interests="машинное обучение",
    )
    block = store.prompt_block(uid)
    assert "Иванов Иван Иванович" in block
    assert "Должности: доцент" in block

    store.save_context(uid, "Любит краткие ответы с примерами.", "d" * 64)
    block = store.prompt_block(uid)
    assert "Анкетные данные" in block
    assert "Контекст о пользователе" in block
    assert "Любит краткие ответы" in block


# ----- история сессий и «Результаты работы» -----


def _mk_session(title="Диалог", user_id=None):
    store = SessionStore(get_settings().runtime_dir / "sessions")
    session = store.create(title=title)
    if user_id:
        session["user_id"] = user_id
    session["messages"] = [{"role": "user", "content": "привет"}]
    store.save(session)
    return session


def _mk_project(store, user_id, title="Проект", **fields):
    """Карточка через онбординговый сценарий: черновик, затем active."""
    card = store.save(user_id, {"title": title})
    payload = {"id": card["id"], "status": "active"}
    payload.update(fields)
    return store.save(user_id, payload)


def test_cabinet_sessions_requires_auth():
    client = _client()
    assert client.get("/api/cabinet/sessions").status_code == 401
    assert client.delete("/api/cabinet/sessions/000000000000").status_code == 401
    assert client.get("/api/cabinet/projects").status_code == 401


def test_cabinet_sessions_lists_only_own():
    client = _client()
    user_token = _register_user(client, login="hist@ex.com")
    other_token = _register_user(client, login="hist2@ex.com", name="Другой Человек Человечевич")
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login("hist@ex.com")["id"]
    other_id = users.get_by_login("hist2@ex.com")["id"]

    # анонимная сессия (гостевой чат) в список владельца не попадает
    _mk_session(title="Анонимная")
    mine = _mk_session(title="Мой диалог", user_id=user_id)
    _mk_session(title="Чужой диалог", user_id=other_id)

    items = client.get("/api/cabinet/sessions", headers=auth(user_token)).json()["items"]
    assert [s["title"] for s in items] == ["Мой диалог"]
    assert items[0]["id"] == mine["id"]
    assert items[0]["message_count"] == 1

    others = client.get("/api/cabinet/sessions", headers=auth(other_token)).json()["items"]
    assert [s["title"] for s in others] == ["Чужой диалог"]


def test_cabinet_session_delete_only_own():
    client = _client()
    user_token = _register_user(client, login="del@ex.com")
    other_token = _register_user(client, login="del2@ex.com", name="Другой Человек Человечевич")
    users = UserStore(get_settings().runtime_dir / "users")
    other_id = users.get_by_login("del2@ex.com")["id"]

    mine = _mk_session(title="Мой диалог", user_id=users.get_by_login("del@ex.com")["id"])
    foreign = _mk_session(title="Чужой диалог", user_id=other_id)

    # чужую сессию удалить нельзя — 404 (неотличимо от несуществующей)
    assert (
        client.delete(f"/api/cabinet/sessions/{foreign['id']}", headers=auth(user_token)).status_code
        == 404
    )
    assert client.delete("/api/cabinet/sessions/000000000000", headers=auth(user_token)).status_code == 404

    # свою удаляет, файл исчезает из хранилища
    resp = client.delete(f"/api/cabinet/sessions/{mine['id']}", headers=auth(user_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == mine["id"]
    assert SessionStore(get_settings().runtime_dir / "sessions").get(mine["id"]) is None
    assert client.get("/api/cabinet/sessions", headers=auth(user_token)).json()["items"] == []


def test_cabinet_result_projects_owner_and_participant():
    client = _client()
    token = _register_user(client, login="prj@ex.com", name="Иванов Иван Иванович")
    other_token = _register_user(client, login="prj2@ex.com", name="Петров Пётр Петрович")
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login("prj@ex.com")["id"]
    other_id = users.get_by_login("prj2@ex.com")["id"]
    store = ProjectStore(get_settings().runtime_dir / "projects")

    own_draft = store.save(user_id, {"title": "Мой черновик"})
    own_active = _mk_project(
        store, user_id, title="Мой проект", goal="цель", budget="10 млн"
    )
    participant = _mk_project(
        store, other_id, title="Совместный проект", participants="Иванов Иван Иванович, Петров Пётр Петрович"
    )
    _mk_project(store, other_id, title="Посторонний проект", participants="Сидоров Сидор Сидорович")

    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    roles = {c["id"]: c["role"] for c in items}
    assert roles[own_active["id"]] == "owner"
    assert roles[own_draft["id"]] == "owner"  # свои черновики видны (статус черновик)
    assert roles[participant["id"]] == "participant"  # участие — по ФИО учётной записи
    assert "Посторонний проект" not in {c["title"] for c in items}

    # у чужой карточки-участия не отдаётся служебный user_id
    part = [c for c in items if c["id"] == participant["id"]][0]
    assert "user_id" not in part

    # анкета «Кабинета» добавляет варианты написания ФИО
    client.put(
        "/api/cabinet/profile",
        json={"last_name": "Иванов", "first_name": "Иван", "patronymic": "Иванович"},
        headers=auth(token),
    )
    initials = _mk_project(store, other_id, title="Проект с инициалами", participants="Иванов И. И.")
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert initials["id"] in {c["id"] for c in items}

    # инициалы без пробела и в другом формате тоже матчатся («Иванов И.И.»)
    tight = _mk_project(store, other_id, title="Проект с инициалами без пробела", participants="Иванов И.И., Петров П. П.")
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert tight["id"] in {c["id"] for c in items}

    # упоминание одной фамилии участником не считается — это не ФИО
    surname = _mk_project(store, other_id, title="Проект с фамилией", participants="Иванов")
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert surname["id"] not in {c["id"] for c in items}

    # участники могут идти по одному на строку, а ФИО в карточке — полнее,
    # чем в учётной записи («Иванов Иван» против «Иванов И.»)
    multiline = _mk_project(
        store, other_id, title="Проект со строками",
        participants="Петров Пётр\nИванов Иван Иванович",
    )
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert multiline["id"] in {c["id"] for c in items}

    # чужой черновик онбординга с совпадающим ФИО показывается:
    # «Участвую» собирает все карточки, где пользователь упомянут
    store.save(other_id, {"title": "Чужой черновик", "participants": "Иванов Иван Иванович"})
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert "Чужой черновик" in {c["title"] for c in items}


# ----- моя команда -----


def test_team_requires_auth():
    client = _client()
    assert client.get("/api/cabinet/team").status_code == 401
    assert (
        client.put(
            "/api/cabinet/team/000000000000/permissions", json={"permissions": {}}
        ).status_code
        == 401
    )


def test_team_listing_and_permissions():
    client = _client()
    token = _register_user(client, login="lead@ex.com", name="Руководитель Руковод Руководович")
    part_token = _register_user(client, login="member@ex.com", name="Участников Участник Участникович")
    users = UserStore(get_settings().runtime_dir / "users")
    lead_id = users.get_by_login("lead@ex.com")["id"]
    member_id = users.get_by_login("member@ex.com")["id"]
    store = ProjectStore(get_settings().runtime_dir / "projects")

    # свой проект (участник указан в карточке) + чужой, где лидер участник:
    # владелец чужой карточки тоже попадает в команду как «руководитель проекта»
    own = _mk_project(
        store, lead_id, title="Мой проект", participants="Участников Участник"
    )
    outsider_token = _register_user(client, login="out@ex.com", name="Сторонний Чужой Чужович")
    outsider_id = users.get_by_login("out@ex.com")["id"]
    _mk_project(store, outsider_id, title="Чужой", participants="Руководитель Руковод")

    resp = client.get("/api/cabinet/team", headers=auth(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    by_id = {r["user_id"]: r for r in items}
    assert set(by_id) == {lead_id, member_id, outsider_id}
    assert by_id[outsider_id]["statuses"] == ["руководитель проекта"]
    assert set(by_id[lead_id]["statuses"]) == {"руководитель проекта", "участник проекта"}
    assert by_id[member_id]["permissions"] == {"projects": False, "ontologies": False, "rag": False}
    assert by_id[lead_id]["permissions"] is None  # себе права не выдаются
    # названия проектов по статусу — материал подсказки при наведении на чип
    assert by_id[lead_id]["projects"]["руководитель проекта"] == ["Мой проект"]
    assert by_id[lead_id]["projects"]["участник проекта"] == ["Чужой"]
    assert by_id[outsider_id]["projects"]["руководитель проекта"] == ["Чужой"]
    # у участника чужой карточки лидер значится участником
    out_items = client.get("/api/cabinet/team", headers=auth(outsider_token)).json()["items"]
    lead_row = [r for r in out_items if r["user_id"] == lead_id][0]
    assert "участник проекта" in lead_row["statuses"]

    # выдача прав: сохранить можно только участнику своей команды
    resp = client.put(
        f"/api/cabinet/team/{member_id}/permissions",
        json={"permissions": {"ontologies": True}},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == {"projects": False, "ontologies": True, "rag": False}

    assert (
        client.put(
            f"/api/cabinet/team/{lead_id}/permissions",
            json={"permissions": {"projects": True}},
            headers=auth(token),
        ).status_code
        == 400
    )  # себе — нельзя
    assert (
        client.put(
            "/api/cabinet/team/000000000000/permissions",
            json={"permissions": {"projects": True}},
            headers=auth(token),
        ).status_code
        == 404
    )  # не в команде
    assert (
        client.put(
            f"/api/cabinet/team/{member_id}/permissions",
            json={"permissions": {"dashboards": True}},
            headers=auth(token),
        ).status_code
        == 422
    )  # неизвестный вид артефактов

    # участник с правом projects правит чужую карточку, без права — 404
    lead_card = _mk_project(store, lead_id, title="Для правки")
    patch = client.patch(
        f"/api/projects/{lead_card['id']}", json={"goal": "новая цель"}, headers=auth(part_token)
    )
    assert patch.status_code == 404, patch.text
    client.put(
        f"/api/cabinet/team/{member_id}/permissions",
        json={"permissions": {"projects": True}},
        headers=auth(token),
    )
    patch = client.patch(
        f"/api/projects/{lead_card['id']}", json={"goal": "новая цель"}, headers=auth(part_token)
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["goal"] == "новая цель"
    # удаление всё равно запрещено (право только на редактирование)
    assert (
        client.delete(f"/api/projects/{lead_card['id']}", headers=auth(part_token)).status_code
        == 403
    )

    # сохранённые права лежат в файле руководителя
    team_store = TeamStore(get_settings().runtime_dir / "team")
    assert team_store.member_permissions(lead_id, member_id) == {
        "projects": True, "ontologies": False, "rag": False,
    }


def test_cabinet_projects_hypotheses_done(monkeypatch, tmp_path):
    """Гипотезы прогресс-бара: карточка hyp-{id}.json (навык «Гипотезы
    проекта») отмечает шаг «Гипотезы сформулированы» — поле
    hypotheses_done в GET /api/cabinet/projects; отсутствие карточки и
    раздел-заглушка без причины пропуска поле не включают."""
    from api.runtime import hypotheses as hyp_runtime

    monkeypatch.setenv("AIOS_TOOLS_ROOT", str(tmp_path / "tools"))
    get_settings.cache_clear()

    client = _client()
    token = _register_user(client)
    store = ProjectStore(get_settings().runtime_dir / "projects")
    card = _mk_project(store, UserStore(get_settings().runtime_dir / "users").get_by_login("user@ex.com")["id"])

    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert items[0]["hypotheses_done"] is False

    hyp_dir = tmp_path / "tools" / "hypotheses"
    hyp_dir.mkdir(parents=True)
    full = {k: [{"id": "h1", "statement": "если–то–проверим"}] for k in hyp_runtime.SECTION_KEYS}
    (hyp_dir / f"hyp-{card['id']}.json").write_text(
        json.dumps({"project_id": card["id"], **full}, ensure_ascii=False), encoding="utf-8"
    )
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert items[0]["hypotheses_done"] is True

    # раздел заменён на пропуск без причины — шаг не отмечается
    skipped = {k: [{"id": "h1", "statement": "если–то–проверим"}] for k in hyp_runtime.SECTION_KEYS}
    skipped["dashboards"] = [{"id": "d1", "skipped": True, "skip_reason": ""}]
    (hyp_dir / f"hyp-{card['id']}.json").write_text(
        json.dumps({"project_id": card["id"], **skipped}, ensure_ascii=False), encoding="utf-8"
    )
    items = client.get("/api/cabinet/projects", headers=auth(token)).json()["items"]
    assert items[0]["hypotheses_done"] is False
