"""Хранилище блоков страницы «Кабинет» (api/runtime/cabinet.py).

Каталог AIOS_RUNTIME_DIR/user — по два JSON-файла на пользователя, каждый
блок отдельным файлом (запись атомарная: временный файл + replace):

    {user_id}-profile.json   — блок «Данные пользователя» (редактирует сам
                               пользователь через PUT /api/cabinet/profile);
    {user_id}-context.json   — скрытый блок «Контекст о пользователе»:
                               формируется языковой моделью по расписанию
                               из сессий и упоминаний (runtime/user_context.py),
                               в интерфейс не выносится и через API не отдаётся.

Доступ жёстко привязан к идентификатору из Bearer-токена: клиент не может
обратиться к чужим файлам — идентификатор из запроса не принимается.
"""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

# тот же формат идентификаторов, что и в хранилище пользователей
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")

CONTEXT_MAX_CHARS = 1_000  # скрытый контекст — не более 1000 символов

# лимиты блока «Данные пользователя»
NAME_MAX_CHARS = 80
POSITION_MAX_CHARS = 120
POSITIONS_MAX_ITEMS = 10
DEGREE_MAX_CHARS = 160
INTERESTS_MAX_CHARS = 2_000

PROFILE_DEFAULTS = {
    "last_name": "",
    "first_name": "",
    "patronymic": "",
    "positions": [],
    "degree": "",
    "interests": "",
}


class CabinetError(ValueError):
    """Некорректные данные блока «Кабинет»."""


def full_name(profile: dict) -> str:
    """ФИО одной строкой («Иванов Иван Иванович»); пустые части пропускаются."""
    parts = [
        (profile.get("last_name") or "").strip(),
        (profile.get("first_name") or "").strip(),
        (profile.get("patronymic") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


class CabinetStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str, block: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise CabinetError("Некорректный идентификатор пользователя")
        return self.root / f"{user_id}-{block}.json"

    def _read(self, path: Path) -> dict | None:
        """JSON-файл блока; отсутствующий или битый файл — None."""
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, path: Path, data: dict) -> None:
        """Атомарная запись блока: временный файл + replace."""
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # ----- блок 1: «Данные пользователя» -----

    def get_profile(self, user_id: str) -> dict:
        """Блок «Данные пользователя»; без файла — значения по умолчанию."""
        try:
            path = self._path(user_id, "profile")
        except CabinetError:
            return dict(PROFILE_DEFAULTS, updated_at=None)
        data = self._read(path)
        if data is None:
            return dict(PROFILE_DEFAULTS, updated_at=None)
        merged = dict(PROFILE_DEFAULTS)
        for key in PROFILE_DEFAULTS:
            if key in data:
                merged[key] = data[key]
        merged["updated_at"] = data.get("updated_at")
        return merged

    def save_profile(self, user_id: str, **fields) -> dict:
        """Валидирует и атомарно сохраняет блок «Данные пользователя»."""
        def clean_str(name: str, limit: int) -> str:
            value = str(fields.get(name) or "").strip()
            if len(value) > limit:
                raise CabinetError(f"Поле «{name}» — до {limit} символов")
            return value

        last_name = clean_str("last_name", NAME_MAX_CHARS)
        first_name = clean_str("first_name", NAME_MAX_CHARS)
        patronymic = clean_str("patronymic", NAME_MAX_CHARS)
        degree = clean_str("degree", DEGREE_MAX_CHARS)
        interests = clean_str("interests", INTERESTS_MAX_CHARS)

        raw_positions = fields.get("positions")
        if raw_positions is None:
            raw_positions = []
        if not isinstance(raw_positions, list):
            raise CabinetError("Должности должны быть списком строк")
        if len(raw_positions) > POSITIONS_MAX_ITEMS:
            raise CabinetError(f"Должностей не может быть больше {POSITIONS_MAX_ITEMS}")
        positions = []
        for item in raw_positions:
            value = str(item or "").strip()
            if not value:
                continue
            if len(value) > POSITION_MAX_CHARS:
                raise CabinetError(f"Каждая должность — до {POSITION_MAX_CHARS} символов")
            positions.append(value)

        data = {
            "last_name": last_name,
            "first_name": first_name,
            "patronymic": patronymic,
            "positions": positions,
            "degree": degree,
            "interests": interests,
            "updated_at": time.time(),
        }
        self._write(self._path(user_id, "profile"), data)
        return data

    # ----- блок 2: скрытый «Контекст о пользователе» -----

    def get_context(self, user_id: str) -> dict | None:
        """Скрытый блок; используется только на сервере (промпт, планировщик)."""
        try:
            path = self._path(user_id, "context")
        except CabinetError:
            return None
        return self._read(path)

    def save_context(self, user_id: str, context: str, source_digest: str) -> dict:
        now = time.time()
        data = {
            "user_id": user_id,
            "context": (context or "").strip()[:CONTEXT_MAX_CHARS],
            "source_digest": source_digest,
            "generated_at": now,
            "checked_at": now,
        }
        self._write(self._path(user_id, "context"), data)
        return data

    def touch_checked(self, user_id: str) -> None:
        """Отметить плановую проверку, не меняя сам контекст."""
        current = self.get_context(user_id)
        if current is None:
            return
        current["checked_at"] = time.time()
        self._write(self._path(user_id, "context"), current)

    # ----- общие операции -----

    def delete_user(self, user_id: str) -> bool:
        """Удаляет оба блока пользователя (при удалении учётной записи)."""
        removed = False
        for block in ("profile", "context"):
            try:
                path = self._path(user_id, block)
            except CabinetError:
                continue
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    def prompt_block(self, user_id: str | None) -> str:
        """Компактный блок о пользователе для системного промпта ассистента.

        Собирает оба блока кабинета: анкетные данные и автоматически
        сформированный контекст. Пусто, если о пользователе ничего не известно.
        """
        if not user_id or not USER_ID_RE.match(user_id):
            return ""
        profile = self.get_profile(user_id)
        context = (self.get_context(user_id) or {}).get("context") or ""
        lines: list[str] = []
        name = full_name(profile)
        if name:
            lines.append(f"- ФИО: {name}")
        if profile.get("positions"):
            lines.append("- Должности: " + "; ".join(profile["positions"]))
        if profile.get("degree"):
            lines.append(f"- Учёная степень: {profile['degree']}")
        if profile.get("interests"):
            lines.append(f"- Научные интересы: {profile['interests']}")
        parts: list[str] = []
        if lines:
            parts.append("Анкетные данные (заполнил сам пользователь):\n" + "\n".join(lines))
        if context:
            parts.append("Контекст о пользователе (собран платформой автоматически):\n" + context)
        if not parts:
            return ""
        return "### Сведения о пользователе\n\n" + "\n\n".join(parts)


@lru_cache(maxsize=1)
def get_store() -> CabinetStore:
    """Синглтон хранилища кабинета; в тестах сбрасывается cache_clear()."""
    return CabinetStore(get_settings().runtime_dir / "user")

