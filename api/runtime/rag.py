"""Хранилище RAG-баз (api/runtime/rag.py).

Черновики живут персонально — AIOS_RUNTIME_DIR/rag/{user_id}/ — по одному
JSON-файлу на базу (id вида rag-…), запись атомарная. Публикация (статус
active) выкладывает карточку в общий каталог data/rag_bases.json, после чего
база появляется в разделе «Данные и знания», в поиске search_knowledge и
становится доступна для подключения в рабочем чате — доступ на чтение
открытый для всех пользователей платформы (модель А: снятие барьеров
передачи знаний — см. цель пилота №3 в README), без разделения по автору
или проекту.

Этот модуль отвечает только за конфигурацию и метаданные базы — какие
документы в неё загружены, в каком они статусе индексации, к какому проекту
и онтологии привязана база. Сами тексты чанков и векторы живут не здесь, а
в Qdrant (коллекция collection_name); JSON-карточка на них только ссылается
через агрегированные счётчики (documents_count, chunks_count) и обновляется
внешним индексатором через update_document().

Формат рассчитан на связку с онтологией (навык «Конструктор онтологий»,
data/skills/ontology-engineer.md): ontology_id — ссылка на опубликованную
онтологию проекта; class_ids каждого документа — классы, которыми размечены
его чанки (см. rag_hints.extraction_targets онтологии), используются на
retrieval как фильтр перед векторным поиском (GraphRAG-лайт: онтология —
понятийный фильтр над векторным поиском, не полный обход графа).

Сценарий сбора конфигурации с пользователем — навык «Конструктор RAG»
(data/skills/rag-builder.md, готовится по аналогии с ontology-engineer.md):
навык досылает карточку целиком после каждого шага, поэтому save() —
upsert с заменой списка документов и мягкой валидацией (полнота требуется
только к публикации).
"""

import json
import os
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from . import artifact_icons
from .config import get_settings
from .projects import bump_artifact_status, get_store as get_project_store

# тот же формат идентификаторов владельца, что и в хранилищах кабинета/
# проектов/онтологий
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
RAG_ID_RE = re.compile(r"^rag-[a-f0-9]{8,12}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-f0-9]{8,12}$")
ONTOLOGY_ID_RE = re.compile(r"^onto-[a-f0-9]{8,12}$")
DOC_ID_RE = re.compile(r"^doc-[a-f0-9]{8,12}$")
# локальные id классов онтологии, на которые ссылаются документы
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE)

DOC_STATUS_QUEUED = "queued"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_READY = "ready"
DOC_STATUS_ERROR = "error"
DOC_STATUSES = (DOC_STATUS_QUEUED, DOC_STATUS_PROCESSING, DOC_STATUS_READY, DOC_STATUS_ERROR)

# фиксированный стек первой версии (см. решение по архитектуре RAG)
VECTOR_DB = "Qdrant"
# через ProxyAPI.ru (тот же ключ, что и LLM); другая модель — AIOS_EMBEDDING_MODEL
# (например, Qwen3-Embedding у OpenAI-совместимого прокси)
EMBEDDING_MODEL = os.getenv("AIOS_EMBEDDING_MODEL") or "text-embedding-3-small"

# лимиты карточки
MAX_TAGS = 10
MAX_CORPUS_TYPES = 10
MAX_TYPICAL_QUERIES = 5
MAX_DOCUMENTS = 500
MAX_CLASS_IDS_PER_DOC = 30

# строковые поля карточки → лимит символов
TEXT_FIELDS = {
    "name": 200,
    "domain": 300,
    "description": 2_500,
    "author": 200,
    "corpus_volume": 200,
    "corpus_format": 60,
    "access_restrictions": 500,
    "owner": 200,
    "update_frequency": 100,
}
TAG_MAX = 60
CORPUS_TYPE_MAX = 120
TYPICAL_QUERY_MAX = 300
FILENAME_MAX = 255
DOC_ERROR_MAX = 500


class RagError(ValueError):
    """Некорректные данные RAG-базы."""


