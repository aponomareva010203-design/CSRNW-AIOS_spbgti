"""Чтение JSON-активов data/: агенты, навыки, RAG-базы, онтологии.

Каталог считается «живыми» данными: файлы могут редактироваться (в том числе
инструментами агента), поэтому читаем с диска на каждый вызов — без кэша.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def load_items(filename: str) -> list:
    """Читает data/<filename> и возвращает items; любая ошибка → пустой список."""
    path = DATA_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def agents() -> list:
    return load_items("agents.json")


def skills() -> list:
    return load_items("skills.json")


def rag_bases() -> list:
    return load_items("rag_bases.json")


def ontologies() -> list:
    return load_items("ontologies.json")


def get_agent(agent_id) -> dict | None:
    if not agent_id:
        return None
    return next((a for a in agents() if a.get("id") == agent_id), None)


def get_skill(skill_id) -> dict | None:
    if not skill_id:
        return None
    return next((s for s in skills() if s.get("id") == skill_id), None)


def skill_body(skill: dict, limit: int = 12_000) -> str:
    """Текст навыка из markdown-файла; file_url вида /data/... маппится на data/."""
    file_url = str(skill.get("file_url") or "").strip()
    data_root = DATA_DIR.resolve()
    path: Path | None = None
    if file_url:
        candidate = (DATA_DIR.parent / file_url.lstrip("/")).resolve()
        if candidate.is_file() and candidate.is_relative_to(data_root):
            path = candidate
    if path is None:
        candidate = (DATA_DIR / "skills" / f"{skill.get('id')}.md").resolve()
        if candidate.is_file() and candidate.is_relative_to(data_root):
            path = candidate
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + "\n\n[…текст навыка усечён]"
    return text
