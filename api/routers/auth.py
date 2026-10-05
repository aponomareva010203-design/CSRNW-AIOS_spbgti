"""HTTP-слой авторизации: регистрация с подтверждением e-mail, вход, токены, админ-раздел.

POST   /api/auth/register     — создать учётную запись (роль «Пользователь», без автовхода);
POST   /api/auth/resend       — повторно отправить письмо подтверждения;
GET    /api/auth/confirm      — подтверждение e-mail по ссылке из письма (редирект на главную);
POST   /api/auth/login        — вход: выдаёт Bearer-токен, обновляет last_login;
POST   /api/auth/logout       — выход: инвалидация текущего токена;
GET    /api/auth/me           — профиль по токену;
GET    /api/usage/me          — расход токенов за сутки и действующий лимит;
GET    /api/admin/users       — [админ] все регистрации;
PATCH  /api/admin/users/{id}  — [админ] смена роли;
DELETE /api/admin/users/{id}  — [админ] удаление пользователя;
GET|PUT /api/admin/limits     — [админ] общий суточный лимит токенов;
PUT    /api/admin/users/{id}/token-limit — [админ] индивидуальный суточный лимит.

Токен передаётся в заголовке Authorization: Bearer <token> и живёт
7 дней (server-side, см. api/runtime/tokens.py). Токены подтверждения
e-mail живут 48 часов (api/runtime/confirmations.py); письма уходят через
api/runtime/mailer.py — без настроенного SMTP ссылка печатается в лог.
"""

import asyncio
import time
from functools import lru_cache

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..runtime.config import get_settings
from ..runtime.confirmations import ConfirmationStore
from ..runtime.cabinet import get_store as get_cabinet_store, full_name as cabinet_full_name
from ..runtime.emailcheck import email_domain_accepts_mail
from ..runtime.llm_settings import (
    LLMSettingsError,
    LLMSettingsStore,
    effective_config,
    mask_key,
)
from ..runtime.mailer import MailError, send_confirmation_email
from ..runtime.ratelimit import RateLimited, check as rate_check, client_ip
from ..runtime.tokens import TokenStore
from ..runtime.usage import get_store as get_usage_store
from ..runtime.usage_limits import UsageLimitsError, get_store as get_limits_store
from ..runtime.user_llm import personal_llm
from ..runtime.projects import (
    get_store as get_project_store,
    participant_needles,
    STATUS_ACTIVE as PROJECT_STATUS_ACTIVE,
)
from ..runtime import reminders
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
    """Тело POST /api/auth/register.

    website — honeypot против ботов: поле скрыто на форме, люди его не
    заполняют. Непустое значение означает бота: учётка не создаётся,
    письмо не уходит, но ответ имитирует успех.
    """

    login: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=4, max_length=128)
    # Имя опционально; правила (2–80, если задано) проверяет UserStore.
    name: str = Field(default="", max_length=80)
    website: str = Field(default="", max_length=200)
    # Согласия по 152-ФЗ: обязательны, без них регистрация отклоняется (422).
    # Факт и дата согласий сохраняются в учётной записи (см. UserStore.create).
    consent_pdn: bool = Field(default=False)
    consent_policy: bool = Field(default=False)


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


class ReminderConfigUpdate(BaseModel):
    """Тело PUT /api/admin/reminders (частичное обновление)."""

    steps: dict[str, dict] | None = None


class ReminderOverrideUpdate(BaseModel):
    """Тело PATCH /api/admin/reminders/self.

    overrides — {step_id: "on" | "off" | null}; null убирает оверрайд шага
    (возврат к обычному поведению — глобальный тумблер + статус выполнения).
    """

    overrides: dict[str, str | None] = Field(default_factory=dict)


class DailyLimitUpdate(BaseModel):
    """Тело PUT /api/admin/limits."""

    daily_limit: int = Field(gt=0)


class UserTokenLimitUpdate(BaseModel):
    """Тело PUT /api/admin/users/{id}/token-limit.

    daily_token_limit — индивидуальный суточный лимит; null убирает
    оверрайд (возврат к общему лимиту платформы).
    """

    daily_token_limit: int | None = Field(default=None, gt=0)


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


