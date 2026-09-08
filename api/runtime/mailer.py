"""Отправка писем подтверждения e-mail через SMTP.

Только стандартная библиотека (smtplib + email.message.EmailMessage) —
без новых зависимостей. Настройки читаются из конфига (AIOS_SMTP_*):

    AIOS_SMTP_TLS = "ssl"      — неявный TLS (SMTP_SSL), порт 465 (Яндекс);
    AIOS_SMTP_TLS = "starttls" — обычное подключение + STARTTLS, порт 587;
    AIOS_SMTP_TLS = "none"     — без шифрования (только локальная отладка).

Dev-фолбэк: пока AIOS_SMTP_HOST не задан, письмо не отправляется —
ссылка подтверждения печатается в лог сервера, чтобы регистрацию можно
было проверить офлайн.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings

logger = logging.getLogger("aios.mailer")

SUBJECT = "Подтверждение регистрации в AIOS"

BODY_TEMPLATE = """Здравствуйте!

Вы зарегистрировались в AIOS Технологического института.

Подтвердите адрес электронной почты — перейдите по ссылке:

{link}

Ссылка действует 48 часов. Если вы не регистрировались в AIOS,
просто проигнорируйте это письмо — учётная запись не активируется.
"""


class MailError(RuntimeError):
    """Письмо не удалось отправить (сеть, SMTP-сервер, отклонён логин)."""


def send_confirmation_email(to: str, link: str) -> str:
    """Отправляет письмо со ссылкой подтверждения.

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
    msg["Subject"] = SUBJECT
    msg["From"] = formataddr(("AIOS Технологического института", from_addr))
    msg["To"] = to
    msg.set_content(BODY_TEMPLATE.format(link=link))

    timeout = 30
    try:
        if settings.smtp_tls == "starttls":
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        elif settings.smtp_tls == "none":
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        else:  # "ssl" — неявный TLS (порт 465)
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc
    return "sent"
