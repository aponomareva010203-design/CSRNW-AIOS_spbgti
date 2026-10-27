"""Хранилище настроек SMTP для раздела «Рассылка писем» в /admin.html.

Файл AIOS_RUNTIME_DIR/newsletter_smtp.json: {host, port, tls, user,
password, from_email, from_name}. Специально НЕ связано с настройками
писем подтверждения регистрации (AIOS_SMTP_* в .env, см.
api/runtime/mailer.send_confirmation_email) — это два независимых канала:
смена SMTP для рассылки не может случайно сломать письма подтверждения,
и наоборот. Если этот файл не заполнен — рассылка недоступна, .env как
фолбэк для неё не используется.

Пароль (пароль приложения Яндекс Почты) хранится только на сервере и
никогда не отдаётся API целиком — только маской (mask_key).
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TLS_MODES = ("ssl", "starttls", "none")


class MailSettingsError(ValueError):
    """Некорректные настройки SMTP."""


@dataclass(frozen=True)
class MailConfig:
    """Действующая конфигурация SMTP рассылки."""

    host: str
    port: int
    tls: str
    user: str
    password: str
    from_email: str
    from_name: str
    source: str  # 'admin' | ''


class MailSettingsStore:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "newsletter_smtp.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(
        self,
        host: str,
        port: int,
        tls: str,
        user: str,
        password: str,
        from_email: str,
        from_name: str,
    ) -> dict:
        host = (host or "").strip()
        tls = (tls or "").strip().lower()
        user = (user or "").strip()
        password = password or ""
        from_email = (from_email or "").strip()
        from_name = (from_name or "").strip()
        if not host:
            raise MailSettingsError("Укажите SMTP-сервер (например, smtp.yandex.ru)")
        if tls not in TLS_MODES:
            raise MailSettingsError(f"Тип шифрования должен быть одним из: {', '.join(TLS_MODES)}")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise MailSettingsError("Порт должен быть числом")
        if not 1 <= port <= 65535:
            raise MailSettingsError("Порт должен быть от 1 до 65535")
        if from_email and not EMAIL_RE.match(from_email):
            raise MailSettingsError("Адрес отправителя должен быть корректным e-mail")
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
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        return data

    def clear(self) -> bool:
        if self.path.is_file():
            self.path.unlink()
            return True
        return False


def effective_mail_config() -> MailConfig:
    """Действующая конфигурация SMTP рассылки — только из своего файла,
    без фолбэка на .env (сознательно, чтобы не зависеть от писем
    подтверждения регистрации)."""
    admin = MailSettingsStore(get_settings().runtime_dir).load()
    if admin and admin.get("host"):
        return MailConfig(
            host=str(admin["host"]),
            port=int(admin.get("port") or 465),
            tls=str(admin.get("tls") or "ssl"),
            user=str(admin.get("user") or ""),
            password=str(admin.get("password") or ""),
            from_email=str(admin.get("from_email") or admin.get("user") or ""),
            from_name=str(admin.get("from_name") or "AIOS · Инновации университета"),
            source="admin",
        )
    return MailConfig(
        host="", port=465, tls="ssl", user="", password="",
        from_email="", from_name="", source="",
    )


def mask_key(value: str) -> str:
    """Маска секрета для отдачи в API — видны первые 3 и последние 4 символа."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:3] + "•" * 6 + value[-4:]
