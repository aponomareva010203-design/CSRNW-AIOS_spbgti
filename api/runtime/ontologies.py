"""Хранилище сконструированных онтологий (api/runtime/ontologies.py).

Черновики живут персонально — AIOS_RUNTIME_DIR/ontologies/{user_id}/ — по
одному JSON-файлу на онтологию (id вида onto-…), запись атомарная. Публикация
(статус active) выкладывает карточку в общий каталог data/ontologies.json и
граф в data/ontologies/objects/{id}.json (раздаётся статикой /data), после
чего онтология появляется в разделе «Данные и знания» и в поиске
search_knowledge.

Формат графа рассчитан на будущую RAG-систему (навык «Конструктор RAG»):

    competency_questions — вопросы, на которые онтология должна отвечать
                           (метод competency questions; готовый тестовый
                           набор запросов для оценки ретрива);
    classes[].description/synonyms — материал для эмбеддингов и NER;
    relations + rag_hints.retrieval_paths — маршруты графового ретрива.

Сценарий сбора онтологии с пользователем — навык «Конструктор онтологий»
(data/skills/ontology-engineer.md); техническая валидация графа —
validate_graph() ниже: навык досылает карточку целиком после каждого шага,
поэтому save() — upsert с заменой графа и мягкой валидацией (полный граф
обязателен только к публикации).
"""

import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings
from .projects import bump_artifact_status, get_store as get_project_store

# тот же формат идентификаторов владельца, что и в хранилищах кабинета/проектов
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
ONTOLOGY_ID_RE = re.compile(r"^onto-[a-f0-9]{8,12}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-f0-9]{8,12}$")
# локальные id классов/связей/атрибутов: латиница, цифры, дефис/подчёркивание
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE)

# лимиты графа онтологии
MAX_CLASSES = 200
MAX_RELATIONS = 120
MAX_CQ = 30
MAX_ATTRIBUTES_PER_CLASS = 30
MAX_SYNONYMS_PER_CLASS = 20
MAX_KEYWORDS_PER_CLASS = 20
MAX_SOURCES = 20

# строковые поля карточки → лимит символов
TEXT_FIELDS = {
    "name": 200,
    "domain": 300,
    "description": 2_500,
    "author": 200,
}
CLASS_NAME_MAX = 120
CLASS_DESC_MAX = 1_000
CQ_MAX = 600
COVERAGE_ITEM_MAX = 300

ATTRIBUTE_TYPES = ("string", "int", "float", "bool", "enum", "datetime", "range")


class OntologyError(ValueError):
    """Некорректные данные онтологии."""


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
        raise OntologyError("Ожидается список строк или многострочная строка")
    items: list[str] = []
    for item in value:
        cleaned = _clean_text(item, item_limit)
        if cleaned:
            items.append(cleaned)
        if len(items) >= max_items:
            break
    return items


# ---------------------------------------------------------------------------
# Валидация графа
# ---------------------------------------------------------------------------


