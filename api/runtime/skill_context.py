"""Динамический контекст навыков для системного промпта (skill_context.py).

Блок «Кабинет» (анкета + скрытый контекст) уже добавляется в loop.py
безусловно. Здесь — упоминательнозначимые блоки, которые подмешиваются
в промпт только когда последний вопрос пользователя их касается:

    «Об организации» — при упоминании организации/показателей/документов
                       (публичные данные, читаются и анонимом);
    «Проекты»    — при упоминании проектов/карточек (только для
                       авторизованных: список активных карточек владельца).

Детекция намеренно дешёвая: поиск подстрок по нижнему регистру в последнем
сообщении пользователя. Модель всегда может досмотреть детали инструментами
get_organization / get_projects — блок лишь помогает ей вспомнить, что такие
данные у платформы есть.
"""

ORGANIZATION_KEYWORDS = (
    "организац",
    "институт",
    "университет",
    "наш вуз",
    "показател",
    "стратегия",
    "программа развития",
    "документ",
    "положени",
    "устав",
)
PROJECTS_KEYWORDS = (
    "проект",
    "карточк",
    "мои проекты",
    "онбординг",
    "бюджет",
    "участник",
    "подразделен",
    "задел",
    "презентац",
    "гипотез",
)

ORG_DESC_LIMIT = 600
PROJECTS_LIMIT = 10


def _mentions(text: str, keywords: tuple) -> bool:
    lowered = (text or "").lower()
    return any(k in lowered for k in keywords)


def organization_block() -> str:
    """Компактный блок «Об организации» — если пользователь упомянул её."""
    from . import organization

    store = organization.get_store()
    profile = store.load_profile()
    fields = {f.get("id"): f.get("value") for f in profile.get("fields", []) or []}
    name = str(fields.get("name") or "").strip()
    description = str(fields.get("description") or "").strip()
    if not (name or description):
        return ""
    lines = ["### Сведения об организации (по запросу пользователя)"]
    if name:
        lines.append(f"- Название: {name}")
    if description:
        if len(description) > ORG_DESC_LIMIT:
            description = description[:ORG_DESC_LIMIT] + "…"
        lines.append(f"- Описание: {description}")
    activity = str(fields.get("activity_areas") or "").strip()
    if activity:
        lines.append(f"- Направления деятельности: {activity}")
    metrics = store.load_metrics()
    values = [
        f"{i.get('title')}: {i.get('value')} {i.get('unit', '')}".strip()
        for i in metrics.get("items", []) or []
        if i.get("value") is not None
    ]
    if values:
        lines.append("- Показатели деятельности: " + "; ".join(values))
    return "\n".join(lines)


def projects_block(user_id: str | None) -> str:
    """Компактный список активных карточек владельца — по упоминанию проектов."""
    if not user_id:
        return ""
    from . import projects

    cards = [
        c
        for c in projects.get_store().list(user_id)
        if c.get("status") == projects.STATUS_ACTIVE
    ]
    if not cards:
        return ""
    lines = ["### Карточки проектов пользователя («Проекты»)"]
    for card in cards[:PROJECTS_LIMIT]:
        title = card.get("title") or card["id"]
        bits = [f"«{title}»", f"id: {card['id']}"]
        if card.get("project_type") in projects.PROJECT_TYPE_TITLES:
            bits.append(projects.PROJECT_TYPE_TITLES[card["project_type"]].lower())
        if card.get("trl"):
            bits.append(f"УГТ {card['trl']}")
        if card.get("goal"):
            goal = card["goal"]
            if len(goal) > 100:
                goal = goal[:100] + "…"
            bits.append(f"цель: {goal}")
        if card.get("budget"):
            bits.append(f"бюджет: {card['budget']}")
        lines.append("- " + " — ".join(bits))
    lines.append(
        "Детали карточки — инструмент get_projects; изменения — save_project_card."
    )
    return "\n".join(lines)


ONTOLOGY_CLASSES_LIMIT = 40
CLASS_LINE_MAX = 160


def _ontology_classes_block(ontology_id: str, user_id: str | None = None) -> str:
    """Компактный список классов подключённой онтологии — id и название
    каждого класса, чтобы модель знала, какие значения передавать в
    search_corpus.class_ids (см. docstring rag.py: онтология работает как
    понятийный фильтр перед векторным поиском, GraphRAG-лайт). Без этого
    блока модель не может использовать фильтр — она не знает, что классы
    вообще существуют. user_id — для тестовых онтологий (admin_only):
    их классы видят только админские сессии."""
    from . import ontologies as ontology_module
    from .users import is_admin_user

    card = ontology_module.get_store().find(ontology_id)
    if not card or card.get("status") != ontology_module.STATUS_ACTIVE:
        return ""
    if card.get("admin_only") and not is_admin_user(user_id):
        # тестовая онтология: классы в промпт подмешиваются только админам
        return ""
    classes = [c for c in (card.get("classes") or []) if isinstance(c, dict)]
    if not classes:
        return ""
    lines = [f"  Классы онтологии «{card.get('name') or ontology_id}» (id: название):"]
    for cls in classes[:ONTOLOGY_CLASSES_LIMIT]:
        cid = cls.get("id")
        name = cls.get("name")
        if not cid or not name:
            continue
        extra = ""
        synonyms = cls.get("synonyms") or []
        if synonyms:
            extra = " (синонимы: " + ", ".join(synonyms[:5]) + ")"
        line = f"    - {cid}: {name}{extra}"
        if len(line) > CLASS_LINE_MAX:
            line = line[:CLASS_LINE_MAX] + "…"
        lines.append(line)
    if len(classes) > ONTOLOGY_CLASSES_LIMIT:
        lines.append(f"    … и ещё {len(classes) - ONTOLOGY_CLASSES_LIMIT} классов")
    return "\n".join(lines)


