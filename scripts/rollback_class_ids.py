"""Откат backfill_class_ids.py — возвращает старые значения class_ids по
сохранённому JSON-бэкапу.

Запуск:
    ./.venv/bin/python3 scripts/rollback_class_ids.py <rag_id> <файл бэкапа>
"""
import argparse
import json
from pathlib import Path

from api.runtime import rag
from api.runtime.indexer import get_qdrant_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("rag_id")
    parser.add_argument("backup_file")
    args = parser.parse_args()

    card = rag.get_store().find(args.rag_id)
    if not card:
        raise SystemExit(f"RAG-база не найдена: {args.rag_id}")
    collection_name = card.get("collection_name") or args.rag_id

    data = json.loads(Path(args.backup_file).read_text(encoding="utf-8"))
    client = get_qdrant_client()

    groups = {}
    for item in data:
        groups.setdefault(tuple(item["class_ids"]), []).append(item["id"])

    for old_ids, point_ids in groups.items():
        client.set_payload(
            collection_name=collection_name,
            payload={"class_ids": list(old_ids)},
            points=point_ids,
        )

    print(f"Откат выполнен: восстановлено {len(data)} точек в {len(groups)} групповых запросах.")


main()
