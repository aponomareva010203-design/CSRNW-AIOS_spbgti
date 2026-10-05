#!/usr/bin/env python3
"""Справка по пилотным проектам участников («Статус пилотного проекта»).

Собирает с платформы активные карточки проектов (aios-runtime/projects/,
статус active — те же, что показывает таблица пилота) и для каждой: название,
описание, цель, руководителя (анкета «Кабинета» владельца, иначе имя учётной
записи), участников с подсчётом, статусы артефактов и гипотезы.

Статусы и гипотезы артефактов считаются тем же кодом, что и таблица на
странице пилота (api/routers/pilot.py: pilot_row + artifact_floors): тексты
«не начатых» артефактов дополняются из опубликованной карточки гипотез
(data/hypotheses/hyp-{id}.json), статусы сверяются с фактически созданными
артефактами (публикация RAG-базы/агента даёт «завершено»). Полные гипотезы
берутся из карточки гипотез проекта независимо от публикации.

Полные формулировки гипотез берутся из карточек гипотез (data/hypotheses/):
файл может лежать под каноническим именем hyp-{id}.json или под одним из
старых вариантов (hyp-{id без «prj-»}.json, {id}-hypotheses.json, {id}.json,
hyp-cir-biopolymers.json и т.п.) — выбирается опубликованная карточка, иначе
самая наполненная из подходящих по полю project_id.

Тестовые проекты администраторов (владелец — Аксёнов или Беляков, см.
EXCLUDED_OWNER_IDS) в справку не попадают: это служебные карточки проверки
платформы, а не проекты участников.

Выход: reports/pilot-status-<дата>.md (человекочитаемая справка) и
reports/pilot-status-<дата>.json (те же данные структурой). Каталог reports/
создаётся; в data/ справку класть нельзя — она содержит персональные данные
участников, а data/ раздаётся без авторизации.

Запуск на сервере:  .venv/bin/python scripts/export_pilot_status.py
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.runtime import cabinet, hypotheses, projects  # noqa: E402
from api.routers import pilot  # noqa: E402
from api.routers.auth import get_user_store  # noqa: E402

# подписи колонок таблицы пилота — как на странице (frontend/js/pilot.js)
ARTIFACT_LABELS = {
    "rag": "RAG и онтология",
    "agents": "Агенты",
    "models": "Модели",
    "polygon": "Виртуальный полигон",
    "social": "Социальная сеть агентов",
}

STATUS_LABELS = {
    0: "не начато",
    1: "гипотеза сформулирована",
    2: "в процессе создания",
    3: "завершено",
}

# разделы карточки гипотез → человекочитаемые названия
SECTION_LABELS = {
    "ontology": "Онтология",
    "rag": "RAG-база",
    "agents": "Агенты",
    "models": "Модели",
    "polygons": "Полигоны",
    "dashboards": "Дашборды",
}

PARTICIPANTS_SPLIT = re.compile(r"[;,\n\r]+")

# владельцы-администраторы, чьи карточки — тестовые проекты платформы,
# в справку по проектам участников не включаются (по указанию заказчика)
EXCLUDED_OWNER_IDS = {
    "12e06f81075c",  # Аксёнов (администратор): ТЕСТОВЫЙ проект + черновики
    "c54f31609203",  # Беляков (администратор)
}


def _md(text) -> str:
    """Текст для ячейки markdown-таблицы (вертикальная черта экранируется)."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


# суффиксы файлов-черновиков гипотез по артефактам (hyp-{id}-agt1.json …) → раздел
DRAFT_SUFFIXES = {
    "ont": "ontology",
    "rag": "rag",
    "agt": "agents",
    "mdl": "models",
    "plg": "polygons",
    "dsh": "dashboards",
}


def load_hyp_card(project_id: str) -> dict:
    """Карточка гипотез проекта независимо от имени файла.

    Каноническое имя — hyp-{project_id}.json (load_hypotheses), но навык
    «Гипотезы проекта» часть карточек сохранил под старыми вариантами
    (hyp-{id без «prj-»}.json, {id}-hypotheses.json, {id}.json, имена по
    теме типа hyp-cir-biopolymers.json) и в легаси-каталоге
    data/projects/{id}/hypotheses.json. Перебираем все файлы с подходящим
    полем project_id, оставляя только основные карточки (есть списки
    разделов SECTION_KEYS — у черновиков отдельных артефактов вместо них
    «records»). Приоритет: опубликованная → самая наполненная → самая
    свежая.
    """
    candidates: list[Path] = list(hypotheses.hypotheses_dir().glob("*.json"))
    legacy = ROOT / "data" / "projects" / project_id / "hypotheses.json"
    if legacy.is_file():
        candidates.append(legacy)
    best, best_key = None, None
    for path in candidates:
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(card, dict) or card.get("project_id") != project_id:
            continue
        if not any(isinstance(card.get(s), list) for s in hypotheses.SECTION_KEYS):
            continue
        entries = sum(
            len(card.get(s) or []) for s in hypotheses.SECTION_KEYS
            if isinstance(card.get(s), list)
        )
        key = (
            1 if card.get("published") else 0,
            entries,
            path.stat().st_mtime,
        )
        if best_key is None or key > best_key:
            best, best_key = card, key
    return best or {}


