"""HTTP-слой авторизации: регистрация с подтверждением e-mail, вход, токены, админ-раздел.

POST   /api/auth/register     — создать учётную запись (роль «Пользователь», без автовхода);
POST   /api/auth/resend       — повторно отправить письмо подтверждения;
GET    /api/auth/confirm      — подтверждение e-mail по ссылке из письма (редирект на главную);
POST   /api/auth/login        — вход: выдаёт Bearer-токен, обновляет last_login;
POST   /api/auth/logout       — выход: инвалидация текущего токена;
GET    /api/auth/me           — профиль по токену;
GET    /api/admin/users       — [админ] все регистрации;
PATCH  /api/admin/users/{id}  — [админ] смена роли;
DELETE /api/admin/users/{id}  — [админ] удаление пользователя.

Токен передаётся в заголовке Authorization: Bearer <token> и живёт
7 дней (server-side, см. api/runtime/tokens.py). Токены подтверждения
e-mail живут 48 часов (api/runtime/confirmations.py); письма уходят через
api/runtime/mailer.py — без настроенного SMTP ссылка печатается в лог.
"""

import time
from functools import lru_cache

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..runtime.config import get_settings
from ..runtime.confirmations import ConfirmationStore
from ..runtime.cabinet import get_store as get_cabinet_store
from ..runtime.llm_settings import (
    LLMSettingsError,
    LLMSettingsStore,
    effective_config,
    mask_key,
)
from ..runtime.mailer import MailError, send_confirmation_email
from ..runtime.tokens import TokenStore
from ..runtime.users import (
    ROLES,
    ROLE_ADMIN,
    DuplicateLogin,
    UserError,
    UserStore,
)

router = APIRouter(prefix="/api")


@lru_cache(maxsize=1)
def get_user_store() -> UserStore:
    """Синглтон хранилища пользователей; в тестах сбрасывается cache_clear()."""
    return UserStore(get_settings().runtime_dir / "users")


@lru_cache(maxsize=1)
def get_token_store() -> TokenStore:
    return TokenStore(get_settings().runtime_dir / "auth")


@lru_cache(maxsize=1)
def get_confirmation_store() -> ConfirmationStore:
    return ConfirmationStore(get_settings().runtime_dir / "confirm")


@lru_cache(maxsize=1)
def get_llm_store() -> LLMSettingsStore:
    return LLMSettingsStore(get_settings().runtime_dir)


class RegisterRequest(BaseModel):
    """Тело POST /api/auth/register."""

    login: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=4, max_length=128)
    # Имя опционально; правила (2–80, если задано) проверяет UserStore.
    name: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    """Тело POST /api/auth/login."""

    login: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=128)


class ResendRequest(BaseModel):
    """Тело POST /api/auth/resend (пароль может быть ещё не введён)."""

    login: str = Field(min_length=3, max_length=120)
    password: str = Field(default="", max_length=128)


class RoleUpdate(BaseModel):
    """Тело PATCH /api/admin/users/{id}."""

    role: str = Field(min_length=1, max_length=30)


class PasswordChange(BaseModel):
    """Тело PATCH /api/auth/password."""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=4, max_length=128)


class LLMConfigRequest(BaseModel):
    """Тело PUT /api/admin/llm и POST /api/admin/llm/test (черновик для проверки)."""

    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)


class LLMModelsRequest(BaseModel):
    """Тело POST /api/admin/llm/models."""

    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


async def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """Зависимость FastAPI: пользователь по Bearer-токену (401 при проблемах)."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    user_id = get_token_store().resolve(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Сессия недействительна или истекла")
    user = get_user_store().get(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Зависимость FastAPI: только роль «Администратор» (иначе 403)."""
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
    return user


def _confirmation_link(user: dict) -> str:
    """Ссылка подтверждения e-mail для письма."""
    token = get_confirmation_store().issue(user["id"])
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        # dev-фолбэк без настроенного домена: относительная ссылка в лог
        return f"/api/auth/confirm?token={token}"
    return f"{base}/api/auth/confirm?token={token}"


