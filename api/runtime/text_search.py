"""Текстовый (лексический) поиск по RAG-базе — запасной путь (api/runtime/text_search.py).

Основной поиск — векторный: Qdrant + модель эмбеддингов (indexer.search).
На ноутбуке участника сессии Qdrant обычно не поднят, а ключ к API
эмбеддингов может быть не задан — тогда search_corpus без этого модуля
отвечал бы ошибкой. Здесь тот же корпус ищется по словам (BM25 с грубым
стеммингом по началу слова — для русского этого хватает, чтобы «патента» и
«патентов» совпадали), поэтому база знаний работает в любой конфигурации.

Источники текстов:
  - data/rag/corpus/{rag_id}/*.md|*.txt — корпуса демо-баз платформы
    (в git, scripts/seed_innovation_knowledge.py);
  - AIOS_RUNTIME_DIR/rag_texts/{rag_id}/{doc_id}.txt — извлечённый текст
    загруженных пользователями документов (пишет indexer.index_document).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from .config import get_settings

RAG_ID_RE = re.compile(r"^rag-[a-f0-9]{8,12}$")
DOC_ID_RE = re.compile(r"^doc-[a-f0-9]{8,12}$")
TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
STEM_LEN = 6
# служебные слова, которые не несут смысла запроса
STOP = {
    "что", "как", "для", "или", "это", "при", "если", "чем", "его", "она", "они",
    "нас", "вас", "вуз", "вуза", "есть", "быть", "так", "уже", "все", "какие",
    "какой", "какая", "когда", "где", "кто", "под", "над", "без", "the", "and",
}
BM25_K1 = 1.4
BM25_B = 0.75


def _stem(token: str) -> str:
    token = token.lower().replace("ё", "е")
    return token[:STEM_LEN]


def terms(text: str) -> list:
    """Нормализованные термы текста (без стоп-слов и коротких слов)."""
    out = []
    for tok in TOKEN_RE.findall(text or ""):
        low = tok.lower()
        if len(low) < 3 and not low.isdigit():
            continue
        if low in STOP:
            continue
        out.append(_stem(low))
    return out


def corpus_dir(rag_id: str) -> Path:
    return get_settings().rag_data_dir / "rag" / "corpus" / rag_id


def texts_dir(rag_id: str) -> Path:
    return get_settings().runtime_dir / "rag_texts" / rag_id


def save_text(rag_id: str, doc_id: str, filename: str, text: str) -> None:
    """Сохраняет извлечённый текст загруженного документа для текстового поиска."""
    if not RAG_ID_RE.match(rag_id or "") or not DOC_ID_RE.match(doc_id or ""):
        return
    folder = texts_dir(rag_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{doc_id}.txt").write_text(text or "", encoding="utf-8")
    meta_path = folder / "_names.json"
    try:
        names = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        names = {}
    names[doc_id] = filename
    meta_path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")


def delete_text(rag_id: str, doc_id: str) -> None:
    if not RAG_ID_RE.match(rag_id or "") or not DOC_ID_RE.match(doc_id or ""):
        return
    try:
        (texts_dir(rag_id) / f"{doc_id}.txt").unlink()
    except OSError:
        pass


def _documents(rag_id: str) -> list:
    """[(doc_id, filename, text)] — все доступные тексты базы."""
    docs: list = []
    folder = corpus_dir(rag_id)
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in (".md", ".txt") and path.is_file():
                docs.append((path.stem, path.name, path.read_text(encoding="utf-8")))
    folder = texts_dir(rag_id)
    if folder.is_dir():
        try:
            names = json.loads((folder / "_names.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            names = {}
        for path in sorted(folder.glob("doc-*.txt")):
            docs.append((path.stem, names.get(path.stem, path.name), path.read_text(encoding="utf-8")))
    return docs


def has_corpus(rag_id: str) -> bool:
    if not RAG_ID_RE.match(rag_id or ""):
        return False
    return bool(_documents(rag_id))


def search(rag_id: str, query: str, limit: int = 5) -> list:
    """Фрагменты базы, ранжированные BM25 по запросу; пустой список — ничего не нашлось."""
    from .indexer import chunk_text  # общий чанкер с векторной индексацией

    if not RAG_ID_RE.match(rag_id or ""):
        return []
    q_terms = set(terms(query))
    if not q_terms:
        return []
    chunks = []
    for doc_id, filename, text in _documents(rag_id):
        for chunk in chunk_text(text):
            tf = Counter(terms(chunk))
            chunks.append((doc_id, filename, chunk, tf, sum(tf.values())))
    if not chunks:
        return []
    n = len(chunks)
    avg_len = sum(c[4] for c in chunks) / n or 1.0
    df = Counter()
    for c in chunks:
        for t in q_terms:
            if c[3].get(t):
                df[t] += 1
    scored = []
    for doc_id, filename, chunk, tf, length in chunks:
        score = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len))
        if score > 0:
            scored.append((score, doc_id, filename, chunk))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [
        {"text": chunk, "doc_id": doc_id, "filename": filename, "score": round(score, 3)}
        for score, doc_id, filename, chunk in scored[: max(1, min(int(limit), 20))]
    ]
