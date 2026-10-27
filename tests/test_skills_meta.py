"""Навыки 2.0: frontmatter-метаданные, подсказки в чате, карта этапов."""

from fastapi.testclient import TestClient

from api.runtime import datarepo
from api.runtime.config import get_settings


def test_frontmatter_parsed_and_body_stripped():
    """Шаблон агента несёт рантайм-метаданные; тело начинается
    с заголовка, а не с YAML-блока (frontmatter — не часть протокола)."""
    meta = datarepo.skill_meta("portfolio-analyst")
    assert meta.get("stage") == "portfolio"
    assert isinstance(meta.get("triggers"), list) and meta["triggers"]
    assert meta.get("status") == "template"

    body = datarepo.skill_body(datarepo.get_skill("portfolio-analyst"))
    assert not body.startswith("---")
    assert body.startswith("# Агент")


def test_suggest_skills_matches_triggers():
    """Триггер-фраза в тексте предлагает навык; короткий текст и exclude
    отсекаются; регистр не важен."""
    found = datarepo.suggest_skills("Нужен АНАЛИЗ ПАТЕНТОВ нашего университета")
    assert [s["id"] for s in found] == ["portfolio-analyst"]

    assert datarepo.suggest_skills("патент") == []  # короче порога
    assert datarepo.suggest_skills("коротко") == []

    query = "подготовьте план достройки экосистемы вуза"
    assert [s["id"] for s in datarepo.suggest_skills(query)] == ["ecosystem-analyst"]
    excluded = datarepo.suggest_skills(query, exclude=["ecosystem-analyst"])
    assert all(s["id"] != "ecosystem-analyst" for s in excluded)


def test_stage_map_chain_and_hot_reload(tmp_path, monkeypatch):
    """Карта этапов читается из data/stage-map.json, next ведёт по цепочке
    конвейера; правка файла подхватывается без перезапуска (кэш по mtime)."""
    stages = datarepo.stage_map()["stages"]
    keys = [s["key"] for s in stages]
    assert keys[0] == "onboarding" and "portfolio" in keys and "agents" in keys

    portfolio = datarepo.stage_entry("portfolio")
    assert portfolio["next"] == "ecosystem"
    assert datarepo.stage_of_skill("portfolio-analyst")["key"] == "portfolio"
    assert datarepo.stage_of_skill("venture-builder")["key"] == "ecosystem"
    assert datarepo.stage_next("portfolio")["key"] == "ecosystem"
    # каждый навык карты есть в каталоге, а его frontmatter указывает на свой этап
    for stage in stages:
        for skill_id in stage["skills"]:
            assert datarepo.get_skill(skill_id), skill_id
            assert datarepo.stage_of_skill(skill_id)["key"] == stage["key"], skill_id
    # неизвестный навык этапа не имеет
    assert datarepo.stage_of_skill("no-such-skill") is None

    # hot-reload: подменяем каталог данных, меняем файл — карта обновляется
    monkeypatch.setattr(datarepo, "DATA_DIR", tmp_path)
    (tmp_path / "stage-map.json").write_text(
        '{"stages": [{"key": "x", "name_ru": "Икс", "skills": [], "next": null}]}',
        encoding="utf-8",
    )
    assert datarepo.stage_map()["stages"][0]["key"] == "x"


def test_skill_references_go_to_prompt_lazy(monkeypatch):
    """references из frontmatter не вшиваются целиком: в промпте появляется
    список путей с инструкцией читать их read_file по необходимости."""
    from api.runtime.prompts import build_system_prompt

    monkeypatch.setattr(
        datarepo,
        "skill_meta",
        lambda skill_id: {"references": ["skills/references/agent-archetypes.md"]},
    )
    prompt = build_system_prompt(skill_ids=["agent-builder"])
    assert "skills/references/agent-archetypes.md" in prompt
    assert "read_file" in prompt


def test_skills_suggest_endpoint_public():
    """GET /api/chat/skills-suggest — лёгкий публичный эндпоинт: работает
    без LLM и авторизации, короткий запрос возвращает пустой список."""
    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/chat/skills-suggest", params={"q": "нужен трекер стартапов студии"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["id"] == "venture-builder"

    short = client.get("/api/chat/skills-suggest", params={"q": "трекер"})
    assert short.status_code == 200
    assert short.json()["items"] == []

    stage_map = client.get("/api/stage-map")
    assert stage_map.status_code == 200
    assert stage_map.json()["stages"]


