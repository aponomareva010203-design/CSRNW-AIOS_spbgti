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

from .prompts import build_system_prompt

HISTORY_LIMIT = 24  # сколько последних сообщений сессии уходит в модель
TOOL_CONTENT_LIMIT = 6_000  # обрезка содержимого tool-сообщения для модели


def _now() -> float:
    return time.time()


async def run_agent_loop(*, settings, provider, registry, store, session, user_text: str):
    model = session.get("model")
    store.append_message(session, {"role": "user", "content": user_text, "ts": _now()})

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(session.get("agent_id"), session.get("skill_ids")),
        }
    ]
    messages.extend(session["messages"][-HISTORY_LIMIT:])

    for _ in range(settings.max_tool_iterations):
        final_text = ""
        tool_calls = []
        async for kind, payload in provider.stream_chat(messages, registry.specs(), model):
            if kind == "text":
                final_text += payload
                yield {"type": "delta", "text": payload}
            elif kind == "tool_calls":
                tool_calls = payload

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
            result = await registry.call(call.name, call.arguments)
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
