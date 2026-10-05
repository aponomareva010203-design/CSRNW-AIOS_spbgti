"""Личные настройки пользователя (api/routers/settings.py).

GET    /api/settings/llm — личные настройки внешнего LLM-API (маска ключа);
PUT    /api/settings/llm — сохранить (api_key пустой/не передан — прежний
                           ключ сохраняется; base_url пустой — возврат к
                           общим настройкам платформы, файл стирается);
POST   /api/settings/llm/test — проверка соединения с личным endpoint
                           (минимальный запрос к chat/completions; можно
                           проверить черновик из формы до сохранения);
DELETE /api/settings/llm — вернуть общие настройки.

Идентификатор пользователя берётся только из Bearer-токена; API-ключ
никогда не отдаётся целиком — только маской (llm_settings.mask_key).
Личная конфигурация действует на генерацию (чат, иконки проектов, скрытый
контекст); индексация и поиск RAG используют общий сервер эмбеддингов.
При активных личных настройках суточный лимит токенов не применяется —
в левом меню показывается только потраченное за сутки (usage/me → limit: null).
Если личный endpoint не отвечает в момент запроса, чат автоматически
уходит в общие настройки (providers.FallbackProvider) — пользователь
видит пометку в начале ответа.
"""

import time

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..runtime import user_llm, user_mail
from ..runtime.llm_settings import effective_config, mask_key
from .auth import get_current_user

router = APIRouter(prefix="/api")


class UserLLMUpdate(BaseModel):
    """Тело PUT /api/settings/llm.

    api_key и api_key2 — поле ключа и его повтор: несовпадение отвергается
    (422). Оба пустые — прежний ключ сохраняется (поле не трогали).
    Модель пустая — используется общая модель платформы.
    """

    provider: str = Field(default="", max_length=user_llm.PROVIDER_MAX_CHARS)
    base_url: str = Field(default="", max_length=user_llm.BASE_URL_MAX_CHARS)
    api_key: str = Field(default="", max_length=user_llm.API_KEY_MAX_CHARS)
    api_key2: str = Field(default="", max_length=user_llm.API_KEY_MAX_CHARS)
    model: str = Field(default="", max_length=user_llm.MODEL_MAX_CHARS)


def _public_view(user_id: str) -> dict:
    """Карточка настроек для интерфейса: без ключа, только маска."""
    data = user_llm.personal_llm(user_id)
    return {
        "provider": (data or {}).get("provider") or "",
        "base_url": (data or {}).get("base_url") or "",
        "api_key_masked": mask_key((data or {}).get("api_key") or ""),
        "model": (data or {}).get("model") or "",
        "active": data is not None,
        "llm_mode": "личные" if data else "общие",
    }


@router.get("/settings/llm")
def get_settings_llm(user: dict = Depends(get_current_user)):
    """Личные настройки внешнего API: активны ли и что сохранено (маска ключа)."""
    return _public_view(user["id"])


@router.put("/settings/llm")
def save_settings_llm(req: UserLLMUpdate, user: dict = Depends(get_current_user)):
    """Сохранить личные настройки внешнего API (Настройки → Внешний API).

    Пустой base_url — «Использовать общие»: личная конфигурация стирается.
    Несовпадение ключа и его повтора — 422; оба пустые — прежний ключ
    сохраняется (как в админских настройках LLM).
    """
    base_url = req.base_url.strip()
    if not base_url:
        user_llm.get_store().clear(user["id"])
        return _public_view(user["id"])
    if req.api_key != req.api_key2:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="Ключи в полях не совпадают")
    try:
        user_llm.get_store().set(
            user["id"],
            provider=req.provider,
            base_url=base_url,
            # пустые поля ключа = не менять сохранённый
            api_key=req.api_key if req.api_key.strip() else None,
            # модель заменяется как есть: пустая — общая модель платформы
            model=req.model,
        )
    except user_llm.UserLLMError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=str(exc))
    return _public_view(user["id"])


