"""Блочная модель автоматизаций и транслятор в JSON воркфлоу n8n.

Пользователь платформы собирает автоматизацию из простых блоков — триггер
и линейная цепочка шагов, — а этот модуль превращает её в ноды и связи
воркфлоу n8n (nodes + connections). Прямого редактирования нод нет:
упрощённый конструктор — единственный источник правды, поэтому JSON в n8n
всегда можно пересобрать из блоков без потерь.

Триггеры:
    manual    — кнопка «Запустить» в AIOS (webhook-нода, responseMode:
                last node, чтобы вызов дождался цепочки и вернул её результат);
    webhook   — внешний вызов (та же webhook-нода, путь генерируется
                aios-<uuid>; показывается владельцу для внешних систем);
    schedule  — расписание каждые N минут (scheduleTrigger).

Шаги:
    agent     — «ИИ-агент AIOS»: HTTP-нода на внутренний эндпоинт
                /api/internal/agent-step (секрет X-AIOS-Service-Key);
    http      — HTTP-запрос (метод/URL/заголовки/JSON-тело);
    email     — письмо через SMTP-credential AIOS (провижинится в n8n
                автоматически при первом сохранении, см. роутер).

В текстовых полях шагов работает токен {{prev}} — «результат предыдущего
шага»: он транслируется в выражение n8n `=… + JSON.stringify($json) + …`.
"""

from __future__ import annotations

import json
import re
import uuid

PREV_TOKEN = "{{prev}}"

TRIGGER_MANUAL = "manual"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_SCHEDULE = "schedule"
TRIGGER_TYPES = (TRIGGER_MANUAL, TRIGGER_WEBHOOK, TRIGGER_SCHEDULE)

STEP_AGENT = "agent"
STEP_HTTP = "http"
STEP_EMAIL = "email"
STEP_TYPES = (STEP_AGENT, STEP_HTTP, STEP_EMAIL)

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

MAX_STEPS = 10
MAX_HEADERS = 10
MAX_PROMPT_CHARS = 8_000
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 120
INTERVAL_MIN = 1
INTERVAL_MAX = 525_600  # год в минутах

URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class BlockError(ValueError):
    """Некорректный блок автоматизации (текст для 422)."""


# ----------------------------------------------------------------------
# Нормализация блоков


def normalize_name(raw: object) -> str:
    name = str(raw or "").strip()
    if not name:
        raise BlockError("Укажите название автоматизации")
    if len(name) > MAX_NAME_CHARS:
        raise BlockError(f"Название слишком длинное (до {MAX_NAME_CHARS} символов)")
    return name


