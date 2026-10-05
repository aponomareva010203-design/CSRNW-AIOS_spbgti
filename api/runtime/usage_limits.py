"""Суточные лимиты токенов LLM (api/runtime/usage_limits.py).

Файл AIOS_RUNTIME_DIR/usage/limits.json:

    {"daily_limit": 3500000, "overrides": {"<user_id>": 8000000},
     "updated_at": float}

  * daily_limit — общий суточный лимит на пользователя (по умолчанию
    DEFAULT_DAILY_LIMIT); задаётся администратором в админ-панели;
  * overrides — индивидуальные лимиты (обычно повышенные) отдельным
    пользователям; отсутствие записи = действует общий лимит.

Проверка лимита — перед каждым шагом обращения к модели в агентном
цикле (runtime/loop.py); сам расход считает UsageStore.used_today
(usage.py). Сутки — по московскому времени, обнуление в 00:00 МСК.

Фоновой расход платформы (SYSTEM_USER_ID в usage.py) лимитом не
ограничивается — проверка применяется только к пользователям чата.
"""

import json
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

DEFAULT_DAILY_LIMIT = 3_500_000

# разумная верхняя граница, чтобы опечатка в админ-панели не сняла лимит вовсе
MAX_DAILY_LIMIT = 1_000_000_000


class UsageLimitsError(ValueError):
    """Некорректное значение лимита."""


class UsageLimitsStore:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "usage" / "limits.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        overrides = data.get("overrides")
        return {
            "daily_limit": _sanitize_limit(data.get("daily_limit"), DEFAULT_DAILY_LIMIT),
            "overrides": overrides if isinstance(overrides, dict) else {},
            "updated_at": float(data.get("updated_at") or 0),
        }

    def _save(self, data: dict) -> None:
        data["updated_at"] = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def config(self) -> dict:
        """Текущая конфигурация для админ-панели (оверарайды — как есть)."""
        data = self._load()
        return {
            "daily_limit": data["daily_limit"],
            "overrides": dict(data["overrides"]),
            "default_limit": DEFAULT_DAILY_LIMIT,
            "updated_at": data["updated_at"] or None,
        }

    def set_global(self, limit: int) -> dict:
        """Общий суточный лимит на всех пользователей."""
        data = self._load()
        data["daily_limit"] = _validate_limit(limit)
        self._save(data)
        return self.config()

    def override(self, user_id: str) -> int | None:
        """Индивидуальный лимит пользователя или None — действует общий."""
        value = self._load()["overrides"].get(user_id or "")
        return _sanitize_limit(value, None)

    def set_override(self, user_id: str, limit: int | None) -> None:
        """Задать индивидуальный лимит; None — убрать (вернуть общий)."""
        if not user_id:
            raise UsageLimitsError("Нужен id пользователя")
        data = self._load()
        if limit is None:
            data["overrides"].pop(user_id, None)
        else:
            data["overrides"][user_id] = _validate_limit(limit)
        self._save(data)

    def effective_limit(self, user_id: str) -> int:
        """Действующий лимит пользователя: индивидуальный либо общий."""
        return self.override(user_id) or self._load()["daily_limit"]


def _validate_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise UsageLimitsError("Лимит должен быть целым числом токенов")
    if limit < 1:
        raise UsageLimitsError("Лимит должен быть не меньше 1 токена")
    if limit > MAX_DAILY_LIMIT:
        raise UsageLimitsError(f"Лимит не может быть больше {MAX_DAILY_LIMIT}")
    return limit


def _sanitize_limit(value, fallback: int | None) -> int | None:
    """Значение из файла: корректное число или fallback (повреждённая запись не ломает платформу)."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return fallback
    if limit < 1:
        return fallback
    return limit


@lru_cache(maxsize=1)
def get_store() -> UsageLimitsStore:
    """Синглтон хранилища лимитов; в тестах сбрасывается cache_clear()."""
    return UsageLimitsStore(get_settings().runtime_dir)
