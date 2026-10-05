"""Агентный цикл: «модель ↔ инструменты», поток событий для SSE-стрима.

run_agent_loop — асинхронный генератор событий-словарей:
    {"type": "delta",       "text": ...}                       — дельта ответа;
    {"type": "tool_call",   "id","name","arguments"}           — модель вызывает инструмент;
    {"type": "approval_request", "id","name","arguments","timeout"}
                                                                — инструмент требует
                                                                  подтверждения человеком;
    {"type": "approval_result", "id","decision"}               — решение получено;
    {"type": "tool_result", "id","name","ok","result"}         — результат инструмента;
    {"type": "session",     "id","title"}                       — обновление сессии
                                                                  (умное именование);
    {"type": "done",        "finish"}                          — финальный ответ сохранён;
    {"type": "error",       "message"}                         — ошибка цикла/провайдера.

Транскрипт (сообщения пользователя, вызовы инструментов и результаты)
сохраняется в сессию после каждого шага — обрыв соединения не теряет историю.
Журнал решений (decision_log сессии) собирает подтверждения действий
и факт генерации заголовка — append-only аудит human-in-the-loop.
"""

import copy
import json
import time

from . import approvals as approval_broker
from . import skill_context
from . import session_title
from .cabinet import get_store as get_cabinet_store
from .prompts import approval_rules_block, build_system_prompt
from .usage import get_store as get_usage_store
from .usage_limits import get_store as get_usage_limits_store
from .user_llm import personal_llm

HISTORY_LIMIT = 24  # сколько последних сообщений сессии уходит в модель
TOOL_CONTENT_LIMIT = 20_000  # обрезка содержимого tool-сообщения для модели


