"""Remote (HTTP) MCP authentication.

The remote server at mcp.mailafrica.online is a *multi-tenant* MCP endpoint:
every customer connects, signs in with CamelAccounts, and the tools then act
on *their own* MailAfrica account. It never holds, or uses, the platform
MAIL_... key at request time.

Flow (OAuth 2.1 Authorization Code + PKCE, served by the ``mcp`` SDK):

    MCP client  ->  mcp.mailafrica.online/authorize   (SDK route)
                ->  CamelAccounts /oauth/authorize    (our authorize())
                ->  user signs in / consents
    CamelAccounts -> mcp.mailafrica.online/oauth/callback  (our custom route)
                ->  exchange code for a CamelAccounts token
                ->  POST /api/auth/camel-accounts/mcp (MailAfrica) -> user JWT+cookie
                ->  we store the delegated session, redirect to the MCP client
    MCP client  ->  /token  (SDK route) -> access + refresh tokens
    MCP client  ->  /mcp    (SDK route, bearer) -> tool calls resolve the user
                     from the access token and ride *their* credentials
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .agent import Agent
from .config import Settings
from .mailafrica import MailAfricaClient, MailAfricaError

logger = logging.getLogger("mailafrica_agent.mcp_oauth")

_CAMEL_SCOPE = "openid profile email"
_CALLBACK_MIN = 600  # an authorization must complete within 10 minutes


# ---------------------------------------------------------------------------
# secrets at rest
# ---------------------------------------------------------------------------


class MCPTokenCipher:
    """Fernet encryption for client secrets and delegated refresh tokens.

    The key comes from ``MCP_CIPHER_KEY`` if set, otherwise from a key file
    (``MCP_CIPHER_KEY_PATH``, default ``.mcp_fernet.key``) that is created
    with 0600 perms on first run so sessions survive restarts.
    """

    def __init__(self, key: bytes):
        self.fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> MCPTokenCipher:
        key = settings.mcp_cipher_key
        if key:
            return cls(key.encode() if isinstance(key, str) else key)
        path = str(
            settings.mcp_cipher_key_path
            if hasattr(settings, "mcp_cipher_key_path")
            else ".mcp_fernet.key"
        )
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return cls(fh.read().strip())
        key_bytes = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key_bytes + b"\n")
        return cls(key_bytes)

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - defensive
            raise ValueError("unable to decrypt secret") from exc


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


# ---------------------------------------------------------------------------
# CamelAccounts OAuth client (used by the authorize flow's second leg)
# ---------------------------------------------------------------------------


class CamelAccountsClient:
    """Small RFC 6749 client for CamelAccounts: build authorize URLs and
    exchange codes. The resulting access token is handed to MailAfrica's
    /api/auth/camel-accounts/mcp endpoint, which re-validates it server-side
    and resolves the MailAfrica account."""

    def __init__(self, settings: Settings):
        self.issuer = settings.camel_accounts_issuer_url.rstrip("/")
        self.client_id = settings.camel_accounts_client_id
        self.client_secret = settings.camel_accounts_client_secret
        self.redirect_uri = settings.mcp_camel_redirect_uri
        self._http = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": _CAMEL_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        import urllib.parse

        return f"{self.issuer}/oauth/authorize?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str) -> str:
        resp = await self._http.post(
            f"{self.issuer}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": code_verifier,
            },
        )
        body = resp.json()
        if resp.status_code >= 400 or not body.get("access_token"):
            logger.warning(
                "camelaccounts code exchange failed: %s",
                body.get("error_description") or resp.status_code,
            )
            raise ValueError("camelaccounts code exchange failed")
        return body["access_token"]


# ---------------------------------------------------------------------------
# OAuth authorization server implementation (RFC 8414 / RFC 7591 surface
# served by the mcp SDK; this provider supplies the storage + logic).
# ---------------------------------------------------------------------------


class MailAfricaProvider(OAuthAuthorizationServerProvider):
    def __init__(
        self, store: Any, cipher: MCPTokenCipher, camel: CamelAccountsClient, settings: Settings
    ):
        self.store = store
        self.cipher = cipher
        self.camel = camel
        self.settings = settings

    # -- client registration (RFC 7591) --------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rec = await self.store.oauth_get_client(client_id)
        if rec is None:
            return None
        secret = self.cipher.decrypt(rec["client_secret_enc"]) if rec["client_secret_enc"] else None
        return OAuthClientInformationFull(
            client_id=rec["client_id"],
            client_id_issued_at=int(rec["created_at"]),
            client_secret=secret,
            client_secret_expires_at=int(rec["expires_at"]) if rec["expires_at"] else None,
            redirect_uris=rec["redirect_uris"],
            token_endpoint_auth_method=rec["token_endpoint_auth_method"],
            grant_types=rec["grant_types"],
            response_types=rec["response_types"],
            scope=rec["scope"],
            client_name=rec["client_name"],
            client_uri=AnyHttpUrl(rec["client_uri"]) if rec["client_uri"] else None,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        secret_enc = (
            self.cipher.encrypt(client_info.client_secret) if client_info.client_secret else ""
        )
        await self.store.oauth_register_client(
            client_id=client_info.client_id,
            client_secret_enc=secret_enc,
            token_endpoint_auth_method=client_info.token_endpoint_auth_method
            or "client_secret_post",
            redirect_uris=[str(u) for u in client_info.redirect_uris or []],
            grant_types=client_info.grant_types or ["authorization_code", "refresh_token"],
            response_types=client_info.response_types or ["code"],
            scope=client_info.scope,
            client_name=client_info.client_name,
            client_uri=str(client_info.client_uri) if client_info.client_uri else None,
            created_at=client_info.client_id_issued_at or time.time(),
            expires_at=client_info.client_secret_expires_at,
        )

    # -- authorization (the browser leg redirects to CamelAccounts) ----------

    async def authorize(self, client: OAuthClientInformationFull, params: Any) -> str:
        created = time.time()
        verifier, challenge = _pkce_pair()
        mcp_state = secrets.token_urlsafe(24)
        await self.store.oauth_store_pending(
            state=mcp_state,
            client_id=client.client_id,
            scopes=params.scopes or [],
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            resource=params.resource,
            client_state=params.state,
            camel_verifier_enc=self.cipher.encrypt(verifier),
            expires_at=created + _CALLBACK_MIN,
            created_at=created,
        )
        return self.camel.authorize_url(state=mcp_state, code_challenge=challenge)

    # -- authorization codes --------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, code: str
    ) -> AuthorizationCode | None:
        rec = await self.store.oauth_get_authorization_code(code)
        if rec is None or rec["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=code,
            scopes=rec["scopes"],
            expires_at=rec["expires_at"],
            client_id=rec["client_id"],
            code_challenge=rec["code_challenge"],
            redirect_uri=AnyUrl(rec["redirect_uri"]),
            redirect_uri_provided_explicitly=rec["redirect_uri_provided_explicitly"],
            resource=rec["resource"],
            subject=rec["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, auth_code: AuthorizationCode
    ) -> OAuthToken:
        await self.store.oauth_delete_authorization_code(auth_code.code)
        return await self._issue_tokens(
            client.client_id, auth_code.scopes, auth_code.subject, auth_code.resource
        )

    # -- refresh tokens (rotated on every use) --------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rec = await self.store.oauth_get_refresh_token(refresh_token)
        if rec is None or rec["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            subject=rec["subject"],
            expires_at=int(rec["expires_at"]),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, rt: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        resource = None
        rec = await self.store.oauth_get_refresh_token(rt.token)
        if rec is not None:
            resource = rec["resource"]
        await self.store.oauth_delete_refresh_token(rt.token)
        return await self._issue_tokens(client.client_id, scopes, rt.subject, resource)

    # -- access tokens / revocation --------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        rec = await self.store.oauth_get_access_token(token)
        if rec is None:
            return None
        return AccessToken(
            token=token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            subject=rec["subject"],
            resource=rec["resource"],
            expires_at=int(rec["expires_at"]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        subject = token.subject
        if not subject:  # pragma: no cover - defensive
            return
        await self.store.oauth_delete_session_tokens(token.client_id, subject)
        await self.store.session_revoke(subject)

    async def _issue_tokens(
        self, client_id: str, scopes: list[str], subject: str | None, resource: str | None
    ) -> OAuthToken:
        created = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        access_ttl = max(self.settings.mcp_access_token_ttl_minutes, 1) * 60
        refresh_ttl = max(self.settings.mcp_refresh_token_ttl_days, 1) * 86400
        await self.store.oauth_store_access_token(
            token=access,
            client_id=client_id,
            scopes=scopes,
            subject=subject,
            resource=resource,
            expires_at=created + access_ttl,
            created_at=created,
        )
        await self.store.oauth_store_refresh_token(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            subject=subject,
            resource=resource,
            expires_at=created + refresh_ttl,
            created_at=created,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_ttl,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )


# ---------------------------------------------------------------------------
# Per-user delegation: CamelAccounts login -> MailAfrica JWT + refresh token
# ---------------------------------------------------------------------------


class OAuthGroup:
    """Bundles the pieces the remote MCP needs and resolves each authenticated
    request down to the *user's own* MailAfrica credentials."""

    def __init__(self, runtime: Any, settings: Settings):
        self.runtime = runtime
        self.settings = settings
        self.cipher = MCPTokenCipher.from_settings(settings)
        self.camel = CamelAccountsClient(settings)
        self.provider = MailAfricaProvider(runtime.store, self.cipher, self.camel, settings)
        self._jwt_cache: dict[int, tuple[str, float]] = {}
        runtime.oauth = self

    async def aclose(self) -> None:
        await self.camel.aclose()

    async def handle_camel_callback(self, request: Request) -> Response:
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "missing code or state"},
                status_code=400,
            )
        pending = await self.runtime.store.oauth_get_pending(state)
        if pending is None or pending["expires_at"] < time.time():
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "authorization expired or not found",
                },
                status_code=400,
            )
        try:
            camel_token = await self.camel.exchange_code(
                code, self.cipher.decrypt(pending["camel_verifier_enc"])
            )
            data = await self._mcp_auth(camel_token)
        except (ValueError, MailAfricaError, httpx.HTTPError) as exc:
            logger.warning("camelaccounts callback failed: %s", exc)
            await self.runtime.store.oauth_delete_pending(state)
            return JSONResponse(
                {
                    "error": "access_denied",
                    "error_description": "authentication with MailAfrica failed",
                },
                status_code=403,
            )

        user = data.get("user") or {}
        subject = str(user.get("id", ""))
        if not subject or not data.get("refresh_token"):
            await self.runtime.store.oauth_delete_pending(state)
            return JSONResponse(
                {
                    "error": "server_error",
                    "error_description": "unexpected MailAfrica session response",
                },
                status_code=500,
            )

        await self.runtime.store.session_upsert(
            subject=subject,
            email=user.get("email") or "",
            mail_user_id=int(subject),
            mail_refresh_enc=self.cipher.encrypt(data["refresh_token"]),
            last_used_at=time.time(),
        )

        created = time.time()
        auth_code = secrets.token_urlsafe(32)
        await self.runtime.store.oauth_store_authorization_code(
            code=auth_code,
            client_id=pending["client_id"],
            scopes=pending["scopes"],
            code_challenge=pending["code_challenge"],
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            resource=pending["resource"],
            subject=subject,
            expires_at=created + _CALLBACK_MIN,
            created_at=created,
        )
        await self.runtime.store.oauth_delete_pending(state)

        return RedirectResponse(
            url=construct_redirect_uri(
                pending["redirect_uri"], code=auth_code, state=pending["client_state"]
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def _mcp_auth(self, camel_access_token: str) -> dict[str, Any]:
        """Exchange a CamelAccounts token for a user-scoped MailAfrica JWT."""
        async with httpx.AsyncClient(
            base_url=self.settings.mailafrica_api_base.rstrip("/"), timeout=20.0
        ) as http:
            resp = await http.post(
                "/api/auth/camel-accounts/mcp", json={"token": camel_access_token}
            )
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
            )
        return body.get("data") or {}

    # -- per-request resolution ------------------------------------------------

    async def mail_for_user(self, subject: str) -> MailAfricaClient:
        session = await self.runtime.store.session_get(subject)
        if session is None:
            raise MailAfricaError(
                "SESSION_INVALID", "MailAfrica MCP session is missing or revoked", 401
            )
        jwt = await self._mail_jwt(session)
        return MailAfricaClient(self.settings.mailafrica_api_base, bearer_token=jwt)

    async def agent_for_user(self, subject: str) -> Agent:
        client = await self.mail_for_user(subject)
        return Agent(self.settings, client, self.runtime.ngamia, self.runtime.store)

    async def _mail_jwt(self, session: Any) -> str:
        """Return a fresh MailAfrica JWT for the session, re-issuing (and
        rotating) its refresh token when the cached one is near expiry."""
        now = time.time()
        cached = self._jwt_cache.get(session.mail_user_id)
        if cached and cached[1] > now + 120:
            return cached[0]
        data = await self._refresh_mail_token(session)
        jwt = data.get("token", "")
        new_refresh = data.get("refresh_token")
        if new_refresh:
            await self.runtime.store.session_update_refresh(
                session.subject, self.cipher.encrypt(new_refresh), now
            )
        ttl = max(self.settings.mcp_access_token_ttl_minutes, 1) * 60
        self._jwt_cache[session.mail_user_id] = (jwt, now + ttl * 0.9)
        return jwt

    async def _refresh_mail_token(self, session: Any) -> dict[str, Any]:
        refresh = self.cipher.decrypt(session.mail_refresh_enc)
        async with httpx.AsyncClient(
            base_url=self.settings.mailafrica_api_base.rstrip("/"), timeout=20.0
        ) as http:
            resp = await http.post("/api/auth/refresh", json={"refresh_token": refresh})
            try:
                body = resp.json()
            except ValueError:
                body = {}
        if resp.status_code >= 400 or not body.get("success", False):
            raise MailAfricaError(
                "TOKEN_REFRESH_FAILED", "MailAfrica refresh token rejected", resp.status_code
            )
        return body.get("data") or {}


def build_http_app(server: Any, runtime: Any) -> Starlette:
    """Wrap FastMCP's streamable-HTTP app so its session manager *and* our
    runtime lifecycle share uvicorn's lifespan (run() alone only starts the
    session manager)."""
    base = server.streamable_http_app()
    routes = list(getattr(base, "routes", []))
    middleware = list(getattr(base, "user_middleware", []))

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await runtime.connect()
        manager = getattr(server, "_session_manager", None)
        task = asyncio.create_task(_drive_session_manager(manager)) if manager else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await runtime.aclose()

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


async def _drive_session_manager(manager: Any) -> None:
    try:
        async with manager.run():
            await asyncio.Event().wait()
    except asyncio.CancelledError:  # noqa: PERF203 - expected on shutdown
        pass
    except Exception:  # noqa: BLE001
        logger.exception("mcp session manager worker exited")
