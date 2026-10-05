#!/usr/bin/env python3
"""Загрузка готовых агентов из scripts/agent_templates.json в хранилище.

Зачем это нужно. Диалог сборки агента с нуля — это 15-20 минут при идеальном
ходе, а на семинаре 17 участников и полтора часа: первый содержательный ответ
агента пришёлся бы на сороковую минуту, если бы всё прошло гладко. Поэтому
участник не собирает агента, а берёт готового под свой проект и дорабатывает
словами. Шаблоны написаны заранее, по одному на проект.

Побочный, но важный эффект: граф в шаблоне написан один раз вручную, а не
порождается моделью для каждого участника — исчезает главный источник отказов
конструктора (рассинхронизация id между nodes и available_tools даёт агента
без инструментов, который молча отвечает из головы).

Использование:

    # посмотреть, что будет сделано, ничего не меняя
    PYTHONPATH=. .venv/bin/python scripts/load_agent_templates.py --dry-run

    # загрузить по сопоставлению проект → шаблон
    PYTHONPATH=. .venv/bin/python scripts/load_agent_templates.py --map map.json

    # один шаблон конкретному владельцу и проекту
    PYTHONPATH=. .venv/bin/python scripts/load_agent_templates.py \
        --template tpl-hex --owner a1b2c3d4e5f6 --project prj-… [--rag rag-…]

Формат map.json — список назначений:

    [{"template": "tpl-hex", "owner": "a1b2c3d4e5f6",
      "project": "prj-1a2b3c", "rag": "rag-…"}, …]

Агенты создаются ЧЕРНОВИКАМИ (status: draft): публикует их владелец сам,
после того как задал агенту настоящий рабочий вопрос и убедился в ответе.
Повторный запуск не плодит дубли — сопоставление идёт по template_id в
карточке, существующий агент обновляется.

ВАЖНО: «обновляется» значит, что инструкция, приветствие и тестовые случаи
заменяются шаблонными, то есть доработка агента владельцем теряется. Если
участники уже правили своих агентов, запускать с --only-new; посмотреть,
кого затронет, — с --dry-run (он помечает доработанных).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "agent_templates.json"

# Служебные поля шаблона: нужны галерее и этому скрипту, в карточку не идут.
META_FIELDS = ("template_id", "project_hint", "capabilities")


def load_templates() -> dict:
    data = json.loads(TEMPLATES.read_text(encoding="utf-8"))
    return {t["template_id"]: t for t in data["templates"]}


def build_card(template: dict, project_id: str, rag_id: str = "") -> dict:
    """Карточка агента из шаблона, привязанная к проекту."""
    card = {k: v for k, v in template.items() if k not in META_FIELDS}
    card["project_id"] = project_id
    card["template_id"] = template["template_id"]  # чтобы не плодить дубли

    nodes = []
    for node in card["nodes"]:
        node = json.loads(json.dumps(node))  # не портим шаблон в памяти
        if node["type"] == "tool.rag_search":
            if not rag_id:
                continue  # у проекта нет базы — узел лишний
            node["config"]["rag_id"] = rag_id
        nodes.append(node)

    kept = {n["id"] for n in nodes}
    for node in nodes:
        if node["type"] == "reasoning":
            node["config"]["available_tools"] = [
                t for t in node["config"]["available_tools"] if t in kept
            ]
    card["nodes"] = nodes
    card["edges"] = [e for e in card["edges"] if e["from"] in kept and e["to"] in kept]
    return card


def existing_by_template(store, owner_id: str, project_id: str, template_id: str):
    for card in store.list(owner_id, project_id):
        if card.get("template_id") == template_id:
            return card
    return None


def was_edited(card: dict, template: dict) -> bool:
    """Правил ли владелец агента после загрузки из шаблона.

    Сравниваются поля, которые меняет доработка словами через конструктор:
    инструкция, приветствие, тестовые случаи. Версия карточки для этого не
    годится — её поднимает и сам повторный запуск скрипта.
    """
    if card.get("system_prompt", "") != template.get("system_prompt", ""):
        return True
    if card.get("welcome_message", "") != template.get("welcome_message", ""):
        return True
    return len(card.get("evals") or []) != len(template.get("evals") or [])


def assign(store, template: dict, owner_id: str, project_id: str,
           rag_id: str = "", dry_run: bool = False, only_new: bool = False) -> str:
    card = build_card(template, project_id, rag_id)
    found = existing_by_template(store, owner_id, project_id, template["template_id"])
    action = "обновлён" if found else "создан"
    edited = bool(found) and was_edited(found, template)

    # Повторная загрузка перезаписывает инструкцию, приветствие и тесты
    # значениями шаблона — то есть стирает доработку, ради которой участник
    # и сидел с конструктором. Раньше это происходило молча.
    if found and only_new:
        mark = "доработан владельцем" if edited else "без правок"
        return f"  [пропущен] {template['title']} -> {found['id']} ({mark}, --only-new)"

    if found:
        card["agent_id"] = found["id"]
    if dry_run:
        tools = [n["type"].removeprefix("tool.") for n in card["nodes"]
                 if n["type"].startswith("tool.")]
        warn = ("\n      ВНИМАНИЕ: агент доработан владельцем — инструкция, "
                "приветствие и тесты будут заменены шаблонными" if edited else "")
        return (f"  [{action}] {template['title']} -> проект {project_id}"
                f"\n      возможности: {', '.join(tools)}"
                f"\n      тестов: {len(card.get('evals') or [])}{warn}")
    saved = store.save(owner_id, card)
    warn = "  ← правки владельца заменены шаблонными" if edited else ""
    return f"  [{action}] {template['title']} -> {saved['id']}  (проект {project_id}){warn}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--map", help="JSON-файл назначений")
    parser.add_argument("--template", help="id шаблона (tpl-…)")
    parser.add_argument("--owner", help="id владельца (12 hex)")
    parser.add_argument("--project", help="id проекта (prj-…)")
    parser.add_argument("--rag", default="", help="id RAG-базы проекта (необязательно)")
    parser.add_argument("--list", action="store_true", help="показать доступные шаблоны")
    parser.add_argument("--dry-run", action="store_true", help="ничего не менять")
    parser.add_argument("--only-new", action="store_true",
                        help="не трогать уже существующих агентов (их доработка "
                             "словами будет сохранена)")
    args = parser.parse_args()

    templates = load_templates()

    if args.list:
        print(f"Шаблонов: {len(templates)}\n")
        for tid, t in templates.items():
            print(f"  {tid:20} {t['icon']} {t['title']}")
            print(f"  {'':20} проект: {t['project_hint']}")
            print(f"  {'':20} возможности: {', '.join(t['capabilities'])}\n")
        return 0

    sys.path.insert(0, str(REPO))
    from api.runtime import agent_store  # noqa: E402 — после настройки пути

    store = agent_store.get_store()

    if args.map:
        assignments = json.loads(Path(args.map).read_text(encoding="utf-8"))
    elif args.template and args.owner and args.project:
        assignments = [{"template": args.template, "owner": args.owner,
                        "project": args.project, "rag": args.rag}]
    else:
        parser.error("нужен --map либо --template с --owner и --project "
                     "(или --list, чтобы посмотреть шаблоны)")
        return 2

    print(f"Назначений: {len(assignments)}"
          f"{'  (пробный прогон, ничего не меняется)' if args.dry_run else ''}\n")
    errors = 0
    for item in assignments:
        template = templates.get(item.get("template"))
        if template is None:
            print(f"  [пропуск] неизвестный шаблон: {item.get('template')}")
            errors += 1
            continue
        try:
            print(assign(store, template, item["owner"], item["project"],
                         item.get("rag", ""), args.dry_run, args.only_new))
        except Exception as exc:  # noqa: BLE001 — одно назначение не валит остальные
            print(f"  [ОШИБКА] {template['title']}: {type(exc).__name__}: {exc}")
            errors += 1

    print(f"\nГотово. Ошибок: {errors}")
    if not args.dry_run and not errors:
        print("Агенты созданы черновиками — владельцы публикуют их сами "
              "после проверки на реальном вопросе.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
