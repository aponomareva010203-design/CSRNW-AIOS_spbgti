"""Отправка писем через SMTP: подтверждение e-mail и рассылка писем.

Два независимых источника настроек, намеренно не связанных друг с другом:

  * письма подтверждения регистрации (send_confirmation_email) — только
    из .env (AIOS_SMTP_*); раздел «Рассылка писем» в /admin.html на них
    никак не влияет;
  * рассылка писем (send_bulk_html_email) — только из отдельного файла
    AIOS_RUNTIME_DIR/newsletter_smtp.json (api/runtime/mail_settings.py),
    который правится в разделе «Рассылка писем»; .env на неё не влияет.

Так смена SMTP для одного канала не может случайно сломать другой.

Только стандартная библиотека (smtplib + email.message.EmailMessage) —
без новых зависимостей.

    tls = "ssl"      — неявный TLS (SMTP_SSL), порт 465 (Яндекс);
    tls = "starttls" — обычное подключение + STARTTLS, порт 587;
    tls = "none"     — без шифрования (только локальная отладка).
"""

import contextlib
import html
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings
from .mail_settings import effective_mail_config

logger = logging.getLogger("aios.mailer")

CONFIRM_SUBJECT = "Подтверждение регистрации в AIOS"

CONFIRM_BODY_TEMPLATE = """Здравствуйте!

Вы зарегистрировались в AIOS Технологического института.

Подтвердите адрес электронной почты — перейдите по ссылке:

{link}

Ссылка действует 48 часов. Если вы не регистрировались в AIOS,
просто проигнорируйте это письмо — учётная запись не активируется.
"""

BUTTON_COLOR = "#5D6C50"


class MailError(RuntimeError):
    """Письмо не удалось отправить (сеть, SMTP-сервер, отклонён логин)."""


def _connect(host: str, port: int, tls: str, user: str, password: str, timeout: int = 30) -> smtplib.SMTP:
    """Открывает и авторизует одно SMTP-соединение по явно переданным параметрам."""
    if tls == "starttls":
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    elif tls == "none":
        server = smtplib.SMTP(host, port, timeout=timeout)
    else:  # "ssl" — неявный TLS (порт 465, Яндекс)
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    if user:
        server.login(user, password)
    return server


def send_confirmation_email(to: str, link: str) -> str:
    """Отправляет письмо со ссылкой подтверждения — только через .env
    (AIOS_SMTP_*), независимо от настроек раздела «Рассылка писем».

    Возвращает "sent" — письмо ушло, или "fallback" — SMTP не настроен
    и ссылка напечатана в лог (dev-режим). Ошибка соединения/отправки
    поднимает MailError.
    """
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning(
            "[DEV] SMTP не настроен — подтверждение e-mail %s по ссылке: %s", to, link
        )
        return "fallback"

    from_addr = settings.smtp_from or settings.smtp_user
    msg = EmailMessage()
    msg["Subject"] = CONFIRM_SUBJECT
    msg["From"] = formataddr(("AIOS Технологического института", from_addr))
    msg["To"] = to
    msg.set_content(CONFIRM_BODY_TEMPLATE.format(link=link))

    try:
        server = _connect(
            settings.smtp_host, settings.smtp_port, settings.smtp_tls,
            settings.smtp_user, settings.smtp_password,
        )
        try:
            server.send_message(msg)
        finally:
            with contextlib.suppress(Exception):
                server.quit()
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc
    return "sent"


