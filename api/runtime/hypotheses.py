"""Чтение JSON-карточек гипотез (api/runtime/hypotheses.py).

Каталог data/hypotheses/ — по одному файлу на проект, имя hyp-{project_id}.json
(конвенция навыка «Гипотезы проекта», data/skills/aios-hypotheses-skill.md).
Файл создаёт LLM-инструмент save_hypotheses в рабочем чате; здесь его только
читают — целиком, как сохранил навык (разделы ontology/rag/agents/models/
polygons/dashboards с гипотезами внутри).
"""

import json
from pathlib import Path

from .config import get_settings
from .projects import PROJECT_ID_RE

HYP_ID_RE = PROJECT_ID_RE


def hypotheses_dir() -> Path:
    return get_settings().tools_root / "hypotheses"


SECTION_KEYS = ("ontology", "rag", "agents", "models", "polygons", "dashboards")

# поля одной гипотезы, которые может редактировать владелец карточки в
# кабинете (PATCH /api/projects/{id}/hypotheses) — остальные поля карточки
# (skipped, skip_reason, metric, deadline, decision и т.д.) не трогаем,
# их формирует только навык «Гипотезы проекта» в рабочем чате
EDITABLE_FIELDS = ("title", "statement")


class HypothesesError(ValueError):
    """Некорректный запрос на правку гипотезы (плохой раздел/индекс)."""


def save_hypothesis_item(project_id: str, section: str, index: int, **fields) -> dict:
    """Правка одной гипотезы внутри карточки (текст, а не структура).

    Карточку создаёт только навык «Гипотезы проекта» в рабочем чате
    (save_hypotheses) — здесь её структура (разделы, число гипотез,
    skipped/skip_reason и служебные поля) не меняется, правится только
    текст уже существующей записи (title/statement), чтобы владелец мог
    уточнить формулировку в кабинете, не переигрывая навык заново.
    """
    if section not in SECTION_KEYS:
        raise HypothesesError(
            "Раздел должен быть одним из: " + ", ".join(SECTION_KEYS)
        )
    card = load_hypotheses(project_id)
    if card is None:
        raise HypothesesError("Карточка гипотез ещё не создана")
    items = card.get(section)
    if not isinstance(items, list) or not (0 <= index < len(items)):
        raise HypothesesError("Гипотеза с таким индексом не найдена")
    item = dict(items[index] or {})
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        if value is None:
            continue
        value = str(value).strip()
        if len(value) > 4000:
            raise HypothesesError(f"Поле «{key}» слишком длинное (до 4000 символов)")
        item[key] = value
    items[index] = item
    card[section] = items
    path = hypotheses_dir() / f"hyp-{project_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return card


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