async def get_optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Пользователь по Bearer-токену или None — для публичных эндпоинтов,
    где ответ зависит от роли (гость и держатель недействительного токена
    трактуются одинаково: это не админ)."""
    token = _bearer(authorization)
    if not token:
        return None
    user_id = get_token_store().resolve(token)
    if not user_id:
        return None
    user = get_user_store().get(user_id)
    return user if user else None


def _confirmation_link(user: dict) -> str:
    """Ссылка подтверждения e-mail для письма."""
    token = get_confirmation_store().issue(user["id"])
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        # dev-фолбэк без настроенного домена: относительная ссылка в лог
        return f"/api/auth/confirm?token={token}"
    return f"{base}/api/auth/confirm?token={token}"


def _check_rate(request: Request) -> None:
    """Лимит попыток регистрации/повтора письма с одного IP (0 — отключён)."""
    limit = get_settings().register_ratelimit
    if limit <= 0:
        return
    ip = client_ip(request, request.headers.get("x-forwarded-for"))
    try:
        rate_check(f"register:{ip}", limit=limit, window=3600.0)
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@router.post("/auth/register", status_code=201)
def register(req: RegisterRequest, request: Request):
    """Регистрация: создаёт неподтверждённую учётку и отправляет письмо.

    Автовхода нет — сначала подтверждение e-mail по ссылке из письма.
    Если письмо отправить не удалось, учётка откатывается, чтобы попытку
    можно было повторить с чистого листа (502).

    Защита от ботов (их регистрации с фиктивных адресов возвращаются
    bounce-письмами на наш ящик):
      * honeypot «website» — заполненный бот получает имитацию успеха,
        без создания учётки и письма;
      * домен e-mail должен принимать почту (MX/A/AAAA-запись DNS);
      * лимит попыток с одного IP (скользящее окно, см. ratelimit.py).
    """
    _check_rate(request)

    if not (req.consent_pdn and req.consent_policy):
        raise HTTPException(
            status_code=422,
            detail="Для регистрации необходимы согласие на обработку персональных данных "
            "и согласие с Политикой обработки персональных данных.",
        )

    if req.website:
        # бот: имитируем успешную регистрацию, ничего не делая
        return {"ok": True, "email": req.login}

    store = get_user_store()
    try:
        user = store.create(
            login=req.login,
            password=req.password,
            name=req.name,
            confirmed=False,
            consent_pdn=req.consent_pdn,
            consent_policy=req.consent_policy,
        )
    except DuplicateLogin:
        raise HTTPException(status_code=409, detail="Этот логин уже занят")
    except UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # несуществующий домен = гарантированный bounce; проверяем до отправки.
    # Без настроенного SMTP (dev) письмо не уходит — проверка не нужна.
    if get_settings().smtp_host and not email_domain_accepts_mail(user["login"]):
        store.delete(user["id"])
        raise HTTPException(
            status_code=422,
            detail="Почтовый домен этого адреса не существует или не принимает письма. "
            "Проверьте адрес и попробуйте снова.",
        )

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
def resend(req: ResendRequest, request: Request):
    """Повторная отправка письма подтверждения.

    Письмо уходит, только если учётка существует, не подтверждена и пароль
    верен (защита от спама чужого ящика). Во всех остальных случаях ответ
    тот же — не раскрываем существование и статус учётных записей.
    Лимит попыток с одного IP — как у регистрации (счётчик общий).
    """
    _check_rate(request)
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


def _profile_filled(user_id: str) -> bool:
    """Заполнен ли блок «Данные пользователя» (онбординг в «Кабинете»)."""
    return bool(get_cabinet_store().get_profile(user_id).get("updated_at"))


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
    # profile_filled — для сценария онбординга: не заполнено «Данные
    # пользователя» — фронтенд ведёт нового пользователя в «Кабинет»
    return {
        "token": token,
        "user": store.public(user),
        "profile_filled": _profile_filled(user["id"]),
    }


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


@router.get("/usage/me")
def usage_me(user: dict = Depends(get_current_user)):
    """Расход токенов вызывающего за текущие сутки и его действующий лимит.

    Данные для счётчика в левом меню (frontend/js/auth.js); used_today —
    сумма prompt+completion за сутки, limit — индивидуальный лимит либо
    общий (is_override подсказывает, какой из них действует). У пользователя
    с личными настройками внешнего API лимита нет — он работает по своему
    ключу и оплачивает токены сам: limit = null, интерфейс показывает
    только потраченное за сутки.
    """
    used = get_usage_store().used_today(user["id"])
    limits = get_limits_store()
    personal = personal_llm(user["id"]) is not None
    return {
        "used_today": used["total"],
        "requests_today": used["requests"],
        "limit": None if personal else limits.effective_limit(user["id"]),
        "is_override": limits.override(user["id"]) is not None,
        "llm_mode": "личные" if personal else "общие",
    }


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


# ---------------------------------------------------------------------------
# Привязка Telegram (api/routers/telegram.py): /start в боте выдаёт одноразовый
# код, пользователь подтверждает его здесь — логин/пароль через бота не вводится
# ---------------------------------------------------------------------------


class TelegramLinkRequest(BaseModel):
    """Тело POST /api/auth/telegram/link — код из /start в боте."""

    code: str = Field(min_length=4, max_length=32)


def _telegram_view(link: dict | None) -> dict:
    """Привязка для клиента: без лишних деталей, with linked — только факт."""
    if not link:
        return {"linked": False}
    return {
        "linked": True,
        "username": link.get("username") or "",
        "linked_at": link.get("linked_at"),
    }


@router.get("/auth/telegram/link")
async def telegram_link_status(user: dict = Depends(get_current_user)):
    """Статус привязки учётной записи к Telegram (+ @username бота для подсказки)."""
    from .telegram import bot_username, get_telegram_store

    link = get_telegram_store().account_of(user["id"])
    view = _telegram_view(link)
    # @username бота — чтобы «Кабинет» подсказал, кого искать в Telegram;
    # недоступный Telegram API не роняет кабинет
    try:
        view["bot_username"] = await asyncio.wait_for(bot_username(), timeout=3.0)
    except Exception:  # noqa: BLE001 — подсказка не критична
        view["bot_username"] = ""
    return view


@router.post("/auth/telegram/link")
def telegram_link(req: TelegramLinkRequest, user: dict = Depends(get_current_user)):
    """Привязка Telegram по одноразовому коду из /start в боте.

    Один Telegram — одна учётная запись и наоборот: повторный код того же
    аккаунта перезаписывает привязку, уже занятый Telegram — 409.
    """
    from .telegram import get_telegram_store

    store = get_telegram_store()
    code_data = store.consume_code(req.code)
    if code_data is None:
        raise HTTPException(status_code=400, detail="Код не найден или истёк — отправьте /start боту заново")
    tg_user_id = int(code_data.get("tg_user_id") or 0)
    if not tg_user_id:
        raise HTTPException(status_code=400, detail="Код повреждён")
    existing = store.link_of(tg_user_id)
    if existing and existing.get("user_id") != user["id"]:
        raise HTTPException(status_code=409, detail="Этот Telegram уже привязан к другой учётной записи")
    store.bind(
        tg_user_id,
        user["id"],
        username=str(code_data.get("username") or ""),
        first_name=str(code_data.get("first_name") or ""),
    )
    return {"ok": True, **_telegram_view(store.link_of(tg_user_id))}


@router.delete("/auth/telegram/link")
def telegram_unlink(user: dict = Depends(get_current_user)):
    """Отвязка Telegram от учётной записи (сторона платформы)."""
    from .telegram import get_telegram_store

    tg_user_id = get_telegram_store().unbind_user(user["id"])
    if tg_user_id is None:
        raise HTTPException(status_code=404, detail="Привязка не найдена")
    return {"ok": True}


def _user_project(project_store, cabinet_store, user: dict) -> tuple[dict | None, str]:
    """Активный проект пользователя для админ-таблицы и роль в нём.

    'lead' — сам владелец подтверждённой карточки (latest_active_project);
    'participant' — упомянут в поле participants чужой активной карточки
    (find_participant_cards уже сортирует по updated_at убыв., берём первую
    подходящую — самую свежую); иначе (None, '') — активного проекта нет.
    """
    owned = reminders.latest_active_project(project_store, user["id"])
    if owned:
        return owned, "lead"
    profile = cabinet_store.get_profile(user["id"])
    needles = participant_needles(user, profile)
    if not needles:
        return None, ""
    for card in project_store.find_participant_cards(needles):
        if card.get("status") == PROJECT_STATUS_ACTIVE:
            return card, "participant"
    return None, ""


def _group_admin_items(items: list[dict]) -> list[dict]:
    """Группирует строки так, чтобы участники одного проекта шли подряд.

    Группы проектов идут по времени создания проекта (старые раньше — тот
    же принцип, что и порядок пользователей по created_at); внутри группы
    сначала руководитель, затем участники по ФИО. Пользователи без
    активного проекта — отдельными «группами» из одной строки, в конце
    списка, в порядке своей регистрации.
    """
    groups: dict = {}
    order: list = []
    for item in items:
        key = item.get("project_id")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    def group_sort_key(key):
        if key is None:
            return (1, 0)
        created = min((g.get("project_created_at") or 0) for g in groups[key])
        return (0, created)

    result: list[dict] = []
    for key in sorted(order, key=group_sort_key):
        members = groups[key]
        if key is None:
            members.sort(key=lambda g: g.get("created_at") or 0)
        else:
            members.sort(key=lambda g: (
                0 if g.get("project_role") == "lead" else 1,
                g.get("fio") or g.get("name") or g.get("login") or "",
            ))
        result.extend(members)
    return result


@router.get("/admin/users")
def admin_list_users(_: dict = Depends(require_admin)):
    """Все регистрации, сгруппированные по проекту (см. _group_admin_items);
    без хешей паролей.

    Для каждого пользователя дополнительно: скрытый контекст о пользователе
    (CabinetStore, блок context.json), агрегаты потраченных токенов LLM
    (UsageStore), анкетные ФИО/должности (CabinetStore, блок profile.json) и
    привязка к активному проекту — свой (role="lead") либо упомянут как
    участник в чужом (role="participant") — с текущим этапом пайплайна
    (reminders.current_stage: первый невыполненный шаг, не зависит от
    настроек напоминаний). Если руководитель указал участника в активной
    карточке, прогресс участника в таблице — это прогресс всего проекта.
    В ответ также входит platform_totals — сводка расхода по всей платформе.
    """
    cabinet_store = get_cabinet_store()
    project_store = get_project_store()
    usage = get_usage_store()
    limits = get_limits_store()
    items = []
    for u in get_user_store().list_all():
        item = dict(u)
        ctx = cabinet_store.get_context(u["id"]) or {}
        item["hidden_context"] = str(ctx.get("context") or "")
        item["context_generated_at"] = ctx.get("generated_at")
        tokens = usage.get(u["id"])
        item["tokens_prompt"] = tokens["prompt"]
        item["tokens_completion"] = tokens["completion"]
        item["tokens_requests"] = tokens["requests"]
        item["tokens_today"] = usage.used_today(u["id"])["total"]
        item["daily_token_limit"] = limits.override(u["id"])
        # внешний API: личные настройки («Настройки» → Внешний API) или общие
        item["personal_llm"] = personal_llm(u["id"]) is not None

        profile = cabinet_store.get_profile(u["id"])
        item["fio"] = cabinet_full_name(profile)
        item["positions"] = profile.get("positions") or []

        project, role = _user_project(project_store, cabinet_store, u)
        if project:
            item["project_id"] = project.get("id")
            item["project_title"] = project.get("title") or "Без названия"
            item["project_role"] = role
            item["project_created_at"] = project.get("created_at")
            stage_id = reminders.current_stage(project)
        else:
            item["project_id"] = None
            item["project_title"] = None
            item["project_role"] = ""
            item["project_created_at"] = None
            stage_id = "onboarding"
        item["stage_id"] = stage_id
        item["stage_label"] = (
            reminders.DEFAULT_LABELS.get(stage_id) if stage_id else "Пайплайн пройден полностью"
        )
        items.append(item)
    items = _group_admin_items(items)
    totals = usage.totals()
    return {"items": items, "platform_totals": totals}


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
    # вместе с ней — личные настройки внешнего API, учёт токенов и лимит
    from ..runtime import user_llm as user_llm_runtime

    user_llm_runtime.get_store().clear(user_id)
    get_usage_store().delete_user(user_id)
    try:
        get_limits_store().set_override(user_id, None)
    except UsageLimitsError:
        pass
    return {"ok": True}


# ----- Суточные лимиты токенов (админ-панель) -----


def _limits_public_view() -> dict:
    cfg = get_limits_store().config()
    return {
        "daily_limit": cfg["daily_limit"],
        "default_limit": cfg["default_limit"],
        "overrides_count": len(cfg["overrides"]),
        "updated_at": cfg["updated_at"],
    }


@router.get("/admin/limits")
def admin_get_limits(_: dict = Depends(require_admin)):
    """Текущие суточные лимиты токенов: общий + сколько индивидуальных."""
    return _limits_public_view()


@router.put("/admin/limits")
def admin_set_limits(req: DailyLimitUpdate, _: dict = Depends(require_admin)):
    """Общий суточный лимит токенов на пользователя (применяется сразу)."""
    try:
        get_limits_store().set_global(req.daily_limit)
    except UsageLimitsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _limits_public_view()


@router.put("/admin/users/{user_id}/token-limit")
def admin_set_user_token_limit(
    user_id: str, req: UserTokenLimitUpdate, _: dict = Depends(require_admin)
):
    """Индивидуальный суточный лимит пользователя; null — вернуть общий."""
    if get_user_store().get(user_id) is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        get_limits_store().set_override(user_id, req.daily_token_limit)
    except UsageLimitsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"user_id": user_id, "daily_token_limit": req.daily_token_limit}


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


# ---------------------------------------------------------------------------
# Напоминания по шагам пайплайна (api/runtime/reminders.py)
# ---------------------------------------------------------------------------


@router.get("/admin/reminders")
def admin_get_reminders(_: dict = Depends(require_admin)):
    """Текущая конфигурация напоминаний (глобальные тумблеры + режим показа)."""
    return reminders.load_config()


@router.put("/admin/reminders")
def admin_set_reminders(req: ReminderConfigUpdate, _: dict = Depends(require_admin)):
    """Обновить глобальные тумблеры шагов и/или режим показа (sequential/all)."""
    try:
        return reminders.update_config(steps=req.steps)
    except reminders.ReminderError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/admin/reminders/self")
def admin_get_own_reminder_overrides(user: dict = Depends(require_admin)):
    """Личные оверрайды напоминаний вызывающего администратора (для тестирования)."""
    return {"reminder_overrides": user.get("reminder_overrides") or {}}


@router.patch("/admin/reminders/self")
def admin_set_own_reminder_overrides(
    req: ReminderOverrideUpdate, user: dict = Depends(require_admin)
):
    """Личный оверрайд напоминаний вызывающего администратора (для тестирования).

    Действует только на аккаунт самого вызывающего и не зависит от
    enabled_global/реального прогресса. "on" — показать шаг всегда, "off" —
    всегда скрыть, null — вернуть обычное поведение.
    """
    store = get_user_store()
    current = store.get(user["id"])
    if current is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    overrides = dict(current.get("reminder_overrides") or {})
    for step_id, value in req.overrides.items():
        if step_id not in reminders.STEP_IDS:
            raise HTTPException(status_code=422, detail=f"Неизвестный шаг: {step_id}")
        if value is None:
            overrides.pop(step_id, None)
        elif value in reminders.OVERRIDE_VALUES:
            overrides[step_id] = value
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Значение должно быть 'on', 'off' или null: {step_id}",
            )
    current["reminder_overrides"] = overrides
    store.save(current)
    return {"reminder_overrides": overrides}


@router.get("/reminders")
def list_reminders(user: dict = Depends(get_current_user)):
    """Активные напоминания текущего пользователя (список [{id, label}])."""
    project = reminders.latest_active_project(get_project_store(), user["id"])
    return {"items": reminders.compute_reminders(user, project)}
