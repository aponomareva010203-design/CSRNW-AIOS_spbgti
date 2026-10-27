"""Инструменты конструктора агентов v5 (tests/test_agent_builder_tools.py).

Конструктор создаёт агента-сценарий .md: модель передаёт короткие поля в
save_skill_draft, файл собирает и проверяет платформа (runtime/skill_drafts).
Здесь проверяются: создание черновика, отказ на инструменте, который в чате
не работает, повторное сохранение (upsert по id) и знание реестром всех
инструментов из арсенала навыка «Конструктор агентов».
"""

import asyncio
from pathlib import Path

import pytest

from api.runtime import skill_drafts
from api.runtime.config import get_settings
from api.runtime.skill_tools import skill_tools
from api.runtime.tools import build_default_registry


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    skill_drafts._STORE = None
    yield
    skill_drafts._STORE = None
    get_settings.cache_clear()


OWNER = "a1b2c3d4e5f6"  # формат платформы: 12 hex

# Триггеры — выдуманные основы: конфликт-проверка идёт по живому каталогу
# data/skills.json (datarepo.DATA_DIR), пересечений с реальными агентами быть не должно.
SPEC = {
    "id": "zitract-calc",
    "name": "Расчёт зитрактиона",
    "description": "Считает зитрактион по исходным данным и показывает метод расчёта.",
    "domain": "Зитрактное оборудование",
    "triggers": ["зитрактион", "квазифазов", "псевдобитум"],
    "role": "помогает технологу считать зитрактное оборудование по исходным данным",
    "task": "по исходным данным выдаёт расчёт с показанным методом и сравнение с аналогами",
    "steps": [
        {"title": "Приветствие", "text": "Поздоровайтесь и спросите исходные данные одним вопросом.",
         "choices": ["Назову параметры текстом", "Пришлю таблицу"]},
        {"title": "Расчёт", "text": "Выполните расчёт и покажите формулу с подстановкой значений."},
    ],
    "tools": [
        {"name": "run_calc", "when": "все числа — только через расчёт"},
        {"name": "get_recipe", "when": "методика расчёта, читать до кода"},
    ],
    "rules": ["Не считайте в уме — только расчёт."],
    "final": "Человек получает расчёт с методом. Предложите пересчитать с другими данными.",
}

AVAILABLE = {"run_calc", "get_recipe", "read_data_file", "literature_search", "make_document",
             "save_record", "get_records", "call_agent", "notify"}


def _tools():
    return {t.name: t for t in skill_tools(lambda: AVAILABLE)}


def _call(name: str, args: dict, ctx: dict | None = None) -> dict:
    tool = _tools()[name]
    return asyncio.run(tool.handler(args, ctx if ctx is not None else {"session": {"user_id": OWNER}}))


def test_save_skill_draft_creates_draft(tmp_path):
    result = _call("save_skill_draft", {"skill": SPEC})
    assert result["ok"], result
    assert result["id"] == "zitract-calc"
    assert result["где"] == "Кабинет → Агенты"
    assert result["запуск_для_проверки"] == "draft:zitract-calc"
    md = Path(get_settings().runtime_dir) / "skills" / OWNER / "zitract-calc.md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "# Агент «Расчёт зитрактиона»" in text
    assert "## Шаги" in text


def test_save_skill_draft_requires_login():
    result = _call("save_skill_draft", {"skill": SPEC}, ctx={"session": {}})
    assert result["ok"] is False
    assert "Нужен вход" in result["error"]


def test_save_skill_draft_rejects_agent_only_tool():
    spec = dict(SPEC, tools=[{"name": "call_agent", "when": "передать часть работы другому агенту"}])
    result = _call("save_skill_draft", {"skill": spec})
    assert result["ok"] is False
    assert any("в чате не работает" in e for e in result["errors"])


def test_save_skill_draft_accepts_chat_memory_and_notify():
    spec = dict(SPEC, tools=[
        {"name": "run_calc", "when": "все числа только через расчёт"},
        {"name": "save_record", "when": "копилка находок навыка"},
        {"name": "get_records", "when": "перед сохранением проверить дубли"},
        {"name": "notify", "when": "пользователь просит прислать итог письмом"},
    ])
    result = _call("save_skill_draft", {"skill": spec})
    assert result["ok"], result


def test_save_skill_draft_upsert_and_list():
    first = _call("save_skill_draft", {"skill": SPEC})
    assert first["ok"], first
    spec2 = dict(SPEC, name="Расчёт зитрактиона v2")
    second = _call("save_skill_draft", {"skill": spec2})
    assert second["ok"], second
    assert second["версия"] == first["версия"] + 1

    listed = _call("get_skill_drafts", {})
    assert listed["ok"], listed
    assert [d["id"] for d in listed["черновики"]] == ["zitract-calc"]

    one = _call("get_skill_drafts", {"id": "zitract-calc"})
    assert one["ok"], one
    assert "Расчёт зитрактиона v2" in one["текст"]


def test_registry_knows_all_constructor_tools(monkeypatch):
    """Инструменты из арсенала конструктора существуют на сервере: если
    какой-то не зарегистрирован, конструктор начнёт обещать несуществующее.
    Расчётные инструменты регистрируются только при установленной песочнице
    (на сервере она есть) — здесь её наличие имитируем."""
    from api.runtime import compute_sandbox

    monkeypatch.setattr(compute_sandbox, "sandbox_available", lambda: True)
    registry = build_default_registry(get_settings().runtime_dir / "tools")
    for name in ("run_calc", "get_recipe", "read_data_file", "literature_search",
                 "compound_lookup", "make_document", "get_projects",
                 "get_catalog", "read_file", "list_files", "get_organization",
                 "read_document", "search_knowledge", "get_rag",
                 "get_ontologies", "format_table", "chart_generate",
                 "save_skill_draft", "get_skill_drafts"):
        assert registry.has(name), f"инструмент не зарегистрирован: {name}"
    # этап «агент с интерфейсом» убран: этих инструментов в чате больше нет
    for name in ("save_agent", "get_agents"):
        assert not registry.has(name), f"инструмент должен быть убран: {name}"


def test_delete_draft_moves_files_to_trash(tmp_path):
    """Удаление черновика мягкое: файлы переезжают в закрытый архив
    skills-removed/{user}/ с меткой deleted_at/deleted_by, живой стор
    черновик больше не отдаёт."""
    store = skill_drafts.get_store()
    text = "---\nname: На удаление\nversion: 1\ntriggers:\n  - псевдотриггер\n---\n\n# Черновик\n"
    store.save(OWNER, "trash-draft", text, name="На удаление", description="тест удаления")
    assert store.get(OWNER, "trash-draft") is not None

    assert store.delete(OWNER, "trash-draft", deleted_by=OWNER) is True

    assert store.get(OWNER, "trash-draft") is None
    assert store.list(OWNER) == []
    trash = get_settings().runtime_dir / "skills-removed" / OWNER
    meta = __import__("json").loads((trash / "trash-draft.json").read_text(encoding="utf-8"))
    assert meta["deleted_by"] == OWNER and meta["deleted_at"] > 0
    assert (trash / "trash-draft.md").is_file()
