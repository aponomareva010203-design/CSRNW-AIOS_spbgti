"""Реестр инструментов харнесса и встроенные инструменты.

Инструмент — это имя, описание, JSON Schema параметров и async-обработчик,
возвращающий JSON-совместимый словарь со статусом ok. Схемы отдаются модели
в формате OpenAI tool-calling; ошибка обработчика не роняет цикл — модель
получает {"ok": false, "error": "..."} и может скорректировать поведение.

Файловые инструменты (read_file/list_files/write_file) ограничены песочницей
tools_root (по умолчанию data/): выход за корень отклоняется.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..catalog import CATALOG
from . import datarepo

MAX_READ_CHARS = 20_000
MAX_WRITE_CHARS = 200_000
MAX_LISTED_FILES = 200
MAX_SEARCH_RESULTS = 8
MAX_CATALOG_ITEMS = 50


@dataclass
class Tool:
    """Описание инструмента для реестра."""

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[dict]]


class ToolRegistry:
    """Реестр инструментов: спецификации для модели + безопасный вызов."""

    def __init__(self) -> None:
        self._tools: dict = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list:
        return sorted(self._tools)

    def specs(self) -> list:
        """Спецификации в формате OpenAI tool-calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def call(self, name: str, arguments: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Неизвестный инструмент: {name}"}
        try:
            return await tool.handler(arguments or {})
        except Exception as exc:  # noqa: BLE001 — ошибка инструмента не роняет цикл
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_default_registry(tools_root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in _builtin_tools(Path(tools_root)):
        registry.register(tool)
    return registry


# ---------------------------------------------------------------------------
# Встроенные инструменты
# ---------------------------------------------------------------------------


def _builtin_tools(root: Path) -> list:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def safe_path(rel: str) -> Path:
        rel = (rel or "").strip()
        if not rel or "\x00" in rel:
            raise ValueError("Пустой или некорректный путь")
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"Путь вне песочницы ({root}): {rel}")
        return candidate

    async def read_file(args: dict) -> dict:
        path = safe_path(str(args.get("path", "")))
        if not path.is_file():
            return {"ok": False, "error": f"Файл не найден: {args.get('path')}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "path": str(path.relative_to(root)),
            "size": path.stat().st_size,
            "truncated": len(text) > MAX_READ_CHARS,
            "content": text[:MAX_READ_CHARS],
        }

    async def list_files(args: dict) -> dict:
        sub = safe_path(str(args.get("subdir") or "."))
        base = sub if sub.is_dir() else root
        files = sorted(
            p for p in base.rglob("*")
            if p.is_file() and not any(part.startswith((".", "__")) for part in p.relative_to(root).parts)
        )
        return {
            "ok": True,
            "root": str(root),
            "count": len(files),
            "truncated": len(files) > MAX_LISTED_FILES,
            "files": [str(p.relative_to(root)) for p in files[:MAX_LISTED_FILES]],
        }

    async def write_file(args: dict) -> dict:
        path = safe_path(str(args.get("path", "")))
        content = str(args.get("content", ""))
        if len(content) > MAX_WRITE_CHARS:
            return {"ok": False, "error": f"Слишком большой файл: {len(content)} > {MAX_WRITE_CHARS} символов"}
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(path.relative_to(root)),
            "bytes": len(content.encode("utf-8")),
            "created": not existed,
        }

    def _blob(card: dict) -> str:
        parts = []
        for key in ("name", "domain", "description_ru", "project", "status"):
            value = card.get(key)
            if isinstance(value, str):
                parts.append(value)
        for key in ("tags", "projects"):
            value = card.get(key)
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
        return " ".join(parts).lower()

    def _score(card: dict, tokens: list) -> int:
        blob = _blob(card)
        name = str(card.get("name", "")).lower()
        tags = " ".join(str(t) for t in card.get("tags") or []).lower()
        score = 0
        for token in tokens:
            if token and token in blob:
                score += 3 if token in name else (2 if token in tags else 1)
        return score

    async def search_knowledge(args: dict) -> dict:
        """Подбор RAG-баз и онтологий по запросу (по карточкам каталога)."""
        query = str(args.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "Пустой запрос"}
        try:
            limit = max(1, min(int(args.get("limit", 5)), MAX_SEARCH_RESULTS))
        except (TypeError, ValueError):
            limit = 5
        tokens = [
            t for t in "".join(ch if ch.isalnum() else " " for ch in query.lower()).split()
            if len(t) > 2
        ]
        results = []
        for kind, items in (("rag", datarepo.rag_bases()), ("ontology", datarepo.ontologies())):
            for card in items:
                score = _score(card, tokens)
                if score <= 0:
                    continue
                entry = {
                    "kind": kind,
                    "id": card.get("id"),
                    "name": card.get("name"),
                    "domain": card.get("domain"),
                    "project": card.get("project") or card.get("projects"),
                    "description_ru": card.get("description_ru"),
                    "tags": card.get("tags"),
                    "status": card.get("status"),
                    "score": score,
                }
                if kind == "rag":
                    entry["documents_count"] = card.get("documents_count")
                    entry["vector_db"] = card.get("vector_db")
                else:
                    entry["classes_count"] = card.get("classes_count")
                    entry["format"] = card.get("format")
                results.append(entry)
        results.sort(key=lambda e: e["score"], reverse=True)
        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results[:limit],
            "note": "Поиск по каталогу баз знаний и онтологий (по карточкам). Полнотекстовый поиск по корпусам (Qdrant) подключается отдельным инструментом.",
        }

    async def get_catalog(args: dict) -> dict:
        """Каталог артефактов: модели, агенты или навыки."""
        kind = str(args.get("kind", "")).strip().lower()
        query = str(args.get("query", "")).strip().lower()
        if kind == "models":
            items = [m.model_dump() for m in CATALOG]
        elif kind == "agents":
            items = datarepo.agents()
        elif kind == "skills":
            items = datarepo.skills()
        else:
            return {"ok": False, "error": "kind должен быть одним из: models, agents, skills"}
        if query:
            items = [i for i in items if query in json.dumps(i, ensure_ascii=False).lower()]
        return {"ok": True, "kind": kind, "count": len(items), "items": items[:MAX_CATALOG_ITEMS]}

    return [
        Tool(
            name="search_knowledge",
            description="Подбирает релевантные RAG-базы знаний и онтологии института по запросу на естественном языке. Используй перед ответами на предметные вопросы, чтобы указать, где искать данные.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS, "default": 5},
                },
                "required": ["query"],
            },
            handler=search_knowledge,
        ),
        Tool(
            name="get_catalog",
            description="Возвращает каталог артефактов AIOS: kind=models (модели), kind=agents (агенты) или kind=skills (навыки). Опциональный query фильтрует по подстроке.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["models", "agents", "skills"]},
                    "query": {"type": "string", "description": "Подстрока для фильтра"},
                },
                "required": ["kind"],
            },
            handler=get_catalog,
        ),
        Tool(
            name="read_file",
            description="Читает текстовый файл из песочницы данных (корень — каталог data/). Путь относительный.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Относительный путь, например skills/scimlchem-architect.md"}},
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="list_files",
            description="Список файлов песочницы данных, начиная с subdir (по умолчанию — корень).",
            parameters={
                "type": "object",
                "properties": {"subdir": {"type": "string", "description": "Относительный подкаталог"}},
            },
            handler=list_files,
        ),
        Tool(
            name="write_file",
            description="Создаёт или перезаписывает текстовый файл в песочнице данных. Используй для заметок, протоколов и черновиков по запросу пользователя.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь файла"},
                    "content": {"type": "string", "description": "Полное содержимое файла"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
    ]
