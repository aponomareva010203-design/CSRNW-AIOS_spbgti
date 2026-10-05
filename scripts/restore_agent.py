#!/usr/bin/env python3
"""Восстановление удалённых агентов из закрытого архива (мягкое удаление).

Удаление агентов на платформе мягкое: файлы переезжают в архив — API и
каталоги их больше не отдают (404), но физически всё лежит на сервере:

  .aios-runtime/agents-removed/{owner}/  — агенты-конструкторы: карточка
        {agent_id}.json, данные {agent_id}/ (записи, evals, трассы),
        логотипы icons/{agent_id}.svg;
  .aios-runtime/skills-removed/{user}/   — черновики агентов (кабинет):
        {id}.md + {id}.json;
  .aios-runtime/skills-removed/_catalog/ — публичные файлы агентов каталога,
        удалённых модерацией: карточка data/skills.json + текст .md.

Терминальный доступ к архиву (запускать на сервере):

    PYTHONPATH=. .venv/bin/python scripts/restore_agent.py

        — список того, что лежит в архиве;

    PYTHONPATH=. .venv/bin/python scripts/restore_agent.py restore agt-xxxx
    PYTHONPATH=. .venv/bin/python scripts/restore_agent.py restore recipe-scout

        — вернуть агента на платформу. Для агента-конструктора: карточка,
        данные и логотип возвращаются владельцу, опубликованный (active)
        агент снова попадает в общий каталог. Черновик возвращается автору
        как черновик (публикацию перепубликует автор из кабинета). Агент
        каталога: карточка и .md возвращаются в каталог.

Механика лежит на сторах (api/runtime/agent_store.py, skill_drafts.py,
datarepo.py), скрипт только двигает файлы по их правилам.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.runtime.config import get_settings  # noqa: E402


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _move(src: Path, dest: Path) -> bool:
    """Переезд файла/каталога в живые сторы; коллизия с существующим именем
    разрешается суффиксом времени (не затираем ни архив, ни живое)."""
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
    src.replace(dest)
    return True


def _dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


# ---------------------------------------------------------------------------
# Восстановление агентов-конструкторов (agents-removed/{owner}/{agent_id}.json)


def restore_constructor_agent(agent_id: str) -> bool:
    """Карточка + накопленные данные + логотип обратно в живой AgentStore."""
    settings = get_settings()
    card_path, card = None, {}
    for owner_dir in _dirs(settings.runtime_dir / "agents-removed"):
        candidate = owner_dir / f"{agent_id}.json"
        if candidate.is_file():
            card_path, card = candidate, (_load(candidate) or {})
            break
    if card_path is None:
        return False

    owner = str(card.get("owner_id") or card_path.parent.name)
    live_dir = settings.runtime_dir / "agents" / owner
    if (live_dir / f"{agent_id}.json").is_file():
        print(f"  ✗ живой агент «{agent_id}» уже существует — восстановление не нужно")
        return False

    moved = _move(card_path, live_dir / f"{agent_id}.json")
    data_dir = card_path.parent / agent_id
    if data_dir.is_dir():
        moved = _move(data_dir, live_dir / agent_id) and moved
    icon = card_path.parent / "icons" / f"{agent_id}.svg"
    if icon.is_file():
        moved = _move(icon, settings.icon_data_dir / "icons" / f"{agent_id}.svg") and moved

    if card.get("status") == "active":
        from api.runtime.agent_store import get_store

        entry = get_store().publish(card)
        print(f"  ✓ агент «{agent_id}» восстановлен и опубликован: «{entry['title']}»")
    else:
        print(f"  ✓ агент «{agent_id}» восстановлен в черновик (владелец {owner})")
    return moved


# ---------------------------------------------------------------------------
# Черновики (skills-removed/{user}/{id}.md + .json)


def restore_draft(skill_id: str) -> bool:
    """Черновик агента обратно автору в skills/{user}/."""
    settings = get_settings()
    for user_dir in _dirs(settings.runtime_dir / "skills-removed"):
        if user_dir.name == "_catalog":
            continue
        md, meta = user_dir / f"{skill_id}.md", user_dir / f"{skill_id}.json"
        if not md.is_file() and not meta.is_file():
            continue
        live = settings.runtime_dir / "skills" / user_dir.name
        if (live / f"{skill_id}.md").is_file() or (live / f"{skill_id}.json").is_file():
            print(f"  ✗ живой черновик «{skill_id}» уже существует")
            return False
        moved = False
        if meta.is_file():
            moved = _move(meta, live / f"{skill_id}.json") and True
            m = _load(live / f"{skill_id}.json") or {}
            if m.get("status") == "published":
                # публичная копия при удалении черновика не снималась с
                # публикации (черновик — редактируемая копия), поэтому
                # восстановленному возвращаем статус черновика: автор
                # перепубликует из кабинета, если агент и правда жив
                m["status"] = "draft"
                m.pop("published_at", None)
                (live / f"{skill_id}.json").write_text(
                    json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print("  ~ статус «published» в черновике сброшен в draft — "
                      "автор перепубликует из кабинета")
        if md.is_file():
            moved = _move(md, live / f"{skill_id}.md") and moved
        print(f"  ✓ черновик «{skill_id}» возвращён автору ({user_dir.name})")
        return moved
    return False


# ---------------------------------------------------------------------------
# Агенты каталога, удалённые модерацией (skills-removed/_catalog/)


def restore_catalog_agent(agent_id: str) -> bool:
    """Карточка data/skills.json и .md-файл обратно в публичный каталог."""
    settings = get_settings()
    manifest = settings.runtime_dir / "skills-removed" / "_catalog" / f"{agent_id}.json"
    record = _load(manifest)
    if record is None:
        return False
    from api.runtime import datarepo

    card = record.get("card") or {}
    catalog_path = datarepo.DATA_DIR / "skills.json"
    data = _load(catalog_path) or {"items": []}
    items = [c for c in (data.get("items") or []) if isinstance(c, dict)]
    if any(c.get("id") == agent_id for c in items):
        print(f"  ✗ карточка «{agent_id}» уже есть в каталоге")
        return False
    data["items"] = items + [card]
    tmp = catalog_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(catalog_path)
    text = str(record.get("text") or "")
    md_path = datarepo.skill_path(card)
    if text and md_path is not None and not md_path.exists():
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(text, encoding="utf-8")
    manifest.unlink(missing_ok=True)
    print(f"  ✓ агент каталога «{agent_id}» возвращён в data/skills.json")
    return True


def list_trash() -> None:
    settings = get_settings()
    agents_trash = settings.runtime_dir / "agents-removed"
    skills_trash = settings.runtime_dir / "skills-removed"

    print("Агенты-конструкторы (.aios-runtime/agents-removed/):")
    found = False
    for owner_dir in _dirs(agents_trash):
        for card_path in sorted(owner_dir.glob("*.json")):
            card = _load(card_path) or {}
            print(f"  {card_path.stem:34} {str(card.get('title'))[:40]:40} "
                  f"владелец {owner_dir.name}  удалён {_when(card.get('deleted_at'))}")
            found = True
    if not found:
        print("  (пусто)")

    print("Черновики (.aios-runtime/skills-removed/):")
    found = False
    for user_dir in _dirs(skills_trash):
        if user_dir.name == "_catalog":
            continue
        for meta_path in sorted(user_dir.glob("*.json")):
            meta = _load(meta_path) or {}
            print(f"  {meta_path.stem:34} {str(meta.get('name'))[:40]:40} "
                  f"автор {user_dir.name}  удалён {_when(meta.get('deleted_at'))}")
            found = True
        for md_path in sorted(user_dir.glob("*.md")):
            if not (user_dir / f"{md_path.stem}.json").is_file():
                print(f"  {md_path.stem:34} {'(только текст)':40} автор {user_dir.name}")
                found = True
    if not found:
        print("  (пусто)")

    catalog_trash = skills_trash / "_catalog"
    if catalog_trash.is_dir() and any(catalog_trash.glob("*.json")):
        print("Агенты каталога, удалённые модерацией (skills-removed/_catalog/):")
        for manifest_path in sorted(catalog_trash.glob("*.json")):
            record = _load(manifest_path) or {}
            print(f"  {manifest_path.stem:34} "
                  f"{str((record.get('card') or {}).get('name'))[:40]:40}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Архив удалённых агентов: список и восстановление")
    parser.add_argument("command", nargs="?", default="list", choices=("list", "restore"))
    parser.add_argument("agent_id", nargs="?")
    args = parser.parse_args()

    if args.command == "restore":
        if not args.agent_id:
            parser.error("укажите id агента: restore <agent_id>")
        agent_id = args.agent_id.strip()
        done = restore_constructor_agent(agent_id) or restore_draft(agent_id) \
            or restore_catalog_agent(agent_id)
        if not done:
            print(f"✗ «{agent_id}» в архиве не найден "
                  f"({get_settings().runtime_dir}/agents-removed, skills-removed)")
            return 1
        return 0

    list_trash()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())