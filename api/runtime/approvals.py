"""Брокер подтверждений действий агента человеком (human-in-the-loop).

Перед исполнением инструмента из списка требующих подтверждения цикл
останавливается и спрашивает пользователя: карточка в чате, решение —
кнопкой (POST /api/chat/approvals). Решение ждём ограниченное время
(AIOS_APPROVAL_TIMEOUT, по умолчанию 55 секунд); тишина считается отказом —
безопасная сторона по умолчанию.

Реестр живёт в памяти процесса: сервис работает одним воркером uvicorn
(как и кэш провайдеров в роутере чата), поэтому событие asyncio.Event
видно и циклу, и HTTP-обработчику решения. Перезапуск сервиса в момент
ожидания просто прерывает turn — транскрипт сессии уже сохранён.

Идентификатор запроса = id вызова инструмента (tool_call id): он уже
уникален в рамках turn'а и приходит в SSE-событии approval_request,
фронтенд возвращает его же в решении.
"""

import asyncio
import time

APPROVE = "approve"
DECLINE = "decline"
TIMEOUT = "declined_by_timeout"

# подчищаем решённые запросы не сразу: клиент может перечитать статус
# (и повторно нажать кнопку — получит честный 409, а не «не найдено»)
_RESOLVED_TTL = 600.0

_pending: dict[str, dict] = {}
_events: dict[str, asyncio.Event] = {}
# id → {"session_id", "decision", "ts"}
_resolved: dict[str, dict] = {}


def create(approval_id: str, session_id: str, name: str, arguments: dict) -> dict:
    """Регистрирует запрос подтверждения и возвращает его карточку."""
    _gc()
    record = {
        "id": approval_id,
        "session_id": session_id,
        "name": name,
        "arguments": arguments,
        "created_at": time.time(),
    }
    _pending[approval_id] = record
    _events[approval_id] = asyncio.Event()
    return record


def get(approval_id: str) -> dict | None:
    """Запрос по id: карточка ожидания или итоговое решение (для перечитывания)."""
    _gc()
    record = _pending.get(approval_id)
    if record is not None:
        return dict(record)
    resolved = _resolved.get(approval_id)
    if resolved:
        return {
            "id": approval_id,
            "session_id": resolved["session_id"],
            "decision": resolved["decision"],
        }
    return None


def resolve(approval_id: str, decision: str) -> bool:
    """Записывает решение пользователя; False — запрос не найден/уже решён."""
    if decision not in (APPROVE, DECLINE):
        return False
    record = _pending.get(approval_id)
    if record is None:
        return False
    _settle(approval_id, record, decision)
    return True


async def wait(approval_id: str, timeout: float) -> str:
    """Ждёт решение; approve/decline, по таймауту — declined_by_timeout."""
    record = _pending.get(approval_id)
    if record is None:
        resolved = _resolved.get(approval_id)
        return resolved["decision"] if resolved else TIMEOUT
    event = _events.get(approval_id)
    if event is None:
        return TIMEOUT
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # тишина = отказ; запрос снимаем, чтобы поздний клик не «оживил» turn
        if _pending.get(approval_id) is record:
            _settle(approval_id, record, TIMEOUT)
        return TIMEOUT
    resolved = _resolved.get(approval_id)
    return resolved["decision"] if resolved else DECLINE


def _settle(approval_id: str, record: dict, decision: str) -> None:
    del _pending[approval_id]
    _resolved[approval_id] = {
        "session_id": record.get("session_id"),
        "decision": decision,
        "ts": time.time(),
    }
    event = _events.get(approval_id)
    if event is not None:
        event.set()


def _gc() -> None:
    """Убирает решённые запросы старше TTL, чтобы реестр не рос."""
    now = time.time()
    stale = [k for k, r in _resolved.items() if now - r["ts"] > _RESOLVED_TTL]
    for key in stale:
        _resolved.pop(key, None)
        _events.pop(key, None)