def normalize_trigger(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise BlockError("Триггер не задан")
    kind = str(raw.get("type") or "").strip()
    if kind not in TRIGGER_TYPES:
        raise BlockError(
            "Тип триггера должен быть одним из: "
            + ", ".join(f"«{t}»" for t in TRIGGER_TYPES)
        )
    trigger: dict = {"type": kind}
    if kind == TRIGGER_SCHEDULE:
        try:
            minutes = int(raw.get("interval_minutes"))
        except (TypeError, ValueError):
            raise BlockError("Интервал расписания должен быть числом минут")
        if not (INTERVAL_MIN <= minutes <= INTERVAL_MAX):
            raise BlockError(f"Интервал расписания: от {INTERVAL_MIN} до {INTERVAL_MAX} минут")
        trigger["interval_minutes"] = minutes
    return trigger


def normalize_steps(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise BlockError("Нужен хотя бы один шаг после триггера")
    if len(raw) > MAX_STEPS:
        raise BlockError(f"Слишком много шагов (до {MAX_STEPS})")
    steps = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise BlockError(f"Шаг {i}: не объект")
        kind = str(item.get("type") or "").strip()
        prefix = f"Шаг {i}"
        if kind == STEP_AGENT:
            steps.append(_norm_agent(item, prefix))
        elif kind == STEP_HTTP:
            steps.append(_norm_http(item, prefix))
        elif kind == STEP_EMAIL:
            steps.append(_norm_email(item, prefix))
        else:
            raise BlockError(
                f"{prefix}: тип шага должен быть одним из: "
                + ", ".join(f"«{t}»" for t in STEP_TYPES)
            )
    return steps


def _norm_agent(item: dict, prefix: str) -> dict:
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        raise BlockError(f"{prefix}: укажите промпт для агента")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise BlockError(f"{prefix}: промпт слишком длинный (до {MAX_PROMPT_CHARS} символов)")
    return {"type": STEP_AGENT, "prompt": prompt}


def _norm_http(item: dict, prefix: str) -> dict:
    url = str(item.get("url") or "").strip()
    if not URL_RE.match(url.replace(PREV_TOKEN, "x")):
        raise BlockError(f"{prefix}: URL должен начинаться с http:// или https://")
    method = str(item.get("method") or "GET").strip().upper()
    if method not in HTTP_METHODS:
        raise BlockError(f"{prefix}: метод должен быть одним из {', '.join(HTTP_METHODS)}")
    step: dict = {"type": STEP_HTTP, "method": method, "url": url}

    raw_headers = item.get("headers")
    if isinstance(raw_headers, list) and raw_headers:
        headers = []
        for h in raw_headers[:MAX_HEADERS]:
            if not isinstance(h, dict):
                continue
            name = str(h.get("name") or "").strip()
            value = str(h.get("value") or "")
            if name:
                headers.append({"name": name[:200], "value": value[:2000]})
        if headers:
            step["headers"] = headers

    body = str(item.get("body") or "").strip()
    if body and method != "GET":
        if len(body) > MAX_TEXT_CHARS:
            raise BlockError(f"{prefix}: тело запроса слишком длинное (до {MAX_TEXT_CHARS} символов)")
        if PREV_TOKEN not in body:
            try:
                json.loads(body)
            except ValueError:
                raise BlockError(f"{prefix}: тело должно быть корректным JSON (например, {{\"key\": \"value\"}})")
        step["body"] = body
    return step


def _norm_email(item: dict, prefix: str) -> dict:
    to = str(item.get("to") or "").strip()
    recipients = [addr.strip() for addr in re.split(r"[;,]", to) if addr.strip()]
    if not recipients:
        raise BlockError(f"{prefix}: укажите хотя бы одного получателя")
    bad = [a for a in recipients if not EMAIL_RE.match(a)]
    if bad:
        raise BlockError(f"{prefix}: некорректный адрес получателя: {bad[0]}")
    subject = str(item.get("subject") or "").strip()
    if not subject:
        raise BlockError(f"{prefix}: укажите тему письма")
    text = str(item.get("text") or "").strip()
    if not text:
        raise BlockError(f"{prefix}: укажите текст письма")
    if len(text) > MAX_TEXT_CHARS:
        raise BlockError(f"{prefix}: текст письма слишком длинный (до {MAX_TEXT_CHARS} символов)")
    return {"type": STEP_EMAIL, "to": recipients[:10], "subject": subject[:500], "text": text}


def new_webhook_path() -> str:
    """Путь production-webhook этой автоматизации: aios-<uuid>."""
    return f"aios-{uuid.uuid4().hex[:16]}"


# ----------------------------------------------------------------------
# Трансляция в ноды n8n


def _expr(text: str) -> str:
    """JS-выражение из текста с токенами {{prev}}: 'Сводка: {{prev}}'
    → '"Сводка: " + JSON.stringify($json)'. Без токена — строковый литерал."""
    parts = text.split(PREV_TOKEN)
    out: list[str] = []
    for i, part in enumerate(parts):
        if part:
            out.append(json.dumps(part, ensure_ascii=False))
        if i < len(parts) - 1:
            out.append("JSON.stringify($json)")
    return " + ".join(out) if out else '""'


def _value(text: str) -> str:
    """Значение поля ноды: с {{prev}} — выражение n8n (префикс =), иначе как есть."""
    return f"={_expr(text)}" if PREV_TOKEN in text else text


def _trigger_node(trigger: dict, webhook_path: str) -> dict:
    if trigger["type"] == TRIGGER_SCHEDULE:
        return {
            "parameters": {
                "rule": {
                    "interval": [
                        {"field": "minutes", "minutesInterval": trigger["interval_minutes"]}
                    ]
                }
            },
            "name": "Триггер расписания",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 0],
        }
    # manual и webhook — одна и та же webhook-нода; отличается лишь то,
    # кто по ней стучится (кнопка AIOS или внешняя система)
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": webhook_path,
            "responseMode": "lastNode",
            "options": {},
        },
        "name": "Триггер",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
        "webhookId": str(uuid.uuid4()),
    }


