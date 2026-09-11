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


def connected_rag_block(session: dict) -> str:
    """RAG-базы, явно подключённые пользователем кнопкой в рабочем чате.

    В отличие от organization_block/projects_block, добавляется безусловно
    (не по упоминанию в тексте вопроса) — раз пользователь явно выбрал базу
    в интерфейсе, модель должна знать о ней в каждом сообщении сессии.
    """
    rag_ids = session.get("rag_ids") if isinstance(session, dict) else None
    if not rag_ids:
        return ""
    from . import rag as rag_module

    store = rag_module.get_store()
    lines = ["### Подключённые пользователем RAG-базы (кнопка в рабочем чате)"]
    found_any = False
    for rag_id in list(rag_ids)[:3]:
        card = store.find(rag_id)
        if not card or card.get("status") != rag_module.STATUS_ACTIVE:
            continue
        found_any = True
        name = card.get("name") or rag_id
        desc = (card.get("description") or "").strip()
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append(f"- id: {rag_id} — «{name}». {desc}")
    if not found_any:
        return ""
    lines.append(
        "Пользователь явно подключил эти базы в интерфейсе рабочего чата — "
        "используйте инструмент search_corpus с указанным rag_id, чтобы "
        "ответить на вопрос по их содержимому, не спрашивая разрешения."
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