def _validate_classes(classes: list, problems: list) -> list:
    """Проверяет классы, возвращает список их корректных id."""
    class_ids: list[str] = []
    for i, cls in enumerate(classes, 1):
        label = f"класс #{i}"
        if not isinstance(cls, dict):
            problems.append(f"{label}: ожидается объект")
            continue
        cid = _clean_text(cls.get("id"), 64)
        if not SLUG_RE.match(cid):
            problems.append(
                f"{label}: некорректный id «{cid}» (латиница/цифры/дефис, 2–64 символа)"
            )
        elif cid in class_ids:
            problems.append(f"класс «{cid}»: дублирующийся id")
        else:
            class_ids.append(cid)
        shown = f"класс «{cid or label}»"
        if not _clean_text(cls.get("name"), CLASS_NAME_MAX):
            problems.append(f"{shown}: пустое имя")
        parent = _clean_text(cls.get("parent"), 64)
        if parent and parent == cid:
            problems.append(f"{shown}: родитель совпадает с самим классом")

        attrs = cls.get("attributes")
        if attrs is not None and not isinstance(attrs, list):
            problems.append(f"{shown}: attributes ожидается списком")
            attrs = []
        attrs = [a for a in (attrs or []) if a is not None]
        if len(attrs) > MAX_ATTRIBUTES_PER_CLASS:
            problems.append(f"{shown}: больше {MAX_ATTRIBUTES_PER_CLASS} атрибутов")
        attr_ids: list[str] = []
        for a in attrs:
            if not isinstance(a, dict):
                problems.append(f"{shown}: атрибут ожидается объектом")
                continue
            aid = _clean_text(a.get("id"), 64)
            if not SLUG_RE.match(aid):
                problems.append(f"{shown}: некорректный id атрибута «{aid}»")
            elif aid in attr_ids:
                problems.append(f"{shown}: дублирующийся атрибут «{aid}»")
            else:
                attr_ids.append(aid)
            if not _clean_text(a.get("name"), CLASS_NAME_MAX):
                problems.append(f"{shown}: атрибут «{aid or 'без id'}» без имени")
            atype = _clean_text(a.get("type"), 30).lower()
            if atype and atype not in ATTRIBUTE_TYPES:
                problems.append(
                    f"{shown}: тип атрибута «{atype}» не из {', '.join(ATTRIBUTE_TYPES)}"
                )

        for key, cap in (
            ("synonyms", MAX_SYNONYMS_PER_CLASS),
            ("keywords", MAX_KEYWORDS_PER_CLASS),
        ):
            seq = cls.get(key)
            if seq is None:
                continue
            if not isinstance(seq, list):
                problems.append(f"{shown}: {key} ожидается списком")
            elif len(seq) > cap:
                problems.append(f"{shown}: {key} больше {cap}")
    return class_ids


def validate_graph(onto: dict, require_complete: bool = False) -> list:
    """Список проблем графа (пустой — граф корректен).

    Мягкий режим (по умолчанию) проверяет то, что заполнено: незаполненные
    части черновика ошибкой не считаются. require_complete=True — полная
    проверка перед публикацией: имя, ≥2 классов, ≥1 связи, ≥1 компетентностного
    вопроса и корректность всех ссылок.
    """
    problems: list[str] = []
    raw_classes = onto.get("classes") or []
    raw_relations = onto.get("relations") or []
    raw_cqs = onto.get("competency_questions") or []
    classes = [c for c in raw_classes if c is not None]
    relations = [r for r in raw_relations if r is not None]
    cqs = [q for q in raw_cqs if q is not None]

    if len(raw_classes) > MAX_CLASSES:
        problems.append(f"классов больше {MAX_CLASSES}")
    if len(raw_relations) > MAX_RELATIONS:
        problems.append(f"связей больше {MAX_RELATIONS}")
    if len(raw_cqs) > MAX_CQ:
        problems.append(f"компетентностных вопросов больше {MAX_CQ}")

    class_ids = _validate_classes(classes, problems)
    id_set = set(class_ids)
    parent_of = {}
    for cls in classes:
        if isinstance(cls, dict) and cls.get("id"):
            parent_of[_clean_text(cls.get("id"), 64)] = _clean_text(cls.get("parent"), 64)

    relation_ids: list[str] = []
    for i, rel in enumerate(relations, 1):
        label = f"связь #{i}"
        if not isinstance(rel, dict):
            problems.append(f"{label}: ожидается объект")
            continue
        rid = _clean_text(rel.get("id"), 64)
        if not SLUG_RE.match(rid):
            problems.append(f"{label}: некорректный id «{rid}»")
        elif rid in relation_ids:
            problems.append(f"связь «{rid}»: дублирующийся id")
        else:
            relation_ids.append(rid)
        shown = f"связь «{rid or label}»"
        if not _clean_text(rel.get("name"), CLASS_NAME_MAX):
            problems.append(f"{shown}: пустое имя")
        for side in ("domain_class", "range_class"):
            ref = _clean_text(rel.get(side), 64)
            if not ref:
                problems.append(f"{shown}: пустой {side}")
            elif ref not in id_set:
                problems.append(f"{shown}: {side} ссылается на неизвестный класс «{ref}»")

    # циклы в иерархии is-a
    for cid in parent_of:
        seen: set = set()
        cur = cid
        while cur:
            if cur in seen:
                problems.append(f"цикл в иерархии классов через «{cur}»")
                break
            seen.add(cur)
            cur = parent_of.get(cur, "")

    cq_ids: list[str] = []
    for i, cq in enumerate(cqs, 1):
        if not isinstance(cq, dict):
            problems.append(f"компетентностный вопрос #{i}: ожидается объект")
            continue
        qid = _clean_text(cq.get("id"), 64) or f"cq-{i}"
        if qid in cq_ids:
            problems.append(f"компетентностный вопрос «{qid}»: дублирующийся id")
        else:
            cq_ids.append(qid)
        if not _clean_text(cq.get("question"), CQ_MAX):
            problems.append(f"компетентностный вопрос «{qid}»: пустая формулировка")
        coverage = cq.get("coverage")
        if coverage is not None and not isinstance(coverage, list):
            problems.append(f"вопрос «{qid}»: coverage ожидается списком")

    hints = onto.get("rag_hints")
    if hints is not None and not isinstance(hints, dict):
        problems.append("rag_hints ожидается объектом")
        hints = {}
    hints = hints or {}
    for key in ("extraction_targets", "retrieval_paths", "sources"):
        if hints.get(key) is not None and not isinstance(hints.get(key), list):
            problems.append(f"rag_hints.{key} ожидается списком")
    for tid in hints.get("extraction_targets") or []:
        if str(tid) not in id_set:
            problems.append(f"rag_hints.extraction_targets: неизвестный класс «{tid}»")
    if len(hints.get("sources") or []) > MAX_SOURCES:
        problems.append(f"rag_hints.sources больше {MAX_SOURCES}")

    if require_complete:
        if not _clean_text(onto.get("name"), TEXT_FIELDS["name"]):
            problems.append("для публикации нужно название онтологии")
        if len(class_ids) < 2:
            problems.append(f"для публикации нужно минимум 2 класса (сейчас {len(class_ids)})")
        if not relation_ids:
            problems.append("для публикации нужна минимум 1 связь между классами")
        if not cq_ids:
            problems.append("для публикации нужен минимум 1 компетентностный вопрос")
    return problems


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


