#!/usr/bin/env python3
"""Пересобирает демо-содержимое раздела «Данные и знания» по теме платформы.

Тема — управление инновациями и технологическими стартапами в вузе
(рабочее пространство проректора по инновациям). Скрипт пишет:

  - data/ontologies.json и data/ontologies/objects/{id}.json — онтологии из
    scripts/seed_knowledge/ontologies.py (граф проверяется validate_graph);
  - data/rag_bases.json — карточки RAG-баз; корпуса документов лежат в
    data/rag/corpus/{rag_id}/*.md и ищутся инструментом search_corpus
    (Qdrant, если он поднят и проиндексирован, иначе — текстовый поиск);
  - data/icons/{id}.svg — логотипы новых карточек;

и удаляет логотипы и графы онтологий/RAG-баз, которых больше нет в каталоге.
Повторный запуск идемпотентен.

Запуск из корня проекта:
    python3 scripts/seed_innovation_knowledge.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from api.runtime.ontologies import validate_graph  # noqa: E402
from seed_knowledge.ontologies import ONTOLOGIES  # noqa: E402

DATA = ROOT / "data"
CORPUS = DATA / "rag" / "corpus"
ARTIFACT_RE = re.compile(r"^(onto|rag)-[a-f0-9]{8,12}$")

RAG_BASES = [
    {
        "id": "rag-1a0e5c0101",
        "name": "Трансфер технологий: служебные РИД, лицензии, МТК",
        "domain": "Правовые и организационные основы трансфера технологий в вузе",
        "description_ru": (
            "Методические конспекты для ЦТТ и проректора: служебные РИД и сроки решения "
            "работодателя (ст. 1370 ГК РФ), лицензия и отчуждение (ст. 1234–1235), вклад права "
            "в капитал МТК (ст. 103 273-ФЗ), ревизия патентного портфеля."
        ),
        "tags": ["РИД", "лицензирование", "МТК", "ЦТТ"],
        "ontology_id": "onto-1a0e5c0003",
    },
    {
        "id": "rag-1a0e5c0102",
        "name": "Уровни готовности: УГТ/TRL, IRL и экспертиза проектов",
        "domain": "Оценка зрелости технологий и проектов вуза",
        "description_ru": (
            "Шкала УГТ 1–9 по ГОСТ Р 58048-2017, смежные шкалы IRL/MRL/CRL, правило «застоя» "
            "проекта, регламент и оценочный лист экспертного совета, решения Go/Pivot/Hold/Kill."
        ),
        "tags": ["УГТ", "TRL", "экспертиза", "портфель проектов"],
        "ontology_id": "onto-1a0e5c0001",
    },
    {
        "id": "rag-1a0e5c0103",
        "name": "Меры поддержки: Фонд содействия инновациям, Платформа УТП, «Приоритет-2030»",
        "domain": "Государственная поддержка университетских проектов и стартапов",
        "description_ru": (
            "Лестница программ Фонда содействия инновациям («УМНИК», «Студенческий стартап», "
            "«Старт», «Развитие», «Коммерциализация»), Платформа университетского "
            "технологического предпринимательства и «Стартап как диплом», «Приоритет-2030» и ПИШ."
        ),
        "tags": ["гранты", "Фонд содействия инновациям", "Приоритет-2030", "Стартап как диплом"],
        "ontology_id": "onto-1a0e5c0004",
    },
    {
        "id": "rag-1a0e5c0104",
        "name": "Методики стартап-студии и акселерации",
        "domain": "Создание и развитие университетских стартапов",
        "description_ru": (
            "Трек стартап-студии по этапам с контрольными точками, роль трекера, формулировка и "
            "проверка гипотез (CustDev, MVP, пилот), юнит-экономика и подготовка к инвестициям."
        ),
        "tags": ["стартап-студия", "гипотезы", "CustDev", "инвестиции"],
        "ontology_id": "onto-1a0e5c0002",
    },
    {
        "id": "rag-1a0e5c0105",
        "name": "Регламенты и KPI офиса проректора по инновациям (шаблоны)",
        "domain": "Локальные акты и система показателей инновационного блока вуза",
        "description_ru": (
            "Шаблоны для адаптации: положение о ЦТТ и комиссии по ИС, порядок создания МТК и "
            "участия вуза в стартапах, карта KPI офиса проректора и ритм управления."
        ),
        "tags": ["регламенты", "KPI", "положение о ЦТТ", "шаблоны"],
        "ontology_id": "onto-1a0e5c0001",
    },
]

ICON_COLORS = ["#1F5AA6", "#2E9D5B", "#B26A00", "#7A3FB0", "#C0392B"]


def onto_icon(i: int) -> str:
    c = ICON_COLORS[i % len(ICON_COLORS)]
    return f"""<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <g stroke="{c}" stroke-width="2" fill="none" stroke-linecap="round">
    <line x1="32" y1="30" x2="16" y2="18" /><line x1="32" y1="30" x2="48" y2="18" />
    <line x1="32" y1="30" x2="18" y2="46" /><line x1="32" y1="30" x2="46" y2="46" />
    <line x1="16" y1="18" x2="48" y2="18" />
  </g>
  <circle cx="16" cy="18" r="4.5" fill="#F2F6FC" stroke="{c}" stroke-width="2" />
  <circle cx="48" cy="18" r="4.5" fill="#F2F6FC" stroke="{c}" stroke-width="2" />
  <circle cx="18" cy="46" r="4.5" fill="#F2F6FC" stroke="{c}" stroke-width="2" />
  <circle cx="46" cy="46" r="4.5" fill="#F2F6FC" stroke="{c}" stroke-width="2" />
  <circle cx="32" cy="30" r="6.5" fill="{c}" />
