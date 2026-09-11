"""Напоминания по шагам пайплайна проекта (api/runtime/reminders.py).

Пайплайн онбординг → гипотезы → онтология → RAG (и зарезервированные на
будущее модели → агенты) отражается участнику как список активных
напоминаний «вам нужно сделать X» — до тех пор, пока шаг не выполнен.

Источник конфигурации — .aios-runtime/reminders_config.json (тот же паттерн
хранения, что у .aios-runtime/llm.json): единый файл, читается/пишется
целиком, правит его администратор через /api/admin/reminders.

Источник истины по выполненности шага — существующие хранилища, без нового
параллельного механизма:
    onboarding  — карточка проекта пользователя в статусе active
                  (projects.STATUS_ACTIVE);
    hypotheses  — hypotheses.hypotheses_done(project_id);
    ontology    — project["artifacts"]["rag"]["status"] >= 2
                  (bump_artifact_status зовётся из ontologies.save() при
                  каждом сохранении онтологии, привязанной к проекту);
    rag         — project["artifacts"]["rag"]["status"] >= 3
                  (bump_artifact_status зовётся из rag.py только при
                  публикации RAG-базы, статус active).
models/agents зарезервированы (enabled_global=False по умолчанию) — своей
детекции выполненности пока нет, добавляется по мере готовности модулей.

Персональный оверрайд администратора (reminder_overrides в карточке
пользователя, users.py) действует только на самого администратора и не
зависит от enabled_global и от реального прогресса — чисто тестовый
инструмент, "on" показывает шаг всегда, "off" всегда скрывает, отсутствие
ключа/None — обычное поведение (глобальный тумблер + статус выполнения).
"""

import json
import time
from pathlib import Path

from .config import get_settings
from .projects import STATUS_ACTIVE as PROJECT_ACTIVE
from .hypotheses import hypotheses_done
from .users import ROLE_ADMIN

# Шаги пайплайна по порядку показа в режиме "sequential". Новые шаги
# добавляются в конец списка с уникальным id и enabled_global=False —
# включаются администратором, когда появится своя детекция выполненности
# в _STEP_CHECKS ниже.
STEP_IDS = ("onboarding", "hypotheses", "ontology", "rag", "models", "agents", "polygon", "social")

DEFAULT_LABELS = {
    "onboarding": "Пройдите онбординг проекта",
    "hypotheses": "Сформулируйте гипотезы",
    "ontology": "Постройте онтологию",
    "rag": "Соберите RAG-базу",
    "models": "Настройте модели",
    "agents": "Соберите агентов",
    "polygon": "Настройте виртуальный полигон",
    "social": "Подключите агента в социальную сеть агентов",
}

# Шаги, у которых уже есть детекция выполненности (см. _STEP_CHECKS) и
# которые поэтому можно безопасно включать администратору прямо сейчас.
DETECTABLE_STEPS = ("onboarding", "hypotheses", "ontology", "rag")

OVERRIDE_VALUES = ("on", "off")  # None/отсутствие ключа — обычное поведение


class ReminderError(ValueError):
    """Некорректные данные конфигурации напоминаний."""


def _config_path() -> Path:
    return get_settings().runtime_dir / "reminders_config.json"


def _default_config() -> dict:
    return {
        "steps": {
            step_id: {
                "label": DEFAULT_LABELS[step_id],
                "enabled_global": step_id in DETECTABLE_STEPS,
            }
            for step_id in STEP_IDS
        },
        "updated_at": time.time(),
    }


def load_config() -> dict:
    """Текущая конфигурация напоминаний; создаёт файл с дефолтами, если его нет."""
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        data = _default_config()
        save_config(data)
        return data
    # мягкая миграция: новый шаг в STEP_IDS, которого нет в сохранённом
    # файле (например, после обновления кода) — добавляем с дефолтом,
    # существующие настройки администратора не трогаем
    changed = False
    for step_id in STEP_IDS:
        if step_id not in data["steps"]:
            data["steps"][step_id] = {
                "label": DEFAULT_LABELS[step_id],
                "enabled_global": step_id in DETECTABLE_STEPS,
            }
            changed = True
    if changed:
        save_config(data)
    return data


