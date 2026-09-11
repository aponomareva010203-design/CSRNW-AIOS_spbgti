"""Тесты страницы «Статус пилотного проекта» (пилот, api/routers/pilot.py).

Строки таблицы формируются на сервере из активных карточек пользователей
(«Мои проекты», статус active): GET /api/pilot/projects — публичный эндпоинт
без персональных полей карточки (бюджет, участники, задел, user_id наружу
не отдаются), ФИО руководителя — из анкеты «Кабинета» владельца, иначе имя
учётной записи. PATCH /api/pilot/projects/{id} — смена статуса артефакта,
только роль «Администратор», с валидацией ключа артефакта и статуса 0–3.
Блок artifacts карточки: нормализация clean_artifacts (все пять артефактов,
статус из наличия гипотезы, ProjectError на неверный статус), замена блока
целиком при частичном сохранении, сохранение presentation при PATCH.
"""

import json

import pytest

from api.routers import auth as auth_router
from api.runtime import cabinet as cabinet_runtime
from api.runtime import projects
from api.runtime.config import get_settings
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"

CARD = {
    "title": "Синтез полимерных мембран",
    "goal": "Повысить выход готового продукта с 85% до 92%.",
    "budget": "грант РНФ, до 500 тыс. руб.",
    "participants": "Иванов И.И., Петров П.П.",
    "presentation": True,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime-каталог, свежие кэши хранилищ."""
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    cabinet_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    cabinet_runtime.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _login(client, login=ADMIN_LOGIN, password=ADMIN_PASSWORD):
    resp = client.post("/api/auth/login", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _admin_id():
    users = UserStore(get_settings().runtime_dir / "users")
    return users.get_by_login(ADMIN_LOGIN)["id"]


# ----- хранилище: блок artifacts -----


def test_clean_artifacts_normalization():
    """Гипотеза без статуса → статус 1; строка вместо словаря; отсечение лишнего."""
    cleaned = projects.clean_artifacts({
        "rag": "Если мы создадим RAG-систему, технолог найдёт режимы быстрее.",
        "models": {"status": 2, "hypothesis": "Модель сократит подбор режима вдвое."},
        "agents": {"hypothesis": ""},
        "unknown": {"status": 3, "hypothesis": "лишний ключ"},
    })
    assert set(cleaned) == set(projects.ARTIFACT_KEYS)
    assert cleaned["rag"] == {
        "status": 1,
        "hypothesis": "Если мы создадим RAG-систему, технолог найдёт режимы быстрее.",
    }
    assert cleaned["models"]["status"] == 2
    assert cleaned["agents"] == {"status": 0, "hypothesis": ""}
    assert cleaned["polygon"] == {"status": 0, "hypothesis": ""}
    assert cleaned["social"] == {"status": 0, "hypothesis": ""}


def test_clean_artifacts_status_validation():
    """Статус-цифра строкой допустим; нечисловой и вне 0–3 — ProjectError."""
    assert projects.clean_artifacts({"social": {"status": "3"}})["social"]["status"] == 3
    with pytest.raises(projects.ProjectError):
        projects.clean_artifacts({"rag": {"status": 7}})
    with pytest.raises(projects.ProjectError):
        projects.clean_artifacts({"rag": {"status": "мусор"}})
    assert projects.clean_artifacts(None)["rag"]["status"] == 0
    assert projects.clean_artifacts("мусор")["rag"]["status"] == 0
    long = projects.clean_artifacts({"rag": "x" * 3000})
    assert len(long["rag"]["hypothesis"]) == projects.ARTIFACT_HYPOTHESIS_MAX_CHARS


def test_store_saves_artifacts_block_replaced_whole(tmp_path):
    """Блок artifacts сохраняется и заменяется целиком при частичном PATCH."""
    store = projects.ProjectStore(tmp_path / "projects")
    saved = store.save(
        "abc123def456",
        dict(CARD, artifacts={"rag": {"status": 1, "hypothesis": "Гипотеза RAG."}}),
    )
    assert saved["artifacts"]["rag"] == {"status": 1, "hypothesis": "Гипотеза RAG."}

    again = store.save(
        "abc123def456",
        {"project_id": saved["id"], "artifacts": {"models": {"status": 2, "hypothesis": "Гипотеза модели."}}},
    )
    assert again["artifacts"]["models"]["status"] == 2
    # rag в этом вызове не передавался — блок заменён целиком
    assert again["artifacts"]["rag"] == {"status": 0, "hypothesis": ""}


def test_store_preserves_presentation_on_partial_update(tmp_path):
    """Частичный PATCH без presentation не сбрасывает флаг."""
    store = projects.ProjectStore(tmp_path / "projects")
    saved = store.save("abc123def456", dict(CARD))
    again = store.save("abc123def456", {"project_id": saved["id"], "goal": "Новая цель"})
    assert again["presentation"] is True


# ----- API: строки таблицы пилота -----


def test_pilot_list_public_no_personal_fields():
    """Публичный GET: только active-карточки, без персональных полей."""
    client = _client()
    uid = _admin_id()
    store = projects.get_store()
    # черновик не попадает в таблицу
    store.save(uid, dict(CARD, title="Черновик онбординга"))
    # активная карточка — попадает
    active = store.save(uid, dict(CARD))
    store.save(uid, {"project_id": active["id"], "status": "active"})

    cabinet_runtime.get_store().save_profile(
        uid, last_name="Харитонов", first_name="Максим", patronymic="Игоревич"
    )

    resp = client.get("/api/pilot/projects")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["title"] for i in items] == [CARD["title"]]
    row = items[0]
    assert set(row) == {"id", "title", "lead", "artifacts"}
    assert row["id"] == active["id"]
    assert row["lead"] == "Харитонов Максим Игоревич"
    assert row["artifacts"]["rag"] == {"status": 0, "hypothesis": ""}
    # персональные поля не утекают
    assert "user_id" not in row and "budget" not in row and "participants" not in row


def test_pilot_lead_falls_back_to_account_name():
    """Без анкеты «Кабинета» ФИО руководителя — имя учётной записи."""
    client = _client()
    users = UserStore(get_settings().runtime_dir / "users")
    users.create("petrov@csr-nw.ru", "pass1234", name="Петров П.", confirmed=True)
    uid = users.get_by_login("petrov@csr-nw.ru")["id"]
    card = projects.get_store().save(uid, dict(CARD))
    projects.get_store().save(uid, {"project_id": card["id"], "status": "active"})

    row = client.get("/api/pilot/projects").json()["items"][0]
    assert row["lead"] == "Петров П."


# ----- API: гипотезы из JSON-карточки (data/hypotheses) -----


def test_pilot_row_merges_published_hypotheses(monkeypatch, tmp_path):
    """Опубликованная карточка гипотез даёт статус 1 и текст гипотезы.

    Разделы карточки гипотез маппятся на колонки таблицы: ontology/rag →
    rag, models → models, agents → agents, polygons → polygon; dashboards
    и «не начатые» без гипотезы остаются 0. Черновик (published: false)
    и явно выставленные статусы (2, 3) не трогаются.
    """
    monkeypatch.setenv("AIOS_TOOLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        uid = _admin_id()
        card = projects.get_store().save(
            uid, dict(CARD, artifacts={"models": {"status": 2, "hypothesis": ""}})
        )
        projects.get_store().save(uid, {"project_id": card["id"], "status": "active"})

        hyp_dir = tmp_path / "hypotheses"
        hyp_dir.mkdir()
        (hyp_dir / f"hyp-{card['id']}.json").write_text(
            json.dumps({
                "project_id": card["id"],
                "status": "active",
                "published": True,
                "ontology": [{"id": "ont-1", "title": "Онтология", "statement": "Если онтология — то словарь.", "skipped": False}],
                "rag": [{"id": "rag-1", "title": "", "statement": "Если RAG — то поиск.", "skipped": False}],
                "models": [{"id": "mdl-1", "title": "Модель", "statement": "Если модель — то прогноз.", "skipped": False}],
                "agents": [{"id": "agt-1", "title": "Агент", "statement": "Если агент — то рутина.", "skipped": False}],
                "polygons": [{"id": "plg-1", "title": "Полигон", "statement": "Если полигон — то прогоны.", "skipped": True, "skip_reason": "позже"}],
                "dashboards": [{"id": "dsh-1", "title": "Дашборд", "statement": "Если дашборд — то статусы.", "skipped": False}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        row = client_get_item(card["id"])
        art = row["artifacts"]
        assert art["rag"] == {"status": 1, "hypothesis": "Онтология: Если онтология — то словарь.\nЕсли RAG — то поиск."}
        assert art["models"]["status"] == 2  # выставленный администратором статус не тронут
        assert art["agents"]["status"] == 1 and "Если агент" in art["agents"]["hypothesis"]
        assert art["polygon"] == {"status": 0, "hypothesis": ""}  # гипотеза помечена skipped
        assert art["social"] == {"status": 0, "hypothesis": ""}   # dashboards — не колонка пилота

        # черновик (published: false) таблицу не меняет
        data = json.loads((hyp_dir / f"hyp-{card['id']}.json").read_text(encoding="utf-8"))
        data["published"] = False
        (hyp_dir / f"hyp-{card['id']}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        row = client_get_item(card["id"])
        assert row["artifacts"]["rag"] == {"status": 0, "hypothesis": ""}
        assert row["artifacts"]["models"]["status"] == 2
    finally:
        get_settings.cache_clear()


def client_get_item(project_id):
    from fastapi.testclient import TestClient

    from api.main import app

    items = TestClient(app).get("/api/pilot/projects").json()["items"]
    return next(i for i in items if i["id"] == project_id)


# ----- API: смена статуса артефакта -----


def _active_card_uid():
    uid = _admin_id()
    store = projects.get_store()
    card = store.save(
        uid,
        dict(CARD, artifacts={"rag": {"status": 1, "hypothesis": "Гипотеза RAG."}}),
    )
    store.save(uid, {"project_id": card["id"], "status": "active"})
    return card["id"]


def test_pilot_patch_requires_admin_and_validates():
    client = _client()
    token = _login(client)
    project_id = _active_card_uid()
    url = f"/api/pilot/projects/{project_id}"

    # без токена — 401; не администратор — 403
    assert client.patch(url, json={"artifact": "rag", "status": 2}).status_code == 401
    users = UserStore(get_settings().runtime_dir / "users")
    users.create("petrov@csr-nw.ru", "pass1234", name="Петров П.", confirmed=True)
    other = _login(client, "petrov@csr-nw.ru", "pass1234")
    assert client.patch(url, headers=auth(other), json={"artifact": "rag", "status": 2}).status_code == 403

    # неверный артефакт и статус — 422
    bad_artifact = client.patch(
        url, headers=auth(token), json={"artifact": "ontology", "status": 2}
    )
    assert bad_artifact.status_code == 422
    bad_status = client.patch(url, headers=auth(token), json={"artifact": "rag", "status": 5})
    assert bad_status.status_code == 422

    # неизвестный id и черновик — 404
    assert client.patch(
        "/api/pilot/projects/prj-deadbeef", headers=auth(token), json={"artifact": "rag", "status": 2}
    ).status_code == 404
    draft = projects.get_store().save(_admin_id(), dict(CARD, title="Черновик"))
    assert client.patch(
        f"/api/pilot/projects/{draft['id']}", headers=auth(token), json={"artifact": "rag", "status": 2}
    ).status_code == 404


def test_pilot_patch_sets_status_and_keeps_hypothesis():
    """Админ меняет статус артефакта: гипотеза и остальные артефакты целы."""
    client = _client()
    token = _login(client)
    project_id = _active_card_uid()
    url = f"/api/pilot/projects/{project_id}"

    resp = client.patch(url, headers=auth(token), json={"artifact": "rag", "status": 3})
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["artifacts"]["rag"]["status"] == 3
    assert row["artifacts"]["rag"]["hypothesis"] == "Гипотеза RAG."
    assert row["artifacts"]["models"]["status"] == 0

    # изменение видно в публичном списке
    listed = client.get("/api/pilot/projects").json()["items"]
    assert listed[0]["artifacts"]["rag"]["status"] == 3

    # возврат в «не начато»
    back = client.patch(url, headers=auth(token), json={"artifact": "rag", "status": 0})
    assert back.status_code == 200 and back.json()["artifacts"]["rag"]["status"] == 0


def test_owner_sets_artifacts_via_project_patch():
    """Владелец карточки заполняет гипотезы через PATCH /api/projects/{id}."""
    client = _client()
    token = _login(client)
    uid = _admin_id()
    card = projects.get_store().save(uid, dict(CARD))
    projects.get_store().save(uid, {"project_id": card["id"], "status": "active"})

    resp = client.patch(
        f"/api/projects/{card['id']}",
        headers=auth(token),
        json={"artifacts": {"polygon": "Если мы создадим полигон, отсеем плохие режимы до натуры."}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifacts"]["polygon"]["status"] == 1

    row = client.get("/api/pilot/projects").json()["items"][0]
    assert row["artifacts"]["polygon"]["hypothesis"].startswith("Если мы создадим полигон")
    assert row["artifacts"]["polygon"]["status"] == 1



# ----- синхронизация артефактов с онтологиями и RAG-базами -----

ONTO_MINI = {
    "name": "Онтология мембран",
    "domain": "Мембраны",
    "description": "Мини-онтология для теста синхронизации.",
    "author": "Тестов Т. Т.",
    "competency_questions": [
        {"id": "cq-1", "question": "Из чего состоит мембрана?", "coverage": []}
    ],
    "classes": [
        {"id": "cls-membrane", "name": "Мембрана", "description": "Плёнка.", "parent": None},
        {"id": "cls-layer", "name": "Слой", "description": "Слой мембраны.", "parent": None},
    ],
    "relations": [
        {
            "id": "rel-has-layer",
            "name": "имеет слой",
            "domain_class": "cls-membrane",
            "range_class": "cls-layer",
        }
    ],
}

RAG_MINI = {
    "name": "База по мембранам",
    "description": "Корпус статей по полимерным мембранам для теста.",
    "typical_queries": ["Какой растворитель выбрать?"],
    "documents": [{"doc_id": "doc-aabbccddeeff", "filename": "a.md", "status": "ready"}],
}


def test_bump_artifact_status_moves_forward_only():
    """bump_artifact_status: статус только растёт, неизвестный проект — False."""
    uid = _admin_id()
    store = projects.get_store()
    card = store.save(uid, dict(CARD))
    store.save(uid, {"project_id": card["id"], "status": "active"})

    assert projects.bump_artifact_status(card["id"], "rag", 2) is True
    saved = store.get(uid, card["id"])
    assert saved["artifacts"]["rag"]["status"] == 2

    # понижать нельзя: админский/владельческий статус выше min_status не трогаем
    assert projects.bump_artifact_status(card["id"], "rag", 1) is False
    assert projects.bump_artifact_status(card["id"], "rag", 3) is True
    assert store.get(uid, card["id"])["artifacts"]["rag"]["status"] == 3

    assert projects.bump_artifact_status("prj-00000000", "rag", 3) is False
    assert projects.bump_artifact_status(card["id"], "нет-такого", 3) is False


def test_ontology_and_rag_saves_sync_pilot_artifact(monkeypatch, tmp_path):
    """Онтология и RAG-база проекта сами двигают колонку rag таблицы пилота.

    Сохранение онтологии с project_id ставит «в процессе создания» (2),
    публикация RAG-базы — «завершено» (3): кейс Аксенова (онтология и база
    созданы, статус в таблице не изменился) больше не воспроизводится.
    """
    from api.runtime import ontologies, rag

    for module, env in (
        (ontologies, "AIOS_ONTOLOGY_DATA_DIR"),
        (rag, "AIOS_RAG_DATA_DIR"),
    ):
        monkeypatch.setenv(env, str(tmp_path / "data"))
        module.get_store.cache_clear()
    try:
        uid = _admin_id()
        store = projects.get_store()
        card = store.save(uid, dict(CARD, artifacts={"rag": {"status": 1}}))
        store.save(uid, {"project_id": card["id"], "status": "active"})

        ontologies.get_store().save(uid, dict(ONTO_MINI, project_id=card["id"]))
        assert store.get(uid, card["id"])["artifacts"]["rag"]["status"] == 2

        rag_store = rag.get_store()
        draft = rag_store.save(uid, dict(RAG_MINI, project_id=card["id"]))
        assert store.get(uid, card["id"])["artifacts"]["rag"]["status"] == 2

        rag_store.save(uid, {"rag_id": draft["id"], "status": "active"})
        assert store.get(uid, card["id"])["artifacts"]["rag"]["status"] == 3
    finally:
        ontologies.get_store.cache_clear()
        rag.get_store.cache_clear()
