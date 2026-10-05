"""Загрузка каталога моделей из data/models.json.

Источник данных для GET /api/models — файл data/models.json,
items соответствуют схеме api/schemas.ModelCard.
Позже загрузчик можно заменить на запросы к Hugging Face Hub API —
контракт ответа и интерфейс страницы при этом не меняются.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import List

from .schemas import ModelCard

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "models.json"


@lru_cache(maxsize=1)
def _load_catalog() -> List[ModelCard]:
    """Читает каталог из JSON и валидирует его схемой ModelCard."""
    raw_items = json.loads(DATA_FILE.read_text(encoding="utf-8"))["items"]
    return [ModelCard.model_validate(item) for item in raw_items]


CATALOG = _load_catalog()
