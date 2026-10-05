"""Умное именование сессии: короткий заголовок по смыслу первого обмена.

По умолчанию заголовок сессии — первые 80 символов первого сообщения.
После первого завершённого обмена цикл один раз просит модель назвать
диалог по смыслу (один дешёвый запрос без инструментов) и обновляет
заголовок; ошибка или пустой ответ молча оставляют прежний заголовок —
нейминг не должен ломать чат.
"""

MAX_TITLE_LEN = 60

_SYSTEM_PROMPT = (
    "Ты называешь диалоги ассистента платформы AIOS. Сформулируй короткий "
    "заголовок диалога по смыслу: не больше 60 символов, без кавычек, "
    "без точки в конце, по-русски. Верни только заголовок, без пояснений."
)


def _clean(raw: str) -> str:
    """Нормализует ответ модели: без кавычек/переводов строк, однострочный."""
    text = (raw or "").replace("\n", " ").strip()
    text = text.strip("«»\"'`").strip()
    text = " ".join(text.split())
    if len(text) > MAX_TITLE_LEN:
        cut = text[:MAX_TITLE_LEN]
        # режем по границе слова, а не посреди
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,.-—:")
    return text


async def generate_title(provider, model: str | None, user_text: str, reply: str) -> tuple[str | None, dict | None]:
    """Заголовок по первому сообщению и первому ответу; (None, None) — не вышло.

    Возвращает (title, usage): usage — точный расход токенов, если провайдер
    его прислал (учитывается вызывающим кодом).
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Сообщение пользователя:\n{user_text[:2000]}\n\n"
                f"Ответ ассистента (начало):\n{reply[:2000]}\n\n"
                "Заголовок диалога:"
            ),
        },
    ]
    parts: list[str] = []
    usage = None
    try:
        async for kind, payload in provider.stream_chat(messages, [], model):
            if kind == "text":
                parts.append(payload)
            elif kind == "usage" and isinstance(payload, dict):
                usage = payload
            elif kind == "tool_calls":
                # нейминг без инструментов; маловероятный tool-call игнорируем
                continue
    except Exception:  # noqa: BLE001 — нейминг не должен ломать turn
        return None, None
    title = _clean("".join(parts))
    return (title or None), usage
