from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite


def sha256_hex(value: str) -> str:
    """Opaque tokens are stored sha256-hashed, mirroring MailAfrica's own
    refresh-token design: a DB leak cannot be replayed as a live token."""
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class ConversationTurn:
    role: str  # user | assistant
    content: str
    message_id: int
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class MCPSession:
    """A MailAfrica user's delegated session: CamelAccounts subject -> MailAfrica
    user_id + refresh token (Fernet-encrypted at rest)."""

    subject: str
    email: str
    mail_user_id: int
    mail_refresh_enc: str
    created_at: float
    last_used_at: float
    revoked: bool = False


class Store:
    """SQLite-backed conversation-memory store.

    Auto-reply *configuration* now lives in MailAfrica's own database (the
    single source of truth shared with the web app); this store keeps only
    thread history, so the LLM sees the full conversation. Threads are keyed
    by normalized subject + the original sender, because MailAfrica's inbound
    parser exposes headers but not In-Reply-To, so reply chains are best
    reconstructed from (subject, sender). Replies sent by the agent are
    recorded back into the same thread.
    """

    def __init__(self, path: str):
        self.path = path

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        # WAL + busy timeout let the webhook process and the MCP stdio
        # process share the same SQLite file without "database is locked".
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_key  TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                message_id  INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_thread
                ON conversations (thread_key, created_at);

            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id                TEXT PRIMARY KEY,
                client_secret_enc        TEXT NOT NULL,
                token_endpoint_auth_method TEXT NOT NULL,
                redirect_uris            TEXT NOT NULL,
                grant_types              TEXT NOT NULL,
                response_types           TEXT NOT NULL,
                scope                    TEXT,
                client_name              TEXT,
                client_uri               TEXT,
                created_at               REAL NOT NULL,
                expires_at               REAL
            );
            CREATE TABLE IF NOT EXISTS oauth_pending (
                state              TEXT PRIMARY KEY,
                client_id          TEXT NOT NULL,
                scopes             TEXT NOT NULL,
                code_challenge     TEXT NOT NULL,
                redirect_uri       TEXT NOT NULL,
                resource           TEXT,
                client_state       TEXT,
                camel_verifier_enc TEXT NOT NULL,
                expires_at         REAL NOT NULL,
                created_at         REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                code_hash                     TEXT PRIMARY KEY,
                client_id                     TEXT NOT NULL,
                scopes                        TEXT NOT NULL,
                code_challenge                TEXT NOT NULL,
                redirect_uri                  TEXT NOT NULL,
                redirect_uri_provided_explicitly INTEGER NOT NULL,
                resource                      TEXT,
                subject                       TEXT,
                expires_at                    REAL NOT NULL,
                created_at                    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id  TEXT NOT NULL,
                scopes     TEXT NOT NULL,
                subject    TEXT NOT NULL,
                resource   TEXT,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                revoked    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id  TEXT NOT NULL,
                scopes     TEXT NOT NULL,
                subject    TEXT NOT NULL,
                resource   TEXT,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mcp_sessions (
                subject         TEXT PRIMARY KEY,
                email           TEXT,
                mail_user_id    INTEGER NOT NULL,
                mail_refresh_enc TEXT NOT NULL,
                created_at      REAL NOT NULL,
                last_used_at    REAL NOT NULL,
                revoked         INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await self.db.commit()

    async def close(self) -> None:
        await self.db.close()

    # --- oauth clients ------------------------------------------------------

    async def oauth_register_client(
        self,
        *,
        client_id: str,
        client_secret_enc: str,
        token_endpoint_auth_method: str,
        redirect_uris: list[str],
        grant_types: list[str],
        response_types: list[str],
        scope: str | None,
        client_name: str | None,
        client_uri: str | None,
        created_at: float,
        expires_at: float | None,
    ) -> None:
        cur = await self.db.execute(
            "INSERT INTO oauth_clients "
            "(client_id, client_secret_enc, token_endpoint_auth_method, redirect_uris, "
            " grant_types, response_types, scope, client_name, client_uri, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                client_id,
                client_secret_enc,
                token_endpoint_auth_method,
                json.dumps(redirect_uris),
                json.dumps(grant_types),
                json.dumps(response_types),
                scope,
                client_name,
                client_uri,
                created_at,
                expires_at,
            ),
        )
        await cur.close()
        await self.db.commit()

    async def oauth_get_client(self, client_id: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "client_secret_enc": row["client_secret_enc"],
            "token_endpoint_auth_method": row["token_endpoint_auth_method"],
            "redirect_uris": json.loads(row["redirect_uris"]),
            "grant_types": json.loads(row["grant_types"]),
            "response_types": json.loads(row["response_types"]),
            "scope": row["scope"],
            "client_name": row["client_name"],
            "client_uri": row["client_uri"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    # --- pending authorizations (CamelAccounts leg) -------------------------

    async def oauth_store_pending(
        self,
        *,
        state: str,
        client_id: str,
        scopes: list[str],
        code_challenge: str,
        redirect_uri: str,
        resource: str | None,
        client_state: str | None,
        camel_verifier_enc: str,
        expires_at: float,
        created_at: float,
    ) -> None:
        cur = await self.db.execute(
            "INSERT OR REPLACE INTO oauth_pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state,
                client_id,
                json.dumps(scopes),
                code_challenge,
                redirect_uri,
                resource,
                client_state,
                camel_verifier_enc,
                expires_at,
                created_at,
            ),
        )
        await cur.close()
        await self.db.commit()

    async def oauth_get_pending(self, state: str) -> dict | None:
        cur = await self.db.execute("SELECT * FROM oauth_pending WHERE state = ?", (state,))
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "scopes": json.loads(row["scopes"]),
            "code_challenge": row["code_challenge"],
            "redirect_uri": row["redirect_uri"],
            "resource": row["resource"],
            "client_state": row["client_state"],
            "camel_verifier_enc": row["camel_verifier_enc"],
            "expires_at": row["expires_at"],
        }

    async def oauth_delete_pending(self, state: str) -> None:
        await self.db.execute("DELETE FROM oauth_pending WHERE state = ?", (state,))
        await self.db.commit()

    # --- authorization codes -------------------------------------------------

    async def oauth_store_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        scopes: list[str],
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        resource: str | None,
        subject: str | None,
        expires_at: float,
        created_at: float,
    ) -> None:
        cur = await self.db.execute(
            "INSERT INTO oauth_authorization_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sha256_hex(code),
                client_id,
                json.dumps(scopes),
                code_challenge,
                redirect_uri,
                1 if redirect_uri_provided_explicitly else 0,
                resource,
                subject,
                expires_at,
                created_at,
            ),
        )
        await cur.close()
        await self.db.commit()

    async def oauth_get_authorization_code(self, code: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM oauth_authorization_codes WHERE code_hash = ?",
            (sha256_hex(code),),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "code_hash": row["code_hash"],
            "client_id": row["client_id"],
            "scopes": json.loads(row["scopes"]),
            "code_challenge": row["code_challenge"],
            "redirect_uri": row["redirect_uri"],
            "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
            "resource": row["resource"],
            "subject": row["subject"],
            "expires_at": row["expires_at"],
        }

    async def oauth_delete_authorization_code(self, code: str) -> None:
        await self.db.execute(
            "DELETE FROM oauth_authorization_codes WHERE code_hash = ?",
            (sha256_hex(code),),
        )
        await self.db.commit()

    # --- access / refresh tokens ----------------------------------------------

    async def oauth_store_access_token(
        self,
        *,
        token: str,
        client_id: str,
        scopes: list[str],
        subject: str | None,
        resource: str | None,
        expires_at: float,
        created_at: float,
    ) -> None:
        cur = await self.db.execute(
            "INSERT INTO oauth_access_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sha256_hex(token),
                client_id,
                json.dumps(scopes),
                subject,
                resource,
                expires_at,
                created_at,
            ),
        )
        await cur.close()
        await self.db.commit()

    async def oauth_get_access_token(self, token: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM oauth_access_tokens WHERE token_hash = ?",
            (sha256_hex(token),),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "scopes": json.loads(row["scopes"]),
            "subject": row["subject"],
            "resource": row["resource"],
            "expires_at": row["expires_at"],
        }

    async def oauth_store_refresh_token(
        self,
        *,
        token: str,
        client_id: str,
        scopes: list[str],
        subject: str | None,
        resource: str | None,
        expires_at: float,
        created_at: float,
    ) -> None:
        cur = await self.db.execute(
            "INSERT INTO oauth_refresh_tokens VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                sha256_hex(token),
                client_id,
                json.dumps(scopes),
                subject,
                resource,
                expires_at,
                created_at,
            ),
        )
        await cur.close()
        await self.db.commit()

    async def oauth_get_refresh_token(self, token: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ? AND revoked = 0",
            (sha256_hex(token),),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "scopes": json.loads(row["scopes"]),
            "subject": row["subject"],
            "resource": row["resource"],
            "expires_at": row["expires_at"],
        }

    async def oauth_delete_refresh_token(self, token: str) -> None:
        await self.db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token_hash = ?",
            (sha256_hex(token),),
        )
        await self.db.commit()

    async def oauth_delete_session_tokens(self, client_id: str, subject: str | None) -> None:
        """Delete every access/refresh token minted for (client, subject). Used
        by /revoke so revoking any token kills the whole MCP session."""
        if subject is None:
            return
        await self.db.execute(
            "DELETE FROM oauth_access_tokens WHERE client_id = ? AND subject = ?",
            (client_id, subject),
        )
        await self.db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE client_id = ? AND subject = ?",
            (client_id, subject),
        )
        await self.db.commit()

    # --- delegated MailAfrica sessions ---------------------------------------

    async def session_upsert(
        self,
        *,
        subject: str,
        email: str,
        mail_user_id: int,
        mail_refresh_enc: str,
        last_used_at: float,
    ) -> None:
        now = datetime.now(UTC).timestamp()
        cur = await self.db.execute(
            "INSERT INTO mcp_sessions "
            "(subject, email, mail_user_id, mail_refresh_enc, created_at, last_used_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(subject) DO UPDATE SET "
            "  email = excluded.email, mail_user_id = excluded.mail_user_id, "
            "  mail_refresh_enc = excluded.mail_refresh_enc, last_used_at = excluded.last_used_at",
            (subject, email, mail_user_id, mail_refresh_enc, now, last_used_at),
        )
        await cur.close()
        await self.db.commit()

    async def session_get(self, subject: str) -> MCPSession | None:
        cur = await self.db.execute(
            "SELECT * FROM mcp_sessions WHERE subject = ? AND revoked = 0",
            (subject,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return MCPSession(
            subject=row["subject"],
            email=row["email"],
            mail_user_id=row["mail_user_id"],
            mail_refresh_enc=row["mail_refresh_enc"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked=bool(row["revoked"]),
        )

    async def session_update_refresh(
        self, subject: str, mail_refresh_enc: str, last_used_at: float
    ) -> None:
        await self.db.execute(
            "UPDATE mcp_sessions SET mail_refresh_enc = ?, last_used_at = ? WHERE subject = ?",
            (mail_refresh_enc, last_used_at, subject),
        )
        await self.db.commit()

    async def session_revoke(self, subject: str) -> None:
        await self.db.execute(
            "UPDATE mcp_sessions SET revoked = 1 WHERE subject = ?",
            (subject,),
        )
        await self.db.commit()

    # --- conversations ------------------------------------------------------

    @staticmethod
    def thread_key(subject: str, sender: str) -> str:
        """Group a message into a conversation with its (normalized) thread."""
        normalized = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", subject, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized).strip().lower()
        return f"{sender.lower()}:::{normalized or '(no subject)'}"

    async def append_turn(self, thread_key: str, role: str, content: str, message_id: int) -> None:
        cur = await self.db.execute(
            "INSERT INTO conversations (thread_key, role, content, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                thread_key,
                role,
                content,
                message_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        await cur.close()
        await self.db.commit()

    async def get_thread(self, thread_key: str, limit: int = 40) -> list[ConversationTurn]:
        cur = await self.db.execute(
            "SELECT role, content, message_id, created_at FROM conversations "
            "WHERE thread_key = ? ORDER BY id DESC LIMIT ?",
            (thread_key, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        turns = [
            ConversationTurn(
                role=row["role"],
                content=row["content"],
                message_id=row["message_id"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]
        return turns
