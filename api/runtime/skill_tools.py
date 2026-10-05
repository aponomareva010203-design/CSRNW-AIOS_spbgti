"""Инструменты конструктора агентов: черновики агентов (api/runtime/skill_tools.py).

Агент платформы — текстовый сценарий (.md), который любой сотрудник запускает
в чате. Модель НЕ пишет файл целиком, а передаёт короткие поля; .md собирает и
проверяет skill_drafts. Черновик приватный (кабинет → раздел «Агенты»),
публикует человек кнопкой, а не модель.

Подключаются в tools.build_default_registry последними, чтобы видеть полный
список имён инструментов сервера (агент не может ссылаться на отсутствующий).
"""
from __future__ import annotations

from typing import Callable

from . import skill_drafts
from .tools import Tool

_SKILL_SCHEMA = {
    "type": "object",
    "description": "Поля агента. Все тексты по-русски, короткие; файл соберёт платформа.",
    "properties": {
        "id": {"type": "string", "description": "латиница в нижнем регистре через дефис, 3–48 символов, например literature-radar"},
        "name": {"type": "string", "description": "русское название агента"},
        "description": {"type": "string", "description": "одно предложение для карточки каталога"},
        "domain": {"type": "string", "description": "предметная область одной фразой"},
        "triggers": {"type": "array", "items": {"type": "string"},
                     "description": "3–6 основ слов из предметной задачи, каждая от 4 символов; не повторяйте основы других агентов"},
        "role": {"type": "string", "description": "продолжение фразы «ты – АГЕНТ, который …»"},
        "task": {"type": "string", "description": "продолжение фразы «твоя задача – …»: что пользователь получает на выходе"},
        "steps": {"type": "array", "description": "порядок работы, один вопрос пользователю на шаг",
                  "items": {"type": "object", "properties": {
                      "title": {"type": "string"}, "text": {"type": "string"},
                      "choices": {"type": "array", "items": {"type": "string"},
                                  "description": "необязательно: варианты быстрых ответов (2–8), по одному на строку"}},
                      "required": ["title", "text"]}},
        "tools": {"type": "array", "description": "только инструменты, работающие в чате (call_agent пока не работает); если у агента есть база знаний, добавьте search_corpus",
                  "items": {"type": "object", "properties": {"name": {"type": "string"}, "when": {"type": "string"}},
                            "required": ["name", "when"]}},
        "rules": {"type": "array", "items": {"type": "string"}, "description": "ограничения и правила работы, по одному на строку"},
        "final": {"type": "string", "description": "что пользователь получил и что предложить дальше"},
        "rag_id": {"type": "string", "description": "необязательно: id выбранной пользователем RAG-базы (rag-…), в которой агент будет искать ответы; пусто, если база не подключается"},
        "model_ids": {"type": "array", "items": {"type": "string"}, "description": "необязательно: до трёх моделей коллектива (mdl-…), по которым агент считает; добавьте агенту run_model (и optimize_model для подбора режима)"},
    },
    "required": ["id", "name", "description", "domain", "triggers", "role", "task", "steps", "tools", "final"],
}


def _session_user(ctx: dict | None) -> str:
    return ((ctx or {}).get("session") or {}).get("user_id") or ""