def _clean_text(value, limit: int) -> str:
    """Строка без лишних пробелов, обрезанная до лимита; None → ''."""
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _clean_list(value, max_items: int, item_limit: int) -> list:
    """Список непустых строк из списка или многострочной строки."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        raise RagError("Ожидается список строк или многострочная строка")
    items: list[str] = []
    for item in value:
        cleaned = _clean_text(item, item_limit)
        if cleaned:
            items.append(cleaned)
        if len(items) >= max_items:
            break
    return items


# ---------------------------------------------------------------------------
# Валидация документов и карточки
# ---------------------------------------------------------------------------


def _validate_documents(documents: list, problems: list) -> list:
    """Проверяет список документов, возвращает список их корректных id."""
    doc_ids: list[str] = []
    if len(documents) > MAX_DOCUMENTS:
        problems.append(f"документов больше {MAX_DOCUMENTS}")
    for i, doc in enumerate(documents, 1):
        label = f"документ #{i}"
        if not isinstance(doc, dict):
            problems.append(f"{label}: ожидается объект")
            continue
        did = _clean_text(doc.get("doc_id"), 64)
        if not DOC_ID_RE.match(did):
            problems.append(f"{label}: некорректный doc_id «{did}»")
        elif did in doc_ids:
            problems.append(f"документ «{did}»: дублирующийся doc_id")
        else:
            doc_ids.append(did)
        shown = f"документ «{did or label}»"
        if not _clean_text(doc.get("filename"), FILENAME_MAX):
            problems.append(f"{shown}: пустое имя файла")
        status = _clean_text(doc.get("status"), 20) or DOC_STATUS_QUEUED
        if status not in DOC_STATUSES:
            problems.append(
                f"{shown}: статус «{status}» не из {', '.join(DOC_STATUSES)}"
            )
        class_ids = doc.get("class_ids")
        if class_ids is not None:
            if not isinstance(class_ids, list):
                problems.append(f"{shown}: class_ids ожидается списком")
            elif len(class_ids) > MAX_CLASS_IDS_PER_DOC:
                problems.append(f"{shown}: class_ids больше {MAX_CLASS_IDS_PER_DOC}")
        chunks_count = doc.get("chunks_count")
        if chunks_count is not None and not isinstance(chunks_count, int):
            problems.append(f"{shown}: chunks_count ожидается числом")
    return doc_ids


def validate_rag(card: dict, require_complete: bool = False) -> list:
    """Список проблем карточки (пустой — карточка корректна).

    Мягкий режим (по умолчанию) проверяет то, что заполнено: незаполненные
    части черновика ошибкой не считаются. require_complete=True — полная
    проверка перед публикацией: имя, описание, минимум 1 документ в статусе
    ready и минимум 1 типовой вопрос.
    """
    problems: list[str] = []
    raw_documents = card.get("documents") or []
    documents = [d for d in raw_documents if d is not None]

    doc_ids = _validate_documents(documents, problems)

    ontology_id = _clean_text(card.get("ontology_id"), 40)
    if ontology_id and not ONTOLOGY_ID_RE.match(ontology_id):
        problems.append(f"некорректный ontology_id: {ontology_id}")

    if require_complete:
        if not _clean_text(card.get("name"), TEXT_FIELDS["name"]):
            problems.append("для публикации нужно название базы")
        if not _clean_text(card.get("description"), TEXT_FIELDS["description"]):
            problems.append("для публикации нужно описание корпуса")
        ready_docs = [
            d for d in documents
            if isinstance(d, dict) and _clean_text(d.get("status"), 20) == DOC_STATUS_READY
        ]
        if not ready_docs:
            problems.append(
                "для публикации нужен минимум 1 документ в статусе ready "
                "(проиндексированный)"
            )
        if not (card.get("typical_queries") or []):
            problems.append("для публикации нужен минимум 1 типовой вопрос")
    return problems


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


class RagStore:
    def __init__(self, root: Path, data_dir: Path) -> None:
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.catalog_path = self.data_dir / "rag_bases.json"

    # ----- низкоуровневые операции -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise RagError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _path(self, user_id: str, rag_id: str) -> Path:
        if not RAG_ID_RE.match(rag_id or ""):
            raise RagError(f"Некорректный идентификатор базы: {rag_id}")
        return self._dir(user_id) / f"{rag_id}.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return card if isinstance(card, dict) and card.get("id") else None

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    # ----- чтение -----

    def list(self, user_id: str) -> list:
        """RAG-базы владельца, свежие сверху."""
        directory = self._dir(user_id)
        if not directory.is_dir():
            return []
        cards = [
            card
            for path in sorted(directory.glob("rag-*.json"))
            if (card := self._read(path)) is not None
        ]
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    def get(self, user_id: str, rag_id: str) -> dict | None:
        try:
            path = self._path(user_id, rag_id)
        except RagError:
            return None
        card = self._read(path)
        if card and card.get("id") != rag_id:
            return None
        return card

    def find(self, rag_id: str) -> dict | None:
        """RAG-база по id у любого владельца (служебное: индексатор, админ)."""
        if not RAG_ID_RE.match(rag_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{rag_id}.json"):
            card = self._read(path)
            if card and card.get("id") == rag_id:
                return card
        return None

    def list_all(self) -> list:
        """Все RAG-базы всех владельцев, свежие сверху (служебное: участникам
        проекта открываются базы, привязанные к их проекту)."""
        cards: list[dict] = []
        if not self.root.is_dir():
            return cards
        for path in self.root.glob("*/*.json"):
            card = self._read(path)
            if card and card.get("id"):
                cards.append(card)
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    # ----- запись -----

    def save(self, user_id: str, card: dict) -> dict:
        """Создаёт или обновляет RAG-базу (upsert), возвращая сохранённую.

        Правила слияния — те же, что у OntologyStore:
          - строковые поля обновляются только переданными значениями;
          - documents при передаче заменяется целиком — навык/индексатор
            досылает текущий список полностью;
          - статус переключается только явно (поле status); создать базу
            сразу со статусом active нельзя — публикация отдельный шаг;
          - каждый save инкрементирует version; статус active автоматически
            (и идемпотентно) публикует базу в каталог data/.
        """
        raw = card if isinstance(card, dict) else {}
        rag_id = _clean_text(raw.get("rag_id") or raw.get("id"), 40)
        existing = self.get(user_id, rag_id) if rag_id else None
        if rag_id and not existing:
            raise RagError(f"RAG-база не найдена: {rag_id}")

        data = dict(existing) if existing else {
            "id": "rag-" + secrets.token_hex(5),
            "user_id": user_id,
            "created_at": time.time(),
            "status": STATUS_DRAFT,
            "version": 0,
            "vector_db": VECTOR_DB,
            "embedding_model": EMBEDDING_MODEL,
            "documents": [],
            "typical_queries": [],
            "tags": [],
            "citation_required": True,
            "sensitive": False,
        }
        if not existing:
            data["collection_name"] = data["id"]

        for field, limit in TEXT_FIELDS.items():
            if field in raw:
                data[field] = _clean_text(raw.get(field), limit)

        if raw.get("project_id") is not None:
            pid = _clean_text(raw.get("project_id"), 40)
            if pid and not PROJECT_ID_RE.match(pid):
                raise RagError(f"Некорректный project_id: {pid}")
            data["project_id"] = pid
        if raw.get("ontology_id") is not None:
            oid = _clean_text(raw.get("ontology_id"), 40)
            if oid and not ONTOLOGY_ID_RE.match(oid):
                raise RagError(f"Некорректный ontology_id: {oid}")
            data["ontology_id"] = oid

        if "tags" in raw:
            data["tags"] = _clean_list(raw.get("tags"), MAX_TAGS, TAG_MAX)
        if "corpus_types" in raw:
            data["corpus_types"] = _clean_list(
                raw.get("corpus_types"), MAX_CORPUS_TYPES, CORPUS_TYPE_MAX
            )
        if "typical_queries" in raw:
            data["typical_queries"] = _clean_list(
                raw.get("typical_queries"), MAX_TYPICAL_QUERIES, TYPICAL_QUERY_MAX
            )
        if "citation_required" in raw:
            data["citation_required"] = bool(raw.get("citation_required"))
        if "sensitive" in raw:
            data["sensitive"] = bool(raw.get("sensitive"))
        # тестовая база (admin_only): флаг сохраняется при правках и уходит
        # в карточку каталога, где фильтруется на клиенте для не-админов
        if "admin_only" in raw:
            data["admin_only"] = bool(raw.get("admin_only"))

        if raw.get("documents") is not None:
            if not isinstance(raw.get("documents"), list):
                raise RagError("Поле documents ожидается списком")
            data["documents"] = raw.get("documents")

        status = _clean_text(raw.get("status"), 20)
        if status:
            if status not in STATUSES:
                raise RagError("Статус должен быть одним из: " + ", ".join(STATUSES))
            if existing is None and status == STATUS_ACTIVE:
                raise RagError(
                    "Новая RAG-база сохраняется как черновик; статус active "
                    "ставится отдельным вызовом после подтверждения пользователя"
                )
            if status != data.get("status"):
                data["status"] = status
                data["status_changed_at"] = time.time()

        problems = validate_rag(data, require_complete=data["status"] == STATUS_ACTIVE)
        if problems:
            raise RagError("; ".join(problems[:12]))

        docs = data.get("documents") or []
        data["documents_count"] = len(docs)
        data["chunks_count"] = sum(
            int(d.get("chunks_count") or 0) for d in docs if isinstance(d, dict)
        )
        class_coverage: list[str] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            for cid in d.get("class_ids") or []:
                cid = str(cid)
                if cid not in class_coverage:
                    class_coverage.append(cid)
        data["class_coverage"] = class_coverage

        data["version"] = int(data.get("version") or 0) + 1
        data["updated_at"] = time.time()
        self._write(self._path(user_id, data["id"]), data)
        if data["status"] == STATUS_ACTIVE:
            self.publish(data)
        if data.get("project_id"):
            # синхронизация таблицы «Статус пилотного проекта»: база в работе —
            # «в процессе создания» (2), опубликованная — «завершено» (3)
            bump_artifact_status(
                data["project_id"], "rag",
                3 if data["status"] == STATUS_ACTIVE else 2,
            )
        return data

    def set_class_snapshot(self, user_id: str, rag_id: str, snapshot: dict) -> dict | None:
        """Отмечает, по какой версии онтологии сейчас размечены чанки RAG-базы
        (см. api/routers/rag.py: ontology-check / reclassify) — техническая
        метка для сверки версий, не пользовательская правка карточки: не
        трогает version/updated_at/публикацию, в отличие от save()."""
        card = self.get(user_id, rag_id)
        if card is None:
            return None
        card = dict(card)
        card["class_snapshot"] = snapshot
        self._write(self._path(user_id, rag_id), card)
        return card

    def update_document(
        self, user_id: str, rag_id: str, doc_id: str, patch: dict
    ) -> dict:
        """Точечно обновляет статус/счётчики одного документа (для индексатора).

        В отличие от save(), не требует пересылки всей карточки — вызывается
        фоновой задачей индексации по мере обработки файла (queued →
        processing → ready/error), не диалоговым навыком.
        """
        card = self.get(user_id, rag_id)
        if card is None:
            raise RagError(f"RAG-база не найдена: {rag_id}")
        documents = list(card.get("documents") or [])
        found = False
        for i, d in enumerate(documents):
            if isinstance(d, dict) and d.get("doc_id") == doc_id:
                merged = dict(d)
                merged.update(patch)
                documents[i] = merged
                found = True
                break
        if not found:
            raise RagError(f"Документ не найден в базе: {doc_id}")
        return self.save(user_id, {**card, "documents": documents})

    def delete(self, user_id: str, rag_id: str) -> bool:
        """Удаляет RAG-базу владельца (и её публикацию); True — если была.

        Не удаляет коллекцию в Qdrant — это отдельный вызов индексатора
        (векторное хранилище вне зоны ответственности этого модуля).
        """
        try:
            path = self._path(user_id, rag_id)
        except RagError:
            return False
        existed = path.is_file()
        if existed:
            path.unlink()
        self._unpublish(rag_id)
        return existed

    # ----- публикация в каталог «Данные и знания» -----

    def _load_catalog(self) -> dict:
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": []}
        return catalog if isinstance(catalog, dict) else {"items": []}

    def catalog_items(self) -> list:
        """Опубликованные карточки из каталога data/rag_bases.json."""
        items = self._load_catalog().get("items")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def _project_title(self, project_id: str) -> str:
        """Название проекта по id; '' — проект не найден или id пуст.

        Нужен, чтобы в каталоге «Данные и знания» вместе с project_id
        показывалось название проекта (кликабельная ссылка на карточку
        проекта на странице «Проекты»).
        """
        if not project_id:
            return ""
        try:
            card = get_project_store().find(project_id)
        except Exception:  # каталог проектов недоступен — публикуем без названия
            return ""
        return (card or {}).get("title") or ""

    def catalog_entry(self, card: dict) -> dict:
        """Карточка опубликованной RAG-базы для data/rag_bases.json.

        Открытый доступ на чтение (модель А): карточка не несёт признака
        владельца как ограничения — author/project только информационные.
        project — название проекта по project_id (не author!); без
        привязки к проекту остаётся пустым, а не подменяется автором.
        image_url — сгенерированный логотип (artifact_icons), если файл
        уже создан; до завершения фоновой генерации — плейсхолдер.
        """
        entry = {
            "id": card["id"],
            "name": card.get("name") or "База без названия",
            "image_url": artifact_icons.logo_url(card["id"])
            or "assets/icons/rag/default.svg",
            "project": self._project_title(card.get("project_id", "")),
            "domain": card.get("domain", ""),
            "description_ru": card.get("description", ""),
            "documents_count": card.get("documents_count", 0),
            "vector_db": card.get("vector_db", VECTOR_DB),
            "embedding_model": card.get("embedding_model", EMBEDDING_MODEL),
            "status": "Активна",
            "access_url": "",
            "tags": card.get("tags") or [],
            "ontology_id": card.get("ontology_id", ""),
            "project_id": card.get("project_id", ""),
            "sensitive": bool(card.get("sensitive")),
        }
        if card.get("admin_only"):
            entry["admin_only"] = True
        return entry

    def publish(self, card: dict) -> dict:
        """Выкладывает карточку в общий каталог; идемпотентно."""
        if card.get("status") != STATUS_ACTIVE:
            raise RagError("Публиковать можно только базу в статусе active")
        problems = validate_rag(card, require_complete=True)
        if problems:
            raise RagError("; ".join(problems[:12]))

        entry = self.catalog_entry(card)
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == entry["id"])
        ]
        items.append(entry)
        self._write(self.catalog_path, {"items": items})
        return entry

    def _unpublish(self, rag_id: str) -> None:
        """Убирает базу из каталога (если была)."""
        if not RAG_ID_RE.match(rag_id or ""):
            return
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == rag_id)
        ]
        self._write(self.catalog_path, {"items": items})

    # ----- представления -----

    def public_card(self, card: dict) -> dict:
        """Карточка для API/UI: без служебного user_id (он и так из токена)."""
        return {k: v for k, v in card.items() if k != "user_id"}

    def summary(self, card: dict) -> dict:
        """Компактная сводка для списков: без полного списка документов."""
        view = self.public_card(card)
        docs = view.pop("documents", []) or []
        view["documents_summary"] = [
            {
                "doc_id": d.get("doc_id"),
                "filename": d.get("filename"),
                "status": d.get("status"),
                "chunks_count": d.get("chunks_count", 0),
            }
            for d in docs
            if isinstance(d, dict)
        ]
        return view


@lru_cache(maxsize=1)
def get_store() -> RagStore:
    """Синглтон хранилища RAG-баз; в тестах сбрасывается cache_clear()."""
    settings = get_settings()
    return RagStore(settings.runtime_dir / "rag", settings.rag_data_dir)
