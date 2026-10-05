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

Права участников — как на GitHub (read/write/admin): руководитель выдаёт
участнику уровень доступа к артефактам своего проекта (блок acl в карточке;
см. access_level). Добавленный в состав команды (поле participants)
зарегистрированный пользователь автоматически получает просмотр артефактов
и черновиков проекта — явный уровень из acl его перекрывает.
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
# порядок артефактов карточки и таблицы пилота: после RAG — агенты, затем модели
ARTIFACT_KEYS = ("rag", "agents", "models", "polygon", "social")
ARTIFACT_STATUS_MIN = 0  # не начато
ARTIFACT_STATUS_MAX = 3  # завершено (1 — гипотеза сформулирована, 2 — в процессе создания)
ARTIFACT_HYPOTHESIS_MAX_CHARS = 2_000

# ----- уровни доступа участников проекта (названия — как на GitHub) -----
#
# read   («Просмотр»)       — видит карточку, артефакты и черновики проекта;
# write  («Редактирование») — то же + правка карточки, гипотез, онтологий
#                             и RAG-баз, привязанных к проекту, загрузка файлов;
# admin  («Полный доступ»)  — то же + публикация артефактов и управление
#                             участниками (выдача/смена/снятие уровней).
# Удаление артефактов на уровнях не выдаётся — прерогатива роли
# «Администратор» (как и раньше).
LEVEL_READ = "read"
LEVEL_WRITE = "write"
LEVEL_ADMIN = "admin"
LEVELS = (LEVEL_READ, LEVEL_WRITE, LEVEL_ADMIN)
LEVEL_RANK = {LEVEL_READ: 0, LEVEL_WRITE: 1, LEVEL_ADMIN: 2}
LEVEL_OWNER = "owner"  # руководитель проекта (владелец карточки), не из acl
LEVEL_LABELS = {
    LEVEL_READ: "Просмотр",
    LEVEL_WRITE: "Редактирование",
    LEVEL_ADMIN: "Полный доступ",
}


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


