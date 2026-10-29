"""Индексация документов RAG-баз (api/runtime/indexer.py).

Пайплайн одного документа: текст → чанки → эмбеддинги (embeddings.py, через
тот же внешний API, что и LLM) → точки в Qdrant (коллекция = collection_name
карточки RAG-базы, см. rag.py) → обновление статуса документа в RagStore
(rag.get_store().update_document()).

Коллекция создаётся лениво при первой индексации документа в конкретную
RAG-базу — размер вектора берётся из реального ответа embeddings.py (сейчас
1536 для text-embedding-3-small), не хардкодится, чтобы не разойтись при
смене модели в rag.EMBEDDING_MODEL.

Обрабатывает документы по одному, не параллельно — сервер ограничен по
памяти (см. README), очередь должна быть последовательной на уровне
вызывающего кода (фоновая задача), не на уровне этого модуля.
"""

import os
import re
import uuid
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from . import ontologies, rag, text_search
from .embeddings import embed_texts, EmbeddingError

CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP_CHARS = 100
EMBED_BATCH_SIZE = 16


class IndexingError(RuntimeError):
    """Ошибка на любом шаге индексации документа."""


class SearchError(RuntimeError):
    """Ошибка на любом шаге поиска по RAG-базе."""


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """Синглтон клиента Qdrant; в тестах сбрасывается cache_clear().

    Адрес — AIOS_QDRANT_URL (в docker-compose — сервис qdrant), по умолчанию
    локальный Qdrant на 6333. Таймаут короткий: если Qdrant не поднят,
    поиск быстро уходит в текстовый режим (text_search), а не висит."""
    return QdrantClient(url=os.getenv("AIOS_QDRANT_URL") or "http://127.0.0.1:6333", timeout=5)


