"""Простой in-memory rate-limit (скользящее окно) для публичных эндпоинтов.

Защита от массовых бот-регистраций: на каждое обращение к /api/auth/register
уходит письмо, а с несуществующих адресов оно возвращается bounce'ом на наш
ящик. Ограничение действует на процесс (без общего хранилища) — для одной
инстанции uvicorn этого достаточно.
"""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


class RateLimited(Exception):
    """Лимит обращений превышен; .retry_after — через сколько секунд можно снова."""


def check(key: str, limit: int, window: float = 3600.0) -> None:
    """Регистрирует обращение и поднимает RateLimited, если лимит исчерпан."""
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            retry = int(window - (now - hits[0])) + 1
            raise RateLimited(f"Слишком много попыток — попробуйте через {retry} c")
        hits.append(now)


def client_ip(request, forwarded_for: str | None) -> str:
    """IP клиента: первый адрес из X-Forwarded-For (за прокси) или peer."""
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"