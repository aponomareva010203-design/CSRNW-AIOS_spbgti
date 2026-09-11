"""Учёт токенов LLM по пользователям (api/runtime/usage.py).

Хранилище — один JSON-файл AIOS_RUNTIME_DIR/usage/tokens.json:

    { "<user_id>": {"prompt": int, "completion": int,
                    "requests": int, "updated_at": float} }

Записывает агрегаты только по пользователям платформы (chat-цикл,
runtime/loop.py, после каждого шага обращения к модели). Провайдер отдаёт
точный usage (stream_options.include_usage); если провайдер его не прислал —
в цикле используется оценка по длине текста (~4 символа на токен).

Файл один и пишется атомарно; при нескольких воркерах uvicorn возможна
потеря редких параллельных инкрементов — для админской статистики это
приемлемо.
"""

import json
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings


def _empty() -> dict:
    return {"prompt": 0, "completion": 0, "requests": 0, "updated_at": 0.0}


# запись под этим ключом — расход фоновых агентов платформы (генерация
# скрытых контекстов и т.п.), а не конкретного пользователя
SYSTEM_USER_ID = "__system__"


class UsageStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tokens.json"

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, user_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Прибавить токены одного шага обращения к модели."""
        if not user_id:
            return
        data = self._load()
        entry = data.get(user_id)
        if not isinstance(entry, dict):
            entry = _empty()
        entry["prompt"] = int(entry.get("prompt") or 0) + max(0, int(prompt_tokens or 0))
        entry["completion"] = int(entry.get("completion") or 0) + max(0, int(completion_tokens or 0))
        entry["requests"] = int(entry.get("requests") or 0) + 1
        entry["updated_at"] = time.time()
        data[user_id] = entry
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

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
        if user_id not in data:
            return False
        del data[user_id]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return True

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