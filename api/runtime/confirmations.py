"""Файловое хранилище токенов подтверждения e-mail.

Токен — secrets.token_urlsafe(32) (43 символа base64url); записи живут в
AIOS_RUNTIME_DIR/confirm/<token>.json: {user_id, created_at, expires_at}.
Время жизни — 48 часов. Просроченные записи удаляются при чтении.

Повторная отправка письма переиспользует живой токен пользователя: ссылка
из прежнего письма продолжает работать, спам-копий токенов не заводится.
"""

import json
import re
import secrets
import time
from pathlib import Path

CONFIRM_TTL = 48 * 3600  # время жизни токена подтверждения, сек (48 часов)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class ConfirmationStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, token: str) -> Path | None:
        """Путь к файлу токена; некорректные значения отвергаются (path traversal)."""
        if not TOKEN_RE.match(token or ""):
            return None
        return self.root / f"{token}.json"

    def issue(self, user_id: str, ttl: int = CONFIRM_TTL) -> str:
        """Токен подтверждения для пользователя.

        Если у пользователя уже есть живой токен, возвращается он (ссылка
        из ранее отправленного письма остаётся действительной).
        """
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("user_id") == user_id and (record.get("expires_at") or 0) >= time.time():
                return path.stem

        token = secrets.token_urlsafe(32)
        now = time.time()
        record = {"user_id": user_id, "created_at": now, "expires_at": now + ttl}
        path = self.root / f"{token}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return token

    def resolve(self, token: str) -> str | None:
        """user_id живого токена; просроченные записи удаляются на месте."""
        path = self._path(token)
        if path is None or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (record.get("expires_at") or 0) < time.time():
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return record.get("user_id")

    def revoke(self, token: str) -> bool:
        path = self._path(token)
        if path is None or not path.is_file():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def revoke_all_for_user(self, user_id: str) -> int:
        """Отзыв всех токенов подтверждения пользователя (например, при удалении учётки)."""
        count = 0
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("user_id") == user_id:
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    pass
        return count
