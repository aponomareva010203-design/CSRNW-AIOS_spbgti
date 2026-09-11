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

import re
import uuid
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from . import rag
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
    """Синглтон клиента Qdrant; в тестах сбрасывается cache_clear()."""
    return QdrantClient(url="http://127.0.0.1:6333")


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
) -> dict:
    """Индексирует один документ целиком: чанкинг → эмбеддинг → запись → статус.

    Вызывающий код (фоновая задача) отвечает за то, что документы
    обрабатываются по одному, не параллельно. При любой ошибке статус
    документа обновляется на error с текстом причины — вызывающий код
    может просто вызвать эту функцию и не оборачивать её отдельным try,
    если не нужна дополнительная реакция на ошибку.
    """
    store = rag.get_store()
    try:
        chunks = chunk_text(text)
        if not chunks:
            raise IndexingError("Документ пуст после извлечения текста")

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

        points = [
            qmodels.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "doc_id": doc_id,
                    "filename": filename,
                    "chunk_index": i,
                    "class_ids": class_ids or [],
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

    except (IndexingError, EmbeddingError) as exc:
        store.update_document(
            user_id, rag_id, doc_id,
            {"status": rag.DOC_STATUS_ERROR, "error": str(exc)[:500]},
        )
        return {"ok": False, "error": str(exc)}


async def search(
    rag_id: str,
    query: str,
    limit: int = 5,
    class_ids: list | None = None,
) -> list:
    """Ищет чанки в коллекции RAG-базы по смыслу вопроса.

    class_ids, если передан непустым, сужает поиск фильтром по метаданным
    (GraphRAG-лайт: онтология как понятийный фильтр перед векторным поиском,
    а не полный обход графа) — используется, только если чанки были
    размечены классами при индексации.
    """
    query = (query or "").strip()
    if not query:
        raise SearchError("Пустой запрос")

    vectors = await embed_texts([query])
    vector = vectors[0]

    client = get_qdrant_client()
    existing = {c.name for c in client.get_collections().collections}
    if rag_id not in existing:
        raise SearchError(f"Коллекция RAG-базы не найдена: {rag_id}")

    query_filter = None
    if class_ids:
        query_filter = qmodels.Filter(
            must=[qmodels.FieldCondition(key="class_ids", match=qmodels.MatchAny(any=class_ids))]
        )

    hits = client.query_points(
        collection_name=rag_id,
        query=vector,
        limit=max(1, min(int(limit), 20)),
        query_filter=query_filter,
        with_payload=True,
    ).points

    return [
        {
            "text": hit.payload.get("text", ""),
            "doc_id": hit.payload.get("doc_id", ""),
            "filename": hit.payload.get("filename", ""),
            "score": hit.score,
        }
        for hit in hits
    ]
