"""Хранилище карточек пользовательских агентов (workflow-графов).

Агент — не отдельный процесс, а JSON-карточка: системный промпт + граф
узлов/рёбер (nodes/edges) + тип интерфейса (interface_type), исполняемый
общим workflow_engine.py поверх run_agent_loop. Один файл на карточку,
атомарная запись (temp + replace), upsert по agent_id — тот же паттерн,
что у ProjectStore/RagStore/OntologyStore.

Хранилище: .aios-runtime/agents/{owner_id}/agent-{id}.json (приватное).
Статус active автоматически (и идемпотентно) публикует карточку в общий
каталог data/user_agents.json — та же схема, что у RagStore.publish().
Не путать с data/skills.json — тот каталог навыков-агентов платформы
для страницы agents.html, разные сущности, разные файлы.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from threading import Lock

from . import artifact_icons

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
VALID_STATUSES = (STATUS_DRAFT, STATUS_ACTIVE)

# Шаблоны представлений страницы /agent.html. interface_type — подсказка
# конструктора («основное» представление), но не онтология агента: набор
# представлений вычисляется платформой (compute_views) и расширяется
# явным card["views"] — один агент может иметь несколько представлений.
VIEW_CHAT = "chat"
VIEWS = (
    "chat",
    "catalog",
    "dashboard",
    "validation_panel",
    "document_generator",
    "options_builder",
    "approval_queue",
    "task_orchestrator",
    "background_monitor",
    "benchmark",
)

# Уровни автономности (какие решения агент принимает без человека);
# фоновый планировщик запускает только semi_autonomous/autonomous.
AUTONOMY_LEVELS = ("manual", "assistive", "semi_autonomous", "autonomous")

INTERFACE_TYPES = VIEWS

# template_id — из какого готового шаблона собран агент (scripts/agent_templates.json).
# Нужен загрузчику, чтобы повторный запуск обновлял карточку, а не плодил дубли,
# и галерее — чтобы показать, что участник взял и насколько доработал.
TEXT_FIELDS = ("title", "role_description", "system_prompt", "welcome_message",
               "icon", "backend", "backend_url", "template_id")
LIST_FIELD_LIMITS = {"nodes": 64, "edges": 128, "history": 200, "views": 10, "evals": 50}

MAX_AGENTS_PER_PROJECT = 20


class AgentError(Exception):
    """Ошибка операции с агентом (не найден, некорректная карточка и т.п.)."""


def compute_views(card: dict, records_count: int = 0) -> list[str]:
    """Какие представления страницы агента показывать — решает платформа.

    Правила (по порядку, вывод упорядочен канонически):
      1. chat — всегда;
      2. interface_type из гипотезы — основное представление, если не chat;
      3. catalog — агент копит записи (узел tool.save_record или записи уже есть);
      4. approval_queue — в манифесте есть действия, требующие утверждения;
      5. background_monitor — агент запускается по расписанию;
      6. явный card["views"] добавляет представления поверх вычисленных.
    """
    present: set[str] = {VIEW_CHAT}
    interface = card.get("interface_type") or VIEW_CHAT
    if interface in VIEWS and interface != VIEW_CHAT:
        present.add(interface)
    node_types = {n.get("type") for n in (card.get("nodes") or []) if isinstance(n, dict)}
    if "tool.save_record" in node_types or records_count > 0:
        present.add("catalog")
    autonomy = card.get("autonomy") or {}
    if autonomy.get("requires_approval"):
        present.add("approval_queue")
    if (card.get("trigger") or {}).get("type") in ("schedule", "cron"):
        present.add("background_monitor")
    for v in card.get("views") or []:
        if v in VIEWS:
            present.add(v)
    return [v for v in VIEWS if v in present]


def _normalize_autonomy(raw: dict) -> dict:
    """Манифест автономности: уровень, что разрешено без человека, что требует
    утверждения. Неизвестные поля отбрасываются, значения ограничены."""
    level = str(raw.get("level") or "assistive").strip()
    if level not in AUTONOMY_LEVELS:
        level = "assistive"
    can_raw = raw.get("can") if isinstance(raw.get("can"), dict) else {}
    can = {str(k)[:40]: bool(v) for k, v in list(can_raw.items())[:10]}
    approval = [str(t)[:100] for t in (raw.get("requires_approval") or []) if str(t).strip()][:10]
    return {
        "level": level,
        "can": can,
        "requires_approval": approval,
        "note": str(raw.get("note") or "")[:1000],
    }


def _normalize_evals(raw: list) -> list[dict]:
    """Тестовые случаи для проверки агента (evals).

    Критериев четыре, все необязательные, проверяются вместе:
      expect_contains  — подстроки, каждая должна встретиться в ответе;
      expect_number    — {value, tolerance, unit}: в ответе есть число в
                         пределах допуска (доля от value, по умолчанию 0.1);
      expect_tool      — инструмент, который агент обязан был вызвать;
      expect_component — компонент, которым он обязан был показать результат.

    Для инженерного агента подстрок почти всегда мало: правильный ответ на
    «посчитай площадь» — это число 95, а не слово. Подстроку можно пройти,
    не сделав работу; число с допуском и факт вызова инструмента — нельзя.
    Случай без единого критерия остаётся «на ручную проверку».
    """
    out: list[dict] = []
    for case in (raw or [])[: LIST_FIELD_LIMITS["evals"]]:
        if not isinstance(case, dict):
            continue
        text = str(case.get("input") or "").strip()
        if not text:
            continue
        expects = [str(s)[:300] for s in (case.get("expect_contains") or []) if str(s).strip()][:20]
        item = {
            "input": text[:2000],
            "expect_contains": expects,
            "note": str(case.get("note") or "")[:500],
        }

        number = case.get("expect_number")
        if isinstance(number, dict):
            try:
                value = float(number.get("value"))
            except (TypeError, ValueError):
                value = None
            if value is not None:
                try:
                    tolerance = float(number.get("tolerance"))
                except (TypeError, ValueError):
                    tolerance = 0.1
                item["expect_number"] = {
                    "value": value,
                    "tolerance": min(max(abs(tolerance), 0.001), 1.0),
                    "unit": str(number.get("unit") or "")[:40],
                }

        tool = str(case.get("expect_tool") or "").strip()[:60]
        if tool:
            item["expect_tool"] = tool
        component = str(case.get("expect_component") or "").strip()[:40]
        if component:
            item["expect_component"] = component

        out.append(item)
    return out



# Блоки рабочей страницы агента. Не «шаблоны страниц» — из них платформа
# собирает страницу под конкретного агента. Каталог готовых страниц всегда
# вырождается: половина сделана наполовину и ни одна не подходит точно;
# блоки же переиспользуются, каждый написан один раз.
CONSOLE_BLOCKS = ("params", "dataset", "results", "talk", "queue", "monitor")


def compute_console(card: dict) -> list[str]:
    """Из каких блоков собрать страницу — решает платформа по тому, что
    агент РЕАЛЬНО умеет, а не по объявленному типу.

    Тот же принцип, что у compute_views: тип интерфейса, выбранный при
    сборке, устаревает на первой же доработке агента, а состав инструментов
    и настройки автономности — нет.

      params   — агент объявил пульт (поля параметров и действия);
      dataset  — агент умеет читать таблицы участника;
      results  — всегда: работа агента должна накапливаться, а не уезжать
                 вверх вместе с лентой сообщений;
      talk     — всегда: свободный вопрос нужен даже там, где есть пульт;
      queue    — есть действия, требующие утверждения человеком;
      monitor  — агент работает по расписанию.

    Чистый чат-бот получит ["results", "talk"] и страницу во всю ширину —
    это правильный исход, если участнику нужен именно собеседник.
    """
    blocks = []
    console = card.get("console") or {}
    if console.get("inputs") or console.get("actions"):
        blocks.append("params")

    node_types = {n.get("type") for n in (card.get("nodes") or []) if isinstance(n, dict)}
    if "tool.read_data_file" in node_types:
        blocks.append("dataset")

    blocks.extend(("results", "talk"))

    if (card.get("autonomy") or {}).get("requires_approval"):
        blocks.append("queue")
    if (card.get("trigger") or {}).get("type") in ("schedule", "cron"):
        blocks.append("monitor")
    return [b for b in CONSOLE_BLOCKS if b in blocks]


# Типы полей пульта. Намеренно мало: пульт должен собираться моделью без
# ошибок, а не покрывать все мыслимые виджеты.
CONSOLE_FIELD_TYPES = ("number", "text", "select", "file", "textarea")
MAX_CONSOLE_INPUTS = 12
MAX_CONSOLE_ACTIONS = 5


# --------------------------------------------------------------------------
# Декларативный интерфейс (ui): тема, плотность, вводный текст.
# Не HTML/JS, а ограниченный словарь поверх стандартной страницы /agent.html:
# страница применяет его CSS-переменными и скрытием блоков (agent_page.js).
# Такой декларация безопасна по построению (никакого исполняемого кода из
# карточки) и меняется PATCH /api/agents/{id} без перезапуска сервера.

UI_THEMES = ("default", "forest", "graphite", "light")
UI_DENSITY = ("comfortable", "compact")
UI_ACCENTS = ("green", "gold", "blue", "red", "teal", "graphite")
UI_INTRO_MAX_CHARS = 1200
UI_ACCENT_HEX = {
    "green": ("#7C8B6C", "#5D6C50"),
    "gold": ("#C2A054", "#9C7B33"),
    "blue": ("#6B87A8", "#4A6785"),
    "red": ("#B0715F", "#8E5142"),
    "teal": ("#5F9490", "#3F7470"),
    "graphite": ("#6E7376", "#4A4F52"),
}


def _normalize_ui(raw) -> dict:
    """Приводит ui-декларацию карточки к безопасному виду: значения из
    белых списков, текст обрезан, ничего исполняемого не проходит."""
    if not isinstance(raw, dict):
        return {}
    theme = str(raw.get("theme") or "").strip()
    if theme not in UI_THEMES:
        theme = ""
    density = str(raw.get("density") or "").strip()
    if density not in UI_DENSITY:
        density = ""
    accent = str(raw.get("accent") or "").strip()
    if accent not in UI_ACCENTS:
        accent = ""
    intro = str(raw.get("intro") or "").strip()[:UI_INTRO_MAX_CHARS]
    hide_empty = raw.get("hide_empty_views")
    ui = {}
    if theme:
        ui["theme"] = theme
    if density:
        ui["density"] = density
    if accent:
        ui["accent"] = accent
    if intro:
        ui["intro"] = intro
    if isinstance(hide_empty, bool):
        ui["hide_empty_views"] = hide_empty
    return ui


def _normalize_console(raw: dict) -> dict:
    """Пульт агента: поля параметров и кнопки действий.

    Зачем он нужен. Инженер приходит к агенту с ПАРАМЕТРАМИ — расход,
    температура, доля волокна, — а не с формулировкой. Заставлять его каждый
    раз писать их прозой значит терять время и получать разные формулировки
    одного и того же вопроса. Пульт превращает вопрос в форму: поля с
    единицами и диапазонами плюс кнопка действия.

    Как это работает: кнопка подставляет значения полей в шаблон вопроса
    ({имя_поля}) и отправляет его обычным сообщением. То есть пульт — это
    интерфейс над тем же каналом, а не второй путь исполнения: ничего
    нового в движке, никакого нового класса отказов.

    Пульта нет — страница остаётся чистым чатом. Это правильно для агентов,
    у которых вход действительно свободный (консультант, разбор литературы).
    """
    if not isinstance(raw, dict):
        return {}

    inputs = []
    for item in (raw.get("inputs") or [])[:MAX_CONSOLE_INPUTS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        label = str(item.get("label") or name).strip()[:120]
        if not name or not label:
            continue
        kind = str(item.get("type") or "text").strip()
        if kind not in CONSOLE_FIELD_TYPES:
            kind = "text"
        field = {"name": name, "label": label, "type": kind}
        for key in ("unit", "hint", "default", "placeholder"):
            value = item.get(key)
            if value not in (None, ""):
                field[key] = str(value)[:200]
        if kind == "number":
            for key in ("min", "max", "step"):
                try:
                    if item.get(key) is not None:
                        field[key] = float(item[key])
                except (TypeError, ValueError):
                    pass
        if kind == "select":
            options = [str(o)[:80] for o in (item.get("options") or []) if str(o).strip()][:20]
            if not options:
                continue  # select без вариантов бесполезен
            field["options"] = options
        inputs.append(field)

    actions = []
    for item in (raw.get("actions") or [])[:MAX_CONSOLE_ACTIONS]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:60]
        prompt = str(item.get("prompt") or "").strip()[:2000]
        if not label or not prompt:
            continue
        actions.append({"label": label, "prompt": prompt,
                        "primary": bool(item.get("primary"))})
    if actions and not any(a["primary"] for a in actions):
        actions[0]["primary"] = True

    if not inputs and not actions:
        return {}
    return {"title": str(raw.get("title") or "Параметры")[:80],
            "inputs": inputs, "actions": actions}


class AgentStore:
    """Файловое хранилище карточек агентов, один каталог на пользователя."""

    def __init__(self, root: Path, data_dir: Path, trash_root: Path | None = None) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self.catalog_path = self.data_dir / "user_agents.json"
        # Мягкое удаление: удалённые карточки, данные и логотипы переезжают
        # сюда (вне _root — ни один store и планировщик их не читают) и лежат
        # до ручного разбора; вернуть можно scripts/restore_agent.py.
        self.trash_root = Path(trash_root) if trash_root else self._root.parent / "agents-removed"
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Пути и запись

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _user_dir(self, user_id: str) -> Path:
        d = self._root / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, user_id: str, agent_id: str) -> Path:
        return self._user_dir(user_id) / f"{agent_id}.json"

    # ------------------------------------------------------------------
    # Чтение (приватное хранилище)

    def get(self, user_id: str, agent_id: str) -> dict | None:
        path = self._path(user_id, agent_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def find_any_owner(self, agent_id: str) -> dict | None:
        """Ищет карточку по id без знания владельца — нужен для открытия
        /agent.html?id=... участником, не обязательно владельцем."""
        if not agent_id or "/" in agent_id or ".." in agent_id:
            return None
        for user_dir in self._root.iterdir():
            if not user_dir.is_dir():
                continue
            candidate = user_dir / f"{agent_id}.json"
            if candidate.is_file():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def list(self, user_id: str, project_id: str | None = None) -> list[dict]:
        out = []
        for path in sorted(self._user_dir(user_id).glob("agt-*.json")):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if project_id and card.get("project_id") != project_id:
                continue
            out.append(card)
        return out

    def list_all(self) -> list[dict]:
        """Все агенты всех пользователей, свежие сверху — для расшаривания
        по проекту (см. api/routers/agents.py: GET /api/agents для
        участников проекта, по образцу RagStore.list_all)."""
        out: list[dict] = []
        if not self._root.is_dir():
            return out
        for user_dir in self._root.iterdir():
            if not user_dir.is_dir():
                continue
            for path in sorted(user_dir.glob("agt-*.json")):
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        out.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        return out

    def summary(self, card: dict) -> dict:
        return {
            "id": card.get("id"),
            "project_id": card.get("project_id"),
            "title": card.get("title"),
            "role_description": card.get("role_description"),
            "interface_type": card.get("interface_type", "chat"),
            "views": compute_views(card),
            "status": card.get("status"),
            "version": card.get("version"),
            "visibility": card.get("visibility", "private"),
            "autonomy_level": (card.get("autonomy") or {}).get("level", "assistive"),
            "trigger_type": (card.get("trigger") or {}).get("type", "chat"),
            "last_run_at": card.get("last_run_at"),
            "updated_at": card.get("updated_at"),
        }

    def public_card(self, card: dict) -> dict:
        """Карточка для API/UI: без служебного owner_id (он и так из токена)."""
        return {k: v for k, v in card.items() if k not in ("owner_id", "_internal")}

    # ------------------------------------------------------------------
    # Публичный каталог (data/user_agents.json)

    def _load_catalog(self) -> dict:
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": []}
        return catalog if isinstance(catalog, dict) else {"items": []}

    def catalog_items(self) -> list:
        """Опубликованные карточки из каталога data/user_agents.json."""
        items = self._load_catalog().get("items")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def _project_title(self, project_id: str) -> str:
        if not project_id:
            return ""
        try:
            from .projects import get_store as get_project_store

            card = get_project_store().find(project_id)
        except Exception:  # noqa: BLE001 — каталог проектов недоступен, публикуем без названия
            return ""
        return (card or {}).get("title") or ""

    def catalog_entry(self, card: dict) -> dict:
        """Карточка опубликованного агента для data/user_agents.json.

        Открытый доступ на чтение: карточка не несёт признака владельца как
        ограничения — owner/project только информационные, как у RagStore.
        image_url — сгенерированный логотип агента (artifact_icons), если
        файл уже создан; иначе привычная SVG-заглушка ассетов.
        """
        entry = {
            "id": card["id"],
            "title": card.get("title") or "Агент без названия",
            "role_description": card.get("role_description", ""),
            "interface_type": card.get("interface_type", "chat"),
            "project": self._project_title(card.get("project_id", "")),
            "project_id": card.get("project_id", ""),
            "status": "Активен",
            "visibility": card.get("visibility", "private"),
            "image_url": artifact_icons.logo_url(card["id"])
            or "assets/icons/agent/default.svg",
        }
        if card.get("admin_only"):
            entry["admin_only"] = True
        return entry

    def publish(self, card: dict) -> dict:
        """Выкладывает карточку в общий каталог; идемпотентно."""
        if card.get("status") != STATUS_ACTIVE:
            raise AgentError("Публиковать можно только агента в статусе active")
        entry = self.catalog_entry(card)
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == entry["id"])
        ]
        items.append(entry)
        self._write(self.catalog_path, {"items": items})
        return entry

    def _unpublish(self, agent_id: str) -> None:
        """Убирает агента из каталога (если был) — например, при возврате в draft."""
        items = [
            item
            for item in (self._load_catalog().get("items") or [])
            if not (isinstance(item, dict) and item.get("id") == agent_id)
        ]
        self._write(self.catalog_path, {"items": items})

    # ------------------------------------------------------------------
    # Запись (приватная карточка + идемпотентная публикация)

    def save(self, user_id: str, card: dict) -> dict:
        if not isinstance(card, dict):
            raise AgentError("card должен быть объектом")

        with self._lock:
            agent_id = str(card.get("agent_id") or card.get("id") or "").strip()
            existing = self.get(user_id, agent_id) if agent_id else None

            if existing is None:
                if not card.get("project_id"):
                    raise AgentError("Для новой карточки агента обязателен project_id")
                count = len(self.list(user_id, project_id=card["project_id"]))
                if count >= MAX_AGENTS_PER_PROJECT:
                    raise AgentError(
                        f"Лимит агентов на проект достигнут ({MAX_AGENTS_PER_PROJECT})"
                    )
                agent_id = agent_id or f"agt-{uuid.uuid4().hex[:10]}"
                merged: dict = {
                    "id": agent_id,
                    "owner_id": user_id,
                    "project_id": card["project_id"],
                    "status": STATUS_DRAFT,
                    "version": 0,
                    "visibility": "private",
                    "interface_type": "chat",
                    "history": [],
                    "created_at": time.time(),
                }
            else:
                merged = dict(existing)
                hist = list(existing.get("history") or [])
                hist.append(
                    {
                        "version": existing.get("version", 1),
                        "snapshot_at": existing.get("updated_at"),
                        "title": existing.get("title"),
                        "status": existing.get("status"),
                    }
                )
                merged["history"] = hist[-LIST_FIELD_LIMITS["history"]:]

            for key in TEXT_FIELDS:
                if key in card:
                    merged[key] = str(card[key])[:20_000]

            for key in ("nodes", "edges"):
                if key in card:
                    value = card[key]
                    if not isinstance(value, list):
                        raise AgentError(f"{key} должен быть списком")
                    merged[key] = value[: LIST_FIELD_LIMITS[key]]

            if "trigger" in card and isinstance(card["trigger"], dict):
                raw_trigger = dict(card["trigger"])
                trigger = {"type": str(raw_trigger.get("type") or "chat").strip()[:40]}
                if "interval_minutes" in raw_trigger:
                    try:
                        trigger["interval_minutes"] = max(0, int(raw_trigger["interval_minutes"]))
                    except (TypeError, ValueError):
                        pass
                # расписание с точностью до дня и часа (панель «Расписание и
                # уведомления» на /agent.html): weekdays — номера дней недели
                # ISO (1=понедельник … 7=воскресенье), time — «ЧЧ:ММ» по
                # местному времени сервера. Планировщик смотрит interval_minutes
                # как на базовый ритм; weekdays/time только сужают его.
                days = raw_trigger.get("weekdays")
                if isinstance(days, list):
                    clean_days = []
                    for d in days:
                        try:
                            day = int(d)
                        except (TypeError, ValueError):
                            continue
                        if 1 <= day <= 7 and day not in clean_days:
                            clean_days.append(day)
                    if clean_days:
                        trigger["weekdays"] = sorted(clean_days)[:7]
                time_str = str(raw_trigger.get("time") or "").strip()
                match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", time_str)
                if match:
                    trigger["time"] = f"{int(match.group(1)):02d}:{match.group(2)}"
                if "prompt" in raw_trigger:
                    trigger["prompt"] = str(raw_trigger["prompt"] or "")[:2000]
                merged["trigger"] = trigger
            merged.setdefault("trigger", {"type": "chat"})

            if card.get("interface_type") in INTERFACE_TYPES:
                merged["interface_type"] = card["interface_type"]

            if "autonomy" in card and isinstance(card["autonomy"], dict):
                merged["autonomy"] = _normalize_autonomy(card["autonomy"])
            merged.setdefault("autonomy", {"level": "assistive", "can": {}, "requires_approval": [], "note": ""})

            if "views" in card and isinstance(card["views"], list):
                merged["views"] = [v for v in card["views"] if v in VIEWS][: LIST_FIELD_LIMITS["views"]]

            if "evals" in card and isinstance(card["evals"], list):
                merged["evals"] = _normalize_evals(card["evals"])

            if "console" in card:
                merged["console"] = _normalize_console(card["console"])

            if "ui" in card:
                merged["ui"] = _normalize_ui(card["ui"])
            # пустая декларация (после нормализации) не должна оставаться
            # в карточке как {"ui": {}} — старая правка не затирается новой
            if isinstance(merged.get("ui"), dict) and not merged["ui"]:
                merged.pop("ui", None)

            if "backend" in card and str(card["backend"]) in ("", "config", "custom"):
                merged["backend"] = str(card["backend"])

            if card.get("visibility") in ("private", "project", "registry"):
                merged["visibility"] = card["visibility"]

            status = card.get("status")
            if status in VALID_STATUSES and status != merged.get("status"):
                merged["status"] = status
                merged["status_changed_at"] = time.time()

            merged["version"] = int(merged.get("version") or 0) + 1
            merged["updated_at"] = time.time()

            path = self._path(user_id, agent_id)
            self._write(path, merged)

            if merged["status"] == STATUS_ACTIVE:
                self.publish(merged)

            if merged.get("project_id"):
                from .projects import bump_artifact_status

                bump_artifact_status(
                    merged["project_id"], "agents",
                    3 if merged["status"] == STATUS_ACTIVE else 2,
                )

            return merged

    def mark_schedule_run(self, user_id: str, agent_id: str, ts: float, status: str) -> None:
        """Отметка фонового запуска по расписанию: last_run_at/last_run_status.

        Внутренняя служебная правка карточки планировщиком — без bump версии
        и без записи в history (версия меняет только конструктор/владелец)."""
        with self._lock:
            card = self.get(user_id, agent_id)
            if card is None:
                return
            card["last_run_at"] = ts
            card["last_run_status"] = str(status)[:40]
            self._write(self._path(user_id, agent_id), card)

    def set_icon(self, user_id: str, agent_id: str, svg: str) -> bool:
        """Сгенерированный логотип агента (inline-SVG для /agent.html).

        Служебная правка артефакт-иконок (artifact_icons), как mark_schedule_run:
        без bump версии и history — логотип декоративная обложка, а не правка
        владельца; ручной icon из конструктора перезаписывает её обычным save().
        """
        with self._lock:
            card = self.get(user_id, agent_id)
            if card is None:
                return False
            card["icon"] = str(svg)[:20_000]
            card["icon_generated_at"] = time.time()
            self._write(self._path(user_id, agent_id), card)
            return True

    def delete(self, user_id: str, agent_id: str, *, deleted_by: str | None = None) -> bool:
        """Удаление агента — мягкое: ничего не стирается физически.

        Карточка, каталог накопленных данных (записи records/, прогоны evals/,
        трассы executions/) и сгенерированный логотип переезжают в закрытое
        хранилище .aios-runtime/agents-removed/{owner}/, публикация снимается
        из data/user_agents.json. Карточка в архиве помечается deleted_at/
        deleted_by. Архив лежит вне _root — ни get/list/find_any_owner, ни
        планировщик его не читают, все API отвечают 404. Вернуть агента может
        администратор через scripts/restore_agent.py. Возвращает False, если
        агента нет. Право на удаление проверяет роутер (владелец или
        «Администратор»).
        """
        with self._lock:
            path = self._path(user_id, agent_id)
            if not path.is_file():
                return False
            card = self.get(user_id, agent_id) or {}
            card["deleted_at"] = time.time()
            card["deleted_by"] = str(deleted_by or user_id)[:80]

            trash_dir = self.trash_root / user_id
            trash_dir.mkdir(parents=True, exist_ok=True)
            self._write(trash_dir / f"{agent_id}.json", card)
            path.unlink()

            data_dir = self._user_dir(user_id) / agent_id
            if data_dir.is_dir():
                self._move_to(data_dir, trash_dir / agent_id)

            # сгенерированный логотип (data/icons/{id}.svg) — тоже в архив
            icon = self.data_dir / "icons" / f"{agent_id}.svg"
            if icon.is_file():
                self._move_to(icon, trash_dir / "icons" / f"{agent_id}.svg")

            self._unpublish(agent_id)
            return True

    @staticmethod
    def _move_to(src: Path, dest: Path) -> None:
        """Переезд файла/каталога в архив; коллизия с прошлым архивом того же
        id разрешается суффиксом времени (повторное удаление после
        восстановления не должно затирать старый архив)."""
        if dest.exists():
            dest = dest.with_name(f"{dest.name}-{int(time.time())}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


class AgentRecordStore:
    """Накопленные предметные записи агентов (не техническая трассировка
    ExecutionLog из workflow_engine.py, а то, что агент явно сохранил как
    результат работы — найденный аналог, извлечённые данные и т.п.).

    .aios-runtime/agents/{owner_id}/{agent_id}/records/{record_id}.json
    Нужен интерфейсам типа catalog/dashboard/benchmark на /agent.html,
    чтобы показывать реально накопленный список, а не пересказ чата.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _dir(self, owner_id: str, agent_id: str) -> Path:
        d = self._root / owner_id / agent_id / "records"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def add(self, owner_id: str, agent_id: str, fields: dict, source: str = "") -> dict:
        record_id = f"rec-{uuid.uuid4().hex[:10]}"
        data = {
            "id": record_id,
            "agent_id": agent_id,
            "created_at": time.time(),
            "source": str(source or "")[:500],
            "status": "",
            "fields": {
                str(k)[:100]: str(v)[:2000] for k, v in list((fields or {}).items())[:20]
            },
        }
        path = self._dir(owner_id, agent_id) / f"{record_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return data

    def _path(self, owner_id: str, agent_id: str, record_id: str) -> Path:
        return self._dir(owner_id, agent_id) / f"{record_id}.json"

    def get(self, owner_id: str, agent_id: str, record_id: str) -> dict | None:
        if not record_id or "/" in record_id or ".." in record_id:
            return None
        path = self._path(owner_id, agent_id, record_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set_status(self, owner_id: str, agent_id: str, record_id: str, status: str) -> dict | None:
        """Смена статуса записи (очередь утверждений: approved/rejected,
        consumed — запрос использован агентом). None — записи нет."""
        record = self.get(owner_id, agent_id, record_id)
        if record is None:
            return None
        record["status"] = str(status)[:40]
        record["status_changed_at"] = time.time()
        self._write(self._path(owner_id, agent_id, record_id), record)
        return record

    def list(self, owner_id: str, agent_id: str, limit: int = 200, status: str | None = None) -> list[dict]:
        out = []
        directory = self._dir(owner_id, agent_id)
        for path in sorted(directory.glob("rec-*.json"), reverse=True)[:limit]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if status is not None and record.get("status") != status:
                continue
            out.append(record)
        return out


_RECORD_STORE: AgentRecordStore | None = None


def get_record_store() -> AgentRecordStore:
    global _RECORD_STORE
    if _RECORD_STORE is None:
        from .config import get_settings

        _RECORD_STORE = AgentRecordStore(get_settings().runtime_dir / "agents")
    return _RECORD_STORE


class AgentEvalStore:
    """История прогонов тестовых случаев (evals) агента.

    .aios-runtime/agents/{owner_id}/{agent_id}/evals/eval-{run_id}.json
    Один файл на прогон: какие случаи, что ответил агент, прошёл/не прошёл
    (или «на ручную проверку» — случай без expect_contains). Хранит историю:
    отвечает на вопрос «работает ли новая версия агента лучше прежней».
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _dir(self, owner_id: str, agent_id: str) -> Path:
        d = self._root / owner_id / agent_id / "evals"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_run(self, owner_id: str, agent_id: str, run: dict) -> dict:
        run_id = f"eval-{uuid.uuid4().hex[:10]}"
        data = {"id": run_id, "agent_id": agent_id, "started_at": time.time(), **run}
        path = self._dir(owner_id, agent_id) / f"{run_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return data

    def list(self, owner_id: str, agent_id: str, limit: int = 20) -> list[dict]:
        out = []
        directory = self._dir(owner_id, agent_id)
        for path in sorted(directory.glob("eval-*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out


_EVAL_STORE: AgentEvalStore | None = None


def get_eval_store() -> AgentEvalStore:
    global _EVAL_STORE
    if _EVAL_STORE is None:
        from .config import get_settings

        _EVAL_STORE = AgentEvalStore(get_settings().runtime_dir / "agents")
    return _EVAL_STORE


_STORE: AgentStore | None = None


def get_store() -> AgentStore:
    global _STORE
    if _STORE is None:
        from .config import get_settings

        settings = get_settings()
        _STORE = AgentStore(settings.runtime_dir / "agents", settings.agent_data_dir)
    return _STORE
