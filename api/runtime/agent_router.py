"""Автоподбор агента в чате: каталог опубликованных агентов в промпте и инструмент use_agent.

Автоподобранный агент хранится в session["auto_skill_ids"], а не в session["skill_ids"]:
skill_ids сравниваются с запросом браузера и при расхождении начинают новую сессию.
"""
from __future__ import annotations

from . import datarepo
from .users import is_admin_user

MAX_CATALOG = 60
_DESC_LIMIT = 170


def active_skill_ids(session: dict | None) -> list:
    """Выбранные вручную агенты плюс автоподобранный (без повторов)."""
    session = session or {}
    out = list(session.get("skill_ids") or [])
    for sid in session.get("auto_skill_ids") or []:
        if sid not in out:
            out.append(sid)
    return out


def catalog_block(session: dict | None) -> str:
    """Список агентов для промпта. Пусто, если агент уже выбран (вручную или
    автоматически). Admin-only агенты видны в подборе только роли
    «Администратор» (роль по user_id сессии)."""
    session = session or {}
    if session.get("skill_ids") or session.get("auto_skill_ids"):
        return ""
    is_admin = is_admin_user(session.get("user_id"))
    lines = []
    for card in datarepo.skills():
        if datarepo.is_admin_only(card) and not is_admin:
            continue
        sid = card.get("id")
        if not sid:
            continue
        desc = str(card.get("description_ru") or "").strip().replace("\n", " ")
        if len(desc) > _DESC_LIMIT:
            desc = desc[:_DESC_LIMIT] + "…"
        lines.append(f"- {sid} — «{card.get('name') or sid}». {desc}")
        if len(lines) >= MAX_CATALOG:
            break
    if not lines:
        return ""
    return (
        "### Агенты платформы\n"
        "Агент — готовый сценарий работы (роль, шаги, инструменты) для определённого типа задач. "
        "Если запрос пользователя явно совпадает с задачей одного из агентов ниже, вызовите "
        "use_agent с его id и дальше работайте по его инструкциям, сообщив пользователю одной "
        "фразой, какой агент подключён. Если ни один не подходит или сомневаетесь, отвечайте "
        "обычно и ничего не подключайте. Не подключайте агента к обычным вопросам и беседе.\n"
        + "\n".join(lines)
    )


async def use_agent(args: dict, ctx: dict | None) -> dict:
    session = (ctx or {}).get("session")
    if not isinstance(session, dict):
        return {"ok": False, "error": "нет сессии чата"}
    if session.get("skill_ids"):
        return {"ok": False, "error": "пользователь уже выбрал агента вручную, менять его нельзя"}
    agent_id = str((args or {}).get("agent_id") or "").strip()
    card = datarepo.get_skill(agent_id) if agent_id else None
    if not card or (datarepo.is_admin_only(card) and not is_admin_user(session.get("user_id"))):
        # admin-only агент для не-админа неотличим от несуществующего
        return {"ok": False, "error": f"агент {agent_id or '(пусто)'} не найден среди опубликованных"}
    try:
        from .prompts import SKILL_BODY_LIMIT as limit
    except Exception:  # noqa: BLE001
        limit = 12000
    body = datarepo.skill_body(card, limit=limit) or card.get("description_ru") or ""
    session["auto_skill_ids"] = [card.get("id", agent_id)]
    return {
        "ok": True,
        "агент": card.get("name") or agent_id,
        "id": card.get("id", agent_id),
        "как_работать": "С этого момента действуйте по инструкциям агента ниже; со следующего сообщения "
                        "они войдут в системный промпт сессии.",
        "инструкции": body,
    }
