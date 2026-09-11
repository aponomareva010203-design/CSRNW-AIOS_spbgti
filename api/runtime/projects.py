"""Хранилище карточек проектов («Проекты», api/runtime/projects.py).

Каталог AIOS_RUNTIME_DIR/projects/{user_id}/ — по одному JSON-файлу на
карточку (id вида prj-…), запись атомарная: временный файл + replace.
Каталог намеренно лежит в .aios-runtime, а не в публичной data/: карточка
содержит персональные данные (ФИО владельца, бюджет), которые должны быть
доступны только владельцу по Bearer-токену — data/ раздаётся без авторизации.

Карточка создаётся навыком «Онбординг проекта» через инструмент
save_project_card и живёт в двух статусах:

    draft  — черновик онбординга (перезаписывается на каждом шаге навыка);
    active — подтверждённая пользователем карточка, видна на «Моих проектах».

Доступ жёстко привязан к идентификатору из Bearer-токена: клиент не может
обратиться к чужим карточкам — идентификатор из запроса не принимается.
"""

import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

# тот же формат идентификаторов владельца, что и в хранилище кабинета
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
PROJECT_ID_RE = re.compile(r"^prj-[a-f0-9]{8,12}$")

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE)

# лимиты полей карточки (схема — навык «Онбординг проекта», Шаги 3–7)
TITLE_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 2_500  # просят ≤2000, жёсткий потолок — 2500
GOAL_MAX_CHARS = 2_000
TEAM_SIZE_MAX_CHARS = 60
PARTICIPANTS_MAX_CHARS = 2_000
DEPARTMENTS_MAX_CHARS = 500
BUDGET_MAX_CHARS = 200
GROUNDWORK_MAX_CHARS = 4_000

# строковые поля карточки → лимит символов
TEXT_FIELDS = {
    "title": TITLE_MAX_CHARS,
    "description": DESCRIPTION_MAX_CHARS,
    "goal": GOAL_MAX_CHARS,
    "team_size": TEAM_SIZE_MAX_CHARS,
    "participants": PARTICIPANTS_MAX_CHARS,
    "departments": DEPARTMENTS_MAX_CHARS,
    "budget": BUDGET_MAX_CHARS,
    "groundwork": GROUNDWORK_MAX_CHARS,
}

# списковые поля (ссылки/файлы) → (макс. элементов, лимит символов на элемент)
LIST_FIELD_LIMITS = {
    "groundwork_links": (10, 500),
    "groundwork_files": (10, 300),
    "presentation_files": (5, 300),
}

# артефакты пилота (таблица «Статус пилотного проекта», pilot.js) и лимит
# гипотезы на артефакт. Блок artifacts пишут навык «Онбординг проекта»
# (save_project_card) и владелец карточки (PATCH /api/projects/{id});
# читает его публичный эндпоинт GET /api/pilot/projects.
ARTIFACT_KEYS = ("rag", "models", "agents", "polygon", "social")
ARTIFACT_STATUS_MIN = 0  # не начато
ARTIFACT_STATUS_MAX = 3  # завершено (1 — гипотеза сформулирована, 2 — в процессе создания)
ARTIFACT_HYPOTHESIS_MAX_CHARS = 2_000