def render_email_html(body_text: str, button_label: str = "", button_url: str = "") -> str:
    """Оборачивает обычный текст администратора в готовый HTML-шаблон письма.

    body_text — простой текст: пустая строка разделяет абзацы, одиночный
    перевод строки внутри абзаца становится <br>. HTML-спецсимволы всегда
    экранируются — администратор пишет текст, а не разметку, битую вёрстку
    прислать нельзя. button_label/button_url — необязательная кнопка-ссылка
    в конце письма (например, «Программа мастерской» → ссылка на программу).
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", (body_text or "").strip()) if p.strip()]
    body_parts = [
        '<p style="margin:0 0 16px;">' + html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    ]
    button_html = ""
    if (button_url or "").strip():
        label = html.escape((button_label or "").strip() or "Подробнее")
        url = html.escape(button_url.strip(), quote=True)
        button_html = (
            '<div style="text-align:center;margin:24px 0;">'
            f'<a href="{url}" target="_blank" rel="noopener" style="background:{BUTTON_COLOR};'
            'color:#ffffff;text-decoration:none;padding:14px 28px;border-radius:6px;'
            'font-family:Arial,Helvetica,sans-serif;font-size:16px;display:inline-block;">'
            f"{label}</a></div>"
        )
    inner = "".join(body_parts) + button_html
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"></head>"
        "<body style=\"margin:0;padding:0;background:#F4F6EF;\">"
        "<div style=\"max-width:640px;margin:0 auto;padding:24px;"
        "font-family:Arial,Helvetica,sans-serif;color:#2C3128;line-height:1.5;\">"
        f"{inner}"
        "</div></body></html>"
    )


def send_with_config(
    recipients: list[str],
    subject: str,
    body_text: str,
    cfg,
) -> dict:
    """Отправка писем через ЯВНО переданную SMTP-конфигурацию (MailConfig).

    Канал писем агентов пользователя (notify с личными настройками SMTP,
    см. user_mail.py): конфигурация приходит снаружи, глобальные настройки
    платформы не читаются. Тот же формат письма, что у рассылки, —
    _build ниже собирает из render_email_html.
    """
    from_addr = cfg.from_email or cfg.user
    if not from_addr:
        raise MailError("Не задан адрес отправителя в SMTP-настройках")

    html_body = render_email_html(body_text, "", "")
    text_body = (body_text or "").strip()

    def _build(to: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((cfg.from_name or "AIOS", from_addr))
        msg["To"] = to
        msg.set_content(text_body or " ")
        msg.add_alternative(html_body, subtype="html")
        return msg

    try:
        server = _connect(cfg.host, cfg.port, cfg.tls, cfg.user, cfg.password)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc

    sent: list[str] = []
    failed: list[dict] = []
    try:
        for to in recipients:
            try:
                server.send_message(_build(to))
                sent.append(to)
            except (OSError, smtplib.SMTPException) as exc:
                failed.append({"email": to, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        with contextlib.suppress(Exception):
            server.quit()
    return {"sent": sent, "failed": failed}


def send_bulk_html_email(
    recipients: list[str],
    subject: str,
    body_text: str,
    button_label: str = "",
    button_url: str = "",
) -> dict:
    """Отправляет одно и то же письмо списку адресов через одно SMTP-
    соединение. Настройки — исключительно из раздела «Рассылка писем»
    (AIOS_RUNTIME_DIR/newsletter_smtp.json), независимо от .env.

    Возвращает {"sent": [адреса], "failed": [{"email", "error"}]}. Один
    отклонённый адрес не прерывает рассылку остальным.
    """
    cfg = effective_mail_config()
    if not cfg.host:
        raise MailError("SMTP не настроен — задайте параметры в разделе «Рассылка писем»")
    from_addr = cfg.from_email or cfg.user
    if not from_addr:
        raise MailError("Не задан адрес отправителя — заполните «Адрес отправителя» в настройках SMTP")

    html_body = render_email_html(body_text, button_label, button_url)
    text_body = (body_text or "").strip()
    if (button_url or "").strip():
        text_body += f"\n\n{(button_label or 'Подробнее').strip()}: {button_url.strip()}"

    def _build(to: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((cfg.from_name or "AIOS", from_addr))
        msg["To"] = to
        msg.set_content(text_body or " ")
        msg.add_alternative(html_body, subtype="html")
        return msg

    try:
        server = _connect(cfg.host, cfg.port, cfg.tls, cfg.user, cfg.password)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc

    sent: list[str] = []
    failed: list[dict] = []
    try:
        for to in recipients:
            try:
                server.send_message(_build(to))
                sent.append(to)
            except (OSError, smtplib.SMTPException) as exc:
                failed.append({"email": to, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        with contextlib.suppress(Exception):
            server.quit()
    return {"sent": sent, "failed": failed}