def skill_tools(names_provider: Callable[[], set]) -> list[Tool]:
    from . import agent_router as _agent_router

    async def get_skill_drafts(args: dict, ctx: dict | None) -> dict:
        user_id = _session_user(ctx)
        if not user_id:
            return {"ok": False, "error": "Нужен вход в AIOS: черновики агентов лежат в вашем кабинете."}
        store = skill_drafts.get_store()
        skill_id = str((args or {}).get("id") or "").strip().lower()
        if skill_id:
            item = store.get(user_id, skill_id)
            if not item:
                return {"ok": False, "error": f"Черновик {skill_id} не найден"}
            return {"ok": True, "черновик": item["meta"], "текст": item["text"]}
        keys = ("id", "name", "status", "version", "dirty", "source_agent_id")
        return {"ok": True, "черновики": [{k: m.get(k) for k in keys} for m in store.list(user_id)]}

    async def save_skill_draft(args: dict, ctx: dict | None) -> dict:
        user_id = _session_user(ctx)
        if not user_id:
            return {"ok": False, "error": "Нужен вход в AIOS: черновик сохраняется в ваш кабинет."}
        raw = (args or {}).get("skill")
        spec = skill_drafts.normalize_spec(raw if isinstance(raw, dict) else args)
        if spec.get("rag_id"):
            reason = skill_drafts.rag_check(spec["rag_id"], user_id)
            if reason:
                return {"ok": False, "errors": [reason]}
        reasons = [r for r in (skill_drafts.model_check(m, user_id) for m in spec.get("model_ids") or []) if r]
        if reasons:
            return {"ok": False, "errors": reasons}
        store = skill_drafts.get_store()
        existing = store.get(user_id, spec["id"]) if spec["id"] else None
        available = set(names_provider())
        errors = skill_drafts.validate_spec(spec, available, own_published=bool(existing and existing["meta"].get("status") == "published"))
        if errors:
            return {"ok": False, "errors": errors,
                    "подсказка": "Исправьте перечисленное и вызовите инструмент снова со всеми полями; сообщите человеку о правках простыми словами."}
        text = skill_drafts.render_md(spec)
        problems = skill_drafts.validate_md(text, available, own_id=spec["id"])
        if problems:
            return {"ok": False, "errors": problems}
        meta = store.save(user_id, spec["id"], text, name=spec["name"], description=spec["description"],
                          domain=spec["domain"], source_agent_id=spec["agent_id"])
        return {"ok": True, "id": meta["id"], "запуск_для_проверки": f"draft:{meta['id']}", "версия": meta["version"],
                "где": "Кабинет → Агенты", "статус": "черновик: публикует человек кнопкой в кабинете",
                "база_знаний": spec.get("rag_id") or None, "модели": spec.get("model_ids") or None}

    return [
        Tool(
            name="get_skill_drafts",
            description="Личные черновики агентов пользователя. Без параметров — список (id, название, статус); "
            "с id — полный текст черновика для доработки. Вызывайте перед созданием нового агента, чтобы "
            "предложить продолжить прежний черновик.",
            parameters={"type": "object", "properties": {"id": {"type": "string", "description": "id черновика (необязательно)"}}},
            handler=get_skill_drafts,
        ),
        Tool(
            name="save_skill_draft",
            description="Создаёт или обновляет черновик агента (upsert по id). Передавайте "
            "КОРОТКИЕ поля, а не файл: .md соберёт и проверит платформа. Черновик приватный, виден только автору "
            "в кабинете (раздел «Агенты»); публикует человек кнопкой. Агенту нельзя давать call_agent (в чате он пока не работает), а также писать в тексте привязку к "
            "проекту, имена и почты. Если пользователь выбрал базу знаний, укажите её rag_id и добавьте search_corpus. "
            "При ok:false в errors — что исправить; исправьте и вызовите снова.",
            parameters={"type": "object", "properties": {"skill": _SKILL_SCHEMA}, "required": ["skill"]},
            handler=save_skill_draft,
        ),
        Tool(
            name="use_agent",
            description="Подключает к чату агента платформы (id из списка «Агенты платформы» в промпте), "
            "когда запрос пользователя явно совпадает с его задачей. Возвращает инструкции агента: "
            "дальше работайте по ним. Не вызывайте для обычных вопросов и если пользователь уже выбрал агента.",
            parameters={"type": "object", "properties": {"agent_id": {"type": "string", "description": "id агента из списка"}},
                        "required": ["agent_id"]},
            handler=_agent_router.use_agent,
        ),
    ]
