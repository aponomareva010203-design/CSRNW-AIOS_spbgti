"""Личные SMTP-настройки пользователя для писем его агентов (notify).

Файл AIOS_RUNTIME_DIR/user/{user_id}-agent-mail.json (рядом с личными
настройками LLM, запись атомарная — tmp + replace):

    {"host": "smtp.yandex.ru", "port": 465, "tls": "ssl", "user": "…",
     "password": "…", "from_email": "…", "from_name": "…", "updated_at": …}

Зачем отдельный канал. Инструмент notify пишет письма владельцу через
SMTP рассылки администратора (newsletter_smtp.json) — письмо уходит от
имени организации. Пользователю, чей агент работает по расписанию,
может понадобиться свой ящик: свои лимиты, своё «от кого», своя история
отправлений. Личные настройки перекрывают админские только для писем
ЭТОГО пользователя (его агентов и напоминаний); рассылка платформы и
письма подтверждения регистрации на них не влияют.

Пароль хранится только на сервере и никогда не отдаётся API целиком —
только маской (mask_password).
"""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TLS_MODES = ("ssl", "starttls", "none")

HOST_MAX_CHARS = 200
USER_MAX_CHARS = 200
PASSWORD_MAX_CHARS = 400
FROM_MAX_CHARS = 200


class UserMailError(ValueError):
    """Некорректные личные SMTP-настройки."""


class UserMailStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, user_id: str) -> Path:
        # тот же формат идентификаторов, что в хранилище личных LLM
        if not re.match(r"^[a-f0-9]{12}$", user_id or ""):
            raise UserMailError("Некорректный идентификатор пользователя")
        return self.root / f"{user_id}-agent-mail.json"

    def load(self, user_id: str) -> dict | None:
        path = self._path(user_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("host") else None

    def save(self, user_id: str, *, host: str, port, tls: str, user: str,
             password: str, from_email: str, from_name: str) -> dict:
        host = (host or "").strip()
        tls = (tls or "").strip().lower()
        user = (user or "").strip()
        password = password or ""
        from_email = (from_email or "").strip()
        from_name = (from_name or "").strip()
        if not host:
            raise UserMailError("Укажите SMTP-сервер (например, smtp.yandex.ru)")
        if len(host) > HOST_MAX_CHARS:
            raise UserMailError("Адрес SMTP-сервера слишком длинный")
        if tls not in TLS_MODES:
            raise UserMailError(f"Тип шифрования должен быть одним из: {', '.join(TLS_MODES)}")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise UserMailError("Порт должен быть числом")
        if not 1 <= port <= 65535:
            raise UserMailError("Порт должен быть числом от 1 до 65535")
        if from_email and not EMAIL_RE.match(from_email):
            raise UserMailError("Адрес отправителя должен быть корректным e-mail")
        data = {
            "host": host,
            "port": port,
            "tls": tls,
            "user": user,
            "password": password,
            "from_email": from_email,
            "from_name": from_name,
            "updated_at": time.time(),
        }
        path = self._path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return data

    def clear(self, user_id: str) -> bool:
        path = self._path(user_id)
        if path.is_file():
            path.unlink()
            return True
        return False


def mask_password(value: str) -> str:
    """Маска секрета для отдачи в API — видны первые 3 и последний символ."""
    value = value or ""
    if len(value) <= 4:
        return "••••" if value else ""
    return value[:3] + "•" * (len(value) - 4) + value[-1:]


@lru_cache
def get_store() -> UserMailStore:
    """Синглтон хранилища; в тестах сбрасывается cache_clear()."""
    return UserMailStore(get_settings().runtime_dir / "user")


def personal_mail(user_id: str | None) -> dict | None:
    """Активные личные SMTP-настройки пользователя или None."""
    if not user_id:
        return None
    try:
        return get_store().load(user_id)
    except UserMailError:
        return None


def public_view(user_id: str) -> dict:
    """Карточка настроек для интерфейса: без пароля, только маска."""
    data = personal_mail(user_id)
    return {
        "host": (data or {}).get("host") or "",
        "port": (data or {}).get("port") or 465,
        "tls": (data or {}).get("tls") or "ssl",
        "user": (data or {}).get("user") or "",
        "password_masked": mask_password((data or {}).get("password") or ""),
        "from_email": (data or {}).get("from_email") or "",
        "from_name": (data or {}).get("from_name") or "",
        "active": data is not None,
        "source": "personal" if data else "",
    }
