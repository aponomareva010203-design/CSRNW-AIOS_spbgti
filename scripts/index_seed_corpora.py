#!/usr/bin/env python3
"""Индексирует корпуса демо-RAG-баз (data/rag/corpus/{rag_id}/) в Qdrant.

Нужен только для векторного поиска: без него базы знаний работают текстовым
поиском (api/runtime/text_search.py). Требует запущенный Qdrant
(AIOS_QDRANT_URL, docker compose поднимает его сам) и настроенный в
админ-панели или .env внешний API с моделью эмбеддингов (AIOS_EMBEDDING_MODEL).
Чанки размечаются классами онтологии базы — тем же способом, что и при
загрузке документов через интерфейс. Повторный запуск пересоздаёт коллекции.

Запуск из корня проекта:
    python scripts/index_seed_corpora.py [rag_id ...]
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qdrant_client.http import models as qmodels  # noqa: E402

from api.runtime import indexer, ontologies, text_search  # noqa: E402
from api.runtime.embeddings import embed_texts  # noqa: E402


async def index_base(card: dict) -> int:
    rag_id = card["id"]
    folder = text_search.corpus_dir(rag_id)
    chunks, meta = [], []
    for path in sorted(folder.glob("*.md")) + sorted(folder.glob("*.txt")):
        for i, chunk in enumerate(indexer.chunk_text(path.read_text(encoding="utf-8"))):
            chunks.append(chunk)
            meta.append({"doc_id": path.stem, "filename": path.name, "chunk_index": i})
    if not chunks:
        return 0
    vectors = []
    for i in range(0, len(chunks), indexer.EMBED_BATCH_SIZE):
        vectors.extend(await embed_texts(chunks[i : i + indexer.EMBED_BATCH_SIZE]))
    class_ids = [[] for _ in chunks]
    if card.get("ontology_id"):
        class_ids = await ontologies.classify_chunks(card["ontology_id"], chunks, vectors)
    client = indexer.get_qdrant_client()
    if rag_id in {c.name for c in client.get_collections().collections}:
        client.delete_collection(rag_id)
    indexer._ensure_collection(client, rag_id, vector_size=len(vectors[0]))
    client.upsert(
        collection_name=rag_id,
        points=[
            qmodels.PointStruct(
                id=str(uuid.uuid4()), vector=v,
                payload={"text": c, "class_ids": cls, **m},
            )
            for c, v, m, cls in zip(chunks, vectors, meta, class_ids)
        ],
    )
    return len(chunks)


async def main(wanted: list) -> None:
    catalog = json.loads((ROOT / "data" / "rag_bases.json").read_text(encoding="utf-8"))["items"]
    for card in catalog:
        if wanted and card["id"] not in wanted:
            continue
        if not text_search.corpus_dir(card["id"]).is_dir():
            continue
        n = await index_base(card)
        print(f"{card['id']}: {n} фрагментов — {card['name']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