def clean_artifacts(raw) -> dict:
    """Нормализует блок artifacts карточки к фиксированной схеме.

    Принимает None или словарь {ключ артефакта: {"status": 0..3,
    "hypothesis": str} | str}. Возвращает словарь со всеми пятью ключами
    ARTIFACT_KEYS в виде {"status": int, "hypothesis": str}. Строка вместо
    словаря трактуется как гипотеза. Статус, не указанный явно, выводится
    из наличия гипотезы: текст есть — «гипотеза сформулирована» (1), иначе
    «не начато» (0). Неизвестные ключи игнорируются, статус вне диапазона
    0–3 (или не число) — ProjectError.
    """
    raw_map = raw if isinstance(raw, dict) else {}
    result: dict[str, dict] = {}
    for key in ARTIFACT_KEYS:
        entry = raw_map.get(key)
        hypothesis = ""
        status = None
        if isinstance(entry, dict):
            hypothesis = _clean_text(entry.get("hypothesis"), ARTIFACT_HYPOTHESIS_MAX_CHARS)
            status = entry.get("status")
        elif isinstance(entry, str):
            hypothesis = _clean_text(entry, ARTIFACT_HYPOTHESIS_MAX_CHARS)
        if status is None:
            status = 1 if hypothesis else 0
        else:
            if isinstance(status, str) and status.strip().isdigit():
                status = int(status.strip())
            if isinstance(status, bool) or not isinstance(status, int):
                raise ProjectError(
                    f"Статус артефакта «{key}» должен быть числом от "
                    f"{ARTIFACT_STATUS_MIN} до {ARTIFACT_STATUS_MAX}"
                )
            if not ARTIFACT_STATUS_MIN <= status <= ARTIFACT_STATUS_MAX:
                raise ProjectError(
                    f"Статус артефакта «{key}» должен быть числом от "
                    f"{ARTIFACT_STATUS_MIN} до {ARTIFACT_STATUS_MAX}"
                )
        result[key] = {"status": status, "hypothesis": hypothesis}
    return result


class ProjectError(ValueError):
    """Некорректные данные карточки проекта."""


def bump_artifact_status(project_id: str, key: str, min_status: int) -> bool:
    """Поднимает статус артефакта карточки проекта до min_status (только вперёд).

    Синхронизация таблицы «Статус пилотного проекта» с реальной работой:
    онтология и RAG-база, привязанные к проекту (save_ontology / save_rag,
    включая публикацию), сами двигают колонку rag — участие администратора
    не требуется. Статус, уже выставленный владельцем или администратором
    выше min_status, не понижается. False — карточки с таким id нет (проект
    ещё черновик или удалён), синхронизировать нечего.
    """
    if key not in ARTIFACT_KEYS or not PROJECT_ID_RE.match(project_id or ""):
        return False
    store = get_store()
    card = store.find(project_id)
    if card is None:
        return False
    artifacts = clean_artifacts(card.get("artifacts"))
    if min_status <= artifacts[key]["status"]:
        return False
    artifacts[key]["status"] = min_status
    store.save(card["user_id"], {"project_id": project_id, "artifacts": artifacts})
    return True


