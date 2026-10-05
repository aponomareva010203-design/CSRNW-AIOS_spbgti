"""Раздел «Автоматизации» (api/routers/automations.py).

Автоматизации платформы исполняет self-hosted n8n; пользователи в n8n не
заводятся — AIOS работает с ним от одного сервисного API-ключа (настройки
в админ-панели, см. n8n_settings) и сам разграничивает права: владелец
карточки или роль «Администратор», чужая автоматизация неотличима от
несуществующей (404, как у агентов).

Эндпоинты:
    GET    /api/automations — свои автоматизации (админу ?all=1 — все);
    POST   /api/automations — создать: блоки → воркфлоу n8n (POST /workflows);
    GET    /api/automations/{id} — карточка;
    PUT    /api/automations/{id} — правка блоков (регенерация JSON, PUT);
    DELETE /api/automations/{id} — удалить (unpublish + delete в n8n);
    POST   /api/automations/{id}/publish | /unpublish — включить/выключить;
    POST   /api/automations/{id}/run — запуск кнопкой: production-webhook
                              (если выключена — сначала публикуем);
    GET    /api/automations/{id}/executions — история запусков из n8n;
    POST   /api/automations/{id}/executions/{exec_id}/retry — повторить.

Служебное:
    POST /api/internal/agent-step — шаг «ИИ-агент AIOS»: вызывается нодами n8n
                              с секретом X-AIOS-Service-Key, исполняет агентный
                              цикл от имени владельца автоматизации;
    GET|PUT /api/admin/n8n — настройки подключения (маска ключа);
    POST /api/admin/n8n/test — проверка связи (GET /api/v1/workflows?limit=1).
"""

import hmac
import time

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from functools import lru_cache
from pydantic import BaseModel, Field

from ..runtime import n8n_blocks
from ..runtime.automations_store import AutomationError, get_store
from ..runtime.llm_settings import mask_key
from ..runtime.mail_settings import effective_mail_config
from ..runtime.n8n_client import N8nApiError, N8nClient
from ..runtime.n8n_settings import (
    N8nSettingsError,
    N8nSettingsStore,
    effective_config,
)
from ..runtime.users import ROLE_ADMIN
from .auth import get_current_user, get_user_store, require_admin

router = APIRouter(prefix="/api")

SMTP_CREDENTIAL_NAME = "AIOS SMTP"
RUN_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


# ---------------------------------------------------------------------------
# Подготовка клиента и карточки


@lru_cache(maxsize=1)
def get_n8n_store() -> N8nSettingsStore:
    from ..runtime.config import get_settings

    return N8nSettingsStore(get_settings().runtime_dir)


def build_client(*, timeout: httpx.Timeout | None = None) -> N8nClient:
    """Клиент public API по действующей конфигурации; 503, если не настроено."""
    cfg = effective_config()
    if not cfg.configured:
        raise HTTPException(
            status_code=503,
            detail="n8n не настроен (админ-панель → Настройки внешних API → n8n)",
        )
    return N8nClient(cfg.base_url, cfg.api_key, timeout=timeout)


def _agent_step_url() -> str:
    """Адрес эндпоинта агентного шага, доступный из контейнера n8n:
    публичный адрес платформы, иначе host.docker.internal (compose-файл
    n8n маппит его на хост)."""
    from ..runtime.config import get_settings

    base = get_settings().public_base_url or "http://host.docker.internal:8000"
    return base.rstrip("/") + "/api/internal/agent-step"


def _require_card(automation_id: str, user: dict) -> dict:
    """Карточка для владельца или админа; остальным — 404."""
    card = get_store().find_any_owner(automation_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Автоматизация не найдена")
    if card.get("owner_id") != user["id"] and user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=404, detail="Автоматизация не найдена")
    return card


def _n8n_http_error(exc: N8nApiError) -> HTTPException:
    status = 502 if exc.status_code in (0, 502) else exc.status_code
    return HTTPException(status_code=status, detail=exc.message)


