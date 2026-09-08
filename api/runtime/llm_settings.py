"""Хранилище настроек внешнего LLM-API, задаваемых администратором.

Файл AIOS_RUNTIME_DIR/llm.json: {base_url, api_key, model}. Приоритет:
настройки админа (файл) → переменные окружения (.env) → не настроено.
API-ключ хранится только на сервере (вне git и вне публичного /data)
и никогда не отдаётся API целиком — только маской (mask_key).
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


class LLMSettingsError(ValueError):
    """Некорректные настройки LLM."""


@dataclass(frozen=True)
class LLMConfig:
    """Действующая конфигурация LLM и её источник."""

    base_url: str
    api_key: str
    model: str
    source: str  # 'admin' | 'env' | ''


class LLMSettingsStore:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "llm.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, base_url: str, api_key: str, model: str) -> dict:
        base_url = (base_url or "").strip().rstrip("/")
        api_key = (api_key or "").strip()
        model = (model or "").strip()
        if not URL_RE.match(base_url):
            raise LLMSettingsError("Base URL должен начинаться с http:// или https:// и быть корректным адресом")
        if not model:
            raise LLMSettingsError("Укажите имя модели")
        if len(model) > 200:
            raise LLMSettingsError("Имя модели слишком длинное (до 200 символов)")
        data = {
            "base_url": base_url,
            "api_key": api_key,  # пустой ключ допустим (локальные эндпоинты без авторизации)
            "model": model,
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


def mask_key(api_key: str) -> str:
    """Маска ключа для отдачи в API: видны первые 3 и последние 4 символа."""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "•" * len(api_key)
    return api_key[:3] + "•" * 6 + api_key[-4:]


def effective_config() -> LLMConfig:
    """Действующая конфигурация: настройки админа → .env → пустая."""
    admin = LLMSettingsStore(get_settings().runtime_dir).load()
    if admin and admin.get("base_url") and admin.get("model"):
        return LLMConfig(
            base_url=str(admin["base_url"]).rstrip("/"),
            api_key=str(admin.get("api_key") or ""),
            model=str(admin["model"]),
            source="admin",
        )
    s = get_settings()
    if s.has_real_provider:
        return LLMConfig(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
            model=s.llm_model,
            source="env",
        )
    return LLMConfig(base_url="", api_key="", model="", source="")