def save_config(data: dict) -> dict:
    """Полная перезапись конфигурации (upsert по всему файлу, не по полям)."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return data


def update_config(*, steps: dict | None = None) -> dict:
    """Частичное обновление (для PUT /api/admin/reminders).

    steps — {step_id: {"enabled_global": bool}}; только переданные шаги и
    только переданные внутри них поля меняются, остальное сохраняется.
    """
    data = load_config()
    if steps:
        for step_id, patch in steps.items():
            if step_id not in STEP_IDS:
                raise ReminderError(f"Неизвестный шаг: {step_id}")
            if not isinstance(patch, dict):
                continue
            current = data["steps"].setdefault(
                step_id,
                {"label": DEFAULT_LABELS[step_id], "enabled_global": False},
            )
            if "enabled_global" in patch:
                current["enabled_global"] = bool(patch["enabled_global"])
    return save_config(data)


# ---------------------------------------------------------------------------
# Детекция выполненности шага
# ---------------------------------------------------------------------------


def _rag_artifact_status(project: dict | None) -> int:
    if not project:
        return 0
    artifacts = project.get("artifacts") or {}
    rag = artifacts.get("rag") or {}
    status = rag.get("status")
    return status if isinstance(status, int) else 0


def _onboarding_done(project: dict | None) -> bool:
    return bool(project) and project.get("status") == PROJECT_ACTIVE


def _hypotheses_done(project: dict | None) -> bool:
    if not project:
        return False
    return hypotheses_done(project.get("id") or "")


def _ontology_done(project: dict | None) -> bool:
    return _rag_artifact_status(project) >= 2


def _rag_done(project: dict | None) -> bool:
    return _rag_artifact_status(project) >= 3


# id шага → функция(project_or_None) -> bool. Шаги без записи здесь (models,
# agents) считаются невыполненными всегда, пока своя детекция не добавлена.
_STEP_CHECKS = {
    "onboarding": _onboarding_done,
    "hypotheses": _hypotheses_done,
    "ontology": _ontology_done,
    "rag": _rag_done,
}


def is_step_done(step_id: str, project: dict | None) -> bool:
    check = _STEP_CHECKS.get(step_id)
    if check is None:
        return False
    return check(project)


# ---------------------------------------------------------------------------
# Вычисление активных напоминаний для пользователя
# ---------------------------------------------------------------------------


def latest_active_project(project_store, user_id: str) -> dict | None:
    """Самая свежая карточка пользователя в статусе active, иначе None.

    ProjectStore.list() уже отсортирован по updated_at убыв. — берём первую
    активную. Нет активной карточки (только черновики или их нет вовсе) —
    пользователь ещё не прошёл онбординг, все шаги дальше по цепочке тоже
    считаются невыполненными.
    """
    for card in project_store.list(user_id):
        if card.get("status") == PROJECT_ACTIVE:
            return card
    return None


def compute_reminders(user: dict, project: dict | None, config: dict | None = None) -> list[dict]:
    """Список активных напоминаний для пользователя: [{"id", "label"}, ...].

    Порядок: по STEP_IDS (совпадает с порядком пайплайна). В режиме
    "sequential" — не более одного, ближайший невыполненный шаг по порядку,
    кроме шагов с персональным оверрайдом "on" у администратора (они
    показываются всегда, вне очереди — тестовый инструмент). В режиме
    "all" — все невыполненные (с учётом тех же правил enabled/override).
    """
    cfg = config if config is not None else load_config()
    is_admin = user.get("role") == ROLE_ADMIN
    overrides = (user.get("reminder_overrides") or {}) if is_admin else {}

    forced_on: list[str] = []
    forced_off: set[str] = set()
    for step_id in STEP_IDS:
        value = overrides.get(step_id)
        if value == "on":
            forced_on.append(step_id)
        elif value == "off":
            forced_off.add(step_id)

    # обычные (не форсированные) невыполненные шаги, разрешённые глобально
    normal_pending: list[str] = []
    for step_id in STEP_IDS:
        if step_id in forced_off or step_id in forced_on:
            continue  # форсированные шаги обрабатываются отдельно ниже
        step_cfg = cfg["steps"].get(step_id, {})
        if not step_cfg.get("enabled_global"):
            continue
        if is_step_done(step_id, project):
            continue
        normal_pending.append(step_id)

    normal_pending = normal_pending[:1]  # всегда один ближайший шаг по очереди

    # итоговый список: обычные (с учётом режима) + форсированные "on" поверх,
    # без дублей, в порядке STEP_IDS
    result_ids = [s for s in STEP_IDS if s in normal_pending or s in forced_on]

    steps_cfg = cfg.get("steps", {})
    return [
        {
            "id": step_id,
            "label": steps_cfg.get(step_id, {}).get("label") or DEFAULT_LABELS[step_id],
        }
        for step_id in result_ids
    ]