def _agent_node(step: dict, index: int, *, url: str, service_key: str, owner_id: str) -> dict:
    body = (
        "={{ JSON.stringify({ prompt: "
        + _expr(step["prompt"])
        + f', owner_id: {json.dumps(owner_id, ensure_ascii=False)}'
        + " }) }}"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": url,
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "X-AIOS-Service-Key", "value": service_key},
                    {"name": "Content-Type", "value": "application/json"},
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": 300},
        },
        "name": f"Агент AIOS {index}",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [260 * index, 0],
    }


def _http_node(step: dict, index: int) -> dict:
    params: dict = {"method": step["method"], "url": _value(step["url"])}
    if step.get("headers"):
        params["sendHeaders"] = True
        params["headerParameters"] = {
            "parameters": [
                {"name": h["name"], "value": _value(h["value"])} for h in step["headers"]
            ]
        }
    if step.get("body"):
        params["sendBody"] = True
        params["specifyBody"] = "json"
        params["jsonBody"] = _value(step["body"])
    params["options"] = {"timeout": 300}
    return {
        "parameters": params,
        "name": f"HTTP-запрос {index}",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [260 * index, 0],
    }


def _email_node(step: dict, index: int, *, credential_id: str, from_email: str) -> dict:
    return {
        "parameters": {
            "fromEmail": from_email,
            "toEmail": ", ".join(step["to"]),
            "subject": _value(step["subject"]),
            "text": _value(step["text"]),
            "options": {},
        },
        "name": f"Письмо {index}",
        "type": "n8n-nodes-base.emailSend",
        "typeVersion": 2.1,
        "position": [260 * index, 0],
        "credentials": {
            "n8n-nodes-base/emailSend": {"id": credential_id, "name": "AIOS SMTP"}
        },
    }


def translate(
    *,
    trigger: dict,
    steps: list[dict],
    webhook_path: str,
    agent_step_url: str,
    service_key: str,
    owner_id: str,
    email_credential_id: str = "",
    email_from: str = "",
) -> tuple[list, dict]:
    """Блоки → (nodes, connections) для POST/PUT /api/v1/workflows."""
    nodes: list[dict] = [_trigger_node(trigger, webhook_path)]
    for i, step in enumerate(steps, start=1):
        if step["type"] == STEP_AGENT:
            nodes.append(
                _agent_node(
                    step,
                    i,
                    url=agent_step_url,
                    service_key=service_key,
                    owner_id=owner_id,
                )
            )
        elif step["type"] == STEP_HTTP:
            nodes.append(_http_node(step, i))
        else:
            if not email_credential_id:
                raise BlockError(
                    "Шаг «Письмо» недоступен: SMTP не настроен (админ-панель → Рассылка)"
                )
            nodes.append(
                _email_node(
                    step,
                    i,
                    credential_id=email_credential_id,
                    from_email=email_from or "aios@localhost",
                )
            )
    connections: dict = {}
    for source, target in zip(nodes, nodes[1:]):
        connections[source["name"]] = {"main": [[{"node": target["name"], "type": "main", "index": 0}]]}
    return nodes, connections
