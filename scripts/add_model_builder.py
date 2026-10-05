"""Добавляет «Конструктор моделей» в живой каталог агентов (data/skills.json).

data/skills.json на сервере меняется при каждой публикации агента
коллективом, поэтому запись не приходит через git (иначе git pull упирался
бы в локальные изменения каталога) — её добавляет этот скрипт. Повторный
запуск ничего не меняет.

    cd ~/AIOS_Technology_Institute_Harness
    PYTHONPATH=. .venv/bin/python scripts/add_model_builder.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "skills.json"
ENTRY = {
    "id": "model-builder",
    "category": "service",
    "name": "Конструктор моделей",
    "domain": "Модели коллективов",
    "description_ru": "Диалоговое интервью для создания модели коллектива — расчётного прибора «условия → "
                      "результат»: таблица опытов, вид модели, порог приёмки, обучение и проверка платформой, "
                      "проба на живом вопросе, страница модели.",
    "author": "Команда AIOS",
    "recommended_model": "—",
    "file_url": "/data/skills/model-builder.md",
    "image_url": "/data/icons/model-builder.svg",
    # агент сопровождает закрытый раздел «Модели» — обычным пользователям не показывается
    "admin_only": True,
}


def main() -> int:
    if not (ROOT / "data" / "skills" / "model-builder.md").is_file():
        print("нет data/skills/model-builder.md — сначала git pull")
        return 1
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if any(isinstance(i, dict) and i.get("id") == ENTRY["id"] for i in items):
        print("«Конструктор моделей» уже в каталоге")
        return 0
    at = next((n + 1 for n, i in enumerate(items) if isinstance(i, dict) and i.get("id") == "agent-builder"), len(items))
    items.insert(at, ENTRY)
    data["items"] = items
    tmp = CATALOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CATALOG)
    print("«Конструктор моделей» добавлен в каталог агентов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
