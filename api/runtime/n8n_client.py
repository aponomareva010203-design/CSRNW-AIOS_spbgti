"""Клиент public API n8n (self-hosted, /api/v1).

Обёртка над httpx для работы от имени сервисного ключа владельца инстанса:
CRUD воркфлоу, publish/unpublish, executions, credentials и вызов webhook.
Клиент создается на запрос (объём небольшой, зато нет состояния и проблем
с ресетами конфигурации); transport инъектится в тестах через httpx.MockTransport.

Ошибки n8n нормализуются в N8nApiError со статусом и читаемым сообщением —
роутер превращает их в HTTPException, фронтенд показывает detail.
"""

from __future__ import annotations

import httpx


class N8nApiError(RuntimeError):
    """Ошибка ответа n8n public API."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message

    @property
    def not_found(self) -> bool:
        return self.status_code == 404


DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
# read-таймаут большой: publish воркфлоу и вызов webhook со «тяжёлым» шагом
# агента могут занимать десятки секунд


class N8nClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.transport = transport

    # ------------------------------------------------------------------
    # Транспорт

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        api: bool = True,
    ) -> httpx.Response:
        headers = {}
        if api:
            headers["X-N8N-API-KEY"] = self.api_key
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                resp = await client.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise N8nApiError(502, f"n8n недоступен: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise N8nApiError(resp.status_code, _error_message(resp))
        return resp

    async def _json(self, method: str, path: str, **kwargs) -> dict | list:
        resp = await self._request(method, path, **kwargs)
        try:
            data = resp.json()
        except ValueError as exc:
            raise N8nApiError(502, f"n8n вернул не-JSON ответ ({path})") from exc
        return data if isinstance(data, (dict, list)) else {}

    # ------------------------------------------------------------------
    # Воркфлоу

    async def list_workflows(self, limit: int = 100, cursor: str | None = None) -> dict:
        params: dict = {"limit": min(max(int(limit), 1), 250)}
        if cursor:
            params["cursor"] = cursor
        return await self._json("GET", "/api/v1/workflows", params=params)

    async def get_workflow(self, workflow_id: str) -> dict:
        return await self._json("GET", f"/api/v1/workflows/{workflow_id}")

    async def create_workflow(
        self, *, name: str, nodes: list, connections: dict, settings: dict
    ) -> dict:
        return await self._json(
            "POST",
            "/api/v1/workflows",
            json_body={"name": name, "nodes": nodes, "connections": connections, "settings": settings},
        )

    async def update_workflow(
        self, workflow_id: str, *, name: str, nodes: list, connections: dict, settings: dict
    ) -> dict:
        return await self._json(
            "PUT",
            f"/api/v1/workflows/{workflow_id}",
            json_body={"name": name, "nodes": nodes, "connections": connections, "settings": settings},
        )

    async def delete_workflow(self, workflow_id: str) -> dict:
        return await self._json("DELETE", f"/api/v1/workflows/{workflow_id}")

    async def publish_workflow(self, workflow_id: str) -> dict:
        # publish/unpublish — актуальные имена; activate/deactivate объявлены
        # deprecated (в n8n v1 действие называлось «активацией»)
        return await self._json("POST", f"/api/v1/workflows/{workflow_id}/publish")

    async def unpublish_workflow(self, workflow_id: str) -> dict:
        return await self._json("POST", f"/api/v1/workflows/{workflow_id}/unpublish")

    # ------------------------------------------------------------------
    # Выполнения

    async def list_executions(
        self, *, workflow_id: str | None = None, limit: int = 50, cursor: str | None = None
    ) -> dict:
        params: dict = {"limit": min(max(int(limit), 1), 250)}
        if workflow_id:
            params["workflowId"] = workflow_id
        if cursor:
            params["cursor"] = cursor
        return await self._json("GET", "/api/v1/executions", params=params)

    async def retry_execution(self, execution_id: str) -> dict:
        return await self._json("POST", f"/api/v1/executions/{execution_id}/retry")

    async def stop_execution(self, execution_id: str) -> dict:
        return await self._json("POST", f"/api/v1/executions/{execution_id}/stop")

    # ------------------------------------------------------------------
    # Credentials (для шага «Письмо»: SMTP-credential из настроек AIOS)

    async def list_credentials(self) -> dict:
        return await self._json("GET", "/api/v1/credentials", params={"limit": 250})

    async def create_credential(self, *, name: str, type_: str, data: dict) -> dict:
        return await self._json(
            "POST",
            "/api/v1/credentials",
            json_body={"name": name, "type": type_, "data": data},
        )

    async def delete_credential(self, credential_id: str) -> dict:
        return await self._json("DELETE", f"/api/v1/credentials/{credential_id}")

    # ------------------------------------------------------------------
    # Webhook: в public API нет «запустить сейчас», поэтому каждая
    # автоматизация собирается с webhook-триггером (respond = last node),
    # и «Запустить» в AIOS — это POST на production webhook

    async def call_webhook(self, path: str, payload: dict | None = None) -> httpx.Response:
        return await self._request(
            "POST", f"/webhook/{path}", json_body=payload or {}, api=False
        )


def _error_message(resp: httpx.Response) -> str:
    """Читаемое сообщение из тела ошибки n8n: {message, reason?}."""
    try:
        data = resp.json()
    except ValueError:
        return f"n8n вернул HTTP {resp.status_code}"
    message = str(data.get("message") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if message and reason:
        return f"n8n: {message} ({reason})"
    return message or f"n8n вернул HTTP {resp.status_code}"