@router.delete("/settings/llm")
def clear_settings_llm(user: dict = Depends(get_current_user)):
    """Удалить личные настройки — платформа снова работает по общим."""
    user_llm.get_store().clear(user["id"])
    return _public_view(user["id"])


class UserLLMTestRequest(BaseModel):
    """Тело POST /api/settings/llm/test — черновик из формы; пустые поля
    берутся из сохранённой личной конфигурации (модель — из общей)."""

    base_url: str = Field(default="", max_length=user_llm.BASE_URL_MAX_CHARS)
    api_key: str = Field(default="", max_length=user_llm.API_KEY_MAX_CHARS)
    model: str = Field(default="", max_length=user_llm.MODEL_MAX_CHARS)


@router.post("/settings/llm/test")
async def test_settings_llm(
    req: UserLLMTestRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """Проверка соединения с личным endpoint внешнего API.

    Минимальный запрос к chat/completions — тот же принцип, что у админской
    проверки. Модель: черновик из формы → личная сохранённая → общая модель
    платформы. Без сохранённой личной конфигурации и черновика — 422:
    нечего проверять. Ошибка соединения не меняет настройки — только отчёт;
    в чате при недоступном личном endpoint ответ всё равно уходит через
    общие настройки (FallbackProvider).
    """
    from fastapi import HTTPException

    draft = req or UserLLMTestRequest()
    saved = user_llm.personal_llm(user["id"]) or {}
    base_url = (draft.base_url or saved.get("base_url") or "").strip().rstrip("/")
    api_key = (draft.api_key or "").strip() or saved.get("api_key") or ""
    model = (
        draft.model.strip()
        or str(saved.get("model") or "").strip()
        or effective_config().model
    )
    if not base_url:
        raise HTTPException(
            status_code=422,
            detail="Нет настроек для проверки: заполните Endpoint (Base URL) или сохраните их",
        )
    if not model:
        return {"ok": False, "error": "Модель не указана, а общая модель платформы не настроена — проверка невозможна"}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        ) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "stream": False,
                },
            )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "error": f"Не удалось связаться с эндпоинтом: {type(exc).__name__}: {exc}",
        }
    latency_ms = int((time.perf_counter() - started) * 1000)
    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
            "latency_ms": latency_ms,
        }
    return {"ok": True, "latency_ms": latency_ms, "model": model}


# --------------------------------------------------------------------------
# Личный SMTP для писем агентов (notify): форма на странице агента
# (панель «Расписание и уведомления»). Пароль не отдаётся — только маска;
# пустое поле пароля при сохранении = прежний сохраняется.


class AgentMailUpdate(BaseModel):
    """Тело PUT /api/settings/agent-mail.

    Пустой host — «использовать платформенную почту»: личные настройки
    стираются. Пустой password — прежний сохраняется (поле не трогали).
    """

    host: str = Field(default="", max_length=user_mail.HOST_MAX_CHARS)
    port: int | None = None
    tls: str = Field(default="ssl", max_length=10)
    user: str = Field(default="", max_length=user_mail.USER_MAX_CHARS)
    password: str = Field(default="", max_length=user_mail.PASSWORD_MAX_CHARS)
    from_email: str = Field(default="", max_length=user_mail.FROM_MAX_CHARS)
    from_name: str = Field(default="", max_length=user_mail.FROM_MAX_CHARS)


@router.get("/settings/agent-mail")
def get_agent_mail(user: dict = Depends(get_current_user)):
    """Личные SMTP-настройки писем агентов: активны ли и что сохранено."""
    return user_mail.public_view(user["id"])