@router.post("/auth/register", status_code=201)
def register(req: RegisterRequest):
    """Регистрация: создаёт неподтверждённую учётку и отправляет письмо.

    Автовхода нет — сначала подтверждение e-mail по ссылке из письма.
    Если письмо отправить не удалось, учётка откатывается, чтобы попытку
    можно было повторить с чистого листа (502).
    """
    store = get_user_store()
    try:
        user = store.create(login=req.login, password=req.password, name=req.name, confirmed=False)
    except DuplicateLogin:
        raise HTTPException(status_code=409, detail="Этот логин уже занят")
    except UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        send_confirmation_email(user["login"], _confirmation_link(user))
    except MailError as exc:
        store.delete(user["id"])
        get_confirmation_store().revoke_all_for_user(user["id"])
        raise HTTPException(
            status_code=502, detail=f"Не удалось отправить письмо для подтверждения: {exc}"
        )
    return {"ok": True, "email": user["login"]}


@router.post("/auth/resend")
def resend(req: ResendRequest):
    """Повторная отправка письма подтверждения.

    Письмо уходит, только если учётка существует, не подтверждена и пароль
    верен (защита от спама чужого ящика). Во всех остальных случаях ответ
    тот же — не раскрываем существование и статус учётных записей.
    """
    store = get_user_store()
    user = store.get_by_login(req.login)
    if user is None or user.get("confirmed", True):
        return {"ok": True}
    if not store.verify_password(user, req.password):
        return {"ok": True}
    try:
        send_confirmation_email(user["login"], _confirmation_link(user))
    except MailError as exc:
        raise HTTPException(
            status_code=502, detail=f"Не удалось отправить письмо для подтверждения: {exc}"
        )
    return {"ok": True}


@router.get("/auth/confirm")
def confirm(token: str = ""):
    """Подтверждение e-mail по ссылке из письма.

    Успех или нет — редирект на главную с ?confirmed=1/0; страница показывает
    соответствующее сообщение. Повторный переход по ссылке после
    подтверждения считается успехом.
    """
    ok = False
    store = get_confirmation_store()
    user_id = store.resolve(token)
    if user_id:
        users = get_user_store()
        user = users.get(user_id)
        if user is not None:
            if not user.get("confirmed", True):
                users.confirm(user)
            ok = True
        store.revoke(token)
    return RedirectResponse(f"/index.html?confirmed={1 if ok else 0}", status_code=303)


@router.post("/auth/login")
def login(req: LoginRequest):
    store = get_user_store()
    user = store.get_by_login(req.login)
    if user is None or not store.verify_password(user, req.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if not user.get("confirmed", True):
        raise HTTPException(
            status_code=403,
            detail="E-mail не подтверждён: перейдите по ссылке из письма или запросите новое письмо",
        )
    user["last_login"] = time.time()
    store.save(user)
    token = get_token_store().issue(user["id"])
    return {"token": token, "user": store.public(user)}


@router.post("/auth/logout")
def logout(
    user: dict = Depends(get_current_user),
    authorization: str | None = Header(default=None),
):
    get_token_store().revoke(_bearer(authorization) or "")
    return {"ok": True, "user": get_user_store().public(user)}


@router.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return get_user_store().public(user)


@router.patch("/auth/password")
def change_password(
    req: PasswordChange,
    user: dict = Depends(get_current_user),
    authorization: str | None = Header(default=None),
):
    """Смена собственного пароля: проверяем старый, выходим из чужих сессий."""
    store = get_user_store()
    if not store.verify_password(user, req.old_password):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")
    try:
        store.set_password(user, req.new_password)
    except UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    get_token_store().revoke_all_for_user(user["id"], keep=_bearer(authorization) or "")
    return {"ok": True}


@router.get("/admin/users")
def admin_list_users(_: dict = Depends(require_admin)):
    """Все регистрации (в порядке создания); без хешей паролей."""
    return {"items": get_user_store().list_all()}


@router.patch("/admin/users/{user_id}")
def admin_set_role(user_id: str, req: RoleUpdate, _: dict = Depends(require_admin)):
    if req.role not in ROLES:
        raise HTTPException(
            status_code=422, detail=f"Роль должна быть одной из: {', '.join(ROLES)}"
        )
    store = get_user_store()
    target = store.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target["role"] == ROLE_ADMIN and req.role != ROLE_ADMIN and store.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="Нельзя снять роль с последнего администратора")
    target["role"] = req.role
    store.save(target)
    return store.public(target)


