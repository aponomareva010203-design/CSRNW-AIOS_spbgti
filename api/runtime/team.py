"""Хранилище прав «Моей команды» (api/runtime/team.py).

Один JSON-файл на руководителя: AIOS_RUNTIME_DIR/team/{owner_id}.json,
запись атомарная (временный файл + replace), по образцу хранилища кабинета:

    {"perms": {member_id: {"projects": bool, "ontologies": bool, "rag": bool}}}

Права выдаёт только руководитель проекта (владелец кабинета) участникам
своей команды — и только на редактирование (правки карточек). Удаление
любых артефактов остаётся прерогативой роли «Администратор», выдача прав
на удаление не предусмотрена вовсе. Виды артефактов ограничены
PERM_KINDS; новые типы (модели, агенты, полигоны) добавятся туда же.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from .config import get_settings

USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# виды артефактов, на которые можно выдать право редактирования
PERM_KINDS = ("projects", "ontologies", "rag")


class TeamError(ValueError):
    """Некорректные данные прав команды."""


class TeamStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, owner_id: str) -> Path:
        if not USER_ID_RE.match(owner_id or ""):
            raise TeamError("Некорректный идентификатор пользователя")
        return self.root / f"{owner_id}.json"

    @staticmethod
    def _read(path: Path) -> dict:
        if not path.is_file():
            return {"perms": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"perms": {}}
        return data if isinstance(data, dict) and isinstance(data.get("perms"), dict) else {"perms": {}}

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _clean_perms(raw) -> dict:
        """{вид артефакта: bool} только по известным видам; пропуски — False."""
        raw_map = raw if isinstance(raw, dict) else {}
        return {kind: bool(raw_map.get(kind)) for kind in PERM_KINDS}

    def member_permissions(self, owner_id: str, member_id: str) -> dict:
        """Права участника member_id в команде руководителя owner_id."""
        try:
            path = self._path(owner_id)
        except TeamError:
            return {kind: False for kind in PERM_KINDS}
        perms = self._read(path)["perms"]
        return self._clean_perms(perms.get(member_id))

    def set_member_permissions(self, owner_id: str, member_id: str, perms) -> dict:
        """Сохраняет права одного участника, возвращает их же."""
        try:
            path = self._path(owner_id)
            self._path(member_id)  # валидация идентификатора участника
        except TeamError:
            raise
        data = self._read(path)
        cleaned = self._clean_perms(perms)
        if any(cleaned.values()):
            data["perms"][member_id] = cleaned
        else:
            data["perms"].pop(member_id, None)  # все права сняты — запись не нужна
        self._write(path, data)
        return cleaned

    def can_edit(self, owner_id: str, member_id: str, kind: str) -> bool:
        """Есть ли у member_id право редактировать артефакты вида kind владельца owner_id."""
        if kind not in PERM_KINDS or owner_id == member_id:
            return False
        return self.member_permissions(owner_id, member_id).get(kind, False)


@lru_cache(maxsize=1)
def get_store() -> TeamStore:
    """Синглтон хранилища прав команды; в тестах сбрасывается cache_clear()."""
    return TeamStore(get_settings().runtime_dir / "team")
