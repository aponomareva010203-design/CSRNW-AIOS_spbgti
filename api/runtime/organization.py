"""Хранилище раздела «Об организации»: data/organization/.

Данные считаются «живыми»: JSON-файлы редактируются из админ-панели и
читаются инструментами агента, поэтому всё читается с диска без кэша
(как datarepo). Каталог настраивается AIOS_ORGANIZATION_DIR и по умолчанию
совпадает с песочницей файловых инструментов агента (data/).

    profile.json    — раздел «Основная информация»;
    metrics.json    — раздел «Показатели деятельности»;
    documents.json  — реестр основных документов (категории: стратегия,
                      программа развития, другие) с короткими .md-описаниями;
    objects/        — сами файлы документов (имя на диске = id + расширение).

Шаблон университета data-driven: поля описаны в самих JSON (id, title, type),
поэтому будущие типы организаций со своими наборами полей не требуют правок
кода — достаточно другого содержимого JSON. Дата последнего обновления
(updated_at, ДД.ММ.ГГГГ) проставляется автоматически при каждом сохранении.
"""

import json
import re
import secrets
import time
from datetime import date
from pathlib import Path

from .config import get_settings

DATE_FORMAT = "%d.%m.%Y"

# Категории основных документов (id → заголовок для UI и инструментов).
CATEGORIES = {
    "strategy": "Стратегия",
    "development": "Программа развития",
    "other": "Другие документы",
}

# Разрешённые к загрузке расширения файлов документов.
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".odt",
    ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx",
    ".txt", ".md",
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 МБ
SUMMARY_MAX_CHARS = 20_000
TITLE_MAX_CHARS = 300

# PDF больше этого размера агент читает только после явного разрешения
# пользователя (диалоговый механизм, см. tools.read_document).
PDF_PERMISSION_THRESHOLD = 100_000

_DOC_ID_RE = re.compile(r"^doc-[a-f0-9]{8,12}$")

# Шаблон университета для профиля (type задаёт тип организации; поля
# данных перечислены в fields — рендер админки идёт по этому списку).
_PROFILE_TEMPLATE: dict = {
    "type": "university",
    "title": "Основная информация",
    "fields": [
        {"id": "name", "title": "Название", "type": "string", "value": ""},
        {"id": "aliases", "title": "Другие названия (aliases)", "type": "string_list", "value": []},
        {"id": "description", "title": "Описание", "type": "text", "value": ""},
        {"id": "activity_areas", "title": "Направления деятельности", "type": "text", "value": ""},
    ],
    "source": "",
    "updated_at": "",
}

# Шаблон университета для показателей деятельности.
_METRICS_TEMPLATE: dict = {
    "type": "university",
    "title": "Показатели деятельности",
    "items": [
        {"id": "rd_income", "title": "Объем доходов от НИОКР", "unit": "тыс. руб", "type": "number", "value": None},
        {"id": "faculty_count", "title": "Число профессорско-преподавательского состава", "unit": "чел.", "type": "integer", "value": None},
        {"id": "researchers_count", "title": "Число научных работников", "unit": "чел.", "type": "integer", "value": None},
        {"id": "students_count", "title": "Численность студентов", "unit": "чел.", "type": "integer", "value": None},
        {"id": "ege_avg", "title": "Средний балл ЕГЭ", "unit": "баллов", "type": "number", "value": None},
    ],
    "source": "",
    "updated_at": "",
}


class OrganizationError(ValueError):
    """Некорректные данные раздела «Об организации»."""


def _today() -> str:
    return date.fromtimestamp(time.time()).strftime(DATE_FORMAT)


