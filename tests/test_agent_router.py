"""Автоподбор агента: каталог в промпте и use_agent."""
import asyncio

from api.runtime import agent_router

CARDS = [
    {"id": "recipe-scout", "name": "Разведчик рецептур", "description_ru": "Подбирает рецептуры."},
    {"id": "rag-builder", "name": "Конструктор RAG", "description_ru": "Собирает RAG-базу."},
]


def _patch(monkeypatch):
    monkeypatch.setattr(agent_router.datarepo, "skills", lambda: list(CARDS))
    monkeypatch.setattr(agent_router.datarepo, "get_skill",
                        lambda sid: next((c for c in CARDS if c["id"] == sid), None))
    monkeypatch.setattr(agent_router.datarepo, "skill_body", lambda card, limit=0: "ТЕЛО " + card["id"])


def test_active_skill_ids_merges_without_duplicates():
    s = {"skill_ids": ["a"], "auto_skill_ids": ["a", "b"]}
    assert agent_router.active_skill_ids(s) == ["a", "b"]
    assert agent_router.active_skill_ids(None) == []


def test_catalog_lists_agents_only_when_none_selected(monkeypatch):
    _patch(monkeypatch)
    block = agent_router.catalog_block({})
    assert "recipe-scout" in block and "use_agent" in block
    assert agent_router.catalog_block({"skill_ids": ["x"]}) == ""
    assert agent_router.catalog_block({"auto_skill_ids": ["x"]}) == ""


def test_use_agent_connects_and_returns_body(monkeypatch):
    _patch(monkeypatch)
    session = {}
    res = asyncio.run(agent_router.use_agent({"agent_id": "recipe-scout"}, {"session": session}))
    assert res["ok"] and "ТЕЛО recipe-scout" in res["инструкции"]
    assert session["auto_skill_ids"] == ["recipe-scout"]


def test_use_agent_rejects_unknown_and_manual_choice(monkeypatch):
    _patch(monkeypatch)
    res = asyncio.run(agent_router.use_agent({"agent_id": "nope"}, {"session": {}}))
    assert res["ok"] is False
    res = asyncio.run(agent_router.use_agent({"agent_id": "recipe-scout"}, {"session": {"skill_ids": ["x"]}}))
    assert res["ok"] is False
    assert asyncio.run(agent_router.use_agent({}, None))["ok"] is False


def test_admin_only_agent_hidden_from_regular_users(monkeypatch):
    """«Конструктор моделей» (admin_only): не попадает в подбор и не
    подключается use_agent без роли «Администратор»; для админа — как
    остальные. Карточка не выдаёт своего существования: ошибка та же,
    что у несуществующего агента."""
    cards = CARDS + [{"id": "model-builder", "name": "Конструктор моделей",
                      "description_ru": "Собирает модель.", "admin_only": True}]
    _patch(monkeypatch)
    monkeypatch.setattr(agent_router.datarepo, "skills", lambda: list(cards))
    monkeypatch.setattr(agent_router.datarepo, "get_skill",
                        lambda sid: next((c for c in cards if c["id"] == sid), None))
    monkeypatch.setattr(agent_router, "is_admin_user", lambda uid: uid == "admin-1")

    # обычный пользователь и аноним: агента в подборе нет, use_agent не находит
    assert "model-builder" not in agent_router.catalog_block({})
    assert "model-builder" not in agent_router.catalog_block({"user_id": "user-1"})
    res = asyncio.run(agent_router.use_agent({"agent_id": "model-builder"}, {"session": {"user_id": "user-1"}}))
    assert res["ok"] is False and res["error"] == "агент model-builder не найден среди опубликованных"

    # администратор: агент доступен и в подборе, и через use_agent
    assert "model-builder" in agent_router.catalog_block({"user_id": "admin-1"})
    session = {"user_id": "admin-1"}
    res = asyncio.run(agent_router.use_agent({"agent_id": "model-builder"}, {"session": session}))
    assert res["ok"] and "ТЕЛО model-builder" in res["инструкции"]
    assert session["auto_skill_ids"] == ["model-builder"]
