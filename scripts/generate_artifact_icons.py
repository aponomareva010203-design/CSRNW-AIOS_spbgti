#!/usr/bin/env python3
"""Генерирует SVG-логотипы уже существующим артефактам платформы.

Новые онтологии/RAG-базы/агенты получают логотип автоматически в момент
сохранения (api/runtime/artifact_icons.py, фоном после save_*-инструментов
конструкторов). Этот скрипт — разовый бэкфилл для артефактов, созданных
до появления этой механики:

  - онтологии: data/ontologies.json + черновики .aios-runtime/ontologies/;
  - RAG-базы: data/rag_bases.json + черновики .aios-runtime/rag/;
  - агенты: data/user_agents.json + карточки .aios-runtime/agents/
    (логотип дополнительно пишется в поле icon приватной карточки);
  - платформенные навыки-агенты: data/skills.json (поле image_url).

Логотип — файл data/icons/{artifact_id}.svg, публично отдаётся как
/data/icons/{artifact_id}.svg. Повторный запуск идемпотентен: у кого
логотип уже есть, тот не перегенерируется (только проверяется ссылка
в каталоге). LLM берётся из действующих настроек платформы (админских,
если запуск без пользователя).

Запуск из корня проекта:
    ./.venv/bin/python3 scripts/generate_artifact_icons.py [--limit N] [--dry-run]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.runtime import artifact_icons  # noqa: E402

CONCURRENCY = 3


def _skills_jobs() -> list[dict]:
    """Навыки из data/skills.json — статический каталог страницы «Агенты»."""
    path = (
        artifact_icons.get_settings().icon_data_dir / "skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [dict(i) for i in items or [] if isinstance(i, dict) and i.get("id")]


async def _process(kind: str, card: dict, sem: asyncio.Semaphore, dry: bool) -> str:
    """Возвращает человекочитаемый статус обработки одного артефакта."""
    artifact_id = str(card.get("id") or "")
    title = card.get("name") or card.get("title") or artifact_id
    if not artifact_id:
        return f"ПРОПУСК   {kind}: нет id ({title})"

    existing = artifact_icons.logo_url(artifact_id)
    if existing:
        if not dry:
            _patch(kind, card, existing)
        return f"ЕСТЬ      {kind} {artifact_id}: {title}"

    if dry:
        return f"БУДЕТ     {kind} {artifact_id}: {title}"

    async with sem:
        svg = await artifact_icons.generate_logo(kind, card)
    if svg is None:
        return f"СБОЙ LLM  {kind} {artifact_id}: {title}"
    url = artifact_icons.store_logo(artifact_id, svg)
    if kind == "agent" and card.get("owner_id"):
        try:
            artifact_icons.apply_agent_icon(str(card["owner_id"]), artifact_id, svg)
        except Exception as exc:  # noqa: BLE001 — карточка не критична для каталога
            print(f"  ! icon в карточку агента не записан: {exc}")
    _patch(kind, card, url)
    return f"СОЗДАН    {kind} {artifact_id}: {title}"


def _patch(kind: str, card: dict, url: str) -> None:
    """Прописывает image_url в каталог артефакта (или skills.json)."""
    if kind == "skill":
        _patch_skills(card["id"], url)
        return
    try:
        artifact_icons.patch_catalog(kind, str(card["id"]), url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! каталог {kind} не обновлён: {exc}")


def _patch_skills(skill_id: str, url: str) -> bool:
    path = artifact_icons.get_settings().icon_data_dir / "skills.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return False
        found = False
        for item in items:
            if isinstance(item, dict) and item.get("id") == skill_id:
                item["image_url"] = url
                found = True
        if not found:
            return False
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)
        return True
    except (OSError, ValueError):
        return False


async def main(limit: int | None, dry: bool) -> None:
    from api.runtime.config import get_settings

    if not get_settings().llm_api_key:
        # администраторская LLM может жить только в .aios-runtime/llm.json,
        # а не в .env — проверяем фактического провайдера
        if artifact_icons._current_provider(None) is None:
            print("LLM не настроена (админ-панель → настройки внешних API). "
                  "Генерация невозможна.")
            return

    jobs: list[tuple[str, dict]] = []
    for kind in ("ontology", "rag", "agent"):
        for card in artifact_icons.iter_existing(kind):
            jobs.append((kind, card))
    for card in _skills_jobs():
        jobs.append(("skill", card))
    if limit:
        jobs = jobs[:limit]
    print(f"Артефактов к обработке: {len(jobs)} "
          f"(онтологии/RAG/агенты/навыки). Конкурентность: {CONCURRENCY}.\n")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def run(idx_kind_card):
        idx, (kind, card) = idx_kind_card
        status = await _process(kind, card, sem, dry)
        return idx, status

    results = await asyncio.gather(
        *(run(job) for job in enumerate(jobs)), return_exceptions=True
    )
    created = have = failed = 0
    for idx, res in sorted(
        (r if isinstance(r, tuple) else (i, f"СБОЙ     {r!r}") for i, r in enumerate(results))
    ):
        if res.startswith("СОЗДАН"):
            created += 1
        elif res.startswith("ЕСТЬ"):
            have += 1
        elif res.startswith("СБОЙ"):
            failed += 1
        print(f"[{idx + 1}/{len(jobs)}] {res}")
    print(f"\nИтого: создано {created}, уже было {have}, сбоев {failed}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="обработать только первые N артефактов (отладка)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план без обращений к LLM")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.dry_run))
