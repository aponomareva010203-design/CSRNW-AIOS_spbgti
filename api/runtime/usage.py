"""Учёт токенов LLM по пользователям (api/runtime/usage.py).

Хранилища — два JSON-файла в AIOS_RUNTIME_DIR/usage/:

    tokens.json — пожизненные агрегаты по пользователям:
    { "<user_id>": {"prompt": int, "completion": int,
                    "requests": int, "updated_at": float} }

    daily.json  — расход за текущие сутки (для суточных лимитов,
    см. usage_limits.py): { "<YYYY-MM-DD>": {"<user_id>": {...}} };
    при записи хранится только сегодняшний ключ — вчерашние записи
    выбрасываются, поэтому счётчик обнуляется сам.

Сутки считаются по московскому времени (UTC+3, без перехода на летнее):
граница суток 00:00 МСК единая для всех пользователей платформы.

Записывает агрегаты только по пользователям платформы (chat-цикл,
runtime/loop.py, после каждого шага обращения к модели). Провайдер отдаёт
точный usage (stream_options.include_usage); если провайдер его не прислал —
в цикле используется оценка по длине текста (~4 символа на токен).

Файлы по одному и пишутся атомарно; при нескольких воркерах uvicorn возможна
потеря редких параллельных инкрементов — для статистики и лимитов это
приемлемо (лимит может быть едва превышен, но не пропущен заметно).
"""

import json
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .config import get_settings

# московское время = UTC+3 круглый год
_MSK = timezone(timedelta(hours=3))


def _empty() -> dict:
    return {"prompt": 0, "completion": 0, "requests": 0, "updated_at": 0.0}


def today_key() -> str:
    """Ключ текущих суток «YYYY-MM-DD» по московскому времени.

    Отдельная функция — тесты подменяют её, чтобы смоделировать смену суток.
    """
    return datetime.now(_MSK).strftime("%Y-%m-%d")


# запись под этим ключом — расход фоновых агентов платформы (генерация
# скрытых контекстов и т.п.), а не конкретного пользователя
SYSTEM_USER_ID = "__system__"


class UsageStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tokens.json"
        self.daily_path = self.root / "daily.json"

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load_daily(self) -> dict:
        if not self.daily_path.is_file():
            return {}
        try:
            data = json.loads(self.daily_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def record(self, user_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Прибавить токены одного шага обращения к модели (итого + сутки)."""
        if not user_id:
            return
        prompt_inc = max(0, int(prompt_tokens or 0))
        completion_inc = max(0, int(completion_tokens or 0))

        data = self._load()
        entry = data.get(user_id)
        if not isinstance(entry, dict):
            entry = _empty()
        entry["prompt"] = int(entry.get("prompt") or 0) + prompt_inc
        entry["completion"] = int(entry.get("completion") or 0) + completion_inc
        entry["requests"] = int(entry.get("requests") or 0) + 1
        entry["updated_at"] = time.time()
        data[user_id] = entry
        self._write(self.path, data)

        daily = self._load_daily()
        today = today_key()
        # храним только сегодняшние сутки — вчерашние записи означают обнуление
        if daily.keys() - {today}:
            daily = {today: daily.get(today, {})}
        day = daily.setdefault(today, {})
        uday = day.get(user_id)
        if not isinstance(uday, dict):
            uday = {"prompt": 0, "completion": 0, "requests": 0}
        uday["prompt"] += prompt_inc
        uday["completion"] += completion_inc
        uday["requests"] = int(uday.get("requests") or 0) + 1
        day[user_id] = uday
        self._write(self.daily_path, daily)

    def used_today(self, user_id: str) -> dict:
        """Расход пользователя за текущие сутки; без записей — нули."""
        day = self._load_daily().get(today_key())
        entry = day.get(user_id) if isinstance(day, dict) else None
        if not isinstance(entry, dict):
            return {"prompt": 0, "completion": 0, "requests": 0, "total": 0}
        out = {
            "prompt": int(entry.get("prompt") or 0),
            "completion": int(entry.get("completion") or 0),
            "requests": int(entry.get("requests") or 0),
        }
        out["total"] = out["prompt"] + out["completion"]
        return out

    def get(self, user_id: str) -> dict:
        """Агрегаты пользователя; без записей — нули."""
        entry = self._load().get(user_id)
        if not isinstance(entry, dict):
            return _empty()
        out = _empty()
        out["prompt"] = int(entry.get("prompt") or 0)
        out["completion"] = int(entry.get("completion") or 0)
        out["requests"] = int(entry.get("requests") or 0)
        out["updated_at"] = float(entry.get("updated_at") or 0)
        return out

    def delete_user(self, user_id: str) -> bool:
        """Убрать пользователя из учёта (при удалении учётной записи)."""
        data = self._load()
        changed = False
        if user_id in data:
            del data[user_id]
            self._write(self.path, data)
            changed = True
        daily = self._load_daily()
        for day in daily.values():
            if isinstance(day, dict) and user_id in day:
                del day[user_id]
                changed = True
        if changed and daily:
            self._write(self.daily_path, daily)
        return changed

    def totals(self) -> dict:
        """Сводка по всей платформе: сумма по пользователям + расход фоновых агентов.

        {"prompt", "completion", "requests", "background": {…}} — фон под
        ключом SYSTEM_USER_ID, в сумму пользователей не входит.
        """
        data = self._load()
        total = _empty()
        background = _empty()
        for uid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            bucket = background if uid == SYSTEM_USER_ID else total
            bucket["prompt"] += int(entry.get("prompt") or 0)
            bucket["completion"] += int(entry.get("completion") or 0)
            bucket["requests"] += int(entry.get("requests") or 0)
        total.pop("updated_at")
        background.pop("updated_at")
        return {"prompt": total["prompt"], "completion": total["completion"],
                "requests": total["requests"], "background": background}


@lru_cache(maxsize=1)
def get_store() -> UsageStore:
    """Синглтон хранилища токенов; в тестах сбрасывается cache_clear()."""
    return UsageStore(get_settings().runtime_dir / "usage")