</svg>
"""


def rag_icon(i: int) -> str:
    c = ICON_COLORS[i % len(ICON_COLORS)]
    return f"""<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <g stroke="{c}" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <ellipse cx="28" cy="16" rx="14" ry="5" />
    <path d="M14 16v28c0 2.8 6.3 5 14 5s14-2.2 14-5V16" />
    <path d="M14 30c0 2.8 6.3 5 14 5s14-2.2 14-5" />
    <circle cx="44" cy="42" r="7" fill="#F2F6FC" />
    <line x1="49" y1="47" x2="54" y2="52" />
  </g>
</svg>
"""


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    icons = DATA / "icons"
    objects = DATA / "ontologies" / "objects"
    keep: set = set()

    onto_items = []
    for i, graph in enumerate(ONTOLOGIES):
        problems = validate_graph(graph, require_complete=True)
        if problems:
            raise SystemExit(f"{graph['id']}: " + "; ".join(problems))
        graph = dict(graph, status="active")
        write_json(objects / f"{graph['id']}.json", graph)
        (icons / f"{graph['id']}.svg").write_text(onto_icon(i), encoding="utf-8")
        keep.add(graph["id"])
        onto_items.append({
            "id": graph["id"],
            "name": graph["name"],
            "image_url": f"/data/icons/{graph['id']}.svg",
            "domain": graph["domain"],
            "description_ru": graph["description"],
            "author": graph["author"],
            "format": "JSON",
            "classes_count": len(graph["classes"]),
            "relations_count": len(graph["relations"]),
            "projects": [],
            "project_ids": [],
            "status": "Готова",
            "file_url": f"/data/ontologies/objects/{graph['id']}.json",
            "tags": graph["tags"],
        })
    write_json(DATA / "ontologies.json", {"items": onto_items})

    rag_items = []
    for i, base in enumerate(RAG_BASES):
        docs = sorted((CORPUS / base["id"]).glob("*.md"))
        if not docs:
            raise SystemExit(f"{base['id']}: нет документов в {CORPUS / base['id']}")
        (icons / f"{base['id']}.svg").write_text(rag_icon(i), encoding="utf-8")
        keep.add(base["id"])
        rag_items.append({
            "id": base["id"],
            "name": base["name"],
            "image_url": f"/data/icons/{base['id']}.svg",
            "project": "",
            "domain": base["domain"],
            "description_ru": base["description_ru"],
            "documents_count": len(docs),
            "documents": [d.name for d in docs],
            "vector_db": "Qdrant",
            "embedding_model": "text-embedding-3-small",
            "status": "Активна",
            "access_url": "",
            "tags": base["tags"],
            "ontology_id": base["ontology_id"],
            "project_id": "",
            "sensitive": False,
            "corpus": f"/data/rag/corpus/{base['id']}/",
        })
    write_json(DATA / "rag_bases.json", {"items": rag_items})

    # логотипы и графы удалённых онтологий/RAG-баз прежней платформы
    removed = 0
    for path in list(icons.glob("onto-*.svg")) + list(icons.glob("rag-*.svg")):
        # только логотипы артефактов (onto-/rag- + hex); rag-builder.svg и т.п. — навыки
        if ARTIFACT_RE.match(path.stem) and path.stem not in keep:
            path.unlink()
            removed += 1
    if objects.is_dir():
        for path in objects.glob("*.json"):
            if path.name.split(".")[0] not in keep:
                path.unlink()
                removed += 1
    print(f"онтологий: {len(onto_items)}, RAG-баз: {len(rag_items)}, удалено старых файлов: {removed}")


if __name__ == "__main__":
    main()