def test_admin_only_skill_hidden_from_suggestions_and_launch():
    """«Конструктор моделей» (admin_only): не подсказывается в чате и не
    запускается по id обычным пользователем; администратору — доступен."""
    from api.main import app
    from api.routers import auth as auth_router
    from api.runtime import skill_drafts, users

    assert datarepo.is_admin_only(datarepo.get_skill("model-builder"))

    # подсказки: обычному пользователю и анониму — нет, админу — да
    assert all(s["id"] != "model-builder"
               for s in datarepo.suggest_skills("нужно создать модель по опытам"))
    admin_ids = [s["id"] for s in datarepo.suggest_skills("нужно создать модель по опытам", is_admin=True)]
    assert "model-builder" in admin_ids

    # запуск по id: гейт skill_available
    assert not skill_drafts.skill_available("model-builder", "uid-1")
    assert skill_drafts.skill_available("model-builder", "uid-1", is_admin=True)

    # эндпоинт: без токена — как обычный пользователь
    client = TestClient(app)
    items = client.get(
        "/api/chat/skills-suggest", params={"q": "нужно создать модель по опытам"}
    ).json()["items"]
    assert all(i["id"] != "model-builder" for i in items)

    # эндпоинт: админ получает агента в подсказках (seed-админ свежего рантайма)
    store = users.UserStore(get_settings().runtime_dir / "users")
    admin_id = store.get_by_login("a.ponomareva@csr-nw.ru")["id"]
    token = auth_router.get_token_store().issue(admin_id)
    items = client.get(
        "/api/chat/skills-suggest",
        params={"q": "нужно создать модель по опытам"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["items"]
    assert any(i["id"] == "model-builder" for i in items)


def test_chat_rejects_admin_only_skill_for_regular_user(tmp_path, monkeypatch):
    """POST /api/chat с skill_ids=["model-builder"] от обычного пользователя —
    400 «Неизвестный навык», от администратора — сессия создаётся."""
    from api.main import app
    from api.routers import chat as chat_router
    from api.runtime.providers import MockProvider

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_provider.cache_clear()
    monkeypatch.setattr(chat_router, "get_provider", lambda *a, **k: MockProvider())

    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "соберём модель", "skill_ids": ["model-builder"]})
    assert resp.status_code == 400 and "Неизвестный навык" in resp.json()["detail"]

    from api.runtime.users import UserStore

    store = UserStore(get_settings().runtime_dir / "users")
    admin_id = store.get_by_login("a.ponomareva@csr-nw.ru")["id"]
    from api.routers import auth as auth_router

    token = auth_router.get_token_store().issue(admin_id)
    resp = client.post("/api/chat", json={"message": "соберём модель", "skill_ids": ["model-builder"]},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()
    try:
        chat_router.get_provider.cache_clear()
    except AttributeError:
        pass


def test_chat_sets_session_stage_for_skill(tmp_path, monkeypatch):
    """Запуск навыка этапа ставит сессии stage (whitelist по карте);
    этап приезжает и в первом SSE-событии session."""
    from api.main import app
    from api.routers import chat as chat_router
    from api.runtime.providers import MockProvider

    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_provider.cache_clear()
    monkeypatch.setattr(chat_router, "get_provider", lambda *a, **k: MockProvider())

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "разберём портфель РИД", "skill_ids": ["portfolio-analyst"]},
    )
    assert resp.status_code == 200
    assert '"stage":"portfolio"' in resp.text.replace(" ", "")

    sid = client.get("/api/sessions").json()["items"][0]["id"]
    full = client.get(f"/api/sessions/{sid}").json()
    assert full["stage"] == "portfolio"
    kinds = [e.get("kind") for e in full.get("decision_log", [])]
    assert "stage_set" in kinds

    get_settings.cache_clear()
    chat_router.get_store.cache_clear()
    chat_router.get_registry.cache_clear()
    try:
        chat_router.get_provider.cache_clear()
    except AttributeError:
        pass