def _find_lists(obj):
    """Собирает ссылки на все списки, встреченные при обходе вложенных
    словарей (списки внутри списков не разбираем — для ответов инструментов
    такой вложенности не бывает)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                if isinstance(v, list):
                    found.append(v)
                elif isinstance(v, dict):
                    walk(v)

    walk(obj)
    return found


def _safe_tool_content(result: dict, limit: int) -> str:
    """Сериализует результат инструмента в JSON, при превышении лимита
    обрезает по границе списков (на любой глубине вложенности), а не
    посимвольно, чтобы JSON оставался валидным и модель явно видела флаг
    truncated вместо оборванной на середине строки."""
    dumped = json.dumps(result, ensure_ascii=False)
    if len(dumped) <= limit:
        return dumped
    if not isinstance(result, dict):
        return dumped[:limit]
    trimmed = copy.deepcopy(result)
    trimmed["truncated"] = True
    lists = _find_lists(trimmed)
    while lists:
        dumped = json.dumps(trimmed, ensure_ascii=False)
        if len(dumped) <= limit:
            return dumped
        target = max(lists, key=len)
        if target:
            target.pop()
        else:
            lists.remove(target)
    dumped = json.dumps(trimmed, ensure_ascii=False)
    if len(dumped) > limit:
        fallback = {
            "ok": bool(result.get("ok", True)),
            "truncated": True,
            "error": "Результат слишком большой, чтобы передать модели целиком",
        }
        return json.dumps(fallback, ensure_ascii=False)
    return dumped
# оценка токенов, если провайдер не прислал usage: ~4 символа на токен
_CHARS_PER_TOKEN = 4


def _now() -> float:
    return time.time()


def _fmt_num(n: int) -> str:
    """Разбивка большого числа по разрядам (2 500 000) для сообщений пользователю."""
    return f"{n:,}".replace(",", "\u2009")


def _token_limit_block(user_id: str | None) -> str | None:
    """Сообщение об исчерпанном суточном лимите или None, если можно работать.

    Вызывается перед каждым шагом к модели: многошаговый ответ (модель ↔
    инструменты) останавливается, как только расход пересёк лимит, —
    начатое сообщение при этом не прерывается на середине стрима.

    Пользователь с личными настройками внешнего API работает по своему
    ключу и оплачивает токены сам — суточный лимит платформы к нему не
    применяется (расход по-прежнему учитывается для показа в меню).
    """
    if not user_id:
        return None
    if personal_llm(user_id):
        return None
    limit = get_usage_limits_store().effective_limit(user_id)
    used = get_usage_store().used_today(user_id)["total"]
    if used < limit:
        return None
    return (
        f"Суточный лимит токенов исчерпан: {_fmt_num(used)} из {_fmt_num(limit)}. "
        "Счётчик обнуляется в 00:00 по московскому времени; "
        "увеличить лимит может администратор платформы."
    )


def grant_pending_permissions(session: dict) -> bool:
    """Переводит запросы разрешений на чтение документов из pending в granted.

    Диалоговый механизм: инструмент read_document при попытке прочитать
    большой PDF помечает запрос pending и велит модели спросить пользователя.
    Следующее сообщение пользователя считается ответом на этот вопрос —
    интент ответа не разбираем (модель обязана уважать отказ), но без
    ответа пользователя сервер чтение не разрешит.
    """
    perms = session.get("doc_permissions")
    if not isinstance(perms, dict):
        return False
    changed = False
    for doc_id, state in list(perms.items()):
        if state == "pending":
            perms[doc_id] = "granted"
            changed = True
    return changed


def _estimate_tokens(messages: list, completion_text: str) -> tuple[int, int]:
    """Оценка токенов шага по длине текста (~4 символа на токен).

    Применяется, когда провайдер не вернул usage (event "usage" не пришёл).
    """
    prompt_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        prompt_chars += len(content) if isinstance(content, str) else 0
    return max(1, prompt_chars // _CHARS_PER_TOKEN), max(1, len(completion_text) // _CHARS_PER_TOKEN)


def _record_usage(user_id: str | None, prompt_tokens: int, completion_tokens: int) -> None:
    """Учёт токенов шага; сессии без учётной записи (user_id пуст) не пишем."""
    if user_id:
        get_usage_store().record(user_id, prompt_tokens, completion_tokens)


def _log_decision(session: dict, kind: str, **fields) -> None:
    """Дописывает событие в журнал решений сессии (append-only аудит).

    Сам не сохраняет: записи уезжают на диск ближайшим append_message/save.
    """
    entry = {"kind": kind, "ts": _now()}
    entry.update(fields)
    session.setdefault("decision_log", []).append(entry)


async def _maybe_rename_session(settings, provider, store, session, reply_text) -> dict | None:
    """Умное именование: после первого обмена заменяет автозаголовок
    (первые 80 символов сообщения) на короткий заголовок по смыслу.

    Один раз за сессию (факт фиксируется в журнале решений, даже если
    не вышло — ретраев на каждом сообщении не нужно). Возвращает новое
    событие session для клиента или None.
    """
    if any(e.get("kind") == "title_generated" for e in session.get("decision_log") or []):
        return None
    first_user = next(
        (m for m in session.get("messages", []) if m.get("role") == "user"), None
    )
    if first_user is None:
        return None
    auto_title = (first_user.get("content") or "")[:80]
    if session.get("title") != auto_title:
        # заголовок уже не автогенерированный — не трогаем
        _log_decision(session, "title_generated", title=session.get("title"), skipped=True)
        return None
    title, usage = await session_title.generate_title(
        provider, session.get("model"), first_user.get("content") or "", reply_text
    )
    if usage:
        _record_usage(
            session.get("user_id"),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
    if title:
        session["title"] = title[:80]
        _log_decision(session, "title_generated", title=title)
        store.save(session)
        return {"type": "session", "id": session["id"], "title": session["title"]}
    _log_decision(session, "title_generated", title=None)
    store.save(session)
    return None


async def run_agent_loop(*, settings, provider, registry, store, session,
                         user_text: str, user_attachments: list | None = None):
    model = session.get("model")
    user_msg = {"role": "user", "content": user_text, "ts": _now()}
    if user_attachments:
        # дескрипторы вложений — для отрисовки файлов в истории чата
        # (frontend/js/workspace.js); сам контекст уже в user_text
        user_msg["attachments"] = user_attachments
    store.append_message(session, user_msg)
    if grant_pending_permissions(session):
        store.save(session)

    from . import agent_router

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(agent_router.active_skill_ids(session), session.get("user_id")),
        }
    ]
    # правила подтверждений действий — только когда gate включён
    # (AIOS_APPROVAL_TOOLS): без него в промпте не должно быть карточек,
    # которые никогда не появятся
    if settings.approval_tools:
        messages[0]["content"] += "\n\n" + approval_rules_block()
    # блок «Кабинет»: анкетные данные и скрытый контекст о пользователе
    # (сессия привязана к учётной записи через Bearer-токен в /api/chat)
    user_block = get_cabinet_store().prompt_block(session.get("user_id"))
    if user_block:
        messages[0]["content"] += "\n\n" + user_block
    # RAG-базы, явно подключённые пользователем кнопкой в рабочем чате —
    # добавляется безусловно, без привязки к тексту вопроса
    rag_block = skill_context.connected_rag_block(session)
    if rag_block:
        messages[0]["content"] += "\n\n" + rag_block
    # модели коллектива, привязанные к агенту (model_ids): агент считает по ним
    models_block = skill_context.connected_models_block(session)
    if models_block:
        messages[0]["content"] += "\n\n" + models_block
    # каталог агентов: чат сам подбирает подходящего (use_agent), пока агент не выбран
    agents_block = agent_router.catalog_block(session)
    if agents_block:
        messages[0]["content"] += "\n\n" + agents_block
    # динамические блоки по упоминаниям в вопросе: «Об организации»,
    # «Проекты» (детекция и сборка — runtime/skill_context.py)
    context_block = skill_context.context_block(session, user_text)
    if context_block:
        messages[0]["content"] += "\n\n" + context_block
    messages.extend(session["messages"][-HISTORY_LIMIT:])

    for _ in range(settings.max_tool_iterations):
        limit_block = _token_limit_block(session.get("user_id"))
        if limit_block:
            yield {"type": "error", "message": limit_block}
            return

        final_text = ""
        tool_calls = []
        usage = None
        async for kind, payload in provider.stream_chat(messages, registry.specs(), model):
            if kind == "text":
                final_text += payload
                yield {"type": "delta", "text": payload}
            elif kind == "tool_calls":
                tool_calls = payload
            elif kind == "usage" and isinstance(payload, dict):
                usage = payload

        # учёт токенов шага: точный usage провайдера либо оценка по длине
        if usage:
            _record_usage(
                session.get("user_id"),
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
        else:
            prompt_t, completion_t = _estimate_tokens(messages, final_text)
            _record_usage(session.get("user_id"), prompt_t, completion_t)

        if not tool_calls:
            store.append_message(session, {"role": "assistant", "content": final_text, "ts": _now()})
            # умное именование: первый обмен завершён — назовём сессию по смыслу
            session_event = await _maybe_rename_session(
                settings, provider, store, session, final_text
            )
            if session_event:
                yield session_event
            yield {"type": "done", "finish": "stop"}
            return

        assistant_msg = {
            "role": "assistant",
            "content": final_text,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ],
            "ts": _now(),
        }
        store.append_message(session, assistant_msg)
        messages.append(assistant_msg)

        for call in tool_calls:
            yield {"type": "tool_call", "id": call.id, "name": call.name, "arguments": call.arguments}
            result = None
            if registry.requires_approval(call.name):
                # human-in-the-loop: карточка подтверждения в чате; запрос
                # пишем в сессию до ожидания (write-ahead) — перезапуск
                # сервиса в момент ожидания не потеряет факт запроса
                approval_broker.create(call.id, session["id"], call.name, call.arguments)
                _log_decision(session, "approval_request", id=call.id, name=call.name)
                store.save(session)
                yield {
                    "type": "approval_request",
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "timeout": settings.approval_timeout,
                }
                decision = await approval_broker.wait(call.id, settings.approval_timeout)
                _log_decision(
                    session, "approval_decision", id=call.id, name=call.name, decision=decision
                )
                yield {"type": "approval_result", "id": call.id, "decision": decision}
                if decision != approval_broker.APPROVE:
                    if decision == approval_broker.TIMEOUT:
                        error = "Пользователь не подтвердил действие (истёк срок ожидания)"
                    else:
                        error = "Пользователь отклонил действие"
                    result = {"ok": False, "error": error, "declined": True}
            if result is None:
                # ctx даёт инструментам состояние сессии (разрешения на чтение PDF)
                result = await registry.call(call.name, call.arguments, ctx={"session": session})
            tool_msg = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": _safe_tool_content(result, TOOL_CONTENT_LIMIT),
                "ts": _now(),
            }
            store.append_message(session, tool_msg)
            messages.append(tool_msg)
            yield {
                "type": "tool_result",
                "id": call.id,
                "name": call.name,
                "ok": bool(result.get("ok", True)),
                "result": result,
            }

    yield {
        "type": "error",
        "message": f"Достигнут лимит итераций с инструментами ({settings.max_tool_iterations}).",
    }
