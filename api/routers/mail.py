"""HTTP-слой рассылки писем администратором.

Администратор пишет обычный текст (не HTML) — сервер сам оборачивает его в
готовый HTML-шаблон письма (см. api/runtime/mailer.render_email_html) и
добавляет необязательную кнопку-ссылку. Настройки SMTP — та же
конфигурация, что использует и письмо подтверждения регистрации (см.
api/runtime/mail_settings.py).

GET  /api/admin/mail/settings   — [админ] текущие настройки SMTP (пароль — маской);
PUT  /api/admin/mail/settings   — [админ] сохранить настройки SMTP;
GET  /api/admin/mail/recipients — [админ] email + ФИО всех пользователей платформы;
POST /api/admin/mail/preview    — [админ] HTML предпросмотра письма (без отправки);
POST /api/admin/mail/test       — [админ] отправить тестовое письмо на произвольный адрес;
POST /api/admin/mail/send       — [админ] отправить письмо отмеченным пользователям.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..runtime.cabinet import get_store as get_cabinet_store, full_name as cabinet_full_name
from ..runtime.config import get_settings
from ..runtime.mail_settings import MailSettingsError, MailSettingsStore, effective_mail_config, mask_key
from ..runtime.mailer import MailError, render_email_html, send_bulk_html_email
from .auth import get_user_store, require_admin

router = APIRouter(prefix="/api/admin/mail")


@lru_cache(maxsize=1)
def get_mail_settings_store() -> MailSettingsStore:
    """Синглтон хранилища настроек SMTP; в тестах сбрасывается cache_clear()."""
    return MailSettingsStore(get_settings().runtime_dir)


class MailSettingsRequest(BaseModel):
    """Тело PUT /api/admin/mail/settings. Пустой password — оставить прежний."""

    host: str = Field(default="", max_length=200)
    port: int = Field(default=465, ge=1, le=65535)
    tls: str = Field(default="ssl", max_length=20)
    user: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    from_email: str = Field(default="", max_length=200)
    from_name: str = Field(default="", max_length=200)


class MailContent(BaseModel):
    """Общее тело письма: обычный текст + необязательная кнопка-ссылка."""

    body_text: str = Field(min_length=1, max_length=50_000)
    button_label: str = Field(default="", max_length=100)
    button_url: str = Field(default="", max_length=500)


class TestMailRequest(MailContent):
    """Тело POST /api/admin/mail/test."""

    to: str = Field(min_length=3, max_length=200)
    subject: str = Field(min_length=1, max_length=300)


class SendMailRequest(MailContent):
    """Тело POST /api/admin/mail/send."""

    subject: str = Field(min_length=1, max_length=300)
    user_ids: list[str] = Field(default_factory=list)


def _settings_public_view() -> dict:
    cfg = effective_mail_config()
    return {
        "configured": bool(cfg.host),
        "source": cfg.source or None,
        "host": cfg.host,
        "port": cfg.port,
        "tls": cfg.tls,
        "user": cfg.user,
        "from_email": cfg.from_email,
        "from_name": cfg.from_name,
        "password_masked": mask_key(cfg.password),
    }


@router.get("/settings")
def get_mail_settings(_: dict = Depends(require_admin)):
    return _settings_public_view()


@router.put("/settings")
def set_mail_settings(req: MailSettingsRequest, _: dict = Depends(require_admin)):
    store = get_mail_settings_store()
    password = req.password.strip()
    if not password:
        saved = store.load()
        if saved:
            password = saved.get("password") or ""
    try:
        store.save(
            req.host, req.port, req.tls, req.user, password, req.from_email, req.from_name
        )
    except MailSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _settings_public_view()


@router.get("/recipients")
def list_recipients(_: dict = Depends(require_admin)):
    """Email + ФИО всех пользователей платформы — чекбоксы формы рассылки."""
    cabinet_store = get_cabinet_store()
    items = []
    for u in get_user_store().list_all():
        profile = cabinet_store.get_profile(u["id"])
        items.append(
            {
                "id": u["id"],
                "email": u["login"],
                "fio": cabinet_full_name(profile) or u.get("name") or "",
            }
        )
    return {"items": items}


@router.post("/preview")
def preview_mail(req: MailContent, _: dict = Depends(require_admin)):
    """HTML предпросмотра письма — без отправки, для iframe в админ-панели."""
    return {"html": render_email_html(req.body_text, req.button_label, req.button_url)}


@router.post("/test")
def send_test(req: TestMailRequest, _: dict = Depends(require_admin)):
    try:
        result = send_bulk_html_email(
            [req.to.strip()], req.subject, req.body_text, req.button_label, req.button_url
        )
    except MailError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if result["failed"]:
        raise HTTPException(status_code=502, detail=result["failed"][0]["error"])
    return {"ok": True}


@router.post("/send")
def send_bulk(req: SendMailRequest, _: dict = Depends(require_admin)):
    if not req.user_ids:
        raise HTTPException(status_code=422, detail="Отметьте хотя бы одного получателя")
    store = get_user_store()
    recipients = []
    for uid in req.user_ids:
        user = store.get(uid)
        if user:
            recipients.append(user["login"])
    if not recipients:
        raise HTTPException(status_code=422, detail="Нет ни одного корректного получателя")
    try:
        result = send_bulk_html_email(
            recipients, req.subject, req.body_text, req.button_label, req.button_url
        )
    except MailError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "sent": len(result["sent"]), "failed": result["failed"]}