class OntologyStore:
    def __init__(self, root: Path, data_dir: Path) -> None:
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.objects_dir = self.data_dir / "ontologies" / "objects"
        self.catalog_path = self.data_dir / "ontologies.json"

    # ----- низкоуровневые операции -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise OntologyError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _path(self, user_id: str, ontology_id: str) -> Path:
        if not ONTOLOGY_ID_RE.match(ontology_id or ""):
            raise OntologyError(f"Некорректный идентификатор онтологии: {ontology_id}")
        return self._dir(user_id) / f"{ontology_id}.json"

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
        """Онтологии владельца, свежие сверху."""
        directory = self._dir(user_id)
        if not directory.is_dir():
            return []
        cards = [
            card
            for path in sorted(directory.glob("onto-*.json"))
            if (card := self._read(path)) is not None
        ]
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    def get(self, user_id: str, ontology_id: str) -> dict | None:
        try:
            path = self._path(user_id, ontology_id)
        except OntologyError:
            return None
        card = self._read(path)
        if card and card.get("id") != ontology_id:
            return None
        return card

    def find(self, ontology_id: str) -> dict | None:
        """Онтология по id у любого владельца (служебное: админские операции)."""
        if not ONTOLOGY_ID_RE.match(ontology_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{ontology_id}.json"):
            card = self._read(path)
            if card and card.get("id") == ontology_id:
                return card
        return None

    def _find_duplicate_draft(
        self, user_id: str, name: str, project_id: str
    ) -> dict | None:
        """Черновик пользователя с тем же названием и проектом (для upsert)."""
        if not name:
            return None
        for card in self.list(user_id):
            if card.get("status") != STATUS_DRAFT:
                continue
            if card.get("name") != name:
                continue
            if (card.get("project_id") or "") != project_id:
                continue
            return card
        return None

    # ----- запись -----

    def save(self, user_id: str, card: dict) -> dict:
        """Создаёт или обновляет онтологию (upsert), возвращая сохранённую.

        Правила слияния:
          - строковые поля обновляются только переданными значениями;
          - граф (classes/relations/competency_questions) при передаче
            заменяется целиком — навык досылает текущий граф полностью;
          - статус переключается только явно (поле status); создать онтологию
            сразу со статусом active нельзя — публикация отдельный шаг;
          - каждый save инкрементирует version; статус active автоматически
            (и идемпотентно) публикует онтологию в каталог data/.
        """
        raw = card if isinstance(card, dict) else {}
        ontology_id = _clean_text(raw.get("ontology_id") or raw.get("id"), 40)
        existing = self.get(user_id, ontology_id) if ontology_id else None
        if ontology_id and not existing:
            raise OntologyError(f"Онтология не найдена: {ontology_id}")
        if existing is None:
            # без явного ontology_id досылаем в уже начатый черновик того же
            # проекта с тем же названием, а не плодим дубликаты
            existing = self._find_duplicate_draft(
                user_id, _clean_text(raw.get("name"), TEXT_FIELDS["name"]),
                _clean_text(raw.get("project_id"), 40),
            )

        data = dict(existing) if existing else {
            "id": "onto-" + secrets.token_hex(5),
            "user_id": user_id,
            "created_at": time.time(),
            "status": STATUS_DRAFT,
            "version": 0,
            "classes": [],
            "relations": [],
            "competency_questions": [],
            "rag_hints": {},
        }

        for field, limit in TEXT_FIELDS.items():
            if field in raw:
                data[field] = _clean_text(raw.get(field), limit)
        if raw.get("project_id") is not None:
            pid = _clean_text(raw.get("project_id"), 40)
            if pid and not PROJECT_ID_RE.match(pid):
                raise OntologyError(f"Некорректный project_id: {pid}")
            data["project_id"] = pid
        if "tags" in raw:
            data["tags"] = _clean_list(raw.get("tags"), 10, 60)

        for key in ("classes", "relations", "competency_questions"):
            if raw.get(key) is not None:
                if not isinstance(raw.get(key), list):
                    raise OntologyError(f"Поле {key} ожидается списком")
                data[key] = raw.get(key)
        if raw.get("rag_hints") is not None:
            if not isinstance(raw.get("rag_hints"), dict):
                raise OntologyError("Поле rag_hints ожидается объектом")
            data["rag_hints"] = raw.get("rag_hints")

        status = _clean_text(raw.get("status"), 20)
        if status:
            if status not in STATUSES:
                raise OntologyError(
                    "Статус должен быть одним из: " + ", ".join(STATUSES)
                )
            if existing is None and status == STATUS_ACTIVE:
                raise OntologyError(
                    "Новая онтология сохраняется как черновик; статус active "
                    "ставится отдельным вызовом после подтверждения пользователя"
                )
            if status != data.get("status"):
                data["status"] = status
                data["status_changed_at"] = time.time()

        problems = validate_graph(
            data, require_complete=data["status"] == STATUS_ACTIVE
        )
        if problems:
            raise OntologyError("; ".join(problems[:12]))

        data["version"] = int(data.get("version") or 0) + 1
        data["classes_count"] = len(data.get("classes") or [])
        data["relations_count"] = len(data.get("relations") or [])
        data["updated_at"] = time.time()
        self._write(self._path(user_id, data["id"]), data)
        if data["status"] == STATUS_ACTIVE:
            self.publish(data)
        if data.get("project_id"):
            # синхронизация таблицы «Статус пилотного проекта»: артефакт
            # «RAG и онтология» переходит в «в процессе создания» (2), как
            # только по проекту строится онтология; завершение (3) ставит
            # публикация RAG-базы (bump только вперёд, см. projects.py)
            bump_artifact_status(data["project_id"], "rag", 2)
        return data

    def delete(self, user_id: str, ontology_id: str) -> bool:
        """Удаляет онтологию владельца (и её публикацию); True — если была."""
        try:
            path = self._path(user_id, ontology_id)
        except OntologyError:
            return False
        existed = path.is_file()
        if existed:
            path.unlink()
        self._unpublish(ontology_id)
        return existed

    # ----- публикация в каталог «Данные и знания» -----

    def _load_catalog(self) -> dict:
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": []}
        return catalog if isinstance(catalog, dict) else {"items": []}

    def catalog_items(self) -> list:
        """Опубликованные карточки из каталога рядом с objects/."""
        items = self._load_catalog().get("items")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def _project_titles(self, project_id: str) -> tuple[list, list]:
        """(названия, id) проекта по привязке карточки; ([], []) — без проекта.

        Нужен, чтобы в каталоге «Данные и знания» название проекта было
        кликабельной ссылкой на карточку проекта (projects.html?id=…).
        """
        if not project_id:
            return [], []
        try:
            card = get_project_store().find(project_id)
        except Exception:  # каталог проектов недоступен — публикуем без названия
            return [], []
        title = (card or {}).get("title") or ""
        return ([title] if title else []), [project_id]

    def catalog_entry(self, graph: dict) -> dict:
        """Карточка опубликованной онтологии для data/ontologies.json.

        projects — названия проектов по project_id (для отображения),
        project_ids — их идентификаторы (для ссылок на страницу «Проекты»).
        """
        projects, project_ids = self._project_titles(graph.get("project_id", ""))
        return {
            "id": graph["id"],
            "name": graph.get("name") or "Онтология без названия",
            "image_url": "assets/model-placeholder.svg",
            "domain": graph.get("domain", ""),
            "description_ru": graph.get("description", ""),
            "author": graph.get("author", ""),
            "format": "JSON",
            "classes_count": len(graph.get("classes") or []),
            "relations_count": len(graph.get("relations") or []),
            "projects": projects,
            "project_ids": project_ids,
            "status": "Готова",
            "file_url": f"/data/ontologies/objects/{graph['id']}.json",
            "tags": graph.get("tags") or [],
        }

    def publish(self, card: dict) -> dict:
        """Выкладывает граф в objects/ и карточку в каталог; идемпотентно."""
        if card.get("status") != STATUS_ACTIVE:
            raise OntologyError("Публиковать можно только онтологию в статусе active")
        problems = validate_graph(card, require_complete=True)
        if problems:
            raise OntologyError("; ".join(problems[:12]))

        graph = dict(card)
        graph.pop("user_id", None)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self._write(self.objects_dir / f"{card['id']}.json", graph)

        entry = self.catalog_entry(graph)
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == entry["id"])
        ]
        items.append(entry)
        self._write(self.catalog_path, {"items": items})
        return entry

    def _unpublish(self, ontology_id: str) -> None:
        """Убирает онтологию из каталога и удаляет файл графа (если были)."""
        if not ONTOLOGY_ID_RE.match(ontology_id or ""):
            return
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == ontology_id)
        ]
        self._write(self.catalog_path, {"items": items})
        obj = self.objects_dir / f"{ontology_id}.json"
        if obj.is_file():
            obj.unlink()

    # ----- представления -----

    def public_card(self, card: dict) -> dict:
        """Онтология для API/UI: без служебного user_id (он и так из токена)."""
        return {k: v for k, v in card.items() if k != "user_id"}

    def summary(self, card: dict) -> dict:
        """Компактная сводка для списков: без графа, но с формулировками CQ."""
        view = self.public_card(card)
        for key in ("classes", "relations", "rag_hints"):
            view.pop(key, None)
        view["competency_questions"] = [
            {
                "id": q.get("id"),
                "question": q.get("question"),
                "coverage": q.get("coverage") or [],
            }
            for q in card.get("competency_questions") or []
            if isinstance(q, dict)
        ]
        return view


@lru_cache(maxsize=1)
def get_store() -> OntologyStore:
    """Синглтон хранилища онтологий; в тестах сбрасывается cache_clear()."""
    settings = get_settings()
    return OntologyStore(
        settings.runtime_dir / "ontologies", settings.ontology_data_dir
    )