def _clean_text(value, limit: int) -> str:
    """Строка без лишних пробелов, обрезанная до лимита; None → ''."""
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _clean_list(value, max_items: int, item_limit: int) -> list:
    """Список непустых строк из списка или многострочной строки."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        raise ProjectError("Ожидается список строк или многострочная строка")
    items: list[str] = []
    for item in value:
        cleaned = _clean_text(item, item_limit)
        if cleaned:
            items.append(cleaned)
        if len(items) >= max_items:
            break
    return items


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ----- низкоуровневые операции -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise ProjectError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _path(self, user_id: str, project_id: str) -> Path:
        if not PROJECT_ID_RE.match(project_id or ""):
            raise ProjectError("Некорректный идентификатор проекта")
        return self._dir(user_id) / f"{project_id}.json"

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

    # ----- чтение -----

    def list(self, user_id: str) -> list:
        """Все карточки пользователя, свежие сверху."""
        directory = self._dir(user_id)
        if not directory.is_dir():
            return []
        items = []
        for path in directory.glob("*.json"):
            card = self._read(path)
            if card and card.get("id"):
                items.append(card)
        items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return items

    def get(self, user_id: str, project_id: str) -> dict | None:
        try:
            card = self._read(self._path(user_id, project_id))
        except ProjectError:
            return None
        if card and card.get("id") != project_id:
            return None
        return card

    def find_participant_cards(self, needles) -> list:
        """Карточки любых владельцев, где в поле participants упомянут
        кто-то из needles (варианты ФИО пользователя).

        Раздел «Результаты работы» в «Кабинете»: проекты, где пользователь
        является участником. Ищем по всем карточкам, включая черновики.
        ФИО в карточке может быть записано иначе, чем в учётной записи
        («Иванов И.И.» против «Иванов Иван Иванович»), поэтому сравниваем
        по токенам с учётом инициалов (_name_matches), а не подстрокой.
        """
        needle_tokens = [
            tokens for tokens in (_name_tokens(n) for n in needles or [])
            if tokens and len(tokens[0]) >= 4  # фамилия в эталоне не короче 4 букв
        ]
        if not needle_tokens or not self.root.is_dir():
            return []
        items = []
        for path in self.root.glob("*/*.json"):
            card = self._read(path)
            if not card or not card.get("id"):
                continue
            participants = str(card.get("participants") or "")
            # участники разделяются запятыми, точками с запятой и переносами строк
            for entry in re.split(r"[;,\n\r]+", participants):
                entry_tokens = _name_tokens(entry)
                # в записи участника нужна фамилия (иначе «И.» матчиться на всех)
                if len(entry_tokens) < 2 or len(entry_tokens[0]) < 4:
                    continue
                if any(_name_matches(entry_tokens, tokens) for tokens in needle_tokens):
                    items.append(card)
                    break
        items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return items

    # ----- запись -----

    def save(self, user_id: str, card: dict) -> dict:
        """Создаёт или обновляет карточку (upsert), возвращая сохранённую.

        Правила слияния:
          - существующая карточка обновляется только переданными полями —
            навык досылает карточку целиком на каждом шаге онбординга,
            частичные дозаписи не теряют уже собранные шаги;
          - статус переключается только явно (поле status); создать карточку
            сразу со статусом active нельзя — подтверждение отдельный шаг.
        """
        raw = card if isinstance(card, dict) else {}
        project_id = _clean_text(raw.get("project_id") or raw.get("id"), 40)
        existing = self.get(user_id, project_id) if project_id else None
        if project_id and not existing:
            raise ProjectError(f"Карточка не найдена: {project_id}")

        data = dict(existing) if existing else {
            "id": "prj-" + secrets.token_hex(5),
            "user_id": user_id,
            "created_at": time.time(),
            "status": STATUS_DRAFT,
        }

        for field, limit in TEXT_FIELDS.items():
            if field in raw:
                data[field] = _clean_text(raw.get(field), limit)
        for field, (max_items, item_limit) in LIST_FIELD_LIMITS.items():
            if field in raw:
                data[field] = _clean_list(raw.get(field), max_items, item_limit)
        if "artifacts" in raw:
            data["artifacts"] = clean_artifacts(raw.get("artifacts"))
        if "presentation" in raw:
            data["presentation"] = bool(raw.get("presentation"))

        status = _clean_text(raw.get("status"), 20)
        if status:
            if status not in STATUSES:
                raise ProjectError(
                    "Статус должен быть одним из: " + ", ".join(STATUSES)
                )
            if existing is None and status == STATUS_ACTIVE:
                raise ProjectError(
                    "Новая карточка сохраняется как черновик; статус active "
                    "ставится отдельным вызовом после подтверждения пользователя"
                )
            if status != data.get("status"):
                data["status"] = status
                data["status_changed_at"] = time.time()

        data["updated_at"] = time.time()
        self._write(self._path(user_id, data["id"]), data)
        return data

    def delete(self, user_id: str, project_id: str) -> bool:
        """Удаляет карточку владельца; True — если карточка существовала."""
        path = self._path(user_id, project_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def find(self, project_id: str) -> dict | None:
        """Карточка по id у любого владельца (служебное: админские операции)."""
        if not PROJECT_ID_RE.match(project_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{project_id}.json"):
            card = self._read(path)
            if card and card.get("id") == project_id:
                return card
        return None

    # ----- файлы карточки (вложения чата, прикреплённые онбордингом) -----

    def files_dir(self, user_id: str, project_id: str) -> Path:
        """Каталог файлов карточки (создаётся при первом файле)."""
        return self._dir(user_id) / "files" / project_id

    def save_file(self, user_id: str, project_id: str, filename: str,
                  content: bytes) -> str:
        """Сохраняет файл карточки, возвращая безопасное имя для скачивания.

        Коллизии имён разрешаются суффиксом -2, -3…: карточка ссылается на
        файл по имени из groundwork_files/presentation_files, поэтому
        перезаписывать существующий файл нельзя.
        """
        from .uploads import clean_filename

        name = clean_filename(filename)
        directory = self.files_dir(user_id, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / name
        stem, suffix = candidate.stem, candidate.suffix
        counter = 2
        while candidate.exists():
            candidate = directory / f"{stem}-{counter}{suffix}"
            counter += 1
        candidate.write_bytes(content)
        return candidate.name

    def file_path(self, user_id: str, project_id: str, filename: str) -> Path | None:
        """Путь к файлу карточки по имени; None — если файла нет."""
        try:
            directory = self.files_dir(user_id, project_id)
        except ProjectError:
            return None
        candidate = (directory / filename).resolve()
        if not candidate.is_relative_to(directory.resolve()) or not candidate.is_file():
            return None
        return candidate

    def list_active(self) -> "list[dict]":
        """Активные карточки всех владельцев — источник строк таблицы пилота.

        Черновики (draft) не попадают: карточка появляется на странице
        «Статус пилотного проекта» после подтверждения пользователем.
        Порядок стабильный: по времени создания, затем по id.
        """
        cards: list[dict] = []
        if not self.root.is_dir():
            return cards
        for path in self.root.glob("*/*.json"):
            card = self._read(path)
            if card and card.get("status") == STATUS_ACTIVE:
                cards.append(card)
        cards.sort(key=lambda c: (c.get("created_at") or c.get("updated_at") or 0, c.get("id") or ""))
        return cards

    def public_card(self, card: dict) -> dict:
        """Карточка для API/UI: без служебного user_id (он и так из токена)."""
        return {k: v for k, v in card.items() if k != "user_id"}


def _name_tokens(text: str) -> list[str]:
    """Слова ФИО в нижнем регистре (инициалы «И.И.» дают токены «и», «и»)."""
    return [t for t in re.split(r"[^a-zа-яё]+", text.lower()) if t]


def _covers(needle: list[str], entry: list[str]) -> bool:
    """Токены needle идут подмножеством entry (в том же порядке, по префиксу)."""
    it = iter(entry)
    return all(
        any(t == n or t.startswith(n) or n.startswith(t) for t in it)
        for n in needle
    )


def _name_matches(entry_tokens: list[str], needle_tokens: list[str]) -> bool:
    """Совпадение двух записей ФИО с учётом инициалов.

    Фамилии (первые токены) должны совпасть целиком или по префиксу; дальше
    достаточно, чтобы токены одной записи покрывали токены другой: «иванов и»
    матчится и с «иванов иван», и с «иванов иван иванович», «иванов и. и.» —
    с «иванов иван иванович», и наоборот (в карточке может быть полнее, чем
    в учётной записи, и чем в анкете).
    """
    if not entry_tokens or not needle_tokens:
        return False
    first_e, first_n = entry_tokens[0], needle_tokens[0]
    if not (first_e == first_n or first_e.startswith(first_n) or first_n.startswith(first_e)):
        return False
    return (
        _covers(needle_tokens[1:], entry_tokens[1:])
        or _covers(entry_tokens[1:], needle_tokens[1:])
    )


def participant_needles(user: dict, profile: dict) -> list[str]:
    """Варианты написания ФИО для поиска пользователя среди участников проектов.

    Тот же принцип, что и в runtime/user_context.py (_mention_needles):
    анкета «Кабинета» важнее имени учётной записи. Поиск без учёта регистра.
    """
    needles: set[str] = set()
    last = (profile.get("last_name") or "").strip()
    first = (profile.get("first_name") or "").strip()
    patronymic = (profile.get("patronymic") or "").strip()
    if last and first:
        needles.add(f"{last} {first}")
        initial = first[0]
        if patronymic:
            needles.add(f"{last} {initial}.{patronymic[0]}.")
            needles.add(f"{last} {first} {patronymic}")
        needles.add(f"{last} {initial}.")
    if user.get("name"):
        needles.add(str(user["name"]).strip())
    return sorted(n for n in needles if len(n) >= 5)


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    """Синглтон хранилища проектов; в тестах сбрасывается cache_clear()."""
    return ProjectStore(get_settings().runtime_dir / "projects")