async def _ensure_smtp_credential(client: N8nClient) -> tuple[str, str]:
    """SMTP-credential в n8n для шага «Письмо»: ищем по имени, иначе создаём
    из настроек рассылки AIOS. Возвращает (credential_id, from_email)."""
    mail = effective_mail_config()
    if not mail.host:
        raise HTTPException(
            status_code=422,
            detail="Шаг «Письмо» недоступен: настройте SMTP (админ-панель → Рассылка)",
        )
    existing = (await client.list_credentials()).get("data") or []
    for item in existing:
        if item.get("name") == SMTP_CREDENTIAL_NAME:
            return str(item.get("id") or ""), mail.from_email
    created = await client.create_credential(
        name=SMTP_CREDENTIAL_NAME,
        type_="n8n-nodes-base/emailSend",
        data={
            "host": mail.host,
            "port": mail.port,
            "secure": mail.tls == "ssl",
            "user": mail.user,
            "password": mail.password,
        },
    )
    return str(created.get("id") or ""), mail.from_email


async def _sync_workflow(card: dict) -> dict:
    """Переносит блоки карточки в n8n: создаёт воркфлоу или обновляет
    существующий. Обновляет и сохраняет карточку."""
    client = build_client()
    cfg = effective_config()
    needs_email = any(s.get("type") == n8n_blocks.STEP_EMAIL for s in card["steps"])
    credential_id = from_email = ""
    if needs_email:
        credential_id, from_email = await _ensure_smtp_credential(client)
    nodes, connections = n8n_blocks.translate(
        trigger=card["trigger"],
        steps=card["steps"],
        webhook_path=card["webhook_path"],
        agent_step_url=_agent_step_url(),
        service_key=cfg.service_key,
        owner_id=card["owner_id"],
        email_credential_id=credential_id,
        email_from=from_email,
    )
    settings = {"executionOrder": "v1"}
    try:
        if card.get("n8n_workflow_id"):
            await client.update_workflow(
                card["n8n_workflow_id"],
                name=card["name"],
                nodes=nodes,
                connections=connections,
                settings=settings,
            )
        else:
            created = await client.create_workflow(
                name=card["name"], nodes=nodes, connections=connections, settings=settings
            )
            card["n8n_workflow_id"] = str(created.get("id") or "")
    except N8nApiError as exc:
        raise _n8n_http_error(exc)
    return get_store().update(card)


# ---------------------------------------------------------------------------
# Схемы


class AutomationUpsert(BaseModel):
    """Тело POST/PUT /api/automations*."""

    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    trigger: dict = Field(default_factory=dict)
    steps: list = Field(default_factory=list)


class N8nConfigRequest(BaseModel):
    """Тело PUT /api/admin/n8n и POST /api/admin/n8n/test (черновик)."""

    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=2000)


class AgentStepRequest(BaseModel):
    """Тело POST /api/internal/agent-step — вызов ноды «Агент AIOS»."""

    owner_id: str = Field(max_length=100)
    prompt: str = Field(max_length=n8n_blocks.MAX_PROMPT_CHARS)


# ---------------------------------------------------------------------------
# CRUD автоматизаций


def _public_view(card: dict) -> dict:
    return get_store().summary(card)


@router.get("/automations")
def list_automations(
    all: bool = False, user: dict = Depends(get_current_user)
):
    """Свои автоматизации; админу с ?all=1 — все карточки платформы."""
    if all and user.get("role") == ROLE_ADMIN:
        items = get_store().list_all()
    else:
        items = get_store().list(user["id"])
    return {"items": [_public_view(c) for c in items], "total": len(items)}


@router.post("/automations")
async def create_automation(req: AutomationUpsert, user: dict = Depends(get_current_user)):
    try:
        card = get_store().create(
            user["id"],
            {
                "name": n8n_blocks.normalize_name(req.name),
                "description": req.description.strip()[:2000],
                "trigger": n8n_blocks.normalize_trigger(req.trigger),
                "steps": n8n_blocks.normalize_steps(req.steps),
                "webhook_path": n8n_blocks.new_webhook_path(),
            },
        )
    except AutomationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except n8n_blocks.BlockError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    card = await _sync_workflow(card)
    return _public_view(card)


@router.get("/automations/{automation_id}")
def get_automation(automation_id: str, user: dict = Depends(get_current_user)):
    return _public_view(_require_card(automation_id, user))


