"""Чтение JSON-карточек гипотез (api/runtime/hypotheses.py).

Каталог data/hypotheses/ — по одному файлу на проект, имя hyp-{project_id}.json
(конвенция навыка «Гипотезы проекта», data/skills/aios-hypotheses-skill.md).
Файл создаёт LLM-инструмент save_hypotheses в рабочем чате; здесь его только
читают — целиком, как сохранил навык (разделы ontology/rag/models/agents/
polygons/dashboards с гипотезами внутри).
"""

import json
from pathlib import Path

from .config import get_settings
from .projects import PROJECT_ID_RE

HYP_ID_RE = PROJECT_ID_RE


def hypotheses_dir() -> Path:
    return get_settings().tools_root / "hypotheses"


SECTION_KEYS = ("ontology", "rag", "models", "agents", "polygons", "dashboards")


def load_hypotheses(project_id: str) -> dict | None:
    """JSON-карточка гипотез проекта или None, если её ещё нет."""
    if not HYP_ID_RE.match(project_id or ""):
        return None
    path = hypotheses_dir() / f"hyp-{project_id}.json"
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hypotheses_done(project_id: str) -> bool:
    """Сформулированы ли гипотезы проекта: карточка есть и по каждому
    разделу SECTION_KEYS есть запись — проработанная или явно пропущенная
    (skipped: true с указанием причины)."""
    card = load_hypotheses(project_id)
    if not card:
        return False
    for key in SECTION_KEYS:
        items = card.get(key)
        if not isinstance(items, list):
            return False
        if not any(
            isinstance(it, dict)
            and (not it.get("skipped") or str(it.get("skip_reason") or "").strip())
            for it in items
        ):
            return False
    return True
