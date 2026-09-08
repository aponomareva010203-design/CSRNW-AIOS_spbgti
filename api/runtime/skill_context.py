"""Динамический контекст навыков для системного промпта (skill_context.py).

Блок «Кабинет» (анкета + скрытый контекст) уже добавляется в loop.py
безусловно. Здесь — упоминательнозначимые блоки, которые подмешиваются
в промпт только когда последний вопрос пользователя их касается:

    «Об организации» — при упоминании организации/показателей/документов
                       (публичные данные, читаются и анонимом);
    «Мои проекты»    — при упоминании проектов/карточек (только для
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
    lines = ["### Карточки проектов пользователя («Мои проекты»)"]
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
