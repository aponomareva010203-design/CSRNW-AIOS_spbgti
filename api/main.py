"""FastAPI-приложение AIOS: API каталога моделей + раздача статики."""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from .catalog import CATALOG
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .schemas import ModelsResponse, ModelSource

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="AIOS API", version="0.3.0")


class _NoCacheStaticMiddleware:
    """ASGI-посредник: статике добавляется Cache-Control: no-cache.

    Без заголовка браузеры применяют эвристическое кеширование (RFC 7234)
    и после обновления фронтенда продолжают выполнять старую закешированную
    копию JS — из-за этого рабочее пространство «залипало» на прежней версии.
    no-cache заставляет браузер сверять ETag при каждом запросе (ответ 304
    без передачи тела), поэтому дёшево и всегда актуально. /api/* (в том
    числе SSE-стрим /api/chat) не затрагиваются.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_no_cache(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.setdefault("headers", [])
                    if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_no_cache)


app.add_middleware(_NoCacheStaticMiddleware)

# Агентный рантайм (харнесс): /api/chat (SSE), /api/sessions, /api/runtime.
app.include_router(chat_router)

# Авторизация и админ-раздел: /api/auth/*, /api/admin/*.
app.include_router(auth_router)


@app.get("/api/models", response_model=ModelsResponse)
def list_models(source: Optional[ModelSource] = Query(default=None)) -> ModelsResponse:
    """Каталог моделей.

    Query-параметры:
        source: фильтр по источнику — только «spbti» или «huggingface».
                Неверное значение отклоняется схемой (HTTP 422).

    Порядок элементов стабилен: соответствует порядку в каталоге.
    """
    items = [m for m in CATALOG if source is None or m.source == source]
    return ModelsResponse(items=items)


# JSON-данные (вне /api), затем статика сайта — порядок как у /api/*
app.mount("/data", StaticFiles(directory=BASE_DIR / "data"), name="data")
app.mount("/", StaticFiles(directory=BASE_DIR / "frontend", html=True), name="site")