def test_approval_registry_marks_configured_tools(tmp_path, monkeypatch):
    """AIOS_APPROVAL_TOOLS управляет маркировкой: по умолчанию gate выключен
    (решение пользователя 23.09.2026 — без подтверждений), явный список —
    помечены только перечисленные."""
    from api.runtime.tools import build_default_registry

    # по умолчанию выключено — инструменты исполняются сразу
    registry = build_default_registry(tmp_path / "tools")
    assert not get_settings().approval_tools
    assert not any(registry.requires_approval(n) for n in registry.names())

    monkeypatch.setenv("AIOS_APPROVAL_TOOLS", "notify")
    get_settings.cache_clear()
    narrow = build_default_registry(tmp_path / "tools2")
    assert narrow.requires_approval("notify")
    assert not narrow.requires_approval("write_file")

    monkeypatch.setenv("AIOS_APPROVAL_TOOLS", "write_file")
    get_settings.cache_clear()
    gate_on = build_default_registry(tmp_path / "tools3")
    assert gate_on.requires_approval("write_file")
    assert not gate_on.requires_approval("read_file")

    monkeypatch.delenv("AIOS_APPROVAL_TOOLS", raising=False)
    get_settings.cache_clear()


TEMPLATE_IDS = ("portfolio-analyst", "ecosystem-analyst", "venture-builder")
TEMPLATE_SECTIONS = ("Назначение", "Роль", "Навыки (skills)", "Входные данные",
                     "Выходные данные", "Ограничения")


def test_research_skills_cards_and_bodies(monkeypatch):
    """Шаблоны агентов проректора: карточки в каталоге (категория general,
    файл data/skills/<id через _>.md), frontmatter с триггерами, обязательные
    разделы тела; все инструменты, упомянутые в «Навыках», есть в рантайме."""
    import re

    from api.runtime import compute_sandbox
    from api.runtime.tools import build_default_registry

    catalog = {s["id"]: s for s in datarepo.skills()}
    mentioned = set()
    for skill_id in TEMPLATE_IDS:
        card = catalog.get(skill_id)
        assert card and card.get("category") == "general" and card.get("source") == "template"
        assert card["file_url"] == f"/data/skills/{skill_id.replace('-', '_')}.md"
        assert datarepo.skill_path(card) is not None, f"{skill_id}: нет файла"
        meta = datarepo.skill_meta(skill_id)
        assert meta.get("name") == card["name"]
        assert isinstance(meta.get("triggers"), list) and meta["triggers"]
        body = datarepo.skill_body(card)
        for section in TEMPLATE_SECTIONS:
            assert f"## {section}" in body, f"{skill_id}: нет раздела «{section}»"
        skills_part = body.split("## Навыки (skills)", 1)[1].split("\n## ", 1)[0]
        mentioned |= set(re.findall(r"`([a-z_]+)`", skills_part))

    # расчётные инструменты регистрируются только при установленной песочнице —
    # проверяем полный арсенал, как на сервере
    monkeypatch.setattr(compute_sandbox, "sandbox_available", lambda: True)
    registry = build_default_registry(get_settings().tools_root)
    assert mentioned, "в разделах «Навыки» должны быть инструменты платформы"
    for tool_name in sorted(mentioned):
        assert registry.has(tool_name), f"инструмент не зарегистрирован: {tool_name}"


def test_research_skills_in_prompt_and_suggestions():
    from api.runtime.prompts import build_system_prompt

    prompt = build_system_prompt(skill_ids=["portfolio-analyst"])
    assert "Портфельный аналитик" in prompt and "read_data_file" in prompt
    prompt = build_system_prompt(skill_ids=["ecosystem-analyst"])
    assert "Аналитик экосистемы" in prompt and "get_organization" in prompt

    found = datarepo.suggest_skills("какие бесперспективные патенты стоит закрыть в этом году")
    assert [s["id"] for s in found][0] == "portfolio-analyst"
    found = datarepo.suggest_skills("оцените инновационная экосистема вуза: чего не хватает вузу")
    assert [s["id"] for s in found][0] == "ecosystem-analyst"