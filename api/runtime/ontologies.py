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

from . import artifact_icons
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
MAX_ATTRIBUTES_PER_CLASS = 40
MAX_SYNONYMS_PER_CLASS = 40
MAX_KEYWORDS_PER_CLASS = 40
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


CLASSIFY_SIMILARITY_THRESHOLD = 0.38
MAX_CLASS_IDS_PER_CHUNK = 5


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

    def list_all(self) -> list:
        """Все онтологии всех владельцев, свежие сверху (служебное: участникам
        проекта открываются онтологии, привязанные к их проекту)."""
        cards: list[dict] = []
        if not self.root.is_dir():
            return cards
        for path in self.root.glob("*/*.json"):
            card = self._read(path)
            if card and card.get("id"):
                cards.append(card)
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    def get_published(self, ontology_id: str) -> dict | None:
        """Опубликованный граф (objects/{id}.json) — зафиксированная публикацией
        версия, а не текущий (возможно, ещё не republish-нутый) черновик
        владельца. Разметка чанков при индексации документа должна идти по
        тому же графу, что был активен на момент публикации — не «плыть»
        вслед за незамеченными правками черновика.
        """
        if not ONTOLOGY_ID_RE.match(ontology_id or ""):
            return None
        card = self._read(self.objects_dir / f"{ontology_id}.json")
        return card if card and card.get("id") == ontology_id else None

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
        # тестовая онтология (admin_only): флаг сохраняется при правках,
        # уходит в карточку каталога и там фильтруется на клиенте
        if "admin_only" in raw:
            data["admin_only"] = bool(raw.get("admin_only"))

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
        image_url — сгенерированный логотип (artifact_icons), если файл
        уже создан; до завершения фоновой генерации — плейсхолдер.
        """
        projects, project_ids = self._project_titles(graph.get("project_id", ""))
        entry = {
            "id": graph["id"],
            "name": graph.get("name") or "Онтология без названия",
            "image_url": artifact_icons.logo_url(graph["id"])
            or "assets/model-placeholder.svg",
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
        if graph.get("admin_only"):
            entry["admin_only"] = True
        return entry

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
        vec = self.objects_dir / f"{ontology_id}.class_vectors.json"
        if vec.is_file():
            vec.unlink()

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


# ----- авто-разметка чанков классами онтологии (GraphRAG-mini) -----
#
# Пространство классов детерминировано онтологией заранее (публикация) —
# разметка чанка при индексации документа лишь проецирует его на уже
# существующие классы, не создаёт и не меняет граф. Без LLM: результат
# должен быть воспроизводим для одного текста и одной версии онтологии,
# иначе фильтр по классам в search_corpus станет ненадёжным. Кандидаты на
# разметку — только классы из rag_hints.extraction_targets, не весь граф:
# так автор онтологии явно решает, что стоит искать в тексте документов.

_CLASS_VECTOR_CACHE: dict = {}
# ontology_id -> (updated_at графа на момент расчёта, [{"id","vector","keywords"}])
# инвалидация — по updated_at опубликованного графа, без ручной чистки.


def _extraction_candidates(graph: dict) -> list:
    """Классы-кандидаты на авто-разметку: явно перечисленные в
    rag_hints.extraction_targets. Если список не задан (пуст) — кандидатами
    становятся ВСЕ классы онтологии: по умолчанию всё, что попало в граф,
    должно участвовать в разметке РАГ-базы, а не требовать отдельного
    ручного включения. Явно заданный непустой список — осознанное сужение
    автором онтологии, оно уважается как есть."""
    classes = [c for c in (graph.get("classes") or []) if isinstance(c, dict) and c.get("id")]
    targets = set((graph.get("rag_hints") or {}).get("extraction_targets") or [])
    if not targets:
        return classes
    return [c for c in classes if c.get("id") in targets]


def _class_reference_text(cls: dict, classes_by_id: dict, relations: list) -> str:
    """Эталонный текст класса для эмбеддинга: имя + определение + синонимы +
    родитель + связи, заданные пользователем в онтологии."""
    parts = [cls.get("name") or "", cls.get("description") or ""]
    parts.extend(s for s in (cls.get("synonyms") or []) if s)

    parent_id = cls.get("parent")
    if parent_id:
        parent = classes_by_id.get(parent_id)
        if parent:
            parts.append(f"Родительский класс: {parent.get('name') or parent_id}")

    cid = cls.get("id")
    rel_phrases = []
    for rel in relations or []:
        if not isinstance(rel, dict):
            continue
        rname = rel.get("name") or ""
        domain_id = rel.get("domain_class")
        range_id = rel.get("range_class")
        if domain_id == cid and range_id:
            other = classes_by_id.get(range_id)
            if other:
                rel_phrases.append(f"{rname} {other.get('name') or range_id}".strip())
        elif range_id == cid and domain_id:
            other = classes_by_id.get(domain_id)
            if other:
                rel_phrases.append(f"{other.get('name') or domain_id} {rname}".strip())
    if rel_phrases:
        parts.append("Связи: " + "; ".join(rel_phrases))

    return ". ".join(p for p in parts if p).strip()


def _cosine(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _class_vector_cache_path(ontology_id: str):
    """Путь дискового кеша эталонных векторов классов — рядом с опубликованным
    графом онтологии, тем же способом хранения (objects_dir)."""
    return get_store().objects_dir / f"{ontology_id}.class_vectors.json"


async def _class_vectors(graph: dict) -> list:
    """Эталонные векторы классов-кандидатов онтологии — кеш в памяти процесса
    и на диске, оба ключом (ontology_id, updated_at) графа.

    Дисковый кеш обязателен для настоящей детерминированности: внешний API
    эмбеддингов не гарантирует побитовую идентичность ответа на один и тот же
    текст между отдельными вызовами — на пограничных по порогу совпадениях
    этого достаточно, чтобы результат классификации чуть менялся между
    перезапусками сервера или отдельными запусками backfill-скрипта, что
    прямо противоречит идее детерминированного пространства классов
    (обнаружено и подтверждено 16 сентября 2026 на реальном backfill: 2 из
    557 чанков дали разное совпадение при двух подряд идущих отдельных
    запусках процесса).
    """
    ontology_id = graph.get("id")
    updated_at = graph.get("updated_at") or 0
    cached = _CLASS_VECTOR_CACHE.get(ontology_id)
    if cached and cached[0] == updated_at:
        return cached[1]

    candidates = _extraction_candidates(graph)
    if not candidates:
        _CLASS_VECTOR_CACHE[ontology_id] = (updated_at, [])
        return []

    disk = OntologyStore._read(_class_vector_cache_path(ontology_id))
    if disk and disk.get("updated_at") == updated_at and disk.get("entries"):
        entries = disk["entries"]
        _CLASS_VECTOR_CACHE[ontology_id] = (updated_at, entries)
        return entries

    from .embeddings import embed_texts  # локальный импорт: избегаем цикла модулей

    classes_by_id = {
        c.get("id"): c for c in (graph.get("classes") or []) if isinstance(c, dict)
    }
    relations = graph.get("relations") or []
    texts = [_class_reference_text(c, classes_by_id, relations) for c in candidates]
    vectors = await embed_texts(texts)
    entries = [
        {
            "id": cls.get("id"),
            "vector": vector,
            "keywords": [
                kw for kw in list(cls.get("keywords") or []) + list(cls.get("synonyms") or [])
                if kw
            ],
        }
        for cls, vector in zip(candidates, vectors)
    ]
    _CLASS_VECTOR_CACHE[ontology_id] = (updated_at, entries)
    OntologyStore._write(
        _class_vector_cache_path(ontology_id),
        {"id": ontology_id, "updated_at": updated_at, "entries": entries},
    )
    return entries


async def classify_chunks(ontology_id: str, chunks: list, chunk_vectors: list) -> list:
    """Список id классов онтологии для каждого чанка (по индексу, как в chunks).

    Пустой список у чанка — нормальный результат (ни один класс-кандидат не
    совпал), не ошибка. Не поднимает исключений при отсутствующей/неопубли-
    кованной онтологии или недоступных эмбеддингах — индексация не должна
    падать из-за необязательного шага, просто вернётся пустая разметка.
    """
    if not ontology_id:
        return [[] for _ in chunks]
    graph = get_store().get_published(ontology_id)
    if not graph:
        return [[] for _ in chunks]

    try:
        class_entries = await _class_vectors(graph)
    except Exception:
        return [[] for _ in chunks]
    if not class_entries:
        return [[] for _ in chunks]

    result = []
    for text, vector in zip(chunks, chunk_vectors):
        lowered = (text or "").lower()
        # Собираем ВСЕ совпадения с оценкой уверенности, обрезаем потолком только
        # после сортировки — иначе потолок отсекает не худшие, а просто более
        # поздние по порядку в графе классы (см. проверку на реальном документе).
        scored = []
        for entry in class_entries:
            cosine = _cosine(vector, entry["vector"])
            keyword_hit = any(
                re.search(r"(?<!\w)" + re.escape(kw.lower()) + r"(?!\w)", lowered)
                for kw in entry["keywords"]
                if kw
            )
            if keyword_hit:
                # буквальное совпадение всегда приоритетнее любого векторного
                scored.append((1.0 + max(cosine, 0.0), entry["id"]))
            elif cosine >= CLASSIFY_SIMILARITY_THRESHOLD:
                scored.append((cosine, entry["id"]))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        result.append([cid for _, cid in scored[:MAX_CLASS_IDS_PER_CHUNK]])
    return result


def _expand_class_neighborhood(graph: dict, class_ids: list) -> list:
    """Расширяет список id классов их соседями по графу онтологии — прямые
    связи (relations, оба направления) и родитель/дети (parent). Даёт
    графовый охват поверх точечного совпадения запроса с одним классом:
    вопрос классифицировался как «класс X» — в выдачу заодно попадут
    чанки классов, явно связанных с X в онтологии, даже если по чистому
    векторному сходству они далеки от текста самого вопроса."""
    classes = [c for c in (graph.get("classes") or []) if isinstance(c, dict) and c.get("id")]
    relations = graph.get("relations") or []
    parent_of = {c.get("id"): c.get("parent") for c in classes}
    children_of: dict[str, list] = {}
    for cid, pid in parent_of.items():
        if pid:
            children_of.setdefault(pid, []).append(cid)

    seed = set(class_ids or [])
    expanded = set(seed)
    for cid in seed:
        parent_id = parent_of.get(cid)
        if parent_id:
            expanded.add(parent_id)
        expanded.update(children_of.get(cid, []))
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            domain_id = rel.get("domain_class")
            range_id = rel.get("range_class")
            if domain_id == cid and range_id:
                expanded.add(range_id)
            elif range_id == cid and domain_id:
                expanded.add(domain_id)
    return sorted(expanded)


def class_definitions(graph: dict, class_ids: list) -> list:
    """Текстовые определения классов онтологии (имя + тот же обогащённый
    текст, что уходит в эталонный эмбеддинг — описание, родитель, связи) —
    для показа модели как явный контекст «из онтологии», а не голые id."""
    wanted = set(class_ids or [])
    if not wanted:
        return []
    classes = [c for c in (graph.get("classes") or []) if isinstance(c, dict) and c.get("id")]
    classes_by_id = {c.get("id"): c for c in classes}
    relations = graph.get("relations") or []
    return [
        {
            "id": c.get("id"),
            "name": c.get("name") or c.get("id"),
            "text": _class_reference_text(c, classes_by_id, relations),
        }
        for c in classes
        if c.get("id") in wanted
    ]


async def classify_query_context(ontology_id: str, query: str, query_vector: list) -> dict:
    """Для текста поискового запроса: к каким классам онтологии он относится
    (та же детерминированная логика, что и для чанков документов), плюс их
    соседи по графу связей/иерархии, плюс текстовые определения — единый
    результат для авто-фильтра поиска и для контекста, показываемого модели.

    Не поднимает исключений — при отсутствующей/неопубликованной онтологии,
    либо любой ошибке классификации, просто возвращает пустой результат:
    поиск не должен падать из-за необязательного шага.
    """
    empty = {"class_ids": [], "definitions": []}
    if not ontology_id or not (query or "").strip():
        return empty
    graph = get_store().get_published(ontology_id)
    if not graph:
        return empty
    try:
        matched = await classify_chunks(ontology_id, [query], [query_vector])
    except Exception:
        return empty
    seed = matched[0] if matched else []
    if not seed:
        return empty
    expanded = _expand_class_neighborhood(graph, seed)
    return {"class_ids": expanded, "definitions": class_definitions(graph, expanded)}


def classification_snapshot(graph: dict) -> list:
    """Снимок классов, реально участвующих в авто-разметке чанков (только
    из rag_hints.extraction_targets) — для сравнения с прошлой разметкой
    RAG-базы при проверке актуальности (см. api/routers/rag.py:
    ontology-check / reclassify)."""
    candidates = _extraction_candidates(graph)
    return [
        {
            "id": c.get("id"),
            "name": c.get("name") or c.get("id"),
            "signature": _class_signature(c),
        }
        for c in candidates
    ]


def _class_signature(cls: dict) -> str:
    """Хэш содержимого класса, влияющего на разметку чанков — меняется при
    правке имени/описания/синонимов/ключевых слов класса, не меняется при
    правках, не влияющих на классификацию (например, атрибутов класса)."""
    import hashlib

    parts = [
        cls.get("name") or "",
        cls.get("description") or "",
        "|".join(sorted(cls.get("synonyms") or [])),
        "|".join(sorted(cls.get("keywords") or [])),
    ]
    return hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()[:16]


def diff_classification_snapshots(old_classes, new_classes: list) -> dict:
    """Сравнивает два снимка classification_snapshot(): какие классы
    добавлены/удалены/изменены (по сигнатуре) — человекочитаемая сводка
    «что изменилось в онтологии» перед актуализацией RAG-базы."""
    old_by_id = {c["id"]: c for c in (old_classes or []) if isinstance(c, dict) and c.get("id")}
    new_by_id = {c["id"]: c for c in (new_classes or []) if isinstance(c, dict) and c.get("id")}

    added = [{"id": cid, "name": c["name"]} for cid, c in new_by_id.items() if cid not in old_by_id]
    removed = [{"id": cid, "name": c["name"]} for cid, c in old_by_id.items() if cid not in new_by_id]
    changed = [
        {"id": cid, "name": new_by_id[cid]["name"]}
        for cid in new_by_id
        if cid in old_by_id and old_by_id[cid].get("signature") != new_by_id[cid].get("signature")
    ]
    return {"added": added, "removed": removed, "changed": changed}


# ----- автоактуализация RAG-баз после правки онтологии -----
# Единственная точка входа: каждая правка (save_ontology из чата, PATCH,
# publish — он внутри зовёт save со статусом active) проходит через
# OntologyStore.save. Опубликованная онтология → отложенная актуализация
# привязанных RAG-баз (runtime/rag_sync.py, группировка правок 60 с).
_save_without_rag_sync = OntologyStore.save


def _save_with_rag_sync(self, user_id, card):
    saved = _save_without_rag_sync(self, user_id, card)
    try:
        if isinstance(saved, dict) and saved.get("status") == STATUS_ACTIVE:
            from . import rag_sync  # локальный импорт: избегаем цикла модулей
            rag_sync.schedule(saved.get("id"))
    except Exception:  # актуализация не должна ломать сохранение онтологии
        pass
    return saved


OntologyStore.save = _save_with_rag_sync