@router.put("/automations/{automation_id}")
async def update_automation(
    automation_id: str, req: AutomationUpsert, user: dict = Depends(get_current_user)
):
    card = _require_card(automation_id, user)
    card["name"] = n8n_blocks.normalize_name(req.name)
    card["description"] = req.description.strip()[:2000]
    card["trigger"] = n8n_blocks.normalize_trigger(req.trigger)
    card["steps"] = n8n_blocks.normalize_steps(req.steps)
    card = await _sync_workflow(card)
    return _public_view(card)


@router.delete("/automations/{automation_id}")
async def delete_automation(automation_id: str, user: dict = Depends(get_current_user)):
    card = _require_card(automation_id, user)
    if card.get("n8n_workflow_id"):
        client = build_client()
        # чистим и n8n; ошибки не фатальны — воркфлоу мог быть уже удалён
        try:
            if card.get("active"):
                await client.unpublish_workflow(card["n8n_workflow_id"])
            await client.delete_workflow(card["n8n_workflow_id"])
        except N8nApiError:
            pass
    get_store().delete(card["owner_id"], card["id"])
    return {"ok": True}


@router.post("/automations/{automation_id}/publish")
async def publish_automation(automation_id: str, user: dict = Depends(get_current_user)):
    """Включить: production-webhook и расписание начинают работать."""
    card = _require_card(automation_id, user)
    if not card.get("n8n_workflow_id"):
        raise HTTPException(status_code=409, detail="Автоматизация ещё не собрана в n8n")
    try:
        await build_client().publish_workflow(card["n8n_workflow_id"])
    except N8nApiError as exc:
        raise _n8n_http_error(exc)
    card["active"] = True
    get_store().update(card)
    return _public_view(card)


@router.post("/automations/{automation_id}/unpublish")
async def unpublish_automation(automation_id: str, user: dict = Depends(get_current_user)):
    card = _require_card(automation_id, user)
    if card.get("n8n_workflow_id"):
        try:
            await build_client().unpublish_workflow(card["n8n_workflow_id"])
        except N8nApiError as exc:
            raise _n8n_http_error(exc)
    card["active"] = False
    get_store().update(card)
    return _public_view(card)


@router.post("/automations/{automation_id}/run")
async def run_automation(automation_id: str, user: dict = Depends(get_current_user)):
    """Запуск кнопкой: POST на production-webhook (responseMode = last node,
    поэтому ответ — результат последнего шага). Выключенная автоматизация
    сначала публикуется — кнопка просто работает."""
    card = _require_card(automation_id, user)
    if not card.get("n8n_workflow_id") or not card.get("webhook_path"):
        raise HTTPException(status_code=409, detail="Автоматизация ещё не собрана в n8n")
    client = build_client(timeout=RUN_TIMEOUT)
    activated = False
    if not card.get("active"):
        try:
            await client.publish_workflow(card["n8n_workflow_id"])
        except N8nApiError as exc:
            raise _n8n_http_error(exc)
        card["active"] = True
        activated = True
        get_store().update(card)
    try:
        resp = await client.call_webhook(card["webhook_path"], {})
    except N8nApiError as exc:
        raise _n8n_http_error(exc)
    card["last_run_at"] = time.time()
    get_store().update(card)
    try:
        result: object = resp.json()
    except ValueError:
        result = resp.text[:4000]
    return {"ok": resp.status_code < 400, "activated": activated, "status": resp.status_code, "result": result}


@router.get("/automations/{automation_id}/executions")
async def list_executions(
    automation_id: str, limit: int = 20, user: dict = Depends(get_current_user)
):
    """История запусков из n8n (без данных шагов)."""
    card = _require_card(automation_id, user)
    if not card.get("n8n_workflow_id"):
        return {"items": [], "total": 0}
    try:
        data = await build_client().list_executions(
            workflow_id=card["n8n_workflow_id"], limit=min(max(limit, 1), 100)
        )
    except N8nApiError as exc:
        raise _n8n_http_error(exc)
    items = [
        {
            "id": str(e.get("id") or ""),
            "status": e.get("status"),
            "mode": e.get("mode"),
            "startedAt": e.get("startedAt"),
            "stoppedAt": e.get("stoppedAt"),
            "retryOfId": e.get("retryOfId"),
        }
        for e in (data.get("data") or [])
        if isinstance(e, dict)
    ]
    return {"items": items, "total": len(items)}


