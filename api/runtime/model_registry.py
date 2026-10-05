"""Реестр моделей коллективов (api/runtime/model_registry.py).

Модель коллектива — сохранённая расчётная зависимость «входы → выход»,
построенная по данным коллектива, с погрешностью, которую посчитала
платформа на опытах, не участвовавших в обучении, областью применимости
и номером версии. Код обучения и расчёта — api/runtime/model_kernel.py
(исполняется в песочнице), оркестрация — api/runtime/model_engine.py.

Хранение — файлы в приватном AIOS_RUNTIME_DIR/models/{user_id}/{mdl-…}/:

    card.json            карточка: название, вид, статус, текущая версия,
                         сводки всех версий (для истории «как модель учится»)
    v1/, v2/, …          версии — НЕ меняются после записи:
        version.json     паспорт: спецификация, коэффициенты, проверка,
                         область применимости, окружение
        train.csv        снимок обучающей таблицы (+ sha256 в паспорте)
        model.onnx       файл импортированной модели (вид onnx)
    observations.json    «прогноз → факт»: замеры после опытов
    usage.json           счётчики вызовов

Правила (архитектурное решение «Модели на платформе AIOS», раздел 6):
  - версия не меняется; переобучение даёт новую версию, старые остаются
    вызываемыми по номеру;
  - «текущая» — метка на версии, откат одной операцией;
  - опубликовать можно только модель, прошедшую порог приёмки;
  - «проверенная» — после подтверждения руководителем коллектива; версия
    замораживается, новая текущая версия снимает подтверждение;
  - модель не удаляют, а отзывают (archived); удаление — только администратор,
    с переносом в корзину.

Опубликованные модели не пишутся в публичный data/: весам и данным там не
место (data/ раздаётся без авторизации), список строится по хранилищу.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
MODEL_ID_RE = re.compile(r"^mdl-[a-f0-9]{10}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-f0-9]{8,12}$")
FILE_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,40}$")

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_VERIFIED = "verified"
STATUS_ARCHIVED = "archived"
STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_VERIFIED, STATUS_ARCHIVED)
PUBLIC_STATUSES = (STATUS_PUBLISHED, STATUS_VERIFIED)
STATUS_TITLES = {
    STATUS_DRAFT: "черновик",
    STATUS_PUBLISHED: "опубликована",
    STATUS_VERIFIED: "проверенная",
    STATUS_ARCHIVED: "отозвана",
}

KINDS = ("table", "formula", "kinetics", "onnx")
KIND_TITLES = {
    "table": "по таблице опытов",
    "formula": "по формуле",
    "kinetics": "кинетика",
    "onnx": "импортированная",
}

MAX_VERSIONS = 60
MAX_OBSERVATIONS = 2000
TEXT_LIMITS = {"name": 120, "description": 600, "domain": 200}


class ModelRegistryError(ValueError):
    """Некорректная операция с моделью — текст для человека."""


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, (bytes, bytearray)):
        tmp.write_bytes(bytes(data))
    else:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def version_summary(payload: dict) -> dict:
    """Короткая сводка версии для карточки и истории «как модель учится»."""
    validation = payload.get("validation") or {}
    per_output = validation.get("per_output") or {}
    outputs = {}
    skills = []
    for name, m in per_output.items():
        outputs[name] = {
            "mae": m.get("mae"),
            "baseline_mae": m.get("baseline_mae"),
            "skill": m.get("skill"),
            "method": m.get("method"),
            "n": m.get("n"),
        }
        if isinstance(m.get("skill"), (int, float)):
            skills.append(m["skill"])
    check = payload.get("threshold_check") or None
    controls = payload.get("controls") or []
    return {
        "version": payload.get("version"),
        "created_at": payload.get("created_at"),
        "created_by": payload.get("created_by"),
        "note": payload.get("note") or "",
        "n": payload.get("n_rows"),
        "outputs": outputs,
        "skill": min(skills) if skills else None,
        "passed": None if check is None else bool(check.get("passed")),
        "controls_passed": all(c.get("passed") for c in controls) if controls else None,
    }


class ModelStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    # ----- пути -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise ModelRegistryError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _mdir(self, user_id: str, model_id: str) -> Path:
        if not MODEL_ID_RE.match(model_id or ""):
            raise ModelRegistryError(f"Некорректный идентификатор модели: {model_id}")
        return self._dir(user_id) / model_id

    def version_dir(self, user_id: str, model_id: str, version: int) -> Path:
        return self._mdir(user_id, model_id) / f"v{int(version)}"

    def version_file(self, user_id: str, model_id: str, version: int, name: str) -> Path:
        if not FILE_NAME_RE.match(name or ""):
            raise ModelRegistryError("Некорректное имя файла версии")
        return self.version_dir(user_id, model_id, version) / name

    # ----- чтение -----

    def get(self, user_id: str, model_id: str) -> dict | None:
        try:
            card = _read_json(self._mdir(user_id, model_id) / "card.json")
        except ModelRegistryError:
            return None
        return card if isinstance(card, dict) and card.get("id") == model_id else None

    def find(self, model_id: str) -> dict | None:
        """Модель по id у любого владельца."""
        if not MODEL_ID_RE.match(model_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{model_id}/card.json"):
            card = _read_json(path)
            if isinstance(card, dict) and card.get("id") == model_id:
                return card
        return None

    def list(self, user_id: str) -> list:
        try:
            directory = self._dir(user_id)
        except ModelRegistryError:
            return []
        if not directory.is_dir():
            return []
        cards = [c for p in directory.glob("mdl-*/card.json") if isinstance(c := _read_json(p), dict)]
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    def list_all(self) -> list:
        if not self.root.is_dir():
            return []
        cards = [c for p in self.root.glob("*/mdl-*/card.json") if isinstance(c := _read_json(p), dict)]
        cards.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return cards

    def version(self, user_id: str, model_id: str, version: int) -> dict | None:
        try:
            payload = _read_json(self.version_dir(user_id, model_id, version) / "version.json")
        except (ModelRegistryError, TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def current(self, card: dict) -> dict | None:
        if not card or not card.get("current_version"):
            return None
        return self.version(card["user_id"], card["id"], card["current_version"])

    # ----- запись -----

    def _save(self, card: dict) -> dict:
        card["updated_at"] = time.time()
        _atomic_write(self._mdir(card["user_id"], card["id"]) / "card.json", card)
        return card

    def create(self, user_id: str, fields: dict) -> dict:
        kind = fields.get("kind")
        if kind not in KINDS:
            raise ModelRegistryError("Вид модели: " + ", ".join(KINDS))
        name = _clean(fields.get("name"), TEXT_LIMITS["name"])
        if len(name) < 3:
            raise ModelRegistryError("Нужно название модели — от 3 символов")
        project_id = _clean(fields.get("project_id"), 40)
        if project_id and not PROJECT_ID_RE.match(project_id):
            raise ModelRegistryError(f"Некорректный идентификатор проекта: {project_id}")
        now = time.time()
        card = {
            "id": "mdl-" + secrets.token_hex(5),
            "user_id": user_id,
            "name": name,
            "description": _clean(fields.get("description"), TEXT_LIMITS["description"]),
            "domain": _clean(fields.get("domain"), TEXT_LIMITS["domain"]),
            "kind": kind,
            "project_id": project_id,
            "author": _clean(fields.get("author"), 120),
            "status": STATUS_DRAFT,
            "current_version": None,
            "versions": [],
            "created_at": now,
            "updated_at": now,
            "published_at": None,
            "verified": None,
            "forked_from": fields.get("forked_from") or None,
        }
        with self._lock:
            self._save(card)
        return card

    def update(self, user_id: str, model_id: str, patch: dict) -> dict:
        with self._lock:
            card = self.get(user_id, model_id)
            if card is None:
                raise ModelRegistryError("Модель не найдена")
            for field, limit in TEXT_LIMITS.items():
                if field in patch:
                    value = _clean(patch.get(field), limit)
                    if field == "name" and len(value) < 3:
                        raise ModelRegistryError("Нужно название модели — от 3 символов")
                    card[field] = value
            if "project_id" in patch:
                pid = _clean(patch.get("project_id"), 40)
                if pid and not PROJECT_ID_RE.match(pid):
                    raise ModelRegistryError(f"Некорректный идентификатор проекта: {pid}")
                card["project_id"] = pid
            return self._save(card)

    def add_version(self, user_id: str, model_id: str, payload: dict, files: dict,
                    *, make_current: bool) -> tuple[dict, dict]:
        """Записывает новую неизменяемую версию. Возвращает (карточка, паспорт)."""
        with self._lock:
            card = self.get(user_id, model_id)
            if card is None:
                raise ModelRegistryError("Модель не найдена")
            if card.get("status") == STATUS_ARCHIVED:
                raise ModelRegistryError("Модель отозвана — новые версии не добавляются")
            if len(card.get("versions") or []) >= MAX_VERSIONS:
                raise ModelRegistryError(f"У модели уже {MAX_VERSIONS} версий")
            number = max([v.get("version") or 0 for v in card.get("versions") or []] + [0]) + 1
            payload = dict(payload, version=number)
            vdir = self.version_dir(user_id, model_id, number)
            if vdir.exists():
                raise ModelRegistryError("Версия уже существует")
            for name, blob in (files or {}).items():
                if not FILE_NAME_RE.match(name):
                    raise ModelRegistryError(f"Некорректное имя файла версии: {name}")
                _atomic_write(vdir / name, blob)
            _atomic_write(vdir / "version.json", payload)
            summary = version_summary(payload)
            card.setdefault("versions", []).append(summary)
            if make_current or not card.get("current_version"):
                self._make_current(card, number)
            return self._save(card), payload

    def _make_current(self, card: dict, number: int) -> None:
        card["current_version"] = number
        if card.get("status") == STATUS_VERIFIED and (card.get("verified") or {}).get("version") != number:
            # подтверждение относится к конкретной версии: новая текущая версия
            # требует повторной проверки руководителем
            card["status"] = STATUS_PUBLISHED
            card["verified"] = dict(card.get("verified") or {}, revoked_at=time.time())

    def set_current(self, user_id: str, model_id: str, number: int) -> dict:
        with self._lock:
            card = self.get(user_id, model_id)
            if card is None:
                raise ModelRegistryError("Модель не найдена")
            if not any(v.get("version") == number for v in card.get("versions") or []):
                raise ModelRegistryError(f"Нет версии {number}")
            current = next(v for v in card["versions"] if v.get("version") == number)
            if card.get("status") in PUBLIC_STATUSES and current.get("passed") is False:
                raise ModelRegistryError("Эта версия не прошла порог приёмки — у опубликованной модели её "
                                         "нельзя сделать текущей")
            self._make_current(card, number)
            return self._save(card)

    def set_status(self, user_id: str, model_id: str, status: str, *, by: str = "",
                   by_name: str = "", evidence: str = "") -> dict:
        if status not in STATUSES:
            raise ModelRegistryError("Статус: " + ", ".join(STATUSES))
        with self._lock:
            card = self.get(user_id, model_id)
            if card is None:
                raise ModelRegistryError("Модель не найдена")
            current = next((v for v in card.get("versions") or []
                            if v.get("version") == card.get("current_version")), None)
            if status in PUBLIC_STATUSES:
                if current is None:
                    raise ModelRegistryError("Сначала обучите модель: у неё нет ни одной версии")
                if current.get("passed") is False:
                    raise ModelRegistryError("Текущая версия не прошла порог приёмки — опубликовать нельзя. "
                                             "Добавьте опыты, смените метод или пересмотрите порог")
                if current.get("passed") is None and card.get("kind") != "onnx":
                    raise ModelRegistryError("Задайте порог приёмки (какую ошибку вы готовы принять) и "
                                             "переобучите модель: без порога публикация не проверяема")
                if (current.get("skill") is not None and current["skill"] <= 0
                        and card.get("kind") != "onnx"):
                    raise ModelRegistryError("Модель не лучше угадывания средним — публиковать её нельзя")
            if status == STATUS_VERIFIED:
                if len(_clean(evidence, 1000)) < 10:
                    raise ModelRegistryError("Опишите, на каких независимых данных модель проверена "
                                             "(от 10 символов)")
                card["verified"] = {"by": by, "by_name": by_name, "at": time.time(),
                                    "evidence": _clean(evidence, 1000), "version": card.get("current_version")}
            if status == STATUS_PUBLISHED and card.get("status") != STATUS_VERIFIED:
                card["published_at"] = time.time()
            if status == STATUS_DRAFT:
                card["published_at"] = None
            card["status"] = status
            return self._save(card)

    # ----- «прогноз → факт» и счётчики -----

    def observations(self, user_id: str, model_id: str) -> list:
        data = _read_json(self._mdir(user_id, model_id) / "observations.json")
        return data if isinstance(data, list) else []

    def add_observation(self, user_id: str, model_id: str, obs: dict) -> dict:
        with self._lock:
            items = self.observations(user_id, model_id)
            if len(items) >= MAX_OBSERVATIONS:
                raise ModelRegistryError(f"Уже {MAX_OBSERVATIONS} замеров — это предел")
            obs = dict(obs, id="obs-" + secrets.token_hex(4), at=time.time())
            items.append(obs)
            _atomic_write(self._mdir(user_id, model_id) / "observations.json", items)
            return obs

    def bump_usage(self, user_id: str, model_id: str, source: str) -> None:
        with self._lock:
            path = self._mdir(user_id, model_id) / "usage.json"
            data = _read_json(path) if path.is_file() else None
            data = data if isinstance(data, dict) else {"calls": 0, "by_source": {}}
            data["calls"] = int(data.get("calls") or 0) + 1
            by = data.setdefault("by_source", {})
            by[source] = int(by.get(source) or 0) + 1
            data["last_at"] = time.time()
            _atomic_write(path, data)

    def usage(self, user_id: str, model_id: str) -> dict:
        data = _read_json(self._mdir(user_id, model_id) / "usage.json")
        return data if isinstance(data, dict) else {"calls": 0, "by_source": {}}

    # ----- копия себе и удаление -----

    def fork(self, source: dict, new_owner: str, author: str = "") -> dict:
        """Копия модели другого коллектива: текущая версия становится первой
        версией нового черновика; дальше её можно дообучать на своих данных."""
        payload = self.current(source)
        if payload is None:
            raise ModelRegistryError("У исходной модели нет версий")
        card = self.create(new_owner, {
            "kind": source.get("kind"), "name": f"{source.get('name')} (копия)"[:TEXT_LIMITS["name"]],
            "description": source.get("description"), "domain": source.get("domain"),
            "author": author, "forked_from": source["id"],
        })
        src_dir = self.version_dir(source["user_id"], source["id"], source["current_version"])
        files = {p.name: p.read_bytes() for p in src_dir.iterdir()
                 if p.is_file() and p.name != "version.json" and FILE_NAME_RE.match(p.name)}
        note = f"копия модели «{source.get('name')}», версия {source['current_version']}"
        payload = dict(payload, note=note, created_at=time.time(), created_by=new_owner)
        card, _ = self.add_version(new_owner, card["id"], payload, files, make_current=True)
        return card

    def delete(self, user_id: str, model_id: str) -> bool:
        """Удаление администратором: каталог уезжает в корзину, а не стирается."""
        with self._lock:
            try:
                mdir = self._mdir(user_id, model_id)
            except ModelRegistryError:
                return False
            if not mdir.is_dir():
                return False
            trash = self.root / ".trash" / f"{model_id}-{int(time.time())}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(mdir), str(trash))
            return True


@lru_cache(maxsize=1)
def get_store() -> ModelStore:
    """Синглтон реестра; в тестах сбрасывается cache_clear()."""
    return ModelStore(get_settings().runtime_dir / "models")
