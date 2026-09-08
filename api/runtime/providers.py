"""LLM-провайдеры агентного цикла.

Единый потоковый интерфейс — stream_chat(messages, tools, model) — асинхронный
генератор событий:
    ("text", str)              — дельта текста ответа;
    ("tool_calls", [ToolCall]) — накопленные вызовы инструментов (в конце стрима).

OpenAICompatProvider работает с любым OpenAI-совместимым эндпоинтом
(OpenAI, OpenRouter, vLLM, Ollama /v1, proxyapi.ru и т.п.). Конфигурация
берётся из llm_settings (настройки администратора в админ-панели, запасной
вариант — .env). MockProvider используется только в тестах: без настроенной
LLM /api/chat отвечает 503, а не mock-ответами.
"""

import json
from dataclasses import dataclass

import httpx

from .llm_settings import LLMConfig


class ProviderError(RuntimeError):
    """Ошибка транспорта или ответа провайдера — уходит клиенту событием error."""


@dataclass
class ToolCall:
    """Вызов инструмента, запрошенный моделью."""

    id: str
    name: str
    arguments: dict


class OpenAICompatProvider:
    """Стриминговый клиент chat/completions OpenAI-совместимого API."""

    def __init__(self, cfg: LLMConfig, read_timeout: float = 120.0) -> None:
        self.base_url = cfg.base_url
        self.api_key = cfg.api_key
        self.default_model = cfg.model
        self.timeout = httpx.Timeout(
            connect=10.0,
            read=read_timeout,
            write=30.0,
            pool=10.0,
        )

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None):
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        calls: dict = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise ProviderError(f"LLM HTTP {resp.status_code}: {body[:400]}")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield ("text", piece)
                        for tc in delta.get("tool_calls") or []:
                            index = tc.get("index", 0)
                            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ошибка транспорта LLM: {exc}") from exc

        if calls:
            parsed = []
            for index in sorted(calls):
                slot = calls[index]
                try:
                    arguments = json.loads(slot["arguments"]) if slot["arguments"] else {}
                except ValueError:
                    arguments = {"_raw": slot["arguments"]}
                parsed.append(
                    ToolCall(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"],
                        arguments=arguments if isinstance(arguments, dict) else {"_raw": arguments},
                    )
                )
            yield ("tool_calls", parsed)


class MockProvider:
    """Детерминированный офлайн-провайдер: демонстрирует цикл с инструментами.

    Поведение:
    • запрос с триггерами поиска → вызывает search_knowledge;
    • после результата инструмента → короткая сводка найденного;
    • иначе → подсказка, как подключить настоящую модель через .env.
    """

    _TRIGGERS = ("найди", "поищи", "поиск", "знан", "онтол", "подбери", "база")

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None):
        user_text = ""
        tool_seen = False
        for msg in messages:
            role = msg.get("role")
            if role == "user":
                user_text = msg.get("content") or ""
            elif role == "tool":
                tool_seen = True

        lowered = user_text.lower()
        if tools and not tool_seen and any(t in lowered for t in self._TRIGGERS):
            yield (
                "tool_calls",
                [ToolCall(id="mock_call_1", name="search_knowledge", arguments={"query": user_text[:120]})],
            )
            return

        if tool_seen:
            summary = self._summarize_tool_result(messages)
            if summary:
                yield ("text", summary)
                return

        yield ("text", self._fallback(user_text))

    @staticmethod
    def _summarize_tool_result(messages: list) -> str:
        for msg in reversed(messages):
            if msg.get("role") != "tool":
                continue
            try:
                data = json.loads(msg.get("content") or "{}")
            except ValueError:
                return ""
            names = [r.get("name") for r in data.get("results", []) if isinstance(r, dict)]
            if not names:
                return "По базам знаний совпадений нет. Попробуйте переформулировать запрос. (mock-провайдер)"
            return "Нашёл релевантные источники: " + ", ".join(filter(None, names[:5])) + ". Подробности — в карточках инструментов. (mock-провайдер)"
        return ""

    @staticmethod
    def _fallback(user_text: str) -> str:
        lines = [
            "Это mock-провайдер AIOS Harness: рантайм, сессии и инструменты работают, но настоящая LLM не подключена.",
            f"Сообщение получено: «{user_text[:200]}».",
            "",
            "Чтобы подключить модель, заполните .env в корне проекта:",
            "  AIOS_LLM_BASE_URL=https://api.openai.com/v1",
            "  AIOS_LLM_API_KEY=<ключ>",
            "  AIOS_LLM_MODEL=<имя модели>",
            "",
            "и перезапустите uvicorn. А пока попробуйте фразу «найди знания про катализ» — увидите живой вызов инструмента.",
        ]
        return "\n".join(lines)


def build_provider(cfg: LLMConfig, read_timeout: float = 120.0):
    """Провайдер из действующей конфигурации; None — LLM не настроена.

    MockProvider в рантайме не используется (только в тестах): без настроек
    /api/chat отвечает 503, чтобы пользователи не получали mock-ответы.
    """
    if cfg.base_url and cfg.model:
        return OpenAICompatProvider(cfg, read_timeout=read_timeout)
    return None
