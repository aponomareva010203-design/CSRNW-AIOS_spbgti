"""Клиент эмбеддингов через OpenAI-совместимый API (api/runtime/embeddings.py).

Использует ту же конфигурацию, что и LLM (base_url/api_key из llm_settings.py,
администратор задаёт её один раз в админ-панели «Настройки внешних API») —
эмбеддинги идут через тот же аккаунт (ProxyAPI.ru или любой другой
OpenAI-совместимый эндпоинт), просто с другим именем модели
(rag.EMBEDDING_MODEL). Отдельного хранилища ключей не заводим: одна точка
настройки на весь рантайм, как и для чата.

Используется индексатором RAG (чанк → вектор → запись в Qdrant) и retrieval
на этапе поиска (инструмент search_corpus, будет добавлен отдельно).
"""

import httpx

from .llm_settings import effective_config
from .rag import EMBEDDING_MODEL


class EmbeddingError(RuntimeError):
    """Ошибка получения эмбеддинга через внешний API."""


async def embed_texts(
    texts: list,
    model: str | None = None,
    timeout: float = 30.0,
) -> list:
    """Векторы для списка текстов; порядок в ответе соответствует входу.

    Поднимает EmbeddingError, если внешний API не настроен или вернул
    ошибку — вызывающий код (индексатор, search_corpus) должен трактовать
    это как временную недоступность сервиса, а не как ошибку в данных.
    """
    if not texts:
        return []

    cfg = effective_config()
    if not cfg.base_url:
        raise EmbeddingError(
            "Эмбеддинги недоступны: в админ-панели не настроен внешний API "
            "(тот же эндпоинт, что используется для рабочего чата)."
        )

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    payload = {"model": model or EMBEDDING_MODEL, "input": texts}

    request_timeout = httpx.Timeout(
        connect=10.0, read=timeout, write=30.0, pool=10.0
    )
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            resp = await client.post(
                f"{cfg.base_url}/embeddings", headers=headers, json=payload
            )
    except httpx.TimeoutException as exc:
        raise EmbeddingError(
            f"Сервис эмбеддингов не ответил за {int(timeout)} сек — превышен "
            "таймаут. Попробуйте повторить запрос позже."
        ) from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Ошибка транспорта при запросе эмбеддингов: {exc}") from exc

    if resp.status_code != 200:
        raise EmbeddingError(
            f"Сервис эмбеддингов вернул ошибку {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
        items = data.get("data") or []
        # OpenAI-совместимый ответ отдаёт элементы с полем index —
        # сортируем на случай, если провайдер вернул их не по порядку.
        items = sorted(items, key=lambda it: it.get("index", 0))
        return [item["embedding"] for item in items]
    except (ValueError, KeyError, TypeError) as exc:
        raise EmbeddingError(f"Некорректный ответ сервиса эмбеддингов: {exc}") from exc