def connected_rag_block(session: dict) -> str:
    """RAG-база, явно подключённая пользователем в рабочем чате.

    Подключается через раздел «RAG-базы» меню кнопки «+» у поля ввода;
    база всегда одна (ChatRequest.rag_ids max_length=1).

    В отличие от organization_block/projects_block, добавляется безусловно
    (не по упоминанию в тексте вопроса) — раз пользователь явно выбрал базу
    в интерфейсе, модель должна знать о ней в каждом сообщении сессии.

    Если у базы указана связанная онтология (ontology_id) — подмешивается
    компактный список её классов (_ontology_classes_block), чтобы модель
    могла осмысленно передать search_corpus.class_ids и реально
    воспользоваться фильтром, а не только знать о его существовании из
    описания инструмента.
    """
    rag_ids = session.get("rag_ids") if isinstance(session, dict) else None
    if not rag_ids:
        return ""
    from . import rag as rag_module
    from .users import is_admin_user

    store = rag_module.get_store()
    lines = ["### Подключённая пользователем RAG-база (меню «+» в рабочем чате)"]
    found_any = False
    user_id = session.get("user_id") if isinstance(session, dict) else None
    for rag_id in list(rag_ids)[:1]:  # база всегда одна (ChatRequest.rag_ids max_length=1)
        card = store.find(rag_id)
        if not card or card.get("status") != rag_module.STATUS_ACTIVE:
            continue
        if card.get("admin_only") and not is_admin_user(user_id):
            # тестовая база: подключается и используется только в админских сессиях
            continue
        found_any = True
        name = card.get("name") or rag_id
        desc = (card.get("description") or "").strip()
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append(f"- id: {rag_id} — «{name}». {desc}")
        ontology_id = (card.get("ontology_id") or "").strip()
        if ontology_id:
            classes_block = _ontology_classes_block(ontology_id, user_id)
            if classes_block:
                lines.append(classes_block)
    if not found_any:
        return ""
    lines.append(
        "Пользователь явно подключил эту базу в интерфейсе рабочего чата — "
        "используйте инструмент search_corpus с указанным rag_id, чтобы "
        "ответить на вопрос по её содержимому, не спрашивая разрешения. "
        "Если у базы выше указаны классы онтологии и вопрос явно про одну "
        "из этих тем — сузьте поиск, передав id подходящих классов в "
        "search_corpus.class_ids; если тема не совпадает ни с одним "
        "классом или неясно — не передавайте class_ids вовсе."
    )
    return "\n".join(lines)


def connected_models_block(session: dict) -> str:
    """Модели коллектива, привязанные к агенту сессии (model_ids).

    Как и подключённая RAG-база, добавляется безусловно: агент с моделью
    должен считать по ней, а не по памяти или по тексту статей.
    """
    model_ids = session.get("model_ids") if isinstance(session, dict) else None
    if not model_ids:
        return ""
    from . import model_registry
    from .model_tools import _compact

    lines = ["### Модели коллектива, привязанные к агенту"]
    for mid in list(model_ids)[:3]:
        card = model_registry.get_store().find(mid)
        if not card or not card.get("current_version"):
            continue
        info = _compact(card)
        ins = "; ".join(
            f"{i['название']} ({i['имя']}{', ' + i['единицы'] if i['единицы'] else ''}"
            + (f", опыты {i['диапазон_опытов'][0]}–{i['диапазон_опытов'][1]}" if i.get("диапазон_опытов") else "")
            + (f", значения: {', '.join(map(str, i['значения']))}" if i.get("значения") else "") + ")"
            for i in info["входы"])
        outs = "; ".join(f"{o['название']} ({o['единицы']}, типичная ошибка {o['типичная_ошибка']})"
                         for o in info["выходы"])
        lines.append(f"- id: {mid} — «{info['название']}», {info['вид']}, версия {info['версия']}. "
                     f"Входы: {ins or 'температурная программа'}. Выходы: {outs}.")
    if len(lines) == 1:
        return ""
    lines.append(
        "Числа, которые считает модель, берите ТОЛЬКО инструментами run_model (прогноз) и optimize_model "
        "(подбор режима под цель), не подгоняйте данные сами и не берите значения из памяти или статей. "
        "Называйте значение ± типичную ошибку с единицами и номер версии модели. Если область прогноза "
        "outside — скажите, что прогноз недостоверен и нужен опыт. После опыта предложите внести "
        "результат (save_model_fact)."
    )
    return "\n".join(lines)


def context_block(session: dict, user_text: str) -> str:
    """Собирает блоки по упоминаниям в последнем сообщении пользователя."""
    parts: list[str] = []
    if _mentions(user_text, ORGANIZATION_KEYWORDS):
        block = organization_block()
        if block:
            parts.append(block)
    if _mentions(user_text, PROJECTS_KEYWORDS):
        block = projects_block(session.get("user_id") if isinstance(session, dict) else None)
        if block:
            parts.append(block)
    return "\n\n".join(parts)
