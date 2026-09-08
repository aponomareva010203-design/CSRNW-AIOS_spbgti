"""Хранилище карточек проектов («Мои проекты», api/runtime/projects.py).

Каталог AIOS_RUNTIME_DIR/projects/{user_id}/ — по одному JSON-файлу на
карточку (id вида prj-…), запись атомарная: временный файл + replace.
Каталог намеренно лежит в .aios-runtime, а не в публичной data/: карточка
содержит персональные данные (ФИО владельца, бюджет), которые должны быть
доступны только владельцу по Bearer-токену — data/ раздаётся без авторизации.

Карточка создаётся навыком «Онбординг проекта» через инструмент
save_project_card и живёт в двух статусах:

    draft  — черновик онбординга (перезаписывается на каждом шаге навыка);
    active — подтверждённая пользователем карточка, видна на «Моих проектах».

Доступ жёстко привязан к идентификатору из Bearer-токена: клиент не может
обратиться к чужим карточкам — идентификатор из запроса не принимается.
"""

import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

# тот же формат идентификаторов владельца, что и в хранилище кабинета
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-f0-9]{8,12}$")

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE)

# лимиты полей карточки (схема — навык «Онбординг проекта», Шаги 3–7)
TITLE_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 2_500  # просят ≤2000, жёсткий потолок — 2500
GOAL_MAX_CHARS = 2_000
TEAM_SIZE_MAX_CHARS = 60
PARTICIPANTS_MAX_CHARS = 2_000
DEPARTMENTS_MAX_CHARS = 500
BUDGET_MAX_CHARS = 200
GROUNDWORK_MAX_CHARS = 4_000

# строковые поля карточки → лимит символов
TEXT_FIELDS = {
    "title": TITLE_MAX_CHARS,
    "description": DESCRIPTION_MAX_CHARS,
    "goal": GOAL_MAX_CHARS,
    "team_size": TEAM_SIZE_MAX_CHARS,
    "participants": PARTICIPANTS_MAX_CHARS,
    "departments": DEPARTMENTS_MAX_CHARS,
    "budget": BUDGET_MAX_CHARS,
    "groundwork": GROUNDWORK_MAX_CHARS,
}

# списковые поля (ссылки/файлы) → (макс. элементов, лимит символов на элемент)
LIST_FIELD_LIMITS = {
    "groundwork_links": (10, 500),
    "groundwork_files": (10, 300),
    "presentation_files": (5, 300),
}


class ProjectError(ValueError):
    """Некорректные данные карточки проекта."""


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
        raise ProjectError("Ожидается список строк или многострочная строка")
    items: list[str] = []
    for item in value:
        cleaned = _clean_text(item, item_limit)
        if cleaned:
            items.append(cleaned)
        if len(items) >= max_items:
            break
    return items


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ----- низкоуровневые операции -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise ProjectError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _path(self, user_id: str, project_id: str) -> Path:
        if not PROJECT_ID_RE.match(project_id or ""):
            raise ProjectError("Некорректный идентификатор проекта")
        return self._dir(user_id) / f"{project_id}.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # ----- чтение -----

    def list(self, user_id: str) -> list:
        """Все карточки пользователя, свежие сверху."""
        directory = self._dir(user_id)
        if not directory.is_dir():
            return []
        items = []
        for path in directory.glob("*.json"):
            card = self._read(path)
            if card and card.get("id"):
                items.append(card)
        items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return items

    def get(self, user_id: str, project_id: str) -> dict | None:
        try:
            card = self._read(self._path(user_id, project_id))
        except ProjectError:
            return None
        if card and card.get("id") != project_id:
            return None
        return card

    # ----- запись -----

    def save(self, user_id: str, card: dict) -> dict:
        """Создаёт или обновляет карточку (upsert), возвращая сохранённую.

        Правила слияния:
          - существующая карточка обновляется только переданными полями —
            навык досылает карточку целиком на каждом шаге онбординга,
            частичные дозаписи не теряют уже собранные шаги;
          - статус переключается только явно (поле status); создать карточку
            сразу со статусом active нельзя — подтверждение отдельный шаг.
        """
        raw = card if isinstance(card, dict) else {}
        project_id = _clean_text(raw.get("project_id") or raw.get("id"), 40)
        existing = self.get(user_id, project_id) if project_id else None
        if project_id and not existing:
            raise ProjectError(f"Карточка не найдена: {project_id}")

        data = dict(existing) if existing else {
            "id": "prj-" + secrets.token_hex(5),
            "user_id": user_id,
            "created_at": time.time(),
            "status": STATUS_DRAFT,
        }

        for field, limit in TEXT_FIELDS.items():
            if field in raw:
                data[field] = _clean_text(raw.get(field), limit)
        for field, (max_items, item_limit) in LIST_FIELD_LIMITS.items():
            if field in raw:
                data[field] = _clean_list(raw.get(field), max_items, item_limit)
        data["presentation"] = bool(raw.get("presentation"))

        status = _clean_text(raw.get("status"), 20)
        if status:
            if status not in STATUSES:
                raise ProjectError(
                    "Статус должен быть одним из: " + ", ".join(STATUSES)
                )
            if existing is None and status == STATUS_ACTIVE:
                raise ProjectError(
                    "Новая карточка сохраняется как черновик; статус active "
                    "ставится отдельным вызовом после подтверждения пользователя"
                )
            if status != data.get("status"):
                data["status"] = status
                data["status_changed_at"] = time.time()

        data["updated_at"] = time.time()
        self._write(self._path(user_id, data["id"]), data)
        return data

    def delete(self, user_id: str, project_id: str) -> bool:
        """Удаляет карточку владельца; True — если карточка существовала."""
        path = self._path(user_id, project_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def find(self, project_id: str) -> dict | None:
        """Карточка по id у любого владельца (служебное: админские операции)."""
        if not PROJECT_ID_RE.match(project_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{project_id}.json"):
            card = self._read(path)
            if card and card.get("id") == project_id:
                return card
        return None

    def public_card(self, card: dict) -> dict:
        """Карточка для API/UI: без служебного user_id (он и так из токена)."""
        return {k: v for k, v in card.items() if k != "user_id"}


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    """Синглтон хранилища проектов; в тестах сбрасывается cache_clear()."""
    return ProjectStore(get_settings().runtime_dir / "projects")