@router.post("/automations/{automation_id}/executions/{execution_id}/retry")
async def retry_execution(
    automation_id: str, execution_id: str, user: dict = Depends(get_current_user)
):
    card = _require_card(automation_id, user)
    try:
        await build_client().retry_execution(execution_id)
    except N8nApiError as exc:
        raise _n8n_http_error(exc)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Служебное: агентный шаг для нод n8n


@router.post("/internal/agent-step")
async def agent_step(
    req: AgentStepRequest,
    x_aios_service_key: str = Header(default="", alias="X-AIOS-Service-Key"),
):
    """Шаг «ИИ-агент AIOS» в автоматизации. Вызывается нодами n8n с секретом
    из настроек подключения; исполняет агентный цикл от имени владельца
    автоматизации (его контекст «Кабинета», учёт токенов — всё как в чате).
    Сессия — одноразовая: транскрипт не засоряет список диалогов, история
    запусков доступна в разделе «Автоматизации»."""
    cfg = effective_config()
    if not cfg.service_key or not hmac.compare_digest(x_aios_service_key, cfg.service_key):
        raise HTTPException(status_code=401, detail="Неверный сервисный ключ")
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="Пустой промпт")
    if get_user_store().get(req.owner_id) is None:
        raise HTTPException(status_code=404, detail="Владелец автоматизации не найден")

    from ..runtime.config import get_settings
    from ..runtime.llm_settings import effective_config_for
    from ..runtime.loop import run_agent_loop
    from .chat import get_provider, get_registry, get_store as get_session_store

    llm_cfg = effective_config_for(req.owner_id)
    provider = get_provider(llm_cfg.base_url, llm_cfg.api_key, llm_cfg.model)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM не настроена администратором — шаг агента недоступен",
        )
    store = get_session_store()
    session = store.create(title="Шаг автоматизации")
    session["user_id"] = req.owner_id
    parts: list[str] = []
    try:
        async for event in run_agent_loop(
            settings=get_settings(),
            provider=provider,
            registry=get_registry(),
            store=store,
            session=session,
            user_text=prompt,
        ):
            if event.get("type") == "delta":
                parts.append(str(event.get("text") or ""))
            elif event.get("type") == "error":
                raise HTTPException(status_code=502, detail=str(event.get("message") or "Ошибка цикла"))
    finally:
        store.delete(session["id"])
    return {"text": "".join(parts)}


# ---------------------------------------------------------------------------
# Настройки подключения (админ-панель)


def _n8n_public_view() -> dict:
    cfg = effective_config()
    return {
        "configured": cfg.configured,
        "source": cfg.source or None,
        "base_url": cfg.base_url,
        "api_key_masked": mask_key(cfg.api_key),
    }


@router.get("/admin/n8n")
def admin_get_n8n(_: dict = Depends(require_admin)):
    return _n8n_public_view()


@router.put("/admin/n8n")
def admin_set_n8n(req: N8nConfigRequest, _: dict = Depends(require_admin)):
    """Сохранить подключение к n8n. Пустой api_key — оставить прежний."""
    store = get_n8n_store()
    api_key = (req.api_key or "").strip()
    if not api_key:
        saved = store.load()
        if saved:
            api_key = saved.get("api_key") or ""
    try:
        store.save(req.base_url, api_key)
    except N8nSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _n8n_public_view()


@router.post("/admin/n8n/test")
async def admin_test_n8n(req: N8nConfigRequest | None = None, _: dict = Depends(require_admin)):
    """Проверка связи с n8n: минимальный запрос к public API.

    Можно передать черновик {base_url, api_key}; пустые поля берутся
    из сохранённой конфигурации.
    """
    cfg = effective_config()
    draft = req or N8nConfigRequest()
    base_url = (draft.base_url or cfg.base_url).strip().rstrip("/")
    api_key = (draft.api_key or "").strip() or cfg.api_key
    if not base_url or not api_key:
        raise HTTPException(status_code=422, detail="Нужны адрес и API-ключ n8n")
    started = time.perf_counter()
    try:
        client = N8nClient(base_url, api_key, timeout=httpx.Timeout(10.0))
        await client.list_workflows(limit=1)
    except N8nApiError as exc:
        return {"ok": False, "error": exc.message}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Не удалось связаться с n8n: {type(exc).__name__}: {exc}"}
    return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)}
