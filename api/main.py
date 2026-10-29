"""FastAPI-приложение AIOS: API каталога моделей + раздача статики."""

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from .catalog import CATALOG
from .routers.auth import router as auth_router
from .routers.cabinet import router as cabinet_router
from .routers.chat import router as chat_router
from .routers.organization import router as organization_router
from .routers.ontologies import router as ontologies_router
from .routers.rag import router as rag_router
from .routers.pilot import router as pilot_router
from .routers.projects import router as projects_router
from .routers.feedback import router as feedback_router
from .routers.mail import router as mail_router
from .routers.settings import router as settings_router
from .routers.automations import router as automations_router
from .routers.catalog import router as catalog_router
from .routers.model_registry import router as model_registry_router
from .routers.telegram import router as telegram_router
from .routers.polygons import router as polygons_router
from .runtime.user_context import scheduler_loop
from .schemas import ModelsResponse, ModelSource

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Фоновые задачи: суточное обновление скрытых «Контекстов о пользователе»,
    запуск автономных агентов по расписанию (trigger.type == schedule) и
    long polling Telegram-бота (AIOS_TG_POLLING=1 — разработка без вебхука).

    TestClient(app) без контекстного менеджера lifespan не запускает —
    тесты остаются детерминированными.
    """
    task = asyncio.create_task(scheduler_loop())
    tg_task = None
    from .runtime.config import get_settings

    if get_settings().tg_polling and get_settings().tg_bot_token:
        from .routers.telegram import poll_updates

        tg_task = asyncio.create_task(poll_updates())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if tg_task:
            tg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tg_task


app = FastAPI(title="AIOS API", version="0.4.0", lifespan=lifespan)


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

# Страница «Кабинет»: /api/cabinet/* — собственные данные пользователя.
app.include_router(cabinet_router)

# Страница «Проекты»: /api/projects/* — карточки проектов пользователя.
app.include_router(projects_router)

# Страница «Статус пилотного проекта»: /api/pilot/* — строки таблицы
# (активные карточки всех пользователей) и статусы артефактов.
app.include_router(pilot_router)

# Онтологии навыка «Конструктор онтологий»: /api/ontologies/*.
app.include_router(ontologies_router)
app.include_router(rag_router)

# Раздел «Об организации» для админ-панели: /api/admin/organization*.
app.include_router(organization_router)

# Предложения пользователей: /api/feedback/* — записи в feedback/proposals.json.
app.include_router(feedback_router)

# Рассылка писем администратором: /api/admin/mail/* — настройки SMTP,
# список получателей, тестовое и массовое письмо (см. api/routers/mail.py).
app.include_router(mail_router)

# Вкладка «Настройки»: /api/settings/* — личные настройки внешнего API (LLM).
app.include_router(settings_router)


# Обратная связь каталога агентов (страница «Агенты»): /api/catalog/* —
# звёзды, публичные комментарии и автоматическая статистика применения
# агентов в проектах.
app.include_router(catalog_router)

# Модели коллективов: /api/models/registry/* — реестр, паспорт версии, прогноз,
# подбор режима, переобучение, публикация (api/runtime/model_engine.py).
# Витрина открытых моделей GET /api/models ниже не меняется.
app.include_router(model_registry_router)

# Автоматизации: /api/automations/* — блочный конструктор поверх n8n
# (public API, отдельного входа для пользователей нет), агентный шаг
# /api/internal/agent-step и настройки подключения /api/admin/n8n*.
app.include_router(automations_router)

# Telegram-бот: /api/telegram/webhook — тот же чат с агентами и RAG-базами,
# привязка учётной записи через «Кабинет» (api/routers/telegram.py).
app.include_router(telegram_router)

# Полигоны: /api/polygons/* — сценарная модель портфеля по УГТ (Монте-Карло,
# без LLM), проверка гипотез и ИИ-фокус-группа (api/runtime/polygons.py).
app.include_router(polygons_router)


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
