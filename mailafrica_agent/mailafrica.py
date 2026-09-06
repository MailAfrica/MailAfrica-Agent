from __future__ import annotations

from typing import Any

import httpx


class MailAfricaError(Exception):
    """Raised when the MailAfrica API returns an error envelope."""

    def __init__(self, code: str, message: str, status: int, request_id: str | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        super().__init__(f"MailAfrica {code}: {message} (status {status})")


class MailAfricaClient:
    """Minimal client for the MailAfrica API (https://api.mailafrica.online).

    Authenticates as either a single account (a MAIL_... API key, used by the
    stdio MCP mode and the webhook agent) or as an individual user (a Bearer
    JWT minted for them via /api/auth/*, used by the remote OAuth MCP mode).
    Responses come wrapped as {"success": bool, "message": str, "data": {...},
    "errors": [...]}.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        bearer_token: str | None = None,
        timeout: float = 20.0,
    ):
        headers = {"Accept": "application/json"}
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif api_key:
            headers["X-API-Key"] = api_key
        else:
            raise ValueError("MailAfricaClient requires an api_key or bearer_token")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._client.request(method, f"/api{path}", **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not body.get("success", False):
            errors = body.get("errors") or []
            first = errors[0] if errors else {}
            raise MailAfricaError(
                code=first.get("code", "HTTP_ERROR"),
                message=first.get("message", body.get("message", resp.text[:200])),
                status=resp.status_code,
                request_id=body.get("request_id"),
            )
        return body.get("data") or {}

    # --- outbound -----------------------------------------------------------

    async def send_email(
        self,
        to: list[str],
        subject: str,
        text_body: str | None = None,
        html_body: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        from_domain_id: int | None = None,
        from_address: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": to, "subject": subject}
        if text_body:
            payload["text_body"] = text_body
        if html_body:
            payload["html_body"] = html_body
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc
        if from_domain_id is not None:
            payload["from_domain_id"] = from_domain_id
        if from_address:
            payload["from_address"] = from_address
        return await self._request("POST", "/outbound/emails", json=payload)

    async def list_outbound(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/outbound/emails?per_page={limit}")
        return data if isinstance(data, list) else []

    async def get_outbound(self, message_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/outbound/emails/{message_id}")

    # --- inbound ------------------------------------------------------------

    async def create_address(self, local_part: str, label: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"local_part": local_part}
        if label:
            payload["label"] = label
        return await self._request("POST", "/inbound/addresses", json=payload)

    async def list_addresses(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/inbound/addresses")
        return data if isinstance(data, list) else []

    async def delete_address(self, address_id: int) -> dict[str, Any]:
        return await self._request("DELETE", f"/inbound/addresses/{address_id}")

    async def list_messages(
        self, address_id: int, unread: bool = False, limit: int = 20, page: int = 1
    ) -> list[dict[str, Any]]:
        query = f"address_id={address_id}&per_page={limit}&page={page}"
        if unread:
            query += "&unread=true"
        data = await self._request("GET", f"/inbound/messages?{query}")
        return data if isinstance(data, list) else []

    async def get_message(self, message_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/inbound/messages/{message_id}")

    async def mark_read(self, message_id: int) -> dict[str, Any]:
        return await self._request("PATCH", f"/inbound/messages/{message_id}/read")

    # --- sending domains ----------------------------------------------------

    async def list_sending_domains(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/domains/")
        return data if isinstance(data, list) else []

    async def add_sending_domain(self, domain: str) -> dict[str, Any]:
        return await self._request("POST", "/domains/", json={"domain": domain})

    async def verify_sending_domain(self, domain_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/domains/{domain_id}/verify")

    # --- webhooks -----------------------------------------------------------

    async def list_webhooks(self, address_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/webhook/webhooks?address_id={address_id}")
        return data if isinstance(data, list) else []

    async def create_webhook(
        self, address_id: int, url: str, secret: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"address_id": address_id, "url": url}
        if secret:
            payload["secret"] = secret
        return await self._request("POST", "/webhook/webhooks", json=payload)

    async def delete_webhook(self, webhook_id: int) -> dict[str, Any]:
        return await self._request("DELETE", f"/webhook/webhooks/{webhook_id}")

    async def test_webhook(self, webhook_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/webhook/webhooks/{webhook_id}/test")

    # --- agent config (single source of truth lives in Mail-API) ------------

    async def get_agent_config(self, address_id: int) -> dict[str, Any] | None:
        """Fetch the auto-reply config Mail-API holds for an inbound address.
        Returns None when nothing has been saved yet."""
        try:
            return await self._request("GET", f"/agent/configs/{address_id}")
        except MailAfricaError as exc:
            if exc.status == 404:
                return None
            raise

    async def set_agent_config(self, address_id: int, **kwargs: Any) -> dict[str, Any]:
        """Upsert the auto-reply config for an inbound address."""
        payload: dict[str, Any] = {}
        for key in ("mode", "persona", "enabled", "reply_from_domain_id", "reply_from_address"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        return await self._request("PUT", f"/agent/configs/{address_id}", json=payload)

    async def list_agent_configs(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/agent/configs")
        return data if isinstance(data, list) else []

    async def draft_reply(self, address_id: int, subject: str, text_body: str) -> str:
        """Ask Mail-API for a one-off auto-reply preview (never sends)."""
        data = await self._request(
            "POST",
            f"/agent/configs/{address_id}/draft",
            json={"subject": subject, "text_body": text_body},
        )
        return data.get("draft") or ""

    # --- billing ------------------------------------------------------------

    async def balance(self) -> dict[str, Any]:
        return await self._request("GET", "/billing/balance")