@router.put("/settings/agent-mail")
def save_agent_mail(req: AgentMailUpdate, user: dict = Depends(get_current_user)):
    """Сохранить личный SMTP для писем агентов; пустой host — сброс на общие."""
    from fastapi import HTTPException

    if not req.host.strip():
        user_mail.get_store().clear(user["id"])
        return user_mail.public_view(user["id"])
    try:
        user_mail.get_store().save(
            user["id"],
            host=req.host,
            port=req.port if req.port is not None else 465,
            tls=req.tls,
            user=req.user,
            # пустые поля ключа = не менять сохранённый
            password=req.password if req.password.strip()
            else (user_mail.personal_mail(user["id"]) or {}).get("password", ""),
            from_email=req.from_email,
            from_name=req.from_name,
        )
    except user_mail.UserMailError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return user_mail.public_view(user["id"])


@router.delete("/settings/agent-mail")
def clear_agent_mail(user: dict = Depends(get_current_user)):
    """Удалить личный SMTP — письма агентов уходят через платформенный."""
    user_mail.get_store().clear(user["id"])
    return user_mail.public_view(user["id"])


class AgentMailTestRequest(BaseModel):
    """Тело POST /api/settings/agent-mail/test — черновик из формы.
    Пустые поля берутся из сохранённой личной конфигурации."""

    host: str = Field(default="", max_length=user_mail.HOST_MAX_CHARS)
    port: int | None = None
    tls: str = Field(default="ssl", max_length=10)
    user: str = Field(default="", max_length=user_mail.USER_MAX_CHARS)
    password: str = Field(default="", max_length=user_mail.PASSWORD_MAX_CHARS)
    from_email: str = Field(default="", max_length=user_mail.FROM_MAX_CHARS)
    from_name: str = Field(default="", max_length=user_mail.FROM_MAX_CHARS)


@router.post("/settings/agent-mail/test")
def test_agent_mail(req: AgentMailTestRequest, user: dict = Depends(get_current_user)):
    """Проверка SMTP: реальная отправка письма на адрес владельца.

    Отправляет короткое письмо на собственный адрес пользователя (email
    учётной записи) — проверяются и сервер, и доставка, а не только
    соединение. Черновик из формы не сохраняется.
    """
    import asyncio
    import uuid

    from fastapi import HTTPException

    from ..runtime import mail_settings
    from ..runtime.mail_settings import MailConfig
    from ..runtime.mailer import MailError, send_with_config
    from ..runtime.users import UserStore

    draft = req
    saved = user_mail.personal_mail(user["id"]) or {}
    host = (draft.host or saved.get("host") or "").strip()
    if not host:
        raise HTTPException(
            status_code=422,
            detail="Нет настроек для проверки: заполните SMTP-сервер или сохраните их",
        )
    password = (draft.password or "").strip() or saved.get("password") or ""
    cfg = MailConfig(
        host=host,
        port=draft.port if draft.port is not None else int(saved.get("port") or 465),
        tls=(draft.tls or saved.get("tls") or "ssl").strip().lower(),
        user=(draft.user or saved.get("user") or "").strip(),
        password=password,
        from_email=(draft.from_email or saved.get("from_email") or "").strip(),
        from_name=(draft.from_name or saved.get("from_name") or "").strip(),
        source="test",
    )
    account = UserStore(get_settings().runtime_dir / "users").get(user["id"]) or {}
    # отдельного поля email у учётной записи нет: логин — и есть адрес
    to = account.get("email") or account.get("login") or ""
    if not to:
        return {"ok": False, "error": "В вашей учётной записи не указан e-mail — некуда отправить проверочное письмо"}
    subject = "Проверка SMTP — письма агентов AIOS"
    body = (
        "Это проверочное письмо из AIOS: настройка SMTP для уведомлений "
        f"агентов работает. Проверка от {time.strftime('%d.%m.%Y %H:%M')}, "
        f"код {uuid.uuid4().hex[:8]}."
    )
    try:
        outcome = asyncio.run(send_with_config([to], subject, body, cfg))
    except MailError as exc:
        return {"ok": False, "error": str(exc)}
    ok = bool(outcome.get("sent"))
    return {
        "ok": ok,
        "sent_to": to,
        "error": None if ok else str(outcome.get("failed")),
    }
