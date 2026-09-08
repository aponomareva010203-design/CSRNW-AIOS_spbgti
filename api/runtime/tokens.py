"""Файловое хранилище токенов авторизации (server-side сессии).

Токен — случайная hex-строка из 64 символов (secrets.token_hex(32));
метаданные живут в AIOS_RUNTIME_DIR/auth/<token>.json:
{user_id, created_at, expires_at}. Клиент передаёт токен в заголовке
Authorization: Bearer <token>. Просроченные токены удаляются при чтении.
"""

import json
import re
import secrets
import time
from pathlib import Path

TOKEN_TTL = 7 * 24 * 3600  # время жизни токена, сек (7 дней)
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


class TokenStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, token: str) -> Path | None:
        """Путь к файлу токена; некорректные значения отвергаются (path traversal)."""
        if not TOKEN_RE.match(token or ""):
            return None
        return self.root / f"{token}.json"

    def issue(self, user_id: str, ttl: int = TOKEN_TTL) -> str:
        token = secrets.token_hex(32)
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
        path.unlink()
        return True

    def revoke_all_for_user(self, user_id: str, keep: str | None = None) -> int:
        """Инвалидация всех токенов пользователя (например, при удалении учётки).

        keep — токен, который нужно сохранить (текущая сессия при смене пароля).
        """
        count = 0
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("user_id") == user_id and path.stem != keep:
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    pass
        return count
