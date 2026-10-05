"""Хранилище карточек автоматизаций (раздел «Автоматизации»).

Автоматизация — блочная модель (триггер + шаги, см. n8n_blocks) плюс связь
с воркфлоу n8n: id воркфлоу и путь webhook. Один файл на карточку, атомарная
запись (temp + replace) — тот же паттерн, что у AgentStore.

Хранилище: .aios-runtime/automations/{owner_id}/auto-{id}.json (приватное).
Права разграничивает AIOS: владелец карточки или роль «Администратор»;
пользователей в n8n нет вовсе — все воркфлоу создаются от сервисного
API-ключа, а соответствие «автоматизация ↔ воркфлоу» живёт здесь.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from threading import Lock

MAX_AUTOMATIONS_PER_USER = 20


class AutomationError(Exception):
    """Ошибка операции с автоматизацией."""


class AutomationStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _user_dir(self, user_id: str) -> Path:
        d = self._root / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, user_id: str, automation_id: str) -> Path:
        return self._user_dir(user_id) / f"{automation_id}.json"

    # ------------------------------------------------------------------
    # Чтение

    def get(self, user_id: str, automation_id: str) -> dict | None:
        path = self._path(user_id, automation_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def find_any_owner(self, automation_id: str) -> dict | None:
        """Карточка по id без знания владельца — права проверяет роутер."""
        if not automation_id or "/" in automation_id or ".." in automation_id:
            return None
        for user_dir in self._root.iterdir():
            if not user_dir.is_dir():
                continue
            candidate = user_dir / f"{automation_id}.json"
            if candidate.is_file():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def list(self, user_id: str) -> list[dict]:
        out = []
        for path in sorted(self._user_dir(user_id).glob("auto-*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def list_all(self) -> list[dict]:
        """Все автоматизации всех пользователей, свежие сверху (админ-раздел)."""
        out: list[dict] = []
        if not self._root.is_dir():
            return out
        for user_dir in self._root.iterdir():
            if not user_dir.is_dir():
                continue
            for path in sorted(user_dir.glob("auto-*.json")):
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        out.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return out

    # ------------------------------------------------------------------
    # Запись

    def create(self, owner_id: str, fields: dict) -> dict:
        with self._lock:
            if len(self.list(owner_id)) >= MAX_AUTOMATIONS_PER_USER:
                raise AutomationError(
                    f"Достигнут лимит автоматизаций ({MAX_AUTOMATIONS_PER_USER} на пользователя)"
                )
            now = time.time()
            card = {
                "id": f"auto-{uuid.uuid4().hex[:10]}",
                "owner_id": owner_id,
                "name": fields["name"],
                "description": fields.get("description") or "",
                "trigger": fields["trigger"],
                "steps": fields["steps"],
                "n8n_workflow_id": "",
                "webhook_path": fields.get("webhook_path") or "",
                "active": False,
                "created_at": now,
                "updated_at": now,
            }
            self._write(self._path(owner_id, card["id"]), card)
            return card

    def update(self, card: dict) -> dict:
        with self._lock:
            card["updated_at"] = time.time()
            self._write(self._path(card["owner_id"], card["id"]), card)
            return card

    def delete(self, user_id: str, automation_id: str) -> bool:
        with self._lock:
            path = self._path(user_id, automation_id)
            if path.is_file():
                path.unlink()
                return True
            return False

    def summary(self, card: dict) -> dict:
        """Карточка для API/UI: блочная модель целиком (конструктор её
        редактирует), но без служебного webhook-пути наружу не отдаём —
        он показывается только владельцу и админу."""
        return {
            "id": card["id"],
            "name": card.get("name"),
            "description": card.get("description") or "",
            "trigger": card.get("trigger") or {},
            "steps": card.get("steps") or [],
            "active": bool(card.get("active")),
            "webhook_path": card.get("webhook_path") or "",
            "n8n_workflow_id": card.get("n8n_workflow_id") or "",
            "created_at": card.get("created_at"),
            "updated_at": card.get("updated_at"),
        }


_STORE: AutomationStore | None = None


def get_store() -> AutomationStore:
    global _STORE
    if _STORE is None:
        from .config import get_settings

        _STORE = AutomationStore(get_settings().runtime_dir / "automations")
    return _STORE
