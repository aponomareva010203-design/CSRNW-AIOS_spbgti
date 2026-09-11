"""Агентный цикл: «модель ↔ инструменты», поток событий для SSE-стрима.

run_agent_loop — асинхронный генератор событий-словарей:
    {"type": "delta",       "text": ...}                       — дельта ответа;
    {"type": "tool_call",   "id","name","arguments"}           — модель вызывает инструмент;
    {"type": "tool_result", "id","name","ok","result"}         — результат инструмента;
    {"type": "done",        "finish"}                          — финальный ответ сохранён;
    {"type": "error",       "message"}                         — ошибка цикла/провайдера.

Транскрипт (сообщения пользователя, вызовы инструментов и результаты)
сохраняется в сессию после каждого шага — обрыв соединения не теряет историю.
"""

import json
import time

from . import skill_context
from .cabinet import get_store as get_cabinet_store
from .prompts import build_system_prompt
from .usage import get_store as get_usage_store

HISTORY_LIMIT = 24  # сколько последних сообщений сессии уходит в модель
TOOL_CONTENT_LIMIT = 6_000  # обрезка содержимого tool-сообщения для модели
# оценка токенов, если провайдер не прислал usage: ~4 символа на токен
_CHARS_PER_TOKEN = 4


def _now() -> float:
    return time.time()


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

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(session.get("agent_id"), session.get("skill_ids")),
        }
    ]
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
    # динамические блоки по упоминаниям в вопросе: «Об организации»,
    # «Проекты» (детекция и сборка — runtime/skill_context.py)
    context_block = skill_context.context_block(session, user_text)
    if context_block:
        messages[0]["content"] += "\n\n" + context_block
    messages.extend(session["messages"][-HISTORY_LIMIT:])

    for _ in range(settings.max_tool_iterations):
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
            # ctx даёт инструментам состояние сессии (разрешения на чтение PDF)
            result = await registry.call(call.name, call.arguments, ctx={"session": session})
            tool_msg = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": json.dumps(result, ensure_ascii=False)[:TOOL_CONTENT_LIMIT],
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
