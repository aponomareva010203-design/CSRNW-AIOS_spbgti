"""Автоактуализация RAG-баз после правки онтологии (api/runtime/rag_sync.py).

Вызывается из хука в конце runtime/ontologies.py: после каждого save()
опубликованной (active) онтологии. Правки одной онтологии группируются
(debounce DEBOUNCE_SECONDS, каждая новая правка сбрасывает таймер), затем
все привязанные RAG-базы актуализируются по очереди: class_ids чанков
пересчитываются по опубликованной версии онтологии — та же логика, что у
POST /api/rag/{id}/reclassify (текст и векторы не трогаются, старые значения
уходят в бэкап ~/aios-backups/rag_class_ids_backfill, формат общий с
scripts/rollback_class_ids.py). Таймер живёт в памяти процесса: рестарт
сервера в окне ожидания отменяет запуск — тогда кнопка «Актуализировать».
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

DEBOUNCE_SECONDS = 60

log = logging.getLogger("uvicorn.error")

_timers: dict = {}
_timers_lock = threading.Lock()
_run_lock = threading.Lock()  # актуализации идут строго по очереди


def schedule(ontology_id: str) -> None:
    """Поставить (или перезапустить) отложенную актуализацию баз онтологии."""
    if not ontology_id:
        return
    with _timers_lock:
        old = _timers.pop(ontology_id, None)
        if old:
            old.cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _run, args=(ontology_id,))
        timer.daemon = True
        _timers[ontology_id] = timer
        timer.start()


def _run(ontology_id: str) -> None:
    with _timers_lock:
        _timers.pop(ontology_id, None)
    with _run_lock:
        try:
            asyncio.run(sync_ontology_rags(ontology_id))
        except Exception:
            log.exception("rag_sync: сбой актуализации баз онтологии %s", ontology_id)


async def sync_ontology_rags(ontology_id: str) -> None:
    from . import rag

    store = rag.get_store()
    for item in store.list_all():
        card = store.get(item.get("user_id") or "", item.get("id") or "")
        if not card or card.get("ontology_id") != ontology_id:
            continue
        try:
            res = await sync_rag(card)
            log.info("rag_sync: %s актуализирована: %s", card["id"], res)
        except Exception as exc:  # одна база не должна останавливать остальные
            log.warning("rag_sync: %s не актуализирована: %s", card.get("id"), exc)


async def sync_rag(card: dict) -> dict:
    from . import indexer, ontologies, rag

    rag_id = card["id"]
    owner_id = card["user_id"]
    graph = ontologies.get_store().get_published(card["ontology_id"])
    if not graph:
        return {"skipped": "онтология не опубликована"}

    collection_name = card.get("collection_name") or rag_id
    client = indexer.get_qdrant_client()
    points = await asyncio.to_thread(indexer.fetch_collection_points, client, collection_name)

    changed_idx: list = []
    old_ids: list = []
    new_ids: list = []
    if points:
        texts = [(p.payload or {}).get("text", "") for p in points]
        vectors = [p.vector for p in points]
        old_ids = [list((p.payload or {}).get("class_ids") or []) for p in points]
        new_ids = await ontologies.classify_chunks(card["ontology_id"], texts, vectors)
        changed_idx = [i for i, (o, n) in enumerate(zip(old_ids, new_ids)) if set(o) != set(n)]

    if changed_idx:
        backup_dir = Path.home() / "aios-backups" / "rag_class_ids_backfill"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"{rag_id}.{int(time.time())}.json"
        backup_file.write_text(
            json.dumps(
                {
                    "rag_id": rag_id,
                    "collection_name": collection_name,
                    "items": [
                        {"point_id": str(points[i].id), "class_ids": old_ids[i]}
                        for i in changed_idx
                    ],
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        groups: dict = {}
        for i in changed_idx:
            groups.setdefault(tuple(sorted(new_ids[i])), []).append(points[i].id)
        for key, point_ids in groups.items():
            await asyncio.to_thread(
                client.set_payload,
                collection_name=collection_name,
                payload={"class_ids": list(key)},
                points=point_ids,
            )

    rag.get_store().set_class_snapshot(
        owner_id, rag_id,
        {
            "ontology_id": graph.get("id"),
            "ontology_updated_at": graph.get("updated_at"),
            "classes": ontologies.classification_snapshot(graph),
            "classified_at": time.time(),
            "auto": True,
        },
    )
    return {"updated": len(changed_idx), "total": len(points)}