class OrganizationStore:
    """Чтение/запись данных организации в каталоге root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"

    # ----- низкоуровневые операции -----

    def _read_json(self, name: str, template: dict) -> dict:
        """Читает root/<name>; отсутствующий/битый файл создаётся из шаблона."""
        path = self.root / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = json.loads(json.dumps(template, ensure_ascii=False))  # глубокая копия
            self._write_json(path, data)
        return data

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # ----- профиль и показатели -----

    def load_profile(self) -> dict:
        return self._read_json("profile.json", _PROFILE_TEMPLATE)

    def load_metrics(self) -> dict:
        return self._read_json("metrics.json", _METRICS_TEMPLATE)

    @staticmethod
    def _clean_source(source: str | None, current: str) -> str:
        if source is None:
            return current
        return str(source).strip()[:SUMMARY_MAX_CHARS]

    def save_profile(self, values: dict, source: str | None = None) -> dict:
        """Обновляет значения полей профиля и пишет их в исходный profile.json.

        values — словарь {field_id: значение}; неизвестные поля игнорируются,
        значения приводятся к типу поля (string/text — строка,
        string_list — список строк, пустые элементы отбрасываются).
        """
        data = self.load_profile()
        fields = data.get("fields")
        if not isinstance(fields, list):
            raise OrganizationError("profile.json повреждён: нет списка fields")
        for field in fields:
            fid = field.get("id")
            if fid is None or fid not in (values or {}):
                continue
            ftype = field.get("type", "string")
            raw = values[fid]
            if ftype == "string_list":
                if isinstance(raw, str):
                    raw = [line.strip() for line in raw.splitlines()]
                field["value"] = [str(v).strip() for v in (raw or []) if str(v).strip()]
            else:
                field["value"] = str(raw or "").strip()
        data["source"] = self._clean_source(source, str(data.get("source") or ""))
        data["updated_at"] = _today()
        self._write_json(self.root / "profile.json", data)
        return data

    def save_metrics(self, values: dict, source: str | None = None) -> dict:
        """Обновляет показатели деятельности в исходном metrics.json.

        values — словарь {metric_id: число|None}; пустое значение → null.
        """
        data = self.load_metrics()
        items = data.get("items")
        if not isinstance(items, list):
            raise OrganizationError("metrics.json повреждён: нет списка items")
        for item in items:
            mid = item.get("id")
            if mid is None or mid not in (values or {}):
                continue
            raw = values[mid]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                item["value"] = None
                continue
            try:
                number = float(str(raw).replace(",", ".").replace(" ", ""))
            except ValueError:
                raise OrganizationError(
                    f"Показатель «{item.get('title', mid)}» должен быть числом"
                ) from None
            if item.get("type") == "integer":
                item["value"] = int(round(number))
            else:
                item["value"] = number
        data["source"] = self._clean_source(source, str(data.get("source") or ""))
        data["updated_at"] = _today()
        self._write_json(self.root / "metrics.json", data)
        return data

    # ----- документы -----

    def load_documents(self) -> list:
        path = self.root / "documents.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            self._write_json(path, {"items": []})
            return []
        return [d for d in items if isinstance(d, dict) and d.get("id")]

    def _save_documents(self, items: list) -> None:
        self._write_json(self.root / "documents.json", {"items": items})

    def get_document(self, doc_id: str) -> dict | None:
        if not _DOC_ID_RE.match(doc_id or ""):
            return None
        return next((d for d in self.load_documents() if d.get("id") == doc_id), None)

    def document_path(self, doc: dict) -> Path:
        """Абсолютный путь файла документа внутри objects/ (защита от выхода)."""
        name = str(doc.get("stored_name") or "")
        path = (self.objects_dir / name).resolve()
        if not name or not path.is_relative_to(self.objects_dir.resolve()):
            raise OrganizationError("Некорректное имя файла документа")
        return path

    def add_document(
        self, *, filename: str, category: str, title: str = "", summary: str = "", data: bytes
    ) -> dict:
        """Сохраняет файл в objects/ и добавляет запись в реестр документов."""
        category = (category or "").strip().lower()
        if category not in CATEGORIES:
            raise OrganizationError(
                "Категория должна быть одной из: "
                + ", ".join(f"{k} ({v})" for k, v in CATEGORIES.items())
            )
        ext = Path(filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise OrganizationError(
                f"Расширение «{ext or 'без расширения'}» не поддерживается "
                f"(разрешено: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
            )
        if not data:
            raise OrganizationError("Файл пуст")
        if len(data) > MAX_UPLOAD_BYTES:
            raise OrganizationError(f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")

        title = (title or "").strip() or (filename or "").strip()
        title = title[:TITLE_MAX_CHARS]
        summary = (summary or "").strip()[:SUMMARY_MAX_CHARS]

        self.objects_dir.mkdir(parents=True, exist_ok=True)
        doc_id = "doc-" + secrets.token_hex(5)
        stored_name = doc_id + ext
        (self.objects_dir / stored_name).write_bytes(data)

        doc = {
            "id": doc_id,
            "category": category,
            "title": title,
            "filename": filename,
            "stored_name": stored_name,
            "size": len(data),
            "uploaded_at": _today(),
            "summary": summary,
        }
        items = self.load_documents()
        items.append(doc)
        self._save_documents(items)
        return doc

    def update_document(
        self, doc_id: str, *, title: str | None = None, category: str | None = None, summary: str | None = None
    ) -> dict | None:
        """Правка названия/категории/.md-описания без перезагрузки файла."""
        items = self.load_documents()
        doc = next((d for d in items if d.get("id") == doc_id), None)
        if doc is None:
            return None
        if title is not None:
            title = title.strip()
            if not title:
                raise OrganizationError("Название документа не может быть пустым")
            doc["title"] = title[:TITLE_MAX_CHARS]
        if category is not None:
            category = category.strip().lower()
            if category not in CATEGORIES:
                raise OrganizationError("Неизвестная категория документа")
            doc["category"] = category
        if summary is not None:
            doc["summary"] = summary.strip()[:SUMMARY_MAX_CHARS]
        self._save_documents(items)
        return doc

    def delete_document(self, doc_id: str) -> bool:
        items = self.load_documents()
        doc = next((d for d in items if d.get("id") == doc_id), None)
        if doc is None:
            return False
        items.remove(doc)
        self._save_documents(items)
        try:
            self.document_path(doc).unlink(missing_ok=True)
        except OSError:
            pass
        return True


def get_store() -> OrganizationStore:
    """Хранилище организации с каталогом из настроек (без кэша — «живые» данные)."""
    return OrganizationStore(get_settings().organization_dir)


def public_document(doc: dict) -> dict:
    """Запись документа для API/UI: ссылка скачивания вместо имени на диске."""
    view = {k: v for k, v in doc.items() if k != "stored_name"}
    view["file_url"] = f"/data/organization/objects/{doc.get('stored_name', '')}"
    return view