def clean_acl(raw) -> dict:
    """Нормализует блок acl карточки к {идентификатор участника: уровень}.

    Принимает None или словарь. Идентификатор — 12 hex-символов (формат
    учётных записей), уровень — один из LEVELS; остальное — ProjectError.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProjectError("Блок acl должен быть словарём {идентификатор: уровень}")
    result: dict[str, str] = {}
    for member_id, level in raw.items():
        mid = str(member_id or "").strip()
        if not USER_ID_RE.match(mid):
            raise ProjectError(f"Некорректный идентификатор участника: {mid[:40]}")
        cleaned = _clean_text(level, 20)
        if cleaned not in LEVELS:
            raise ProjectError(
                "Уровень доступа должен быть одним из: " + ", ".join(LEVELS)
            )
        result[mid] = cleaned
    return result


class ProjectError(ValueError):
    """Некорректные данные карточки проекта."""


def is_admin_only(card: dict | None) -> bool:
    """Тестовая карточка (видна на интерфейсе только администраторам).

    Флаг ставится вручную в JSON карточки или через PATCH с полем
    admin_only; названия таких карточек помечаются «ТЕСТОВЫЙ».
    """
    return bool((card or {}).get("admin_only"))


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
        items = [
            card
            for card in (self._read(path) for path in self.root.glob("*/*.json"))
            if card and card.get("id") and card_matches_participant(card, needle_tokens)
        ]
        items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return items

    def list_all(self) -> list:
        """Все карточки всех владельцев, свежие сверху (служебное: выдача
        участникам черновиков проектов и общих артефактов по уровню доступа)."""
        items: list[dict] = []
        if not self.root.is_dir():
            return items
        for path in self.root.glob("*/*.json"):
            card = self._read(path)
            if card and card.get("id"):
                items.append(card)
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
        if "acl" in raw:
            acl = clean_acl(raw.get("acl"))
            if acl:
                data["acl"] = acl
            else:
                data.pop("acl", None)  # пустой acl — поле из карточки убирается
        if "presentation" in raw:
            data["presentation"] = bool(raw.get("presentation"))
        # тестовые карточки (админские): флаг сохраняется только при явной
        # передаче, навыки онбординга его не шлют — обычные карточки не задеть
        if "admin_only" in raw:
            data["admin_only"] = bool(raw.get("admin_only"))

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

    def set_icon(self, project_id: str, svg: str) -> dict | None:
        """Сохраняет сгенерированную SVG-иконку карточки (поле icon_svg).

        В отличие от save(), пишет поле напрямую в JSON карточки любого
        владельца в обход allowlist TEXT_FIELDS: иконка декоративна и общая
        для всех, кто видит карточку в списке «Проекты», генерация не
        привязана к владельцу. None — карточки с таким id нет.
        """
        if not PROJECT_ID_RE.match(project_id or "") or not self.root.is_dir():
            return None
        for path in self.root.glob(f"*/{project_id}.json"):
            card = self._read(path)
            if card and card.get("id") == project_id:
                card["icon_svg"] = svg
                card["icon_svg_generated_at"] = time.time()
                self._write(path, card)
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
        """Карточка для API/UI: без служебного user_id (он и так из токена)
        и без блока acl — уровни участников отдаёт GET .../members тем,
        кто состоит в проекте, а не всем читателям раздела «Проекты»."""
        return {k: v for k, v in card.items() if k not in ("user_id", "acl")}


def _name_tokens(text: str) -> list[str]:
    """Слова ФИО в нижнем регистре (инициалы «И.И.» дают токены «и», «и»)."""
    return [t for t in re.split(r"[^a-zа-яё]+", text.lower()) if t]


def card_matches_participant(card: dict, needle_tokens: list) -> bool:
    """Совпадает ли кто-то из needle_tokens (токены вариантов ФИО) с одной из
    записей participants карточки (та же эвристика, что у find_participant_cards:
    в записи участника нужна фамилия — иначе инициал «И.» матчится на всех)."""
    participants = str((card or {}).get("participants") or "")
    # участники разделяются запятыми, точками с запятой и переносами строк
    for entry in re.split(r"[;,\n\r]+", participants):
        entry_tokens = _name_tokens(entry)
        if len(entry_tokens) < 2 or len(entry_tokens[0]) < 4:
            continue
        if any(_name_matches(entry_tokens, tokens) for tokens in needle_tokens):
            return True
    return False


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


def user_profile(user_id: str) -> dict:
    """Анкета «Кабинета» пользователя (пустой словарь, если не заполнена).

    Ленивый импорт: cabinet — соседний рантайм-модуль; прямая зависимость
    нужна только проверкам доступа (ФИО из анкеты важнее имени учётной
    записи), цикла нет, но и тащить его на верхний уровень не нужно.
    """
    from . import cabinet

    try:
        return cabinet.get_store().get_profile(user_id)
    except cabinet.CabinetError:
        return {}


def access_level(card: dict | None, user: dict, profile: dict | None = None) -> str | None:
    """Эффективный уровень доступа пользователя к карточке проекта.

    owner — руководитель (владелец карточки); read/write/admin — явный
    уровень из блока acl карточки; read также автоматически получают
    зарегистрированные пользователи, чьё ФИО указано в поле participants
    (добавление в состав команды само по себе открывает просмотр артефактов
    и черновиков — явный уровень из acl его перекрывает). None — доступа
    сверх общего нет.
    """
    if not isinstance(card, dict) or not isinstance(user, dict) or not user.get("id"):
        return None
    if card.get("user_id") and card.get("user_id") == user.get("id"):
        return LEVEL_OWNER
    acl = card.get("acl")
    explicit = acl.get(user["id"]) if isinstance(acl, dict) else None
    if explicit in LEVELS:
        return explicit
    if profile is None:
        profile = user_profile(user["id"])
    needles = participant_needles(user, profile)
    needle_tokens = [
        tokens for tokens in (_name_tokens(n) for n in needles)
        if tokens and len(tokens[0]) >= 4
    ]
    if needle_tokens and card_matches_participant(card, needle_tokens):
        return LEVEL_READ
    return None


def level_allows(level: str | None, need: str) -> bool:
    """Покрывает ли уровень доступа требуемый (read ≤ write ≤ admin; owner — всё)."""
    if level == LEVEL_OWNER:
        return True
    rank = LEVEL_RANK.get(level or "")
    return rank is not None and rank >= LEVEL_RANK.get(need, 99)


def artifact_level(project_id: str, user: dict, profile: dict | None = None) -> str | None:
    """Уровень доступа пользователя к проекту, к которому привязан артефакт
    (онтология/RAG-база с полем project_id). None — проект не указан, не
    найден или доступа нет: артефакты без проекта участникам не открываются."""
    if not project_id or not isinstance(user, dict) or not user.get("id"):
        return None
    card = get_store().find(project_id)
    if card is None:
        return None
    return access_level(card, user, profile)


def matched_participant_ids(card: dict, user_needles: list) -> list[str]:
    """Идентификаторы зарегистрированных пользователей, чьи ФИО-варианты
    совпали с одной из записей participants карточки.

    user_needles — список пар (user_id, варианты ФИО), как собирает раздел
    «Моя команда» кабинета; совпадение ищет _name_matches с учётом инициалов.
    """
    matched: list[str] = []
    entries = [
        e for e in re.split(r"[;,\n\r]+", str((card or {}).get("participants") or ""))
        if e.strip()
    ]
    for entry in entries:
        entry_tokens = _name_tokens(entry)
        if len(entry_tokens) < 2 or len(entry_tokens[0]) < 4:
            continue
        for user_id, needles in user_needles or []:
            if user_id in matched:
                continue
            if any(_name_matches(entry_tokens, _name_tokens(n)) for n in needles or []):
                matched.append(user_id)
    return matched


def unmatched_participant_names(card: dict, user_needles: list) -> list[str]:
    """Записи participants, которым не нашлась зарегистрированная учётная
    запись (руководителю показываются в подсказке: доступ откроется после
    регистрации человека либо совпадения ФИО)."""
    names: list[str] = []
    entries = [
        e.strip() for e in re.split(r"[;,\n\r]+", str((card or {}).get("participants") or ""))
        if e.strip()
    ]
    all_needles = [
        _name_tokens(n)
        for _user_id, needles in (user_needles or [])
        for n in (needles or [])
    ]
    for entry in entries:
        entry_tokens = _name_tokens(entry)
        if len(entry_tokens) < 2 or len(entry_tokens[0]) < 4:
            names.append(entry)  # короткая запись без фамилии не матчится никому
            continue
        if not any(_name_matches(entry_tokens, tokens) for tokens in all_needles):
            names.append(entry)
    return names


def set_member_level(project_id: str, member_id: str, level: str) -> dict:
    """Сохраняет явный уровень доступа участника в блоке acl карточки.

    Возвращает обновлённую карточку; ProjectError — проект не найден,
    идентификатор участника или уровень некорректны. Запись acl перекрывает
    автопросмотр по ФИО, удаление артефактов она не даёт (только роль
    «Администратор»).
    """
    if level not in LEVELS:
        raise ProjectError("Уровень доступа должен быть одним из: " + ", ".join(LEVELS))
    store = get_store()
    card = store.find(project_id)
    if card is None:
        raise ProjectError(f"Карточка не найдена: {project_id}")
    if not USER_ID_RE.match(member_id or ""):
        raise ProjectError("Некорректный идентификатор участника")
    acl = clean_acl(card.get("acl"))
    acl[member_id] = level
    return store.save(card["user_id"], {"project_id": project_id, "acl": acl})


def remove_member_level(project_id: str, member_id: str) -> dict:
    """Снимает явный уровень доступа участника (автопросмотр по ФИО остаётся,
    пока человек указан в поле participants карточки). Возвращает карточку."""
    store = get_store()
    card = store.find(project_id)
    if card is None:
        raise ProjectError(f"Карточка не найдена: {project_id}")
    if not USER_ID_RE.match(member_id or ""):
        raise ProjectError("Некорректный идентификатор участника")
    acl = clean_acl(card.get("acl"))
    acl.pop(member_id, None)
    return store.save(card["user_id"], {"project_id": project_id, "acl": acl})


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    """Синглтон хранилища проектов; в тестах сбрасывается cache_clear()."""
    return ProjectStore(get_settings().runtime_dir / "projects")
