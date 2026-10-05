"""Умное именование сессий: генерация заголовка по первому обмену."""

import asyncio

from api.runtime import session_title
from api.runtime.config import get_settings
from api.runtime.loop import run_agent_loop
from api.runtime.providers import ToolCall
from api.runtime.sessions import SessionStore
from api.runtime.tools import build_default_registry


class PerCallProvider:
    """Сценарий по вызовам: каждый элемент — список событий одного обращения
    к модели. Первый вызов цикла — ответ ассистента, второй — заголовок
    (цикл спрашивает название уже после завершения первого обмена)."""

    def __init__(self, calls_events):
        self.calls_events = [list(events) for events in calls_events]
        self.calls = 0

    async def stream_chat(self, messages, tools=None, model=None):
        self.calls += 1
        events = self.calls_events.pop(0) if self.calls_events else [("text", "")]
        for kind, payload in events:
            yield (kind, payload)


class FailingProvider:
    async def stream_chat(self, messages, tools=None, model=None):
        raise RuntimeError("нет связи с моделью")
        yield  # pragma: no cover — делает функцию генератором


def test_clean_normalizes_model_answer():
    assert session_title._clean('  «Подбор катализатора»  \n') == "Подбор катализатора"
    long = "слово " * 30
    cleaned = session_title._clean(long)
    assert len(cleaned) <= session_title.MAX_TITLE_LEN
    assert session_title._clean("   ") == ""


def test_generate_title_with_usage():
    provider = PerCallProvider(
        [[("text", "«Расчёт "), ("text", "реактора»"), ("usage", {"prompt_tokens": 10, "completion_tokens": 5})]]
    )
    title, usage = asyncio.run(session_title.generate_title(provider, "m", "вопрос", "ответ"))
    assert title == "Расчёт реактора"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5}


def test_generate_title_swallows_provider_errors():
    title, usage = asyncio.run(session_title.generate_title(FailingProvider(), "m", "в", "о"))
    assert title is None and usage is None


def test_loop_renames_session_once(tmp_path):
    """После первого обмена автозаголовок заменяется сгенерированным,
    факт фиксируется в журнале решений, клиенту уходит событие session;
    второй обмен сессию уже не переименовывает."""

    question = "подобери источники по ALD-процессам, пожалуйста"

    async def run():
        provider = PerCallProvider(
            [
                [("text", "Подобрал три источника по ALD.")],
                [("text", '  «Обзор ALD-процессов»  ')],
            ]
        )
        store = SessionStore(tmp_path / "sessions")
        registry = build_default_registry(tmp_path / "tools")
        session = store.create(title=question[:80])
        first_events = []
        async for event in run_agent_loop(
            settings=get_settings(), provider=provider, registry=registry,
            store=store, session=session, user_text=question,
        ):
            first_events.append(event)
        return provider, store, session, first_events

    provider, store, session, first_events = asyncio.run(run())
    assert session["title"] == "Обзор ALD-процессов"
    session_events = [e for e in first_events if e.get("type") == "session"]
    assert session_events and session_events[-1]["title"] == "Обзор ALD-процессов"
    title_logs = [e for e in session["decision_log"] if e.get("kind") == "title_generated"]
    assert len(title_logs) == 1

    # второй обмен: нейминг уже не вызывается — заголовок стабилен
    async def second():
        provider2 = PerCallProvider([[("text", "Дополнительно: ещё два источника.")]])
        events = []
        async for event in run_agent_loop(
            settings=get_settings(), provider=provider2,
            registry=build_default_registry(tmp_path / "tools"),
            store=store, session=session,
            user_text="ещё источники есть?",
        ):
            events.append(event)
        return events

    asyncio.run(second())
    saved = store.get(session["id"])
    assert saved["title"] == "Обзор ALD-процессов"  # не изменился
    assert len([e for e in saved["decision_log"] if e["kind"] == "title_generated"]) == 1


def test_loop_keeps_custom_title(tmp_path):
    """Заголовок, отличный от автогенерированного префикса, нейминг не трогает."""

    async def run():
        provider = PerCallProvider(
            [[("text", "Ответ.")], [("text", "Не должен примениться")]]
        )
        store = SessionStore(tmp_path / "sessions")
        registry = build_default_registry(tmp_path / "tools")
        session = store.create(title="Моя важная сессия")
        async for _ in run_agent_loop(
            settings=get_settings(), provider=provider, registry=registry,
            store=store, session=session, user_text="вопрос",
        ):
            pass
        return session

    session = asyncio.run(run())
    assert session["title"] == "Моя важная сессия"
