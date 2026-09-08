"""Файловое хранилище сессий диалогов.

Каждая сессия — отдельный JSON-файл в AIOS_RUNTIME_DIR/sessions; запись
атомарная (временный файл + replace). Хранилище нарочито простое, без БД:
каркас должен читаться и заменяться по мере роста проекта.
"""

import json
import re
import time
import uuid
from pathlib import Path

SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class SessionError(ValueError):
    """Некорректный идентификатор сессии."""


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id or ""):
            raise SessionError("Некорректный идентификатор сессии")
        return self.root / f"{session_id}.json"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def create(
        self,
        title: str = "",
        model: str | None = None,
        agent_id: str | None = None,
        skill_ids: list | None = None,
    ) -> dict:
        now = time.time()
        session = {
            "id": self.new_id(),
            "title": (title or "Новый диалог")[:80],
            "created_at": now,
            "updated_at": now,
            "model": model,
            "agent_id": agent_id,
            "skill_ids": list(skill_ids or []),
            "messages": [],
        }
        self.save(session)
        return session

    def save(self, session: dict) -> None:
        path = self._path(session["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def get(self, session_id: str) -> dict | None:
        try:
            path = self._path(session_id)
        except SessionError:
            return None
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def append_message(self, session: dict, message: dict) -> None:
        """Добавляет сообщение и сразу сохраняет сессию на диск."""
        session.setdefault("messages", []).append(message)
        session["updated_at"] = time.time()
        self.save(session)

    def summaries(self, limit: int = 100) -> list:
        items = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(
                {
                    "id": data.get("id"),
                    "title": data.get("title", ""),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "model": data.get("model"),
                    "message_count": len(data.get("messages") or []),
                }
            )
        items.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
        return items[:limit]

    def iter_all(self):
        """Все сессии (полные транскрипты) — для служебного анализа платформы.

        Используется формирователем скрытого «Контекста о пользователе»
        (runtime/user_context.py): свои сессии и упоминания пользователя.
        """
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                yield data

    def delete(self, session_id: str) -> bool:
        try:
            path = self._path(session_id)
        except SessionError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True
