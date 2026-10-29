"""Демо-содержимое «Данные и знания» по теме платформы и текстовый поиск RAG.

Каталоги онтологий и RAG-баз — про управление инновациями в вузе; графы
онтологий проходят валидацию публикации; у каждой базы есть корпус; поиск
search_corpus без Qdrant и эмбеддингов работает текстовым режимом."""

import asyncio
import json
from pathlib import Path

import pytest

from api.runtime import indexer, projects, text_search
from api.runtime.config import get_settings
from api.runtime.ontologies import validate_graph

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_QDRANT_URL", "http://127.0.0.1:9")  # заведомо недоступен
    get_settings.cache_clear()
    indexer.get_qdrant_client.cache_clear()
    yield
    get_settings.cache_clear()
    indexer.get_qdrant_client.cache_clear()


def _items(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))["items"]


def test_ontologies_catalog_matches_graphs():
    items = _items("ontologies.json")
    assert len(items) >= 3
    for card in items:
        graph = json.loads((DATA / card["file_url"].lstrip("/").removeprefix("data/")).read_text(encoding="utf-8"))
        assert validate_graph(graph, require_complete=True) == []
        assert card["classes_count"] == len(graph["classes"])
        assert (DATA / "icons" / f"{card['id']}.svg").is_file()


def test_rag_bases_have_corpus_and_known_ontology():
    onto_ids = {o["id"] for o in _items("ontologies.json")}
    for base in _items("rag_bases.json"):
        assert text_search.has_corpus(base["id"])
        assert base["documents_count"] == len(base["documents"])
        assert base["ontology_id"] in onto_ids


def test_no_legacy_chemistry_content():
    blob = json.dumps(_items("ontologies.json") + _items("rag_bases.json") + _items("models.json"),
                      ensure_ascii=False).lower()
    for word in ("polymer", "полимер", "smiles", "protac", "chemllm"):
        assert word not in blob


def test_text_search_ranks_relevant_document():
    hits = text_search.search("rag-1a0e5c0101", "срок решения работодателя по служебному изобретению", limit=3)
    assert hits and hits[0]["filename"] == "01_sluzhebnyj_rid.md"


def test_search_falls_back_to_text_without_qdrant_and_embeddings():
    result = asyncio.run(indexer.search("rag-1a0e5c0103", "грант Студенческий стартап", ontology_id="onto-1a0e5c0004"))
    assert result["search_mode"] == "text"
    assert result["hits"]


def test_search_corpus_tool_on_catalog_base():
    from api.runtime.tools import build_default_registry

    reg = build_default_registry(get_settings().tools_root)
    out = asyncio.run(reg._tools["search_corpus"].handler({"rag_id": "rag-1a0e5c0102", "query": "застой УГТ"}, None))
    assert out["ok"] and out["search_mode"] == "text" and out["count"] > 0


def test_uploaded_text_is_searchable():
    text_search.save_text("rag-abcdef0123", "doc-0123456789", "regl.txt", "Положение о стартап-студии: трекер ведёт команду.")
    hits = text_search.search("rag-abcdef0123", "трекер стартап-студии")
    assert hits and hits[0]["filename"] == "regl.txt"
    text_search.delete_text("rag-abcdef0123", "doc-0123456789")
    assert text_search.search("rag-abcdef0123", "трекер") == []


def test_project_type_validation(tmp_path):
    store = projects.ProjectStore(tmp_path / "prj")
    card = store.save("a" * 12, {"title": "Студия", "project_type": "initiative", "kpi": "8 стартапов"})
    assert card["project_type"] == "initiative" and card["kpi"] == "8 стартапов"
    with pytest.raises(projects.ProjectError):
        store.save("a" * 12, {"title": "x", "project_type": "chemistry"})
