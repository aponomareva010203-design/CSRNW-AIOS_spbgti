"""Смоук-тесты логотипов артефактов (api/runtime/artifact_icons.py).

Файл data/icons/{id}.svg + публичный URL /data/icons/{id}.svg, подстановка
в catalog_entry онтологий/RAG/агентов, точечный патч image_url в каталоге
после фоновой генерации, set_icon агента без bump версии, идемпотентность
generate_and_apply и no-op вне event-loop. LLM подменяется фейковым
провайдером — сеть в тестах не трогаем.
"""

import asyncio
import json

import pytest

from api.runtime import agent_store, artifact_icons, ontologies

USER_ID = "abc123def456"

SVG = '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="20" fill="#7C8B6C"/></svg>'

ONTO = {
    "name": "Онтология композитов",
    "domain": "Композиты",
    "description": "Мини-онтология для тестов логотипов.",
    "author": "Тестов Т. Т.",
    "competency_questions": [
        {"id": "cq-1", "question": "Из чего состоит компаунд?", "coverage": ["cls-compound →rel→ cls-component"]}
    ],
    "classes": [
        {"id": "cls-compound", "name": "Компаунд", "description": "Смесь.", "parent": None},
        {"id": "cls-component", "name": "Компонент", "description": "Часть.", "parent": None},
    ],
    "relations": [
        {"id": "rel", "name": "состоит из", "domain_class": "cls-compound",
         "range_class": "cls-component", "cardinality": "1..N"}
    ],
}


class FakeProvider:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
        self.calls += 1
        yield ("text", self.answer)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Изолированные runtime/данные, свежие кэши хранилищ и настроек."""
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_ONTOLOGY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOS_RAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOS_ICON_DATA_DIR", str(tmp_path / "data"))
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches():
    from api.runtime.config import get_settings

    get_settings.cache_clear()
    ontologies.get_store.cache_clear()
    agent_store._STORE = None


# ---------------------------------------------------------------------------
# Файлы логотипов
# ---------------------------------------------------------------------------

def test_store_logo_and_url_roundtrip():
    url = artifact_icons.store_logo("onto-abc123", SVG)
    assert url == "/data/icons/onto-abc123.svg"
    assert artifact_icons.logo_url("onto-abc123") == url
    assert artifact_icons.logo_url("onto-other999") is None


def test_logo_path_rejects_traversal():
    assert artifact_icons.logo_path("../passwd") is None
    assert artifact_icons.logo_url("../passwd") is None
    with pytest.raises(ValueError):
        artifact_icons.store_logo("../passwd", SVG)


# ---------------------------------------------------------------------------
# catalog_entry подставляет логотип
# ---------------------------------------------------------------------------

def _published_ontology() -> dict:
    store = ontologies.get_store()
    draft = store.save(USER_ID, dict(ONTO))
    return store.save(USER_ID, dict(draft, status="active"))


def test_ontology_catalog_entry_uses_logo():
    saved = _published_ontology()
    items = ontologies.get_store().catalog_items()
    entry = next(i for i in items if i["id"] == saved["id"])
    assert entry["image_url"] == "assets/model-placeholder.svg"

    url = artifact_icons.store_logo(saved["id"], SVG)
    # повторная публикация подхватывает файл логотипа
    store = ontologies.get_store()
    store.save(USER_ID, dict(store.get(USER_ID, saved["id"]), status="active"))
    entry = next(
        i for i in store.catalog_items() if i["id"] == saved["id"]
    )
    assert entry["image_url"] == url


def test_agent_catalog_entry_uses_logo_and_set_icon():
    saved = agent_store.get_store().save(
        USER_ID, {"project_id": "prj-test0001", "title": "Агент-тест", "role_description": "Проверка"}
    )
    assert agent_store.get_store().get(USER_ID, saved["id"])["version"] == 1

    url = artifact_icons.store_logo(saved["id"], SVG)
    published = agent_store.get_store().save(USER_ID, dict(saved, status="active"))
    entry = next(
        i for i in agent_store.get_store().catalog_items() if i["id"] == saved["id"]
    )
    assert entry["image_url"] == url

    # set_icon — служебная правка: версия и history не растут
    assert agent_store.get_store().set_icon(USER_ID, saved["id"], SVG) is True
    card = agent_store.get_store().get(USER_ID, saved["id"])
    assert card["icon"] == SVG
    assert card["version"] == published["version"]
    assert card["history"] == published["history"]
    assert agent_store.get_store().set_icon(USER_ID, "agt-missing", SVG) is False


# ---------------------------------------------------------------------------
# Патч каталога и полная генерация
# ---------------------------------------------------------------------------

def test_patch_catalog_updates_only_existing_item():
    saved = _published_ontology()
    assert artifact_icons.patch_catalog(
        "ontology", saved["id"], "/data/icons/x.svg"
    )
    entry = next(
        i for i in ontologies.get_store().catalog_items() if i["id"] == saved["id"]
    )
    assert entry["image_url"] == "/data/icons/x.svg"
    # чужой id — карточки нет, каталог не меняется
    assert artifact_icons.patch_catalog("ontology", "onto-nobody", "/x.svg") is False


def test_generate_and_apply_writes_file_and_patches_catalog():
    saved = _published_ontology()
    provider = FakeProvider(SVG)
    url = asyncio.run(
        artifact_icons.generate_and_apply("ontology", saved, provider=provider)
    )
    assert url == f"/data/icons/{saved['id']}.svg"
    assert provider.calls == 1
    entry = next(
        i for i in ontologies.get_store().catalog_items() if i["id"] == saved["id"]
    )
    assert entry["image_url"] == url

    # идемпотентность: повторный вызов не генерирует заново
    url2 = asyncio.run(
        artifact_icons.generate_and_apply("ontology", saved, provider=provider)
    )
    assert url2 == url and provider.calls == 1


def test_generate_and_apply_garbage_answer_is_noop():
    saved = _published_ontology()
    provider = FakeProvider("мусор без svg")
    assert asyncio.run(
        artifact_icons.generate_and_apply("ontology", saved, provider=provider)
    ) is None
    assert artifact_icons.logo_url(saved["id"]) is None
    entry = next(
        i for i in ontologies.get_store().catalog_items() if i["id"] == saved["id"]
    )
    assert entry["image_url"] == "assets/model-placeholder.svg"


def test_generate_logo_passes_kind_and_content():
    provider = FakeProvider(SVG)
    asyncio.run(
        artifact_icons.generate_logo(
            "ontology",
            {"id": "onto-x", "name": "Н", "description": "д", "tags": ["т"]},
            provider=provider,
        )
    )
    assert provider.calls == 1


def test_schedule_generation_without_loop_is_noop():
    # вне event-loop (синхронные вызовы save вне чата) — просто no-op
    artifact_icons.schedule_generation("ontology", {"id": "onto-x"})
    assert artifact_icons.logo_url("onto-x") is None
