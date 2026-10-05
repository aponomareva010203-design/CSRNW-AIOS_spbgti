"""Пересчитывает class_ids у уже проиндексированных чанков RAG-базы по
привязанной онтологии (см. Architecture_AIOS.md §2.15) — для баз, документы
в которые загружались до появления авто-разметки. Текст и вектор чанка не
трогаются, меняется только поле class_ids в payload. Перед записью сохраняет
старые значения в JSON-бэкап (откат — rollback_class_ids.py).

Запуск:
    ./.venv/bin/python3 scripts/backfill_class_ids.py <rag_id> [--dry-run]
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from api.runtime import rag, ontologies
from api.runtime.indexer import get_qdrant_client

BACKUP_DIR = Path.home() / "aios-backups" / "rag_class_ids_backfill"


def fetch_all_points(client, collection_name):
    points = []
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


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("rag_id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    card = rag.get_store().find(args.rag_id)
    if not card:
        raise SystemExit(f"RAG-база не найдена: {args.rag_id}")
    ontology_id = card.get("ontology_id")
    if not ontology_id:
        raise SystemExit(f"У базы {args.rag_id} нет привязанной онтологии — backfill не имеет смысла")
    collection_name = card.get("collection_name") or args.rag_id

    client = get_qdrant_client()
    points = fetch_all_points(client, collection_name)
    if not points:
        print("В коллекции нет точек.")
        return

    texts = [p.payload.get("text") or "" for p in points]
    vectors = [p.vector for p in points]
    old_class_ids = [p.payload.get("class_ids") or [] for p in points]

    new_class_ids = await ontologies.classify_chunks(ontology_id, texts, vectors)

    changed = [
        (p.id, old, new)
        for p, old, new in zip(points, old_class_ids, new_class_ids)
        if old != new
    ]

    print(f"Онтология: {ontology_id}")
    print(f"Коллекция: {collection_name}")
    print(f"Всего чанков: {len(points)}")
    print(f"Изменится: {len(changed)}")
    print(f"Останется без изменений: {len(points) - len(changed)}")
    for pid, old, new in changed[:8]:
        print(f"  {pid}: {old} -> {new}")

    if args.dry_run:
        print("\n[--dry-run] Ничего не записано.")
        return

    if not changed:
        print("Изменений нет, запись не требуется.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"{args.rag_id}.{ts}.json"
    backup_payload = [{"id": pid, "class_ids": old} for pid, old, _ in changed]
    backup_path.write_text(json.dumps(backup_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nБэкап старых значений: {backup_path}")

    groups = {}
    for pid, _, new in changed:
        groups.setdefault(tuple(new), []).append(pid)

    for new_ids, point_ids in groups.items():
        client.set_payload(
            collection_name=collection_name,
            payload={"class_ids": list(new_ids)},
            points=point_ids,
        )

    print(f"Готово: обновлено {len(changed)} чанков в {len(groups)} групповых запросах.")


asyncio.run(main())
