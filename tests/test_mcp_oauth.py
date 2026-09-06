"""Offline tests for the remote (OAuth) MCP server.

Covers the authorization-server storage and the CamelAccounts delegation
without any network: CamelAccounts exchange, MailAfrica MCP-auth and refresh
legs are stubbed; a real temp SQLite store exercises the persistence paths
(encrypted-at-rest secrets, single-use codes, rotating refresh tokens,
per-user session resolution and revocation).
"""

import asyncio
import base64
import hashlib
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from mailafrica_agent.config import Settings
from mailafrica_agent.mcp_oauth import OAuthGroup
from mailafrica_agent.store import Store


def _settings(dbfile: str) -> Settings:
    return Settings(
        mailafrica_api_key="SK-myfakekey",
        agent_db_path=dbfile,
        mcp_cipher_key="",
        mcp_cipher_key_path=dbfile + ".key",
        camel_accounts_issuer_url="https://ca.example",
        camel_accounts_client_id="ca-client",
        camel_accounts_client_secret="ca-secret",
        mcp_camel_redirect_uri="https://mcp.mailafrica.online/oauth/callback",
        mcp_access_token_ttl_minutes=60,
        mcp_refresh_token_ttl_days=30,
    )


class _Params:
    state = "client-state-abc"
    scopes = ["email"]
    code_challenge = "abc123challenge"
    redirect_uri = "http://127.0.0.1:3333/callback"
    redirect_uri_provided_explicitly = True
    resource = None


class McpOAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.dbfile = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.dbfile)
        asyncio.run(self.store.connect())
        runtime = type("R", (), {})()
        runtime.store = self.store
        self.group = OAuthGroup(runtime, _settings(self.dbfile))

        async def _exchange(code, verifier):
            return "CAMEL_ACCESS_TOKEN"

        async def _mcp_auth(tok):
            return {
                "user": {"id": 42, "email": "customer@example.com"},
                "token": "JWTVALUE",
                "refresh_token": "REF_v1",
            }

        async def _refresh(sess):
            return {"token": "JWTVALUE2", "refresh_token": "REF_v2"}

        self.group.camel.exchange_code = _exchange
        self.group._mcp_auth = _mcp_auth
        self.group._refresh_mail_token = _refresh
        self.cid = "client-1"

    def tearDown(self) -> None:
        asyncio.run(self.store.close())

    def _register(self) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        asyncio.run(
            self.group.provider.register_client(
                OAuthClientInformationFull(
                    client_id=self.cid,
                    client_secret="plain-secret",
                    redirect_uris=["http://127.0.0.1:3333/callback"],
                    token_endpoint_auth_method="client_secret_post",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                )
            )
        )

    def test_secret_encrypted_at_rest_and_roundtrips(self) -> None:
        self._register()
        rec = asyncio.run(self.store.oauth_get_client(self.cid))
        self.assertNotEqual(rec["client_secret_enc"], "plain-secret")
        got = asyncio.run(self.group.provider.get_client(self.cid))
        self.assertEqual(got.client_secret, "plain-secret")

    def test_full_flow(self) -> None:
        self._register()
        url = asyncio.run(
            self.group.provider.authorize(
                asyncio.run(self.group.provider.get_client(self.cid)), _Params()
            )
        )
        mcp_state = parse_qs(urlparse(url).query)["state"][0]
        pending = asyncio.run(self.store.oauth_get_pending(mcp_state))
        self.assertEqual(pending["client_state"], "client-state-abc")

        scope = {
            "type": "http",
            "query_string": ("code=C1&state=" + mcp_state).encode(),
            "method": "GET",
            "headers": [],
            "path": "/oauth/callback",
            "server": ("mcp", 80),
            "client": ("1.1.1.1", 1),
            "scheme": "https",
            "app": None,
            "root_path": "",
        }
        resp = asyncio.run(self.group.handle_camel_callback(Request(scope)))
        self.assertEqual(resp.status_code, 302)
        code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
        session = asyncio.run(self.store.session_get("42"))
        self.assertEqual(session.email, "customer@example.com")
        self.assertTrue(session.mail_refresh_enc)

        client = asyncio.run(self.group.provider.get_client(self.cid))
        auth_code = asyncio.run(self.group.provider.load_authorization_code(client, code))
        token = asyncio.run(self.group.provider.exchange_authorization_code(client, auth_code))
        self.assertEqual(token.token_type, "Bearer")
        self.assertEqual(asyncio.run(self.store.oauth_get_authorization_code(code)), None)
        at = asyncio.run(self.group.provider.load_access_token(token.access_token))
        self.assertEqual(at.subject, "42")

        rt = asyncio.run(self.group.provider.load_refresh_token(client, token.refresh_token))
        token2 = asyncio.run(self.group.provider.exchange_refresh_token(client, rt, ["email"]))
        self.assertNotEqual(token2.access_token, token.access_token)
        self.assertEqual(asyncio.run(self.store.oauth_get_refresh_token(token.refresh_token)), None)

    def test_refresh_token_reuse_fails(self) -> None:
        self.test_full_flow()
        rec = asyncio.run(self.store.oauth_get_refresh_token("SYNTH"))
        self.assertIsNone(rec)

    def test_mail_for_user_uses_delegated_session_and_revocation(self) -> None:
        subject = "42"
        created = __import__("time").time()
        asyncio.run(
            self.store.session_upsert(
                subject=subject,
                email="customer@example.com",
                mail_user_id=42,
                mail_refresh_enc=self.group.cipher.encrypt("REF_vX"),
                last_used_at=created,
            )
        )
        client = asyncio.run(self.group.mail_for_user(subject))
        self.assertEqual(client._client.headers.get("Authorization"), "Bearer JWTVALUE2")

        asyncio.run(self.store.session_revoke(subject))
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self.group.mail_for_user(subject))
        self.assertIn("SESSION", str(ctx.exception))


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


if __name__ == "__main__":
    unittest.main()
