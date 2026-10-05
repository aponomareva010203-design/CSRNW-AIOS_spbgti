"""Хранилище настроек подключения к n8n, задаваемых администратором.

Файл AIOS_RUNTIME_DIR/n8n.json: {base_url, api_key, service_key}. Приоритет:
настройки админа (файл) → переменные окружения (.env) → не настроено.

base_url — адрес self-hosted n8n (обычно http://127.0.0.1:5678: контейнер
n8n и AIOS живут на одном сервере, наружу n8n не публикуется).
api_key  — ключ public API, созданный владельцем n8n один раз через UI
           (Settings → n8n API → Create an API key). Все автоматизации
           платформы работают от этого ключа; пользователи n8n не создаются,
           права разграничивает сам AIOS по своим учёткам.
service_key — секрет для внутреннего эндпоинта /api/internal/agent-step
           (шаг «ИИ-агент AIOS» в автоматизациях): n8n-нода вызывает его
           с заголовком X-AIOS-Service-Key. Генерируется при первом
           сохранении настроек и не отдаётся наружу.

API-ключ и service_key хранятся только на сервере и никогда не отдаются
API целиком — только маской (llm_settings.mask_key).
"""

import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


class N8nSettingsError(ValueError):
    """Некорректные настройки подключения к n8n."""


@dataclass(frozen=True)
class N8nConfig:
    """Действующая конфигурация n8n и её источник."""

    base_url: str
    api_key: str
    service_key: str
    source: str  # 'admin' | 'env' | ''

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


def _new_service_key() -> str:
    return f"aios-svc-{secrets.token_hex(24)}"


class N8nSettingsStore:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "n8n.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, base_url: str, api_key: str) -> dict:
        base_url = (base_url or "").strip().rstrip("/")
        api_key = (api_key or "").strip()
        if not URL_RE.match(base_url):
            raise N8nSettingsError(
                "Адрес n8n должен начинаться с http:// или https:// и быть корректным URL"
            )
        if not api_key:
            raise N8nSettingsError("Укажите API-ключ n8n (Settings → n8n API → Create an API key)")
        saved = self.load() or {}
        data = {
            "base_url": base_url,
            "api_key": api_key,
            # service_key переживает смену адреса/ключа: пересоздание сломало бы
            # уже собранные автоматизации со старым секретом в нодах
            "service_key": saved.get("service_key") or _new_service_key(),
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


def effective_config() -> N8nConfig:
    """Действующая конфигурация: настройки админа → .env → пустая."""
    admin = N8nSettingsStore(get_settings().runtime_dir).load()
    if admin and admin.get("base_url") and admin.get("api_key"):
        return N8nConfig(
            base_url=str(admin["base_url"]).rstrip("/"),
            api_key=str(admin["api_key"]),
            service_key=str(admin.get("service_key") or ""),
            source="admin",
        )
    s = get_settings()
    if s.n8n_base_url and s.n8n_api_key:
        return N8nConfig(
            base_url=s.n8n_base_url,
            api_key=s.n8n_api_key,
            # для env-конфигурации секрет генерируется лениво и живёт в файле
            service_key=_ensure_env_service_key(),
            source="env",
        )
    return N8nConfig(base_url="", api_key="", service_key="", source="")


def _ensure_env_service_key() -> str:
    """service_key при конфигурации из .env: кладём в отдельный файл,
    чтобы не терялся между перезапусками (env сам секрет не хранит)."""
    store = N8nSettingsStore(get_settings().runtime_dir)
    saved = store.load() or {}
    key = str(saved.get("service_key") or "")
    if not key:
        key = _new_service_key()
        saved["service_key"] = key
        tmp = store.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(store.path)
    return key
