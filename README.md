# MailAfrica Agent

Give [MailAfrica](https://mailafrica.online) agentic power. This service does
two things:

1. **MCP server** — exposes the entire MailAfrica API (send/receive email,
   domains, webhooks, wallet) as tools to any AI agent (Claude Desktop, Claude
   Code, Cursor, or any MCP client), so the agent can operate your mail
   programmatically. It ships in two flavours: a local **stdio** server driven
   by your `MAIL_...` key, and a hosted **streamable-HTTP** server
   (`mcp.mailafrica.online`) where every customer signs in with CamelAccounts
   and the tools act on *their own* account.
2. **Auto-reply agent** — when email arrives at one of your inbound addresses,
   a webhook fires, an LLM (via the [Ngamia](https://docs.ngamia.cc)
   OpenAI-compatible gateway) reads the full conversation and writes a reply,
   and MailAfrica sends it back — from your own domain if you configure it.

```
Incoming email ──► MailAfrica inbound ──► webhook ──► this service ──► Ngamia (LLM)
 (support@you.com)   (parses & stores)   (signed)      │                    │
                                                        │  thread memory     │
                                                        │  (SQLite)          │
                                                        └────► reply sent via MailAfrica outbound
                                                              (auto or draft, per-address config)
```

The **auto-reply config itself lives in MailAfrica's database** (the same
endpoints the web app uses), so you can turn a mailbox's auto-reply on/off
from the **MailAfrica dashboard** — Inbox → select an address → *AI
Auto-reply* — and this service just reads that config before replying.

---

## Table of contents

- [Architecture](#architecture)
- [Components](#components)
- [How authentication works](#how-authentication-works)
- [Setup](#setup)
- [Configuration reference](#configuration-reference)
- [Running](#running)
- [MCP tools reference](#mcp-tools-reference)
- [The auto-reply agent](#the-auto-reply-agent)
- [Conversation / threading model](#conversation--threading-model)
- [Safety](#safety)
- [Deployment (VPS)](#deployment-vps)
- [Development](#development)

---

## Architecture

Three pieces talk to each other:

| Piece | Technology | Role |
|---|---|---|
| **MailAfrica** | Go API (`api.mailafrica.online`) | The email infrastructure. Receives inbound email, parses it, stores it, delivers webhook notifications, and sends outbound email. |
| **Ngamia** | OpenAI-compatible gateway (`api.ngamia.cc/v1`) | The brain. Generates reply text from a system prompt + conversation history. One `ngm_...` key, many models, TZS credit billing. |
| **MailAfrica Agent** (this repo) | Python (FastMCP + FastAPI + SQLite) | The glue. Exposes MailAfrica as MCP tools, receives webhook notifications, keeps thread memory, calls Ngamia, sends replies. |

Three processes run from this repo (they can share the same SQLite file thanks
to WAL mode):

```
┌────────────────────────────────────────────┐
│  Process 1: `mailafrica-agent mcp`         │  stdio MCP server,
│  → spoken to by Claude/Cursor/any agent    │  credentials from .env
├────────────────────────────────────────────┤
│  Process 2: `mailafrica-agent mcp-http`    │  streamable-HTTP MCP server,
│  → mcp.mailafrica.online (multi-tenant     │  OAuth (CamelAccounts login);
│    OAuth; per-user credentials)            │  no platform key involved
├────────────────────────────────────────────┤
│  Process 3: `mailafrica-agent webhook`     │  FastAPI HTTP server,
│  → receives MailAfrica webhook POSTs       │  HMAC-verified
└────────────────────────────────────────────┘
```

---

## Components

### `mailafrica_agent/mailafrica.py` — MailAfrica API client

A thin `httpx` client over `https://api.mailafrica.online/api/...`. Every
method maps to a MailAfrica endpoint and unwraps the `{success, data, errors}`
response envelope, raising `MailAfricaError` on failure. All endpoints accept
the same `MAIL_...` API key via the `X-API-Key` header. Besides the mail
operations it also exposes the agent-config endpoints
(`get_agent_config` / `set_agent_config` / `list_agent_configs` /
`draft_reply`) — that is how the pipeline and the `agent_*` MCP tools talk to
MailAfrica's database.

### `mailafrica_agent/ngamia.py` — Ngamia client

A thin wrapper over the official OpenAI SDK pointed at
`https://api.ngamia.cc/v1` with your `ngm_...` key. Because Ngamia mirrors
OpenAI's wire format exactly, model ids from `GET /v1/models` are passed
through verbatim (e.g. `openai/gpt-4o-mini`) — nothing is hardcoded.

### `mailafrica_agent/store.py` — conversation-memory store

SQLite (via `aiosqlite`) holding just the message history: one `conversations`
row per turn (`user` / `assistant`), keyed by thread, so the LLM sees the
whole conversation, not just the latest email. Auto-reply *configuration*
does **not** live here anymore — it lives in MailAfrica's own database, the
single source of truth shared with the web app (see
[The auto-reply agent](#the-auto-reply-agent)).

### `mailafrica_agent/agent.py` — the auto-reply pipeline

`Agent.handle_message(message_id, address_id)`:

1. Fetches the full message from MailAfrica.
2. **Safety gate** — drops bounces (`MAILER-DAEMON`, `postmaster`),
   auto-responders (`X-Auto-Reply`, `Auto-Submitted: auto`, `Precedence: auto`),
   and bulk mail (`List-Unsubscribe`). These are never replied to.
3. Loads the address's config **from MailAfrica** (`GET /api/agent/configs/{id}`);
   on a network error it falls back to the default persona / mode rather than
   drop the message.
4. Appends the incoming message to its thread, builds the LLM prompt
   (`system` persona + full thread history).
5. Asks Ngamia for a reply.
6. Records the reply in the thread, then either **sends** it (`auto` mode)
   via MailAfrica outbound or **returns it unsent** (`draft` mode).

### `mailafrica_agent/mcp_server.py` — the MCP surface

`build_server(Runtime)` returns a `FastMCP` server with the tools in the
[tools reference](#mcp-tools-reference). `Runtime` is a small container that
wires the client, the store and the agent together and manages their
lifespans (connect on start, close on exit). Tools resolve their credentials
*per request*: under OAuth each call rides the connecting user's MailAfrica
JWT (`mail_for_context` / `agent_for_context`); under stdio they fall back to
the platform key, so the two modes share the same tool code.

### `mailafrica_agent/mcp_oauth.py` — the remote MCP OAuth server

The multi-tenant server is its own OAuth *authorization server* (RFC 6749 +
PKCE, served by the `mcp` SDK at `/register`, `/authorize`, `/token`,
`/revoke`, `/.well-known/...`), with the user identity delegated to
CamelAccounts: `/authorize` redirects to CamelAccounts; our
`/oauth/callback` exchanges the code, asks MailAfrica's
`/api/auth/camel-accounts/mcp` endpoint for that user's JWT + refresh cookie,
stashes the session, and returns an MCP authorization code to the client.
`MailAfricaProvider` implements the storage: client secrets and delegated
refresh tokens are Fernet-encrypted at rest (`MCP_CIPHER_KEY` /
`MCP_CIPHER_KEY_PATH`), OAuth tokens are single-use and stored as SHA-256
hashes, and refresh tokens rotate on every use (60-minute access / 30-day
refresh, mirroring MailAfrica's own cookie lifecycle). Revocation deletes the
session so the user's keys can never be used again.

### `mailafrica_agent/webhook.py` — the webhook receiver

A FastAPI app with one endpoint, `POST /webhooks/mailafrica`. It:

1. (If configured) verifies the HMAC-SHA256 signature MailAfrica attaches,
   rejecting forged deliveries.
2. Checks the event type (`inbound.message_received`).
3. Kicks off `Agent.handle_message(...)` as a background task so MailAfrica
   gets a fast `2xx` and never retries.
4. Returns `{"status": "queued"}`.

### `mailafrica_agent/__main__.py` — CLI

`mailafrica-agent mcp | mcp-http | webhook | check`.

---

## How authentication works

There are **three separate credential boundaries** in this system. Keep them
straight:

### 1. MailAfrica API key (`MAILAFRICA_API_KEY`)

- A `MAIL_...` key minted in the MailAfrica dashboard (`POST /api/apikeys`).
- Sent on **every** request this service makes to MailAfrica in the
  `X-API-Key` header. MailAfrica's auth middleware accepts either
  `X-API-Key` or a JWT `Authorization: Bearer`, so this one key is enough
  for inbound, outbound, domains, webhooks and billing.
- It lives **only** in `.env` on the server. It is never put in prompts, MCP
  tool call transcripts, or logs.

### 2. Webhook signature (`AGENT_WEBHOOK_SECRET`)

- When MailAfrica delivers a webhook notification, it signs the raw request
  body with the webhook's secret:
  `signature = hex( HMAC-SHA256(secret, body) )`.
- MailAfrica sends that in the `X-Signature` and `X-Webhook-Signature`
  headers. The agent compares the recomputed HMAC against the provided value
  with a constant-time compare (`hmac.compare_digest`) before doing anything.
- Set `AGENT_WEBHOOK_SECRET` to the same secret you used when you created the
  webhook in MailAfrica. If it is empty, signatures are **not** verified
  (fine for local testing, bad for the internet).

### 3. Ngamia key (`NGAMIA_API_KEY`)

- An `ngm_...` key used by the OpenAI SDK as `Authorization: Bearer <key>`
  when calling `POST /v1/chat/completions` on `api.ngamia.cc`.
- Stored only in `.env`. Never logged or sent to MailAfrica.

### 4. MCP transport

- The stdio MCP server runs over **stdio**: your MCP client (Claude Desktop,
  Claude Code, Cursor) spawns `mailafrica-agent mcp` as a local child process.
  There is no network listener and no client authentication — the client is
  whoever can launch the process, and it reads credentials from `.env` on
  the same machine. Do not run the stdio server on a shared host.

### 5. Remote MCP OAuth (multi-tenant)

- `mailafrica-agent mcp-http` serves the same tools at
  `mcp.mailafrica.online`. It is its own OAuth authorization server:
  a customer opens `/authorize`, signs in with **CamelAccounts**, and is
  redirected back with an MCP access token bound to *their* MailAfrica
  account. Every tool call then forwards that user's MailAfrica JWT
  (`Authorization: Bearer ...`) — the hosted server never holds, sees, or
  uses your `MAIL_...` key.
- Secrets at rest (MCP client secrets, delegated refresh cookies) are
  Fernet-encrypted in `mcp.key` (mode `0600`) on the server; the file is
  git-ignored and lives only on the VPS.
- In production the reverse proxy terminates TLS and must forward the real
  `Host` header (the SDK rejects unknown hosts).

### Auth flow diagram

```
MailAfrica ──X-API-Key: MAIL_...──►  agent  ──Bearer ngm_...──►  Ngamia
   │                                  ▲
   └─ webhook POST ──► agent (HMAC verify with AGENT_WEBHOOK_SECRET)
                        │
                        ▼
   reply POST ──X-API-Key: MAIL_...──► MailAfrica outbound
```

---

## Setup

Requirements: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
cd MailAfrica-Agent
cp .env.example .env

# .env
MAILAFRICA_API_KEY=MAIL_xxx
NGAMIA_API_KEY=ngm_xxx

uv sync
uv run mailafrica-agent check      # validates config + both APIs
```

`check` prints your MailAfrica balance, the number of Ngamia models, and the
configured model — a quick way to confirm both keys work.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `MAILAFRICA_API_BASE` | `https://api.mailafrica.online` | MailAfrica API root. |
| `MAILAFRICA_API_KEY` | _(none)_ | `MAIL_...` key; authenticates every MailAfrica call. |
| `NGAMIA_BASE_URL` | `https://api.ngamia.cc/v1` | Ngamia OpenAI-compatible endpoint. |
| `NGAMIA_API_KEY` | _(none)_ | `ngm_...` key for chat completions. |
| `NGAMIA_MODEL` | `openai/gpt-4o-mini` | Model id used for replies. Pull the current list with the `list_models` tool. |
| `AGENT_WEBHOOK_SECRET` | _(empty)_ | Secret for verifying MailAfrica webhook signatures. Set it in production. |
| `AGENT_DB_PATH` | `agent.db` | SQLite file for conversation/thread memory (config lives in MailAfrica). |
| `AGENT_DEFAULT_PERSONA` | built-in assistant persona | Fallback system prompt for addresses without a custom persona. |
| `AGENT_DEFAULT_MODE` | `off` | Default mode: `auto`, `draft` or `off`. |
| `AGENT_HOST` / `AGENT_PORT` | `0.0.0.0` / `8097` | Webhook HTTP server bind address. |
| `MCP_PORT` | `8098` | Remote MCP server bind address. |
| `MCP_ISSUER_URL` | `https://mcp.mailafrica.online` | OAuth issuer URL for the remote MCP server. |
| `MCP_RESOURCE_URL` | `https://mcp.mailafrica.online/mcp` | The protected resource (`/mcp`) URL. |
| `MCP_SERVICE_DOCUMENTATION_URL` | `https://docs.mailafrica.online/mcp` | OAuth service-documentation link. |
| `MCP_CAMEL_REDIRECT_URI` | `https://mcp.mailafrica.online/oauth/callback` | CamelAccounts callback; path is served automatically. |
| `MCP_ACCESS_TOKEN_TTL_MINUTES` | `60` | MCP access-token lifetime. |
| `MCP_REFRESH_TOKEN_TTL_DAYS` | `30` | MCP refresh-token lifetime. |
| `MCP_CIPHER_KEY` | _(empty)_ | Fernet key for secrets at rest. Empty → generate into `MCP_CIPHER_KEY_PATH`. |
| `MCP_CIPHER_KEY_PATH` | `.mcp_fernet.key` | Where the Fernet key file lives (git-ignored, `0600`). |
| `MCP_REGISTRATION_ENABLED` | `true` | Allow dynamic client registration (RFC 7591). |
| `MCP_DEFAULT_SCOPES` / `MCP_REQUIRED_SCOPES` | _(empty)_ | Optional OAuth scopes for the remote MCP server. |
| `CAMEL_ACCOUNTS_ISSUER_URL` | _(empty)_ | CamelAccounts base URL used by the MCP authorize flow. |
| `CAMEL_ACCOUNTS_CLIENT_ID` | _(empty)_ | CamelAccounts OAuth client id (redirect URI = `MCP_CAMEL_REDIRECT_URI`). |
| `CAMEL_ACCOUNTS_CLIENT_SECRET` | _(empty)_ | CamelAccounts OAuth client secret. |

---

## Running

### MCP server (agent-facing)

```bash
uv run mailafrica-agent mcp
```

Register in Claude Desktop — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mailafrica-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/MailAfrica-Agent", "mailafrica-agent", "mcp"]
    }
  }
}
```

### Remote MCP server (multi-tenant OAuth)

```bash
uv run mailafrica-agent mcp-http
```

Customers connect any OAuth-capable MCP client to
`https://mcp.mailafrica.online/mcp`. The client dynamic-registers, then the
user signs in with CamelAccounts on first use; afterwards tokens refresh
silently for up to 30 days before another sign-in is needed.

```json
{
  "mcpServers": {
    "mailafrica": {
      "type": "http",
      "url": "https://mcp.mailafrica.online/mcp"
    }
  }
}
```

### Webhook server (auto-reply)

```bash
uv run mailafrica-agent webhook
```

### Docker deployment

Build and start with **Docker Compose**:

```bash
docker compose up -d --build
```

Or run directly with **Docker**:

```bash
docker build -t mailafrica-agent .
docker run -d --name mailafrica-agent \
  --env-file .env \
  -p 8097:8097 \
  -v mailafrica_agent_data:/app/data \
  mailafrica-agent
```

Then in the MailAfrica dashboard create a webhook on the inbound address you
want to auto-answer, with:
- **URL**: `https://<your-host>/webhooks/mailafrica`
- **Secret**: same value as `AGENT_WEBHOOK_SECRET`

---

## MCP tools reference

### Outbound

| Tool | What it does |
|---|---|
| `send_email` | Send email; optional `from_domain_id` / `from_address` to send from a verified domain. |
| `list_outbound_emails` | Recent outbound sends. |
| `get_outbound_email` | One outbound message. |

### Inbound

| Tool | What it does |
|---|---|
| `list_inbound_addresses` | Your receiving addresses. |
| `create_inbound_address` | New address (`foo@yourdomain.com` or `foo@mailafrica.online`). |
| `delete_inbound_address` | Stop receiving at an address. |
| `list_inbound_messages` | Mail received at an address (optionally unread only). |
| `get_inbound_message` | Full message: from, subject, body, headers. |

### Sending domains

| Tool | What it does |
|---|---|
| `list_sending_domains` | Domains + status + DNS records. |
| `add_sending_domain` | Register a domain; returns DKIM/CNAME records to publish. |
| `verify_sending_domain` | Re-check DNS with ZeptoMail. |

### Webhooks

| Tool | What it does |
|---|---|
| `list_webhooks` | Webhooks on an address. |
| `create_webhook` | New webhook (URL + secret). |
| `delete_webhook` | Remove a webhook. |
| `test_webhook` | Trigger a test ping. |

### Billing

| Tool | What it does |
|---|---|
| `wallet_balance` | Current TZS balance. |

### Agent (auto-reply)

| Tool | What it does |
|---|---|
| `agent_config` | Configure an address: `mode` (`auto`/`draft`/`off`), `persona`, and the From used for replies. Saved to MailAfrica — same store the web app UI writes. |
| `agent_get_config` | Current config for one address. |
| `agent_status` | All configured addresses and their modes (from MailAfrica). |
| `agent_draft` | One-off reply preview via MailAfrica (never sends). |
| `agent_handle_message` | Run the pipeline for a message now. |
| `list_models` | Models available on Ngamia. |

---

## The auto-reply agent

Auto-reply is **per inbound address** and **off by default**. You can
configure it two ways — both write to the same place (MailAfrica's database):

1. **The web app**: MailAfrica dashboard → **Inbox** → select an address →
   the *AI Auto-reply* card. Pick a mode, paste persona instructions, choose
   the From address, save — and hit *Test a reply* for a no-send preview.
2. **The MCP tool**: pick the address id (from `list_inbound_addresses`) and
   call `agent_config`:

```
agent_config(
  address_id=12,
  mode="auto",
  reply_from_domain_id=23,       # a verified sending domain
  reply_from_address="noreply@ziadapos.com",
  persona="You handle Tanzanian customers in Swahili or English, politely and concisely."
)
```

Modes:

- **`auto`** — the reply is generated and **sent**.
- **`draft`** — the reply is generated and returned to the caller but **not
  sent** (great for reviewing quality first).
- **`off`** — nothing happens.

If `reply_from_domain_id`/`reply_from_address` are unset, replies are sent
from the platform sender; set them to reply from your own verified domain.

**Flow when mail arrives:** MailAfrica stores the message → fires the webhook
to this service → the pipeline fetches the address's config from MailAfrica
→ if `auto`, generates + sends the reply (recorded in thread memory); if
`draft`, generates but doesn't send.

---

## Conversation / threading model

Threads are keyed by `(sender email, normalized subject)`:

```
normalized subject = lower( subject with leading "Re:" / "Fw:" / "Fwd:" removed )
thread key = "<sender>::::<normalized subject>"
```

MailAfrica's inbound parser exposes message headers but not `In-Reply-To`, so
this is the most reliable way to reconstruct a conversation. Every inbound
message is stored as a `user` turn and every generated reply as an
`assistant` turn in the same thread. The LLM prompt is:

```
[system persona]
[oldest user turn]
[oldest assistant turn]
...
[latest user turn]      <- the email you are replying to
```

The last 40 turns are used, so multi-turn conversations stay coherent.

---

## Safety

- **Reply-loop protection**: never auto-reply to bounces, `postmaster`,
  auto-responders (`X-Auto-Reply`, `Auto-Submitted: auto`, `Precedence: auto`),
  or bulk mail (`List-Unsubscribe`).
- **Off by default**: auto-reply must be explicitly enabled per address.
- **Draft mode** lets you review replies before they go out.
- **Signature verification**: webhook deliveries are HMAC-verified when
  `AGENT_WEBHOOK_SECRET` is set.
- **No credential leakage**: keys live in `.env` only; prompts never contain
  them; the persona never reveals it is an automated agent.
- **SQLite WAL**: the two processes share `agent.db` safely.

---

## Deployment (VPS)

The agent deploys to the same VPS that hosts MailAfrica's API, fronted by the
`agent.mailafrica.online` (webhook) and `mcp.mailafrica.online` (remote MCP)
subdomains. Everything is in `deploy/`:

- **`.github/workflows/deploy.yml`** — on push to `main`, GitHub Actions SSHes
  to the VPS with a restricted deploy key. Pushing triggers an auto-deploy,
  mirroring Mail-API's pipeline.
- **`deploy.sh`** — the forced command on the deploy key: `git pull --ff-only`,
  `uv sync --frozen`, then restarts whichever of `mailafrica-agent-webhook` /
  `mailafrica-agent-mcp` are installed.
- **`deploy/mailafrica-agent-webhook.service`** — systemd unit for the webhook
  process (binds `0.0.0.0:8097`).
- **`deploy/mailafrica-agent-mcp.service`** — systemd unit for the remote MCP
  process (binds `0.0.0.0:8098`, OAuth on `mcp.mailafrica.online`).
- **`deploy/setup_vps.sh`** — one-time root script on the VPS: creates the
  `mailafrica` user, clones the repo, installs uv deps, generates the
  restricted deploy key, installs both units, and prints the exact Caddy/nginx
  snippets (`agent.mailafrica.online` → `127.0.0.1:8097`,
  `mcp.mailafrica.online` → `127.0.0.1:8098`) plus the GitHub secrets to set
  (`SSH_PRIVATE_KEY`, `SSH_HOST`, `SSH_PORT`, `SSH_USER`).

```bash
# from your laptop, run once on the VPS as root:
scp deploy/setup_vps.sh root@<VPS>:/tmp/
ssh root@<VPS> bash /tmp/setup_vps.sh
# then: edit the .env, systemctl enable --now mailafrica-agent-webhook mailafrica-agent-mcp
```

**Going live on `mcp.mailafrica.online` (once, by the platform team):**

1. Point the subdomain at the VPS and add a reverse proxy → `127.0.0.1:8098`
   (Caddy preserves `Host`; the SDK requires the real host).
2. Create a CamelAccounts OAuth **client** with redirect URI
   `https://mcp.mailafrica.online/oauth/callback` and put its id/secret +
   `CAMEL_ACCOUNTS_ISSUER_URL` in `.env`.
3. `systemctl enable --now mailafrica-agent-mcp`; the Fernet key file is
   created on first boot (`MCP_CIPHER_KEY_PATH`, `0600`).

The **stdio MCP server runs locally on your machine** (nothing to deploy);
only the webhook and remote MCP processes live on the VPS.

---

## Development

```bash
uv sync --dev        # installs dev deps if any
uv run ruff check .  # lint
uv run ruff format . # format
```

The smoke test in `/tmp` style runs against the live API; for offline testing,
stub `Runtime.mail.get_message` and `Runtime.ngamia.complete` to avoid real
calls while exercising the pipeline.