def load_hyp_drafts(project_id: str, covered: set) -> dict:
    """Черновики гипотез отдельных артефактов для разделов без основной карточки.

    Навык «Гипотезы проекта» сохраняет утверждённые гипотезы и поштучно
    (hyp-{id}-agt1.json, hyp-{id}-mdl1.json, …, поле records), а сводный файл
    собирает в финале — если сессия не дошла до финала, утверждённые записи
    существуют только в черновиках. Разделы, уже покрытые основной карточкой,
    не трогаем (там черновики устарели).
    """
    directory = hypotheses.hypotheses_dir()
    out: dict[str, list] = {}
    for path in sorted(directory.glob(f"hyp-{project_id}-*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records = card.get("records") if isinstance(card, dict) else None
        if not records:
            continue
        section = card.get("section")
        if section not in hypotheses.SECTION_KEYS:
            match = re.match(r"[a-z]+", path.name[len(f"hyp-{project_id}-"):])
            section = DRAFT_SUFFIXES.get(match.group(0)) if match else None
        if section not in hypotheses.SECTION_KEYS or section in covered:
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            item = {
                "title": str(rec.get("title") or "").strip(),
                "statement": str(rec.get("statement") or "").strip(),
                "draft": True,
            }
            if not item["title"] and not item["statement"]:
                continue
            out.setdefault(section, []).append(item)
    return out


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def split_participants(text) -> list:
    return [e.strip() for e in PARTICIPANTS_SPLIT.split(str(text or "")) if e.strip()]


def leader_name(user_id) -> str:
    """ФИО руководителя: анкета кабинета → имя учётной записи → логин."""
    name = pilot.lead_name(user_id)
    if name:
        return name
    user = get_user_store().get(user_id or "")
    if not user:
        return user_id or ""
    return user.get("name") or user.get("login") or user_id


def collect_card(card, floors) -> dict:
    project_id = card.get("id") or ""
    artifacts = pilot.pilot_row(card, floors)["artifacts"]
    participants = split_participants(card.get("participants"))
    hyp_card = load_hyp_card(project_id)

    hyp_sections: dict[str, list] = {}
    for section in hypotheses.SECTION_KEYS:
        entries = []
        for entry in hyp_card.get(section) or []:
            if not isinstance(entry, dict):
                continue
            item = {
                "title": str(entry.get("title") or "").strip(),
                "statement": str(entry.get("statement") or "").strip(),
            }
            if entry.get("skipped"):
                item["skipped"] = True
                item["skip_reason"] = str(entry.get("skip_reason") or "").strip()
            note = str(entry.get("note") or "").strip()
            if note:
                item["note"] = note
            if not any((item["title"], item["statement"], item.get("skip_reason"), item.get("note"))):
                continue
            entries.append(item)
        if entries:
            hyp_sections[section] = entries
    # разделы без основной карточки дополняются утверждёнными черновиками
    hyp_sections.update(load_hyp_drafts(project_id, set(hyp_sections)))

    return {
        "id": project_id,
        "title": card.get("title") or "Без названия",
        "status": card.get("status"),
        "admin_only": projects.is_admin_only(card),
        "lead": leader_name(card.get("user_id")),
        "created": _fmt_ts(card.get("created_at")),
        "updated": _fmt_ts(card.get("updated_at")),
        "description": str(card.get("description") or "").strip(),
        "goal": str(card.get("goal") or "").strip(),
        "departments": str(card.get("departments") or "").strip(),
        "team_size": str(card.get("team_size") or "").strip(),
        "budget": str(card.get("budget") or "").strip(),
        "groundwork": str(card.get("groundwork") or "").strip(),
        "participants": participants,
        "participants_count": len(participants),
        "artifacts": artifacts,
        "hypotheses_published": bool(hyp_card.get("published")),
        "hypotheses": hyp_sections,
    }


def is_excluded(card) -> bool:
    """Тестовая карточка администратора — в справку не включается."""
    return card.get("user_id") in EXCLUDED_OWNER_IDS or projects.is_admin_only(card)


def collect() -> dict:
    store = projects.get_store()
    floors = pilot.artifact_floors()
    active = [collect_card(c, floors) for c in store.list_active() if not is_excluded(c)]
    drafts = [
        {
            "id": c.get("id") or "",
            "title": c.get("title") or "(без названия)",
            "lead": leader_name(c.get("user_id")),
            "updated": _fmt_ts(c.get("updated_at")),
            "has_participants": bool(split_participants(c.get("participants"))),
        }
        for c in store.list_all()
        if c.get("status") != projects.STATUS_ACTIVE and not is_excluded(c)
    ]
    return {
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "active": active,
        "drafts": drafts,
    }


def render_markdown(data: dict) -> str:
    real = data["active"]
    out = [
        "# Справка по пилотным проектам участников",
        "",
        f"*Сформировано {data['generated_at']} из карточек проектов платформы "
        f"(раздел «Проекты», статус «подтверждён»; источники: таблица «Статус "
        f"пилотного проекта» + карточки гипотез). Подтверждённых проектов: "
        f"{len(real)}, черновиков онбординга — {len(data['drafts'])}. "
        f"Тестовые проекты администраторов (Аксёнов, Беляков) исключены.*",
        "",
        "## Сводная таблица",
        "",
        "| № | Проект | Руководитель | Участников | "
        + " | ".join(ARTIFACT_LABELS[k] for k in projects.ARTIFACT_KEYS) + " |",
        "|---|---|---|---|" + "---|" * len(projects.ARTIFACT_KEYS),
    ]
    for i, c in enumerate(real, 1):
        cells = [STATUS_LABELS[c["artifacts"][k]["status"]].replace(" ", "\u00a0")
                 for k in projects.ARTIFACT_KEYS]
        out.append(
            f"| {i} | {_md(c['title'])} | {_md(c['lead'])} "
            f"| {c['participants_count']} | " + " | ".join(cells) + " |"
        )
    out += ["", "## Проекты — подробности", ""]
    for i, c in enumerate(real, 1):
        out += render_project(i, c)
    out += [
        "## Черновики онбординга (не подтверждены владельцем, в таблицу пилота не попадают)",
        "",
        "| Проект | Руководитель | Обновлён |",
        "|---|---|---|",
    ]
    for d in data["drafts"]:
        out.append(f"| {_md(d['title'])} | {_md(d['lead'])} | {d['updated']} |")
    out.append("")
    return "\n".join(out)


def render_project(num, c: dict) -> list:
    head = f"### {num}. {c['title']} (`{c['id']}`)"
    lines = [head, ""]
    meta = [f"**Руководитель:** {c['lead'] or '—'}"]
    if c["departments"]:
        meta.append(f"**Подразделение:** {c['departments']}")
    if c["team_size"]:
        meta.append(f"**Заявленный размер команды:** {c['team_size']}")
    meta.append(f"**Участников (по карточке):** {c['participants_count']}")
    if c["budget"]:
        meta.append(f"**Бюджет:** {c['budget']}")
    meta.append(f"**Карточка:** создана {c['created']}, обновлена {c['updated']}")
    if c["hypotheses_published"]:
        meta.append("**Карточка гипотез:** опубликована")
    lines += meta + [""]
    if c["description"]:
        lines += [f"**Описание.** {c['description']}", ""]
    if c["goal"]:
        lines += [f"**Цель.** {c['goal']}", ""]
    if c["groundwork"]:
        lines += [f"**Задел.** {c['groundwork']}", ""]
    if c["participants"]:
        lines += [f"**Участники ({c['participants_count']}):**", ""]
        lines += [f"- {p}" for p in c["participants"]] + [""]
    lines += [
        "**Статус артефактов и гипотезы (колонки таблицы пилота):**",
        "",
        "| Артефакт | Статус | Гипотеза / пояснение |",
        "|---|---|---|",
    ]
    for key in projects.ARTIFACT_KEYS:
        a = c["artifacts"][key]
        lines.append(
            f"| {_md(ARTIFACT_LABELS[key])} | {STATUS_LABELS[a['status']]} "
            f"| {_md(a['hypothesis']) or '—'} |"
        )
    lines.append("")
    if c["hypotheses"]:
        lines.append("**Карточка гипотез проекта (полные формулировки):**")
        lines.append("")
        for section, entries in c["hypotheses"].items():
            draft_only = all(h.get("draft") for h in entries)
            label = SECTION_LABELS.get(section, section)
            if draft_only:
                label += " — черновики поштучных гипотез (сводной карточки ещё нет)"
            lines.append(f"*{label}:*")
            lines.append("")
            for j, h in enumerate(entries, 1):
                suffix = ""
                if h.get("skipped"):
                    reason = h.get("skip_reason") or "без указания причины"
                    suffix = f" *(пропущено: {reason})*"
                elif h.get("draft"):
                    suffix = " *(черновик)*"
                title = f"{j}. **{_md(h['title'])}**" if h["title"] else f"{j}."
                lines.append(f"{title}{suffix}")
                text = h["statement"] or h.get("note") or ""
                if text:
                    lines.append(f"   {text}")
                lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def main() -> None:
    data = collect()
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    stem = f"pilot-status-{date.today().isoformat()}"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    total_participants = sum(c["participants_count"] for c in data["active"])
    print(f"OK: {md_path.name}, {json_path.name}")
    print(f"активных: {len(data['active'])}, черновиков: {len(data['drafts'])}")
    print(f"участников по карточкам (с повторами между проектами): {total_participants}")


if __name__ == "__main__":
    main()