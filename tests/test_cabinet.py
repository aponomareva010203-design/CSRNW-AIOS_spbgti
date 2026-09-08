"""Смоук-тесты страницы «Кабинет».

Блок «Данные пользователя» (GET|PUT /api/cabinet/profile): только
собственные данные по Bearer-токену, лимиты полей. Скрытый блок
«Контекст о пользователе» через API не отдаётся. Планировщик
(run_user_context_job): генерация по fake-провайдеру, пропуски
по расписанию, дайджесту и отсутствию материала. Удаление учётной
записи администратором чистит блоки кабинета.
"""

import asyncio
import json
import time

import pytest

from api.runtime.cabinet import CabinetError, CabinetStore, full_name, get_store
from api.runtime.config import get_settings
from api.runtime.sessions import SessionStore
from api.runtime.user_context import run_user_context_job
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime-каталог, свежие кэши хранилищ."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_store.cache_clear()
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
        "/api/auth/register", json={"login": login, "password": password, "name": name}
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

    # никаких других кабинетных эндпоинтов, отдающих скрытый блок
    paths = client.get("/openapi.json").json()["paths"]
    assert [p for p in paths if p.startswith("/api/cabinet")] == ["/api/cabinet/profile"]


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

    async def stream_chat(self, messages, tools=None):
        self.calls.append(list(messages))
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