def fetch_collection_points(client: QdrantClient, collection_name: str) -> list:
    """Все точки коллекции с payload и вектором (постранично через scroll).

    Общая реализация для сверки/актуализации разметки классов онтологии
    (см. api/routers/rag.py: ontology-check / reclassify) — тот же подход,
    что в scripts/backfill_class_ids.py."""
    points: list = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=collection_name,
            with_payload=True,
            with_vectors=True,
            limit=256,
            offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break
    return points


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list:
    """Режет текст на фрагменты по границам абзацев/предложений.

    Простой чанкер без внешних зависимостей: сначала делит по пустым строкам
    (абзацы), затем склеивает соседние абзацы пока не превышен max_chars;
    слишком длинный одиночный абзац режется по предложениям. overlap —
    сколько символов хвоста предыдущего чанка повторить в начале следующего,
    чтобы не терять контекст на границе.
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    pieces: list = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        sentences = re.split(r"(?<=[.!?…])\s+", para)
        buf = ""
        for sent in sentences:
            if buf and len(buf) + 1 + len(sent) > max_chars:
                pieces.append(buf)
                buf = sent
            else:
                buf = f"{buf} {sent}".strip()
        if buf:
            pieces.append(buf)

    chunks: list = []
    buf = ""
    for piece in pieces:
        if not buf:
            buf = piece
        elif len(buf) + 2 + len(piece) <= max_chars:
            buf = f"{buf}\n\n{piece}"
        else:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = f"{tail}\n\n{piece}".strip() if tail else piece
    if buf:
        chunks.append(buf)
    return chunks


def _ensure_collection(client: QdrantClient, collection_name: str, vector_size: int) -> None:
    """Создаёт коллекцию, если её ещё нет; идемпотентно."""
    existing = {c.name for c in client.get_collections().collections}
    if collection_name in existing:
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=qmodels.VectorParams(size=vector_size, distance=qmodels.Distance.COSINE),
    )


async def index_document(
    user_id: str,
    rag_id: str,
    doc_id: str,
    filename: str,
    text: str,
    class_ids: list | None = None,
    ontology_id: str | None = None,
) -> dict:
    """Индексирует один документ целиком: чанкинг → эмбеддинг → запись → статус.

    Вызывающий код (фоновая задача) отвечает за то, что документы
    обрабатываются по одному, не параллельно. При любой ошибке статус
    документа обновляется на error с текстом причины — вызывающий код
    может просто вызвать эту функцию и не оборачивать её отдельным try,
    если не нужна дополнительная реакция на ошибку.
    """
    store = rag.get_store()
    chunks: list = []
    try:
        chunks = chunk_text(text)
        if not chunks:
            raise IndexingError("Документ пуст после извлечения текста")
        # текст сохраняется всегда: по нему работает текстовый поиск, если
        # Qdrant или модель эмбеддингов недоступны (text_search.py)
        text_search.save_text(rag_id, doc_id, filename, text)

        vectors: list = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i : i + EMBED_BATCH_SIZE]
            vectors.extend(await embed_texts(batch))

        card = store.get(user_id, rag_id)
        if card is None:
            raise IndexingError(f"RAG-база не найдена: {rag_id}")
        collection_name = card.get("collection_name") or rag_id

        client = get_qdrant_client()
        _ensure_collection(client, collection_name, vector_size=len(vectors[0]))

        if class_ids:
            # ручной оверрайд (если когда-нибудь появится в UI загрузки) —
            # один и тот же список классов на все чанки документа.
            per_chunk_class_ids = [list(class_ids)] * len(chunks)
        elif ontology_id:
            per_chunk_class_ids = await ontologies.classify_chunks(ontology_id, chunks, vectors)
        else:
            per_chunk_class_ids = [[] for _ in chunks]

        points = [
            qmodels.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "doc_id": doc_id,
                    "filename": filename,
                    "chunk_index": i,
                    "class_ids": per_chunk_class_ids[i],
                },
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        client.upsert(collection_name=collection_name, points=points)

        store.update_document(
            user_id, rag_id, doc_id,
            {"status": rag.DOC_STATUS_READY, "chunks_count": len(chunks), "error": ""},
        )
        return {"ok": True, "chunks_count": len(chunks)}

    except IndexingError as exc:
        store.update_document(
            user_id, rag_id, doc_id,
            {"status": rag.DOC_STATUS_ERROR, "error": str(exc)[:500]},
        )
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # эмбеддинги или Qdrant недоступны
        if not chunks:
            store.update_document(
                user_id, rag_id, doc_id,
                {"status": rag.DOC_STATUS_ERROR, "error": str(exc)[:500]},
            )
            return {"ok": False, "error": str(exc)}
        # документ остаётся доступен текстовому поиску; векторный индекс
        # достраивается повторной загрузкой, когда Qdrant/эмбеддинги появятся
        store.update_document(
            user_id, rag_id, doc_id,
            {
                "status": rag.DOC_STATUS_READY,
                "chunks_count": len(chunks),
                "error": "",
                "search_mode": "text",
            },
        )
        return {"ok": True, "chunks_count": len(chunks), "search_mode": "text",
                "note": f"Векторный индекс недоступен ({type(exc).__name__}) — документ доступен текстовому поиску"}


async def search(
    rag_id: str,
    query: str,
    limit: int = 5,
    class_ids: list | None = None,
    ontology_id: str | None = None,
) -> dict:
    """Ищет чанки в коллекции RAG-базы по смыслу вопроса.

    class_ids, если передан непустым, сужает поиск фильтром по метаданным
    (GraphRAG-лайт: онтология как понятийный фильтр перед векторным поиском,
    а не полный обход графа) — используется, только если чанки были
    размечены классами при индексации.

    Если class_ids не передан явно, а у базы задан ontology_id — запрос сам
    классифицируется по онтологии (той же логикой, что и чанки при индекса-
    ции) и набор классов расширяется по графу связей/иерархии
    (ontologies.classify_query_context) — фильтр применяется автоматически,
    без необходимости, чтобы вызывающая модель сама угадывала class_ids.

    Возвращает не список, а словарь: "hits" — найденные фрагменты, как и
    раньше, "ontology_context" — текстовые определения задействованных
    классов онтологии (пусто, если онтология не участвовала).
    """
    query = (query or "").strip()
    if not query:
        raise SearchError("Пустой запрос")

    try:
        return await _vector_search(rag_id, query, limit, class_ids, ontology_id)
    except Exception as exc:
        if not text_search.has_corpus(rag_id):
            if isinstance(exc, SearchError):
                raise
            raise SearchError(f"Поиск недоступен: {exc}") from exc
        reason = str(exc) if isinstance(exc, SearchError) else type(exc).__name__
    return {
        "hits": text_search.search(rag_id, query, limit=limit),
        "ontology_context": _ontology_definitions(ontology_id, query),
        "search_mode": "text",
        "note": f"Текстовый поиск по корпусу базы (векторный недоступен: {reason})",
    }


def _ontology_definitions(ontology_id: str | None, query: str) -> list:
    """Определения понятий онтологии, упомянутых в запросе (для текстового режима)."""
    if not ontology_id:
        return []
    try:
        graph = ontologies.get_store().get_published(ontology_id)
    except Exception:
        graph = None
    if not graph:
        return []
    q = set(text_search.terms(query))
    hit_ids = []
    for cls in graph.get("classes") or []:
        names = [cls.get("name", "")] + list(cls.get("synonyms") or [])
        if any(set(text_search.terms(n)) & q for n in names if n):
            hit_ids.append(cls.get("id"))
    if not hit_ids:
        return []
    try:
        return ontologies.class_definitions(graph, hit_ids[:5])
    except Exception:
        return []


async def _vector_search(rag_id, query, limit, class_ids, ontology_id) -> dict:
    vectors = await embed_texts([query])
    vector = vectors[0]

    client = get_qdrant_client()
    existing = {c.name for c in client.get_collections().collections}
    if rag_id not in existing:
        raise SearchError(f"Коллекция RAG-базы не найдена: {rag_id}")

    ontology_context: list = []
    effective_class_ids = class_ids
    if not effective_class_ids and ontology_id:
        try:
            ctx = await ontologies.classify_query_context(ontology_id, query, vector)
        except Exception:
            ctx = {"class_ids": [], "definitions": []}
        effective_class_ids = ctx["class_ids"] or None
        ontology_context = ctx["definitions"]

    query_filter = None
    if effective_class_ids:
        query_filter = qmodels.Filter(
            must=[qmodels.FieldCondition(key="class_ids", match=qmodels.MatchAny(any=effective_class_ids))]
        )

    hits = client.query_points(
        collection_name=rag_id,
        query=vector,
        limit=max(1, min(int(limit), 20)),
        query_filter=query_filter,
        with_payload=True,
    ).points

    return {
        "hits": [
            {
                "text": hit.payload.get("text", ""),
                "doc_id": hit.payload.get("doc_id", ""),
                "filename": hit.payload.get("filename", ""),
                "score": hit.score,
            }
            for hit in hits
        ],
        "ontology_context": ontology_context,
        "search_mode": "vector",
    }
