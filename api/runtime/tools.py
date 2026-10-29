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
from . import agent_feedback
from . import artifact_icons
from . import datarepo
from . import ontologies
from . import organization
from . import projects
from . import indexer
from . import rag
from . import uploads
from .config import get_settings
from .users import is_admin_user

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
    requires_approval: bool = False


class ToolRegistry:
    """Реестр инструментов: спецификации для модели + безопасный вызов."""

    def __init__(self) -> None:
        self._tools: dict = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list:
        return sorted(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    def requires_approval(self, name: str) -> bool:
        """Инструмент требует подтверждения пользователем перед исполнением."""
        tool = self._tools.get(name)
        return bool(tool and tool.requires_approval)

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
    # Инженерные возможности (расчёт в песочнице, чтение таблиц участника,
    # поиск публикаций в OpenAlex/arXiv/PubMed/Crossref/Semantic Scholar,
    # справка PubChem,
    # сборка документов, уведомления) заведены отдельным модулем: этот файл
    # уже 75 КБ, и прошлая сессия потеряла format_table при вставке через
    # одинаковый якорь str_replace.
    from .engineering_tools import engineering_tools

    for tool in engineering_tools():
        registry.register(tool)

    # Химические возможности (порт инструментов ChemCrow: RDKit через
    # расчётную песочницу, GHS PubChem, ретросинтез IBM RXN) — отдельным
    # модулем по той же причине.
    from .chemistry_tools import chemistry_tools

    for tool in chemistry_tools():
        registry.register(tool)

    # Модели коллективов (реестр, прогноз, подбор режима, обучение): всё
    # считается в песочнице, поэтому, как и run_calc, инструменты появляются
    # только при установленной песочнице — иначе модель строила бы на них планы.
    from . import compute_sandbox as _sandbox

    if _sandbox.sandbox_available():
        from .model_tools import model_tools

        for tool in model_tools():
            registry.register(tool)

    # Черновики навыков (второй этап конструктора агентов). Подключаются после
    # всех остальных: навык не может ссылаться на инструмент, которого нет.
    from .skill_tools import skill_tools

    for tool in skill_tools(lambda: set(registry.names())):
        registry.register(tool)
    # Human-in-the-loop: инструменты из AIOS_APPROVAL_TOOLS исполняются
    # только после подтверждения пользователем (карточка в чате); список
    # настраивается окружением, маркировка — здесь, единое место.
    approval_names = set(get_settings().approval_tools)
    for name in approval_names:
        tool = registry._tools.get(name)
        if tool is not None:
            tool.requires_approval = True
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
        """Каталог артефактов: модели или навыки-агенты."""
        kind = str(args.get("kind", "")).strip().lower()
        query = str(args.get("query", "")).strip().lower()
        if kind == "models":
            items = [m.model_dump() for m in CATALOG]
        elif kind == "skills":
            # admin-only агенты — только в сессиях администратора
            session = (ctx or {}).get("session")
            is_admin = is_admin_user(
                session.get("user_id") if isinstance(session, dict) else None
            )
            items = [
                s for s in datarepo.skills()
                if not datarepo.is_admin_only(s) or is_admin
            ]
        else:
            return {"ok": False, "error": "kind должен быть одним из: models, skills"}
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
        # применение активных агентов сессии в проекте — для карточек
        # каталога («Агенты»): учёт не должен ронять сохранение карточки
        agent_feedback.record_usage_from_session(session, saved["id"], "project")
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
            foreign = False
            if card is None:
                # чужая онтология: опубликованная версия из каталога (только чтение)
                pub = store.get_published(ontology_id)
                if pub is None or pub.get("admin_only"):
                    return {"ok": False, "error": f"Онтология не найдена: {ontology_id}"}
                card, foreign = pub, True
            view = store.public_card(card)
            dumped = json.dumps(view, ensure_ascii=False)
            result = {"ok": True, "scope": "one", "ontology": view}
            if foreign:
                result["read_only"] = True
                result["note"] = (
                    "Онтология чужая: опубликованная версия из каталога «Данные и знания», "
                    "только чтение (править её может только автор)."
                )
            if len(dumped) > 18_000:  # лимит цикла TOOL_CONTENT_LIMIT = 20 000 (loop.py)
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
        artifact_icons.schedule_generation("ontology", saved, user_id=user_id)
        agent_feedback.record_usage_from_session(
            ctx.get("session") if isinstance(ctx, dict) else None,
            saved.get("project_id"),
            "ontology",
        )
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
        artifact_icons.schedule_generation("rag", saved, user_id=user_id)
        agent_feedback.record_usage_from_session(
            ctx.get("session") if isinstance(ctx, dict) else None,
            saved.get("project_id"),
            "rag",
        )
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

    # ----- Агенты прежних версий конструктора (с интерфейсом) -----
    # Инструменты get_agents/save_agent убраны: конструктор агентов (v5)
    # создаёт агентов-сценариев .md через save_skill_draft (skill_tools.py).
    # Движок агентов (agent_store, /api/agents, планировщик) продолжает
    # работать с уже созданными агентами.

    async def search_corpus(args: dict, ctx: dict | None) -> dict:
        """Поиск по содержимому опубликованной RAG-базы (векторный поиск; фильтр по классам онтологии — явный или, если не передан, определяется автоматически по смыслу запроса)."""
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
        card = rag.get_store().find(rag_id)
        if card is not None and card.get("admin_only") and not is_admin_user(_rag_user(ctx)):
            return {
                "ok": False,
                "error": "Это тестовая RAG-база (admin_only): поиск по ней доступен только администраторам.",
            }
        if card is None:
            # опубликованные демо-базы платформы живут только в каталоге
            card = next((c for c in datarepo.rag_bases() if c.get("id") == rag_id), None)
        try:
            result = await indexer.search(
                rag_id,
                query,
                limit=limit,
                class_ids=class_ids,
                ontology_id=(card or {}).get("ontology_id"),
            )
        except indexer.SearchError as exc:
            return {"ok": False, "error": str(exc)}
        hits = result["hits"]
        ontology_context = result.get("ontology_context") or []
        note = (
            "Отвечайте только на основе этих результатов и указывайте "
            "источник (filename/doc_id) — не придумывайте факты сверх найденного."
        )
        if ontology_context:
            note += (
                " Поле ontology_context — определения понятий онтологии, к "
                "которым отнесён запрос (и связанных с ними по графу); они "
                "поясняют терминологию, но не заменяют содержимое найденных "
                "фрагментов."
            )
        if result.get("search_mode") == "text":
            note += " " + result.get("note", "")
        return {
            "ok": True,
            "rag_id": rag_id,
            "query": query,
            "count": len(hits),
            "results": hits,
            "ontology_context": ontology_context,
            "search_mode": result.get("search_mode", "vector"),
            "note": note,
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


    async def format_table(args: dict, ctx: dict | None) -> dict:
        """Собирает список записей (словарей) в Markdown-таблицу по заданному
        порядку колонок. Не запрашивает данные ниоткуда — только форматирует
        то, что уже есть у модели (например, результат search_corpus)."""
        columns = args.get("columns")
        rows = args.get("rows")
        if not isinstance(columns, list) or not columns:
            return {"ok": False, "error": "columns должен быть непустым списком строк"}
        if not isinstance(rows, list):
            return {"ok": False, "error": "rows должен быть списком объектов"}
        columns_full = [str(c) for c in columns]
        columns = columns_full[:20]
        truncated_cols = len(columns_full) > 20
        rows_full = rows
        rows = rows_full[:200]
        truncated_rows = len(rows_full) > 200

        _SYN = {
            "название": ("title", "name"),
            "авторы": ("authors",),
            "журнал": ("journal", "source"),
            "год": ("year",),
            "цитирования": ("цитирований", "citations", "cited_by_count"),
            "цитирований": ("цитирования", "citations", "cited_by_count"),
            "doi / pdf": ("doi", "pdf", "ссылка"),
            "doi": ("ссылка",),
        }

        def _row_value(row, col):
            if not isinstance(row, dict):
                return ""
            if col in row:
                return row[col]
            norm = {str(k).strip().casefold(): v for k, v in row.items()}
            key = str(col).strip().casefold()
            for cand in (key,) + _SYN.get(key, ()):
                if cand in norm and norm[cand] not in (None, ""):
                    return norm[cand]
            return ""

        def cell(row: dict, col: str) -> str:
            value = _row_value(row, col)
            text = "" if value is None else str(value)
            return text.replace("|", "\\|").replace("\n", " ")[:300]

        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        body_lines = [
            "| " + " | ".join(cell(row, c) for c in columns) + " |"
            for row in rows
            if isinstance(row, dict)
        ]
        markdown = "\n".join([header, divider, *body_lines])

        return {
            "ok": True,
            "markdown": markdown,
            "rows_count": len(body_lines),
            "truncated": truncated_rows or truncated_cols,
        }

    def _svg_chart(style: str, labels: list, values: list, title: str,
                   target: float | None = None, target_label: str = "") -> str:
        """Ручная сборка простого line/bar SVG без внешних библиотек."""
        import math
        import textwrap
        from html import escape

        width, height = 640, 380
        pad_left, pad_right, pad_top, pad_bottom = 60, 20, 40, 90
        plot_w = width - pad_left - pad_right
        plot_h = height - pad_top - pad_bottom
        nums = [float(v) for v in values]
        extra = [target] if target is not None else []
        vmin, vmax = min(nums + extra + [0]), max(nums + extra + [1])
        if vmax == vmin:
            vmax = vmin + 1
        vtop = vmax + (vmax - vmin) * 0.08  # запас сверху под подписи значений
        n = max(1, len(nums) - 1) if style == "line" else max(1, len(nums))
        step = plot_w / max(1, len(nums))
        slot = (plot_w / n) if style == "line" else step

        def x_at(i: int) -> float:
            if style == "line":
                return pad_left + (plot_w * i / n if n else 0)
            return pad_left + step * i + step * 0.15

        def y_at(v: float) -> float:
            return pad_top + plot_h - (plot_h * (v - vmin) / (vtop - vmin))

        def fmt(v: float) -> str:
            return f"{v:g}"

        parts = [
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
            f'font-family="sans-serif" font-size="12">',
            f'<text x="{width/2}" y="20" text-anchor="middle" font-size="14" font-weight="bold">'
            f'{escape(title)}</text>',
            f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top+plot_h}" stroke="#999"/>',
            f'<line x1="{pad_left}" y1="{pad_top+plot_h}" x2="{pad_left+plot_w}" y2="{pad_top+plot_h}" stroke="#999"/>',
        ]

        show_values = len(nums) <= 20
        if style == "line":
            points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(nums))
            parts.append(f'<polyline points="{points}" fill="none" stroke="#2563eb" stroke-width="2"/>')
            for i, v in enumerate(nums):
                parts.append(f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3" fill="#2563eb"/>')
                if show_values:
                    parts.append(
                        f'<text x="{x_at(i):.1f}" y="{y_at(v)-8:.1f}" text-anchor="middle" font-size="11">{fmt(v)}</text>'
                    )
        else:
            bar_w = step * 0.7
            for i, v in enumerate(nums):
                bx = x_at(i)
                by = y_at(v)
                bh = (pad_top + plot_h) - by
                parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="#2563eb"/>')
                if show_values:
                    parts.append(
                        f'<text x="{bx + bar_w/2:.1f}" y="{by-5:.1f}" text-anchor="middle" font-size="11">{fmt(v)}</text>'
                    )

        if target is not None:
            ty = y_at(target)
            parts.append(
                f'<line x1="{pad_left}" y1="{ty:.1f}" x2="{pad_left+plot_w}" y2="{ty:.1f}" '
                f'stroke="#dc2626" stroke-width="1.5" stroke-dasharray="6 4"/>'
            )
            parts.append(
                f'<text x="{pad_left+plot_w}" y="{ty-5:.1f}" text-anchor="end" fill="#dc2626" font-size="11">'
                f'{escape(target_label or "цель")}: {fmt(target)}</text>'
            )

        skip = max(1, math.ceil(46 / slot))
        wrap_w = max(6, int(slot * skip / 6.2))
        for i, label in enumerate(labels):
            if i % skip:
                continue
            lx = x_at(i) if style == "line" else x_at(i) + step * 0.35
            lines = textwrap.wrap(str(label), width=wrap_w, break_long_words=True)[:3] or [""]
            tspans = "".join(
                f'<tspan x="{lx:.1f}" dy="{0 if k == 0 else 13}">{escape(t)}</tspan>'
                for k, t in enumerate(lines)
            )
            parts.append(
                f'<text x="{lx:.1f}" y="{pad_top+plot_h+16}" text-anchor="middle" font-size="11">{tspans}</text>'
            )
        for tick in range(5):
            v = vmin + (vmax - vmin) * tick / 4
            parts.append(f'<text x="{pad_left-8}" y="{y_at(v)+4:.1f}" text-anchor="end">{v:.1f}</text>')

        parts.append("</svg>")
        return "".join(parts)

    async def run_polygon(args: dict, ctx: dict | None) -> dict:
        """Прогон полигона (сценарная модель портфеля по УГТ) — проверка гипотезы."""
        from . import polygons

        polygon_id = str(args.get("polygon_id") or "").strip()
        base: dict = {}
        if polygon_id:
            found = polygons.get_store().get(polygon_id, _rag_user(ctx))
            if found is None:
                return {"ok": False, "error": f"Полигон не найден: {polygon_id}"}
            base = found
        params = {**(base.get("params") or {}), **(args.get("params") or {})}
        scenarios = args.get("scenarios") if isinstance(args.get("scenarios"), list) else base.get("scenarios") or []
        hypothesis = args.get("hypothesis") if isinstance(args.get("hypothesis"), dict) else base.get("hypothesis") or {}
        try:
            sim = polygons.simulate(params, scenarios, hypothesis)
        except polygons.PolygonError as exc:
            return {"ok": False, "error": str(exc)}
        rows = []
        for r in sim["results"]:
            f = r["final"]
            rows.append({
                "сценарий": r["name"],
                "стартапы": f["startups"]["mean"],
                "лицензии": f["licenses"]["mean"],
                "застрявшие проекты": f["stale"]["mean"],
                "затраты, тыс. ₽": f["cost"]["mean"],
                "отдача на 1 ₽": f["roi"]["mean"],
                "гипотеза подтверждается, % прогонов": (
                    round(r["hypothesis_probability"] * 100) if r["hypothesis_probability"] is not None else None
                ),
            })
        return {
            "ok": True,
            "polygon_id": polygon_id or None,
            "hypothesis": sim["hypothesis"],
            "horizon_years": sim["params"]["horizon_years"],
            "runs": sim["params"]["runs"],
            "results": rows,
            "page_url": f"/polygons.html?id={polygon_id}" if polygon_id else "/polygons.html",
            "note": "Средние по прогонам Монте-Карло; ≥70 % прогонов — гипотеза устойчива, 40–70 % — зависит от удачи, <40 % — не подтверждается. Покажите таблицу через format_table и дайте ссылку на полигон.",
        }

    async def chart_generate(args: dict, ctx: dict | None) -> dict:
        """Строит line/bar график по числовым данным — SVG без внешних библиотек."""
        style = str(args.get("style") or "line").strip().lower()
        if style not in ("line", "bar"):
            return {"ok": False, "error": "style должен быть line или bar"}
        labels = args.get("labels")
        values = args.get("values")
        if not isinstance(labels, list) or not isinstance(values, list) or not values:
            return {"ok": False, "error": "labels и values должны быть непустыми списками одинаковой длины"}
        if len(labels) != len(values):
            return {"ok": False, "error": "labels и values разной длины"}
        try:
            [float(v) for v in values]
        except (TypeError, ValueError):
            return {"ok": False, "error": "values должны быть числами"}
        if len(values) > 50:
            return {"ok": False, "error": "Не более 50 точек на график"}
        title = str(args.get("title") or "")[:120]
        target = args.get("target")
        if target is not None:
            try:
                target = float(target)
            except (TypeError, ValueError):
                return {"ok": False, "error": "target должен быть числом"}
        target_label = str(args.get("target_label") or "")[:40]
        svg = _svg_chart(style, labels[:50], values[:50], title, target, target_label)
        return {"ok": True, "svg": svg, "points_count": len(values)}

    # Записи чата: ключ (пользователь сессии, первый навык сессии), отдельный
    # корень chat_records, чтобы не смешиваться с карточками агентов agt-….
    def _chat_records(ctx):
        import re
        from .agent_store import AgentRecordStore
        from .config import get_settings

        session = (ctx or {}).get("session") or {}
        user_id = str(session.get("user_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", user_id):
            return None, "", ""
        ids = session.get("skill_ids") or []
        skill = str(ids[0]) if ids else "chat"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", skill):
            skill = "chat"
        return AgentRecordStore(get_settings().runtime_dir / "chat_records"), user_id, skill

    async def save_record(args: dict, ctx: dict | None) -> dict:
        store, user_id, skill = _chat_records(ctx)
        if store is None:
            return {"ok": False, "error": "Записи доступны только авторизованному пользователю"}
        fields = args.get("fields")
        if not isinstance(fields, dict) or not fields:
            return {"ok": False, "error": "fields — непустой объект «поле: значение»"}
        if sum(1 for _ in store._dir(user_id, skill).glob("rec-*.json")) >= 1000:
            return {"ok": False, "error": "Достигнут предел: 1000 записей для этого навыка"}
        rec = store.add(user_id, skill, fields, str(args.get("source") or ""))
        return {"ok": True, "id": rec["id"], "сохранено_полей": len(rec["fields"])}

    async def get_records(args: dict, ctx: dict | None) -> dict:
        import time as _t
        store, user_id, skill = _chat_records(ctx)
        if store is None:
            return {"ok": False, "error": "Записи доступны только авторизованному пользователю"}
        try:
            limit = int(args.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        recs = store.list(user_id, skill, limit=1000)
        recs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        items = [
            {
                "id": r.get("id"),
                "сохранено": _t.strftime("%Y-%m-%d %H:%M", _t.localtime(r.get("created_at") or 0)),
                "источник": r.get("source") or "",
                "поля": r.get("fields") or {},
            }
            for r in recs[:limit]
        ]
        return {"ok": True, "всего": len(recs), "записи": items}

    async def _call_agent_stub(args: dict, ctx: dict | None) -> dict:
        """Заглушка: реальный вызов агента идёт через workflow_engine._call_tool_node,
        минуя обычный ToolRegistry.call (нужен sync-доступ к state/depth текущего
        графа, которого у обычного цикла нет). Если этот инструмент вызван из
        обычного run_agent_loop (не из workflow_engine) — сообщаем об этом честно,
        а не притворяемся, что вызов произошёл."""
        return {
            "ok": False,
            "error": "call_agent в чате пока не поддерживается: агенты работают по одному.",
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
            description="Возвращает каталог артефактов AIOS: kind=models (модели) или kind=skills (навыки-агенты платформы). Опциональный query фильтрует по подстроке.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["models", "skills"]},
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
                        "description": "Карточка: {project_id?, project_type?: startup|initiative, title?, description?, goal?, team_size?, participants?, departments?, budget?, groundwork?, technology?, trl?, market?, funding_stage?, commercialization?, kpi?, milestones?, risks?, groundwork_links?, groundwork_files?, presentation_files?, presentation?, artifacts?, status?}. artifacts — статусы и гипотезы артефактов пилота {rag?, models?, agents?, polygon?, social?}: значение — {status: 0..3, hypothesis?} или строка-гипотеза (тогда статус = 1 «гипотеза сформулирована»). Статусы: 0 не начато, 1 гипотеза сформулирована, 2 в процессе создания, 3 завершено.",
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
                        "description": "id онтологии (onto-…) для полного графа: своей или опубликованной в каталоге (чужая — только чтение)",
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
            description="Ищет ответы по содержимому опубликованной RAG-базы (векторный поиск; если у базы есть онтология и class_ids не передан явно — запрос сам классифицируется по онтологии и фильтр применяется автоматически, с расширением по связанным классам графа). Используйте, когда пользователь подключил RAG-базу в рабочем чате или явно попросил найти информацию в конкретной базе. Отвечайте только на основе найденных результатов, всегда указывайте источник (filename/doc_id) — не придумывайте факты сверх найденного; поле ontology_context в ответе — определения задействованных понятий онтологии, используйте их для точности терминологии.",
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
                        "description": "опционально: явно сузить поиск классами онтологии; если не задано и у базы есть онтология — определяется автоматически по смыслу запроса",
                    },
                },
                "required": ["rag_id", "query"],
            },
            handler=search_corpus,
        ),
        Tool(
            name="call_agent",
            description="Вызывает другого агента проекта как инструмент и дожидается его ответа "
            "(делегирование задачи). Вызывать можно только агентов того же проекта — чужие проекты "
            "недоступны. Используйте, когда часть работы явно относится к компетенции другого агента "
            "этого проекта (например, оркестрация расчёта передаёт задачу агенту подбора конструкции). "
            "Не вызывайте себя же или агента, который уже вызвал вас (глубина цепочки ограничена "
            "платформой).",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "id вызываемого агента (agt-…)"},
                    "query": {"type": "string", "description": "задача/вопрос для вызываемого агента на естественном языке"},
                },
                "required": ["agent_id", "query"],
            },
            handler=_call_agent_stub,
        ),
        Tool(
            name="get_records",
            description="Возвращает накопленные записи этого агента (его память: что он уже сохранил "
            "через save_record) — используйте перед сохранением новой записи, чтобы не дублировать "
            "уже найденное, и для сводок по накопленному. Записи привязаны к пользователю и навыку "
            "и сохраняются между сессиями.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "сколько последних записей вернуть (по умолчанию 50)"},
                },
            },
            handler=get_records,
        ),
        Tool(
            name="save_record",
            description="Сохраняет одну предметную запись как результат вашей работы (например, "
            "найденный аналог, извлечённые данные из документа, посчитанное значение) — используйте, "
            "когда ваша роль подразумевает накопление списка результатов (не для обычного текстового "
            "ответа на вопрос). Каждый вызов добавляет одну запись в накопительный список "
            "этого навыка: он свой у каждого пользователя и сохраняется между сессиями.",
            parameters={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "object",
                        "description": "поля записи (до 20 пар ключ-значение), например {\"название\": \"...\", \"значение\": \"...\"}",
                    },
                    "source": {"type": "string", "description": "источник записи (документ, ссылка и т.п.)"},
                },
                "required": ["fields"],
            },
            handler=save_record,
        ),
        Tool(
            name="format_table",
            description="Собирает переданные записи в Markdown-таблицу с заданным порядком колонок. "
            "Используйте, когда нужно оформить структурированные данные (например, результат "
            "search_corpus или посчитанные значения) в виде таблицы в ответе — не пересказывайте "
            "данные текстом построчно, если их больше двух-трёх.",
            parameters={
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "названия колонок в нужном порядке",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "записи; ключи объектов должны совпадать с названиями колонок",
                    },
                },
                "required": ["columns", "rows"],
            },
            handler=format_table,
        ),
        Tool(
            name="run_polygon",
            description="Проверяет гипотезу офиса проректора на полигоне — сценарной модели портфеля проектов по УГТ (Монте-Карло, без LLM): сравнивает базовый сценарий с управленческими решениями (стартап-студия, гранты, контрольные точки) и считает, в какой доле прогонов гипотеза подтверждается. Передайте polygon_id примера (pg-1a0e5c0201 — стартап-студия, pg-1a0e5c0202 — ревизия портфеля) или своего полигона; params/scenarios/hypothesis — необязательные правки поверх него.",
            parameters={
                "type": "object",
                "properties": {
                    "polygon_id": {"type": "string", "description": "id полигона (pg-…)"},
                    "params": {"type": "object", "description": "правки параметров: horizon_years, intake_per_year, entry_level, exit_level, annual_budget (тыс. ₽), fixed_cost, p_license, license_income, investment_per_startup, initial {УГТ: число}, levels {УГТ: {p_up, p_drop, cost}}"},
                    "scenarios": {"type": "array", "items": {"type": "object"}, "description": "сценарии: {id, name, valley_boost %, up_boost %, drop_change %, p_license_change п.п., intake_per_year, annual_budget, fixed_cost}"},
                    "hypothesis": {"type": "object", "description": "{statement, metric: startups|licenses|exits|income|investments|cost|roi|stale|active, op: >=|<=, value, scenario_id}"},
                },
            },
            handler=run_polygon,
        ),
        Tool(
            name="chart_generate",
            description="Строит line (линейный) или bar (столбчатый) график по числовым данным, "
            "возвращает готовый SVG в поле svg. Используйте для наглядного представления "
            "зависимостей (например, свойство от параметра, изменение показателя по замерам) "
            "вместо перечисления чисел текстом. График показывается пользователю автоматически "
            "сразу после вызова: НЕ копируйте поле svg в ответ и не пишите блоков кода с SVG, "
            "а кратко (1–2 предложения) поясните выводы. Необязательные target и "
            "target_label рисуют целевую линию (например, цель ТЗ).",
            parameters={
                "type": "object",
                "properties": {
                    "style": {"type": "string", "enum": ["line", "bar"], "default": "line"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "подписи точек по оси X",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "числовые значения по оси Y, той же длины, что labels",
                    },
                    "title": {"type": "string", "description": "заголовок графика"},
                    "target": {"type": "number", "description": "необязательно: значение целевой линии (например, цель по ТЗ) в тех же единицах, что values"},
                    "target_label": {"type": "string", "description": "необязательно: подпись целевой линии, например «цель ТЗ»"},
                },
                "required": ["labels", "values"],
            },
            handler=chart_generate,
        ),
    ]