@router.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: str, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить собственную учётную запись")
    if not get_user_store().delete(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    get_token_store().revoke_all_for_user(user_id)
    get_confirmation_store().revoke_all_for_user(user_id)
    # блоки «Кабинета» (данные пользователя и скрытый контекст) удаляются вместе с учёткой
    get_cabinet_store().delete_user(user_id)
    return {"ok": True}


# ----- Настройки внешнего LLM-API (админ-панель) -----


def _llm_public_view() -> dict:
    """Текущая конфигурация для UI; ключ — только маской."""
    cfg = effective_config()
    return {
        "configured": bool(cfg.base_url and cfg.model),
        "source": cfg.source or None,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "api_key_masked": mask_key(cfg.api_key),
    }


@router.get("/admin/llm")
def admin_get_llm(_: dict = Depends(require_admin)):
    return _llm_public_view()


@router.put("/admin/llm")
def admin_set_llm(req: LLMConfigRequest, _: dict = Depends(require_admin)):
    """Сохранить настройки LLM (эндпоинт, ключ, модель) — применяются на лету.

    Пустой api_key означает «оставить прежний ключ» (например, при смене модели).
    """
    store = get_llm_store()
    api_key = (req.api_key or "").strip()
    if not api_key:
        saved = store.load()
        if saved:
            api_key = saved.get("api_key") or ""
    try:
        store.save(req.base_url, api_key, req.model)
    except LLMSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # пересобираем провайдера, чтобы новые настройки применились без рестарта
    from .chat import get_provider

    get_provider.cache_clear()
    return _llm_public_view()


@router.post("/admin/llm/test")
async def admin_test_llm(req: LLMConfigRequest | None = None, _: dict = Depends(require_admin)):
    """Проверка соединения с LLM: минимальный запрос к chat/completions.

    Можно передать черновик {base_url, api_key, model}; пустые поля берутся
    из сохранённой конфигурации.
    """
    cfg = effective_config()
    draft = req or LLMConfigRequest()
    base_url = (draft.base_url or cfg.base_url).strip().rstrip("/")
    api_key = (draft.api_key or "").strip() or cfg.api_key
    model = (draft.model or cfg.model).strip()
    if not base_url or not model:
        raise HTTPException(status_code=422, detail="Нужны base_url и модель")

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
        return {"ok": False, "error": f"Не удалось связаться с эндпоинтом: {type(exc).__name__}: {exc}"}

    latency_ms = int((time.perf_counter() - started) * 1000)
    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}", "latency_ms": latency_ms}
    return {"ok": True, "latency_ms": latency_ms, "model": model}


@router.post("/admin/llm/models")
async def admin_list_llm_models(req: LLMModelsRequest, _: dict = Depends(require_admin)):
    """Модели, доступные ключу на эндпоинте (OpenAI-совместимый GET /models)."""
    cfg = effective_config()
    api_key = (req.api_key or "").strip() or cfg.api_key
    base_url = req.base_url.strip().rstrip("/")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось связаться с эндпоинтом: {type(exc).__name__}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Эндпоинт вернул HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Эндпоинт вернул не-JSON ответ")
    ids = sorted(
        {
            str(m.get("id"))
            for m in (data.get("data") or [])
            if isinstance(m, dict) and m.get("id")
        }
    )
    if not ids:
        raise HTTPException(status_code=502, detail="Эндпоинт не вернул список моделей — введите имя вручную")
    return {"items": [{"id": i} for i in ids]}
