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

import asyncio
import json
from dataclasses import dataclass

import httpx

from .llm_settings import LLMConfig

# Временные ошибки ретраятся: 1 начальная попытка + до 3 повторов с задержками.
RETRY_DELAYS = (1.0, 2.0, 4.0)
# 400 ретраится только для upstream-ошибок шлюза; остальные 4xx — на нашей стороне.
_TRANSIENT_STATUSES = {408, 429}


class ProviderError(RuntimeError):
    """Ошибка транспорта или ответа провайдера — уходит клиенту событием error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def transient(self) -> bool:
        """Временная ли ошибка: стоит ли автоматически повторить запрос."""
        if self.status_code is None:
            return True  # таймаут или обрыв транспорта
        if self.status_code in _TRANSIENT_STATUSES or self.status_code >= 500:
            return True
        if self.status_code == 400:
            return "upstream provider error" in str(self).lower()
        return False


class FallbackProvider:
    """Личный провайдер пользователя с откатом к общим настройкам платформы.

    Пока личный endpoint не выдал ни одного события стрима, его сбой
    (не отвечает, неверный ключ, нет такой модели) переключает ответ на
    общую конфигурацию: пользователь всё равно получает результат, а в
    начале ответа — пометка, что отвечено через общие настройки. После
    первого события откат невозможен: обрыв середины ответа провайдер
    отрабатывает сам (ретраи только до первой выдачи), ошибка уходит
    клиенту как есть.
    """

    def __init__(self, personal, fallback) -> None:
        self._personal = personal
        self._fallback = fallback

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None,
                          max_tokens: int | None = None):
        emitted = False
        try:
            async for event in self._personal.stream_chat(messages, tools, model, max_tokens):
                emitted = True
                yield event
            return
        except ProviderError as exc:
            # откат только пока личный endpoint не выдал ничего: сбой посреди
            # ответа — на совести личного провайдера (задублировать начатое
            # нельзя, продолжать общим — тоже)
            if emitted or self._fallback is None:
                raise
            reason = str(exc)
            yield (
                "text",
                "⚠ Личный внешний API недоступен (" + reason[:160] + ") — "
                "ответ через общие настройки платформы.\n\n",
            )
        async for event in self._fallback.stream_chat(messages, tools, model, max_tokens):
            yield event


@dataclass
class ToolCall:
    """Вызов инструмента, запрошенный моделью."""

    id: str
    name: str
    arguments: dict


class OpenAICompatProvider:
    """Стриминговый клиент chat/completions OpenAI-совместимого API."""

    def __init__(
        self,
        cfg: LLMConfig,
        read_timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = cfg.base_url
        self.api_key = cfg.api_key
        self.default_model = cfg.model
        self._transport = transport  # для тестов (httpx.MockTransport)
        self.timeout = httpx.Timeout(
            connect=10.0,
            read=read_timeout,
            write=30.0,
            pool=10.0,
        )

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None,
                           max_tokens: int | None = None):
        """stream_chat с ретраями: до 3 повторов временных ошибок (1/2/4 сек).

        Повтор возможен только пока стрим ещё ничего не выдал клиенту —
        иначе частичный ответ задублировался бы. Пользователь видит ошибку
        лишь после того, как все повторы исчерпаны.

        События: ("text", …), ("tool_calls", …) и ("usage", {prompt_tokens,
        completion_tokens, …}) — usage приходит последним, если провайдер
        поддерживает stream_options.include_usage; иначе его оценивает цикл.
        Строгие провайдеры, не знающие stream_options, отвечают 400 — тогда
        одна повторная попытка без этого поля.
        """
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            produced = False
            try:
                try:
                    async for event in self._stream_once(messages, tools, model, include_usage=True, max_tokens=max_tokens):
                        produced = True
                        yield event
                except ProviderError as exc:
                    if produced or exc.status_code != 400 or "stream_options" not in str(exc).lower():
                        raise
                    async for event in self._stream_once(messages, tools, model, include_usage=False, max_tokens=max_tokens):
                        produced = True
                        yield event
                return
            except ProviderError as exc:
                if produced or not exc.transient or attempt == len(RETRY_DELAYS):
                    raise

    async def _stream_once(self, messages: list, tools: list | None = None, model: str | None = None,
                           include_usage: bool = True, max_tokens: int | None = None):
        """Одна попытка стрима chat/completions без ретраев."""
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        calls: dict = {}
        usage: dict | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise ProviderError(
                            f"LLM HTTP {resp.status_code}: {body[:400]}", status_code=resp.status_code
                        )
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
                        # финальный chunk с usage может прийти с пустым choices
                        usage_chunk = chunk.get("usage")
                        if isinstance(usage_chunk, dict) and usage_chunk:
                            usage = usage_chunk
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
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"Модель не ответила за {int(self.timeout.read)} сек — превышен таймаут "
                "чтения. Соединение с LLM нестабильно или модель перегружена; "
                "попробуйте повторить запрос позже."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ошибка транспорта LLM: {exc}") from exc

        if usage:
            yield ("usage", usage)

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

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None,
                           max_tokens: int | None = None):
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
