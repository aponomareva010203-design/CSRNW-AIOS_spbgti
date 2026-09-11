"""Реестр инструментов харнесса и встроенные инструменты.

Инструмент — это имя, описание, JSON Schema параметров и async-обработчик,
возвращающий JSON-совместимый словарь со статусом ok. Схемы отдаются модели
в формате OpenAI tool-calling; ошибка обработчика не роняет цикл — модель
получает {"ok": false, "error": "..."} и может скорректировать поведение.

Файловые инструменты (read_file/list_files/write_file) ограничены песочницей
tools_root (по умолчанию data/): выход за корень отклоняется.

Обработчики получают (args, ctx): ctx — контекст цикла {"session": …};
он нужен инструментам с состоянием сессии (read_document хранит в ней
разрешения пользователя на чтение больших PDF).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..catalog import CATALOG
from . import datarepo
from . import ontologies
from . import organization
from . import projects
from . import indexer
from . import rag
from . import uploads

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
    handler: Callable[..., Awaitable[dict]]


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

    async def call(self, name: str, arguments: dict, ctx: dict | None = None) -> dict:
        """Безопасный вызов инструмента; ctx — контекст цикла (сессия)."""
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Неизвестный инструмент: {name}"}
        try:
            return await tool.handler(arguments or {}, ctx)
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

    async def read_file(args: dict, ctx: dict | None) -> dict:
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

    async def list_files(args: dict, ctx: dict | None) -> dict:
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

    async def write_file(args: dict, ctx: dict | None) -> dict:
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

    async def search_knowledge(args: dict, ctx: dict | None) -> dict:
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

    async def get_catalog(args: dict, ctx: dict | None) -> dict:
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

    # ----- раздел «Об организации» -----

    async def get_organization(args: dict, ctx: dict | None) -> dict:
        """Данные организации: профиль, показатели и документы с .md-описаниями."""
        section = str(args.get("section", "all")).strip().lower() or "all"
        if section not in ("profile", "metrics", "documents", "all"):
            return {"ok": False, "error": "section должен быть одним из: profile, metrics, documents, all"}
        store = organization.get_store()
        result: dict = {"ok": True, "section": section}
        if section in ("profile", "all"):
            result["profile"] = store.load_profile()
        if section in ("metrics", "all"):
            result["metrics"] = store.load_metrics()
        if section in ("documents", "all"):
            result["documents"] = [organization.public_document(d) for d in store.load_documents()]
            result["note"] = (
                "У каждого документа есть короткое .md-описание (summary). Полную версию "
                "читайте инструментом read_document; большие PDF — только после явного "
                "разрешения пользователя."
            )
        return result

    # ----- карточки проектов («Проекты») -----

    def _projects_user(ctx: dict | None) -> str | None:
        """Владелец карточек — пользователь, привязанный к сессии по токену."""
        session = ctx.get("session") if isinstance(ctx, dict) else None
        user_id = session.get("user_id") if isinstance(session, dict) else None
        return user_id or None

    async def get_projects(args: dict, ctx: dict | None) -> dict:
        """Карточки проектов текущего пользователя («Проекты»)."""
        user_id = _projects_user(ctx)
        if not user_id:
            return {
                "ok": False,
                "error": "Карточки проектов доступны только авторизованным "
                "пользователям. Попросите пользователя войти в AIOS.",
            }
        store = projects.get_store()
        cards = [store.public_card(c) for c in store.list(user_id)]
        return {
            "ok": True,
            "count": len(cards),
            "items": cards,
            "note": "status: draft — черновик онбординга; active — подтверждён "
            "пользователем и виден на странице «Проекты».",
        }

    async def save_project_card(args: dict, ctx: dict | None) -> dict:
        """Создаёт/обновляет карточку проекта (upsert по project_id).

        Подстраховка от потери project_id между шагами навыка (см. HANDOFF
        §4.2 и предложение пользователя о множестве черновиков одного и того
        же проекта): если модель не передала project_id в card, но текущая
        сессия уже помнит id с предыдущего вызова — используем его, а не
        создаём новую карточку. После сохранения id заново кладём в сессию.
        """
        user_id = _projects_user(ctx)
        if not user_id:
            return {
                "ok": False,
                "error": "Карточки проектов доступны только авторизованным "
                "пользователям. Попросите пользователя войти в AIOS.",
            }
        card = args.get("card")
        if not isinstance(card, dict):
            card = {k: v for k, v in args.items() if k != "card"}
        session = ctx.get("session") if isinstance(ctx, dict) else None
        if isinstance(session, dict):
            if not (card.get("project_id") or card.get("id")) and session.get("project_id"):
                card = dict(card)
                card["project_id"] = session["project_id"]
        store = projects.get_store()
        saved = store.save(user_id, card)
        if isinstance(session, dict):
            session["project_id"] = saved["id"]
        return {
            "ok": True,
            "project_id": saved["id"],
            "status": saved["status"],
            "card": store.public_card(saved),
            "note": "Карточка сохранена. При следующих вызовах передавайте этот "
            "project_id — карточка обновится, а не создастся заново. Статус "
            "active ставьте только после явного подтверждения пользователя.",
        }

    async def attach_project_file(args: dict, ctx: dict | None) -> dict:
        """Прикрепляет вложение чата к карточке проекта (файлы задела/презентации).

        Вложение (загружено пользователем кнопкой 📎 в чате) перемещается
        в files/ карточки, имя дописывается в groundwork_files или
        presentation_files. Карточка должна уже существовать (save_project_card).
        """
        user_id = _projects_user(ctx)
        if not user_id:
            return {
                "ok": False,
                "error": "Карточки проектов доступны только авторизованным "
                "пользователям. Попросите пользователя войти в AIOS.",
            }
        project_id = str(args.get("project_id") or "").strip()
        attachment_id = str(args.get("attachment_id") or "").strip()
        field = str(args.get("field") or "").strip()
        if field not in ("groundwork_files", "presentation_files"):
            return {
                "ok": False,
                "error": "field должен быть groundwork_files (файлы задела) "
                "или presentation_files (файлы презентации)",
            }
        up_store = uploads.get_store()
        meta = up_store.get(user_id, attachment_id)
        if meta is None:
            return {"ok": False, "error": f"Вложение не найдено: {attachment_id}"}
        store = projects.get_store()
        card = store.get(user_id, project_id)
        if card is None:
            return {"ok": False, "error": f"Карточка не найдена: {project_id}"}
        path = up_store.content_path(user_id, meta)
        if not path.is_file():
            return {"ok": False, "error": f"Файл вложения отсутствует: {attachment_id}"}
        try:
            stored_name = store.save_file(
                user_id, card["id"], meta["filename"], path.read_bytes()
            )
            current = list(card.get(field) or [])
            if stored_name not in current:
                current.append(stored_name)
            saved = store.save(user_id, {"project_id": card["id"], field: current})
        except (projects.ProjectError, uploads.UploadError) as exc:
            return {"ok": False, "error": str(exc)}
        up_store.delete(user_id, attachment_id)
        return {
            "ok": True,
            "project_id": saved["id"],
            "field": field,
            "filename": stored_name,
            "files": saved.get(field) or [],
            "download_url": f"/api/projects/{saved['id']}/files/{stored_name}",
            "note": "Файл прикреплён к карточке и доступен на странице «Проекты». "
            "Сообщите пользователю, что файл загружен.",
        }

    # ----- онтологии (навык «Конструктор онтологий») -----

    def _ontology_user(ctx: dict | None) -> str | None:
        """Владелец онтологий — пользователь, привязанный к сессии по токену."""
        session = ctx.get("session") if isinstance(ctx, dict) else None
        user_id = session.get("user_id") if isinstance(session, dict) else None
        return user_id or None

    def _ontology_login_error() -> dict:
        return {
            "ok": False,
            "error": "Онтологии доступны только авторизованным пользователям. "
            "Попросите пользователя войти в AIOS.",
        }

    async def get_ontologies(args: dict, ctx: dict | None) -> dict:
        """Онтологии текущего пользователя (включая черновики) или каталог."""
        user_id = _ontology_user(ctx)
        if not user_id:
            return _ontology_login_error()
        store = ontologies.get_store()
        scope = str(args.get("scope") or "mine")
        if scope == "catalog":
            items = store.catalog_items()[:MAX_CATALOG_ITEMS]
            return {
                "ok": True,
                "scope": "catalog",
                "count": len(items),
                "items": items,
                "note": "Каталог опубликованных онтологий раздела «Данные и знания».",
            }
        ontology_id = str(args.get("ontology_id") or "").strip()
        if ontology_id:
            card = store.get(user_id, ontology_id)
            if card is None:
                return {"ok": False, "error": f"Онтология не найдена: {ontology_id}"}
            view = store.public_card(card)
            dumped = json.dumps(view, ensure_ascii=False)
            result = {"ok": True, "scope": "one", "ontology": view}
            if len(dumped) > 5_500:
                result["truncated_hint"] = (
                    "Онтология большая: цикл общения обрежет её в истории. "
                    "Продолжайте правки по шагам, пересобирая граф из диалога."
                )
            return result
        cards = store.list(user_id)[:MAX_CATALOG_ITEMS]
        return {
            "ok": True,
            "scope": "mine",
            "count": len(cards),
            "items": [store.summary(c) for c in cards],
            "note": "Ваши онтологии: draft — черновик конструктора, active — "
            "опубликована в каталоге. Для полного графа укажите ontology_id; "
            "новый диалог начинайте с продолжения черновика, а не с нуля.",
        }

    async def save_ontology(args: dict, ctx: dict | None) -> dict:
        """Создаёт/обновляет онтологию (upsert по ontology_id)."""
        user_id = _ontology_user(ctx)
        if not user_id:
            return _ontology_login_error()
        card = args.get("card")
        if not isinstance(card, dict):
            return {"ok": False, "error": "Аргумент card ожидается объектом"}
        saved = ontologies.get_store().save(user_id, card)
        result = {
            "ok": True,
            "ontology_id": saved["id"],
            "status": saved["status"],
            "version": saved.get("version"),
            "classes_count": saved.get("classes_count", 0),
            "relations_count": saved.get("relations_count", 0),
            "competency_questions_count": len(saved.get("competency_questions") or []),
            "project_id": saved.get("project_id", ""),
            "note": "При следующих вызовах передавайте этот ontology_id и карточку "
            "целиком. Статус active — только после явного подтверждения "
            "пользователем: он публикует онтологию в каталог «Данные и знания».",
        }
        if saved["status"] == ontologies.STATUS_ACTIVE:
            result["published"] = True
            result["file_url"] = f"/data/ontologies/objects/{saved['id']}.json"
            result["viewer_url"] = f"/ontology.html?id={saved['id']}"
        else:
            result["published"] = False
        return result

    # ----- RAG-базы (навык «Конструктор RAG») -----

    def _rag_user(ctx: dict | None) -> str | None:
        """Владелец RAG-баз — пользователь, привязанный к сессии по токену."""
        session = ctx.get("session") if isinstance(ctx, dict) else None
        user_id = session.get("user_id") if isinstance(session, dict) else None
        return user_id or None

    def _rag_login_error() -> dict:
        return {
            "ok": False,
            "error": "RAG-базы доступны только авторизованным пользователям. "
            "Попросите пользователя войти в AIOS.",
        }

    async def get_rag(args: dict, ctx: dict | None) -> dict:
        """RAG-базы текущего пользователя (включая черновики) или каталог."""
        user_id = _rag_user(ctx)
        if not user_id:
            return _rag_login_error()
        store = rag.get_store()
        scope = str(args.get("scope") or "mine")
        if scope == "catalog":
            items = store.catalog_items()[:MAX_CATALOG_ITEMS]
            return {
                "ok": True,
                "scope": "catalog",
                "count": len(items),
                "items": items,
                "note": "Каталог опубликованных RAG-баз раздела «Данные и знания».",
            }
        rag_id = str(args.get("rag_id") or "").strip()
        if rag_id:
            card = store.get(user_id, rag_id)
            if card is None:
                return {"ok": False, "error": f"RAG-база не найдена: {rag_id}"}
            return {"ok": True, "scope": "one", "rag": store.public_card(card)}
        cards = store.list(user_id)[:MAX_CATALOG_ITEMS]
        return {
            "ok": True,
            "scope": "mine",
            "count": len(cards),
            "items": [store.summary(c) for c in cards],
            "note": "Ваши RAG-базы: draft — черновик конструктора, active — "
            "опубликована в каталоге. Для полной карточки укажите rag_id; "
            "новый диалог начинайте с продолжения черновика, а не с нуля.",
        }

    async def save_rag(args: dict, ctx: dict | None) -> dict:
        """Создаёт/обновляет RAG-базу (upsert по rag_id)."""
        user_id = _rag_user(ctx)
        if not user_id:
            return _rag_login_error()
        card = args.get("card")
        if not isinstance(card, dict):
            return {"ok": False, "error": "Аргумент card ожидается объектом"}
        saved = rag.get_store().save(user_id, card)
        result = {
            "ok": True,
            "rag_id": saved["id"],
            "status": saved["status"],
            "version": saved.get("version"),
            "documents_count": saved.get("documents_count", 0),
            "chunks_count": saved.get("chunks_count", 0),
            "project_id": saved.get("project_id", ""),
            "ontology_id": saved.get("ontology_id", ""),
            "note": "При следующих вызовах передавайте этот rag_id и карточку "
            "целиком. Статус active — только после явного подтверждения "
            "пользователем и при наличии хотя бы одного готового документа.",
        }
        if saved["status"] == rag.STATUS_ACTIVE:
            result["published"] = True
        else:
            result["published"] = False
        return result

    async def search_corpus(args: dict, ctx: dict | None) -> dict:
        """Поиск по содержимому опубликованной RAG-базы (векторный поиск + опциональный фильтр по классам онтологии)."""
        rag_id = str(args.get("rag_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if not rag_id:
            return {"ok": False, "error": "Не указан rag_id"}
        if not query:
            return {"ok": False, "error": "Пустой запрос"}
        try:
            limit = max(1, min(int(args.get("limit", 5)), 20))
        except (TypeError, ValueError):
            limit = 5
        class_ids = args.get("class_ids")
        if class_ids is not None and not isinstance(class_ids, list):
            class_ids = None
        try:
            hits = await indexer.search(rag_id, query, limit=limit, class_ids=class_ids)
        except indexer.SearchError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "rag_id": rag_id,
            "query": query,
            "count": len(hits),
            "results": hits,
            "note": "Отвечайте только на основе этих результатов и указывайте "
            "источник (filename/doc_id) — не придумывайте факты сверх найденного.",
        }

    def _extract_pdf_pages(path: Path, args: dict) -> dict:
        """Извлекает текст PDF (pypdf) с диапазоном страниц и лимитом символов."""
        from pypdf import PdfReader  # лениво: остальной рантайм работает и без pypdf

        reader = PdfReader(str(path))
        pages_total = len(reader.pages)

        def _int_arg(key: str, default: int) -> int:
            try:
                return int(args.get(key, default))
            except (TypeError, ValueError):
                return default

        start = max(1, min(_int_arg("start_page", 1), pages_total))
        end = max(start, min(_int_arg("end_page", pages_total), pages_total))
        chunks: list[str] = []
        used = 0
        truncated = end < pages_total
        for idx in range(start - 1, end):
            page_text = (reader.pages[idx].extract_text() or "").strip()
            if used + len(page_text) > MAX_READ_CHARS:
                page_text = page_text[: MAX_READ_CHARS - used]
                truncated = True
            chunks.append(page_text)
            used += len(page_text)
            if truncated:
                break
        text = "\n\n".join(c for c in chunks if c)
        return {
            "pages_total": pages_total,
            "pages_read": [start, end],
            "truncated": truncated,
            "content": text,
        }

    async def read_document(args: dict, ctx: dict | None) -> dict:
        """Документ организации: .md-описание или (full=true) полная версия файла."""
        doc_id = str(args.get("document_id", "")).strip()
        full = bool(args.get("full", False))
        store = organization.get_store()
        doc = store.get_document(doc_id)
        if doc is None:
            return {"ok": False, "error": f"Документ не найден: {doc_id or '—'}"}

        if not full:
            # первый уровень доступа: короткое .md-описание — без разрешений
            summary = str(doc.get("summary") or "").strip()
            return {
                "ok": True,
                "document_id": doc_id,
                "title": doc.get("title"),
                "category": organization.CATEGORIES.get(doc.get("category"), doc.get("category")),
                "filename": doc.get("filename"),
                "summary": summary
                or "(короткое .md-описание не заполнено — попросите администратора добавить его в админ-панели)",
                "hint": "Это короткое описание. Полная версия — read_document с full=true "
                "(для больших PDF нужно разрешение пользователя).",
            }

        try:
            path = store.document_path(doc)
        except organization.OrganizationError as exc:
            return {"ok": False, "error": str(exc)}
        if not path.is_file():
            return {"ok": False, "error": "Файл документа отсутствует в organization/objects/"}
        size = path.stat().st_size
        ext = path.suffix.lower()

        if ext == ".pdf":
            # большие PDF — диалоговое разрешение пользователя (state в сессии)
            if size > organization.PDF_PERMISSION_THRESHOLD:
                session = ctx.get("session") if isinstance(ctx, dict) else None
                perms = session.get("doc_permissions") if isinstance(session, dict) else None
                if not (isinstance(perms, dict) and perms.get(doc_id) == "granted"):
                    if isinstance(session, dict):
                        session.setdefault("doc_permissions", {})[doc_id] = "pending"
                    return {
                        "ok": False,
                        "needs_permission": True,
                        "document_id": doc_id,
                        "title": doc.get("title"),
                        "size": size,
                        "message": (
                            f"Документ «{doc.get('title', doc_id)}» — большой PDF "
                            f"({size // 1024} КБ). Сначала спросите у пользователя разрешение "
                            "прочитать этот документ, дождитесь ответа и вызовите read_document "
                            "повторно с full=true и confirmed=true. Пока разрешения нет — "
                            "используйте короткое .md-описание (read_document без full)."
                        ),
                    }
            try:
                extracted = _extract_pdf_pages(path, args)
            except ImportError:
                return {"ok": False, "error": "Библиотека pypdf не установлена на сервере"}
            except Exception as exc:  # noqa: BLE001 — битый PDF не должен ронять цикл
                return {"ok": False, "error": f"Не удалось прочитать PDF: {type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "document_id": doc_id,
                "title": doc.get("title"),
                "filename": doc.get("filename"),
                "format": "pdf",
                "size": size,
                **extracted,
            }

        # текстовые форматы читаются полностью без разрешения
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "document_id": doc_id,
            "title": doc.get("title"),
            "filename": doc.get("filename"),
            "format": ext.lstrip("."),
            "size": size,
            "truncated": len(text) > MAX_READ_CHARS,
            "content": text[:MAX_READ_CHARS],
        }


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
        Tool(
            name="get_organization",
            description="Данные организации из раздела «Об организации»: profile — основная информация (название, aliases, описание, направления деятельности), metrics — показатели деятельности, documents — основные документы с короткими .md-описаниями. Разрешение пользователя не требуется.",
            parameters={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["profile", "metrics", "documents", "all"],
                        "default": "all",
                        "description": "Какой раздел вернуть",
                    },
                },
            },
            handler=get_organization,
        ),
        Tool(
            name="read_document",
            description="Читает документ организации. Без full возвращает короткое .md-описание документа (разрешение не нужно) — начинай всегда с него. С full=true читает полную версию файла; большие PDF требуют явного разрешения пользователя: инструмент вернёт needs_permission — спроси пользователя, дождись ответа и вызови снова с full=true и confirmed=true. Для PDF доступны start_page/end_page.",
            parameters={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Идентификатор документа (doc-…) из get_organization"},
                    "full": {"type": "boolean", "default": False, "description": "Читать полную версию файла вместо .md-описания"},
                    "confirmed": {"type": "boolean", "default": False, "description": "Пользователь разрешил чтение большого PDF"},
                    "start_page": {"type": "integer", "minimum": 1, "description": "Первая страница PDF (с 1)"},
                    "end_page": {"type": "integer", "minimum": 1, "description": "Последняя страница PDF включительно"},
                },
                "required": ["document_id"],
            },
            handler=read_document,
        ),
        Tool(
            name="get_projects",
            description="Карточки проектов текущего пользователя со страницы «Проекты» (id, название, категория, стадия, бюджет, «Задел» и т.д.). Работает только для авторизованных пользователей.",
            parameters={"type": "object", "properties": {}},
            handler=get_projects,
        ),
        Tool(
            name="save_project_card",
            description="Создаёт или обновляет карточку проекта пользователя (upsert по project_id). После каждого шага онбординга передавайте накопленную карточку целиком; project_id берите из результата первого вызова и не выдумывайте. Статус active — только после явного подтверждения пользователя (первое сохранение всегда draft).",
            parameters={
                "type": "object",
                "properties": {
                    "card": {
                        "type": "object",
                        "description": "Карточка: {project_id?, title?, description?, goal?, team_size?, participants?, departments?, budget?, groundwork?, groundwork_links?, groundwork_files?, presentation_files?, presentation?, artifacts?, status?}. artifacts — статусы и гипотезы артефактов пилота {rag?, models?, agents?, polygon?, social?}: значение — {status: 0..3, hypothesis?} или строка-гипотеза (тогда статус = 1 «гипотеза сформулирована»). Статусы: 0 не начато, 1 гипотеза сформулирована, 2 в процессе создания, 3 завершено.",
                    },
                },
                "required": ["card"],
            },
            handler=save_project_card,
        ),
        Tool(
            name="attach_project_file",
            description="Прикрепляет файл, загруженный пользователем в чат (кнопка 📎), "
            "к карточке проекта: имя дописывается в groundwork_files (задел) или "
            "presentation_files (презентация), файл становится доступен для скачивания "
            "на странице «Проекты». Вызывайте после загрузки файла пользователем; "
            "attachment_id берите из сообщения пользователя (файл: att-…), не выдумывайте. "
            "Карточка должна быть уже создана (save_project_card).",
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "id карточки (prj-…)"},
                    "attachment_id": {"type": "string", "description": "id вложения чата (att-…) из сообщения пользователя"},
                    "field": {"type": "string", "enum": ["groundwork_files", "presentation_files"], "description": "Куда приложить файл"},
                },
                "required": ["project_id", "attachment_id", "field"],
            },
            handler=attach_project_file,
        ),
        Tool(
            name="get_ontologies",
            description="Онтологии текущего пользователя (scope=mine — включая черновики навыка «Конструктор онтологий»; для полного графа передайте ontology_id) или каталог опубликованных онтологий (scope=catalog). Вызывайте в начале диалога: не начинайте новую онтологию, если есть незавершённый черновик.",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["mine", "catalog"],
                        "default": "mine",
                        "description": "Свои онтологии или каталог «Данные и знания»",
                    },
                    "ontology_id": {
                        "type": "string",
                        "description": "id онтологии (onto-…) для полного графа; работает со scope=mine",
                    },
                },
            },
            handler=get_ontologies,
        ),
        Tool(
            name="save_ontology",
            description="Создаёт или обновляет онтологию (upsert по ontology_id; карточка передаётся целиком: name, domain, description, author?, project_id?, competency_questions, classes, relations, rag_hints?, tags?, status?). ontology_id берите из результата первого вызова. Первое сохранение всегда draft; status=active публикует онтологию в каталог «Данные и знания» — только после явного подтверждения пользователя.",
            parameters={
                "type": "object",
                "properties": {
                    "card": {
                        "type": "object",
                        "description": "Онтология: {ontology_id?, name?, domain?, description?, author?, project_id?, competency_questions: [{id, question, coverage?}], classes: [{id, name, description?, parent?, attributes?, synonyms?, keywords?}], relations: [{id, name, domain_class, range_class, cardinality?, description?}], rag_hints?, tags?, status?}",
                    },
                },
                "required": ["card"],
            },
            handler=save_ontology,
        ),
        Tool(
            name="get_rag",
            description="RAG-базы текущего пользователя (scope=mine, включая черновики навыка «Конструктор RAG»; для полной карточки передайте rag_id) или каталог опубликованных баз (scope=catalog). Вызывайте в начале диалога: не начинайте новую базу, если есть незавершённый черновик.",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["mine", "catalog"],
                        "default": "mine",
                        "description": "Свои RAG-базы или каталог «Данные и знания»",
                    },
                    "rag_id": {
                        "type": "string",
                        "description": "id RAG-базы (rag-…) для полной карточки; работает со scope=mine",
                    },
                },
            },
            handler=get_rag,
        ),
        Tool(
            name="save_rag",
            description="Создаёт или обновляет RAG-базу (upsert по rag_id; карточка передаётся целиком: name, domain, description, author?, project_id?, ontology_id?, tags?, corpus_types?, corpus_volume?, corpus_format?, access_restrictions?, sensitive?, typical_queries?, citation_required?, owner?, update_frequency?, status?). rag_id берите из результата первого вызова. Первое сохранение всегда draft; status=active публикует базу в каталог «Данные и знания» — только после явного подтверждения пользователя и при наличии хотя бы одного проиндексированного документа.",
            parameters={
                "type": "object",
                "properties": {
                    "card": {
                        "type": "object",
                        "description": "RAG-база: {rag_id?, name?, domain?, description?, author?, project_id?, ontology_id?, tags?, corpus_types?, corpus_volume?, corpus_format?, access_restrictions?, sensitive?, typical_queries?, citation_required?, owner?, update_frequency?, status?}",
                    },
                },
                "required": ["card"],
            },
            handler=save_rag,
        ),
        Tool(
            name="search_corpus",
            description="Ищет ответы по содержимому опубликованной RAG-базы (векторный поиск, с опциональным сужением по классам онтологии). Используйте, когда пользователь подключил RAG-базу в рабочем чате или явно попросил найти информацию в конкретной базе. Отвечайте только на основе найденных результатов, всегда указывайте источник (filename/doc_id) — не придумывайте факты сверх найденного.",
            parameters={
                "type": "object",
                "properties": {
                    "rag_id": {
                        "type": "string",
                        "description": "id RAG-базы (rag-…), по которой ищем",
                    },
                    "query": {
                        "type": "string",
                        "description": "поисковый запрос на естественном языке",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 5,
                        "description": "сколько фрагментов вернуть (1-20)",
                    },
                    "class_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "опционально: сузить поиск классами онтологии",
                    },
                },
                "required": ["rag_id", "query"],
            },
            handler=search_corpus,
        ),
    ]
