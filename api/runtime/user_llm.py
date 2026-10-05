"""Личные настройки внешнего LLM-API пользователя (api/runtime/user_llm.py).

Файл AIOS_RUNTIME_DIR/user/{user_id}-llm.json (рядом с блоками «Кабинета»,
запись атомарная — tmp + replace):

    {"provider": "", "base_url": "https://…", "api_key": "…", "model": "",
     "updated_at": …}

Задаётся самим пользователем на вкладке «Настройки». Если конфигурация
задана (заполнен base_url), все обращения к генеративной модели от имени
этого пользователя (чат, иконки проектов, скрытый контекст) идут через
его endpoint и ключ. Модель — тоже личная, если указана; пустая — общая
модель платформы (её выбирает администратор централизованно). Суточный
лимит токенов к таким пользователям не применяется (usage_me отдаёт
limit: null — платформа считает только потраченное). Пустая конфигурация
(файла нет или base_url пуст) — работа по общим настройкам платформы.

API-ключ хранится только на сервере (вне git и вне публичного /data) и никогда
не отдаётся API целиком — только маской (llm_settings.mask_key).
"""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

# тот же формат идентификаторов, что и в хранилище кабинета
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
# тот же формат адреса, что в админских настройках LLM
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)

PROVIDER_MAX_CHARS = 60
BASE_URL_MAX_CHARS = 300
API_KEY_MAX_CHARS = 400
MODEL_MAX_CHARS = 200


class UserLLMError(ValueError):
    """Некорректные личные настройки LLM."""


class UserLLMStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise UserLLMError("Некорректный идентификатор пользователя")
        return self.root / f"{user_id}-llm.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def get(self, user_id: str) -> dict | None:
        """Активная личная конфигурация или None (работа по общим настройкам).

        Активность определяет заполненный base_url: ключ и модель не
        обязательны (локальные серверы работают без авторизации; модель без
        указания наследуется от платформы).
        """
        try:
            data = self._read(self._path(user_id))
        except UserLLMError:
            return None
        if not data or not str(data.get("base_url") or "").strip():
            return None
        return {
            "provider": str(data.get("provider") or "").strip(),
            "base_url": str(data["base_url"]).strip().rstrip("/"),
            "api_key": str(data.get("api_key") or ""),
            "model": str(data.get("model") or "").strip(),
        }

    def set(
        self,
        user_id: str,
        provider: str = "",
        base_url: str = "",
        api_key: str | None = None,
        model: str | None = None,
    ) -> dict:
        """Сохраняет личную конфигурацию, возвращает её.

        api_key=None — сохранить прежний ключ (пользователь не трогал поле);
        строка (в том числе пустая) — заменить. model=None — сохранить
        прежнюю модель; строка — заменить (пустая — использовать общую
        модель платформы).
        """
        path = self._path(user_id)
        clean_url = str(base_url or "").strip().rstrip("/")
        if not URL_RE.match(clean_url):
            raise UserLLMError(
                "Endpoint (Base URL) должен начинаться с http:// или https:// "
                "и быть корректным адресом"
            )
        old = self._read(path) or {}
        clean_provider = str(provider or "").strip()[:PROVIDER_MAX_CHARS]
        if api_key is None:
            clean_key = str(old.get("api_key") or "")
        else:
            clean_key = str(api_key).strip()[:API_KEY_MAX_CHARS]
        if model is None:
            clean_model = str(old.get("model") or "").strip()[:MODEL_MAX_CHARS]
        else:
            clean_model = str(model).strip()[:MODEL_MAX_CHARS]
        data = {
            "provider": clean_provider,
            "base_url": clean_url,
            "api_key": clean_key,
            "model": clean_model,
            "updated_at": time.time(),
        }
        self._write(path, data)
        return self.get(user_id) or {}

    def clear(self, user_id: str) -> bool:
        """Удаляет личную конфигурацию (возврат к общим настройкам)."""
        try:
            path = self._path(user_id)
        except UserLLMError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True


@lru_cache(maxsize=1)
def get_store() -> UserLLMStore:
    """Синглтон хранилища; в тестах сбрасывается cache_clear()."""
    return UserLLMStore(get_settings().runtime_dir / "user")


def personal_llm(user_id: str | None) -> dict | None:
    """Активная личная конфигурация пользователя или None."""
    if not user_id:
        return None
    return get_store().get(user_id)


def any_personal() -> bool:
    """Есть ли у кого-то личные настройки (быстрая проверка каталога).

    Служебная: планировщик «Контекста о пользователе» решает, стоит ли
    вообще идти по пользователям, когда общая LLM не настроена.
    """
    root = get_store().root
    return root.is_dir() and any(root.glob("*-llm.json"))
