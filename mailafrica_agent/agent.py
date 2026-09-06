from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings
from .mailafrica import MailAfricaClient
from .ngamia import NgamiaClient
from .store import Store

logger = logging.getLogger("mailafrica_agent")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")

# Addresses/headers that must never be auto-replied to: they are bounces,
# loop-prone auto responders, or bulk mail. Replying risks reply-loops and
# sending-list penalties.
_SKIP_SENDERS = {"mailer-daemon", "postmaster", "mailerdaemon"}
_AUTO_REPLY_HEADERS = (
    "x-auto-reply",
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
    "x-no-reply",
    "precedence: auto",
    "precedence: bulk",
    "auto-submitted: auto",
)


MAILAFRICA_SUPPORT_SYSTEM_PROMPT = """You are the MailAfrica AI Assistant — an expert developer assistant, support guide, and platform manager for MailAfrica (https://mailafrica.online).

MailAfrica is Tanzania's premier email infrastructure platform providing inbound email processing, webhooks, sender IDs, transactional outbound sending, DNS management, TZS wallet top-ups via USSD push, PDPC compliance, and sandbox SMTP testing.

You have complete knowledge of MailAfrica and docs.mailafrica.online:

### 1. Key Endpoints & Portals
- Public API: https://api.mailafrica.online
- Dashboard: https://app.mailafrica.online
- Documentation: https://docs.mailafrica.online
- Agent Webhook & AI Assistant: https://agent.mailafrica.online

### 2. Authentication
- Bearer JWT tokens for Dashboard sessions.
- `X-API-Key: MAIL_...` header for API key authentication in external applications and SDKs.

### 3. Inbound Infrastructure
- **Sender IDs / Receiving Addresses**: Platform-provided `@mailafrica.online` or custom domains. Up to 100 addresses per account. Local part format: `[a-z0-9-]+`.
- **Custom Receiving Domains**:
  - TXT record: Host `@`, Value `mail-verify=<token>`.
  - MX record: Host `@`, Priority `10`, Target `mx.mailafrica.online`.
  - A record for `mx.mailafrica.online` is managed by MailAfrica.
- **Webhooks**:
  - Delivers parsed inbound email JSON (`event: "inbound.message_received"`).
  - Verified via HMAC-SHA256 headers: `X-Signature` or `X-Webhook-Signature`.

### 4. Outbound Infrastructure
- **Transactional Sending**: `POST /api/v1/outbound/send`. Single/bulk recipients, HTML/text, attachments up to 10MB each (20MB total).
- **Custom Sending Domains**: DKIM + CNAME records via ZeptoMail integration.

### 5. Billing & Pricing (TZS)
- Inbound mailafrica.online: 5 TZS / message.
- Inbound custom domain: 10 TZS / message.
- Outbound mailafrica.online: 5 TZS / recipient.
- Outbound custom domain: 10 TZS / recipient.
- Payments: USSD Push (Vodacom M-Pesa, Tigo Pesa, Airtel Money, HaloPesa) & Checkout.

### 6. Sandbox & Compliance
- Sandbox SMTP: Free disposable testing at `sandbox.mailafrica.online:587`.
- PDPC Compliance: Registered with Tanzania Personal Data Protection Commission.

Guidelines:
- Provide clear, helpful answers with cURL, Python, Node.js or Go code snippets when relevant.
- Be concise, professional, and friendly.
"""


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an outbound email to one or more recipient email addresses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses e.g. ['recipient@example.com']",
                    },
                    "subject": {"type": "string", "description": "Subject of the email"},
                    "text_body": {
                        "type": "string",
                        "description": "Plain text body content of the email",
                    },
                },
                "required": ["to", "subject", "text_body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wallet_balance",
            "description": "Get current MailAfrica wallet balance in TZS.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_inbound_addresses",
            "description": "List inbound receiving email addresses registered on the MailAfrica account.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class Agent:
    """The auto-reply pipeline: inbound message -> thread memory -> Ngamia -> reply.

    Runs either synchronously from the webhook HTTP endpoint (FastAPI) or on
    demand through MCP tools. Never auto-replies to bounces, auto-responders
    or bulk mail, and never reveals credentials or its own system prompt.
    """

    def __init__(
        self, settings: Settings, mail: MailAfricaClient, ngamia: NgamiaClient, store: Store
    ):
        self.settings = settings
        self.mail = mail
        self.ngamia = ngamia
        self.store = store

    async def chat(self, messages: list[dict[str, str]]) -> str:
        llm_messages = [{"role": "system", "content": MAILAFRICA_SUPPORT_SYSTEM_PROMPT}]
        for m in messages:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content"):
                llm_messages.append({"role": m["role"], "content": m["content"]})
        if len(llm_messages) <= 1 or llm_messages[-1]["role"] != "user":
            return "Hello! I am your MailAfrica AI Assistant. How can I help you today?"

        try:
            msg = await self.ngamia.complete_with_tools(llm_messages, CHAT_TOOLS)
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    fn_name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments or "{}")

                    if fn_name == "send_email":
                        to = args.get("to") or []
                        if isinstance(to, str):
                            to = [to]
                        subject = args.get("subject") or "No Subject"
                        text_body = args.get("text_body") or ""
                        result = await self.mail.send_email(
                            to=to, subject=subject, text_body=text_body
                        )
                        result_str = f"Email sent successfully: {result}"
                    elif fn_name == "wallet_balance":
                        res = await self.mail.balance()
                        result_str = f"Wallet balance: {res}"
                    elif fn_name == "list_inbound_addresses":
                        res = await self.mail.list_addresses()
                        result_str = f"Inbound addresses: {res}"
                    else:
                        result_str = "Tool executed."

                    llm_messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tool_call.id,
                                    "type": "function",
                                    "function": {
                                        "name": fn_name,
                                        "arguments": tool_call.function.arguments,
                                    },
                                }
                            ],
                        }
                    )
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result_str,
                        }
                    )

                reply = await self.ngamia.complete(llm_messages)
                return reply
            elif getattr(msg, "content", None):
                return msg.content
        except Exception as exc:
            logger.warning("complete_with_tools fallback to basic completion: %s", exc)

        reply = await self.ngamia.complete(llm_messages)
        return reply

    async def handle_message(self, message_id: int, address_id: int) -> dict[str, Any]:
        msg = await self.mail.get_message(message_id)

        sender = _extract_email(msg.get("from_addr") or "")
        if not sender:
            logger.info("message %s: no usable from address, skipping", message_id)
            return {"message_id": message_id, "action": "skipped", "reason": "no from address"}

        if self._is_no_reply_target(msg):
            logger.info("message %s: bounce/auto-reply/bulk, skipping", message_id)
            return {
                "message_id": message_id,
                "action": "skipped",
                "reason": "bounce or automated sender",
            }

        cfg = await self._address_config(address_id)
        mode = (cfg or {}).get("mode") or self.settings.agent_default_mode
        enabled = cfg.get("enabled", True) if cfg else True
        if mode not in ("auto", "draft") or not enabled:
            return {"message_id": message_id, "action": "off", "mode": mode}

        body = (msg.get("text_body") or "").strip() or (msg.get("html_body") or "").strip()
        if not body:
            return {"message_id": message_id, "action": "skipped", "reason": "empty body"}

        subject = msg.get("subject") or ""
        thread_key = self.store.thread_key(subject, sender)
        await self.store.append_turn(thread_key, "user", body, message_id)

        persona = (cfg or {}).get("persona") or self.settings.agent_default_persona
        history = await self.store.get_thread(thread_key, limit=40)
        llm_messages = [{"role": "system", "content": persona}]
        llm_messages += [{"role": t.role, "content": t.content} for t in history]
        if llm_messages[-1]["role"] != "user":
            llm_messages.append({"role": "user", "content": body})

        logger.info(
            "message %s: generating reply via Ngamia (thread %s)", message_id, thread_key[:40]
        )
        reply = await self.ngamia.complete(llm_messages)

        out = {
            "message_id": message_id,
            "action": "draft" if mode == "draft" else "auto",
            "draft": reply,
        }
        if mode == "draft":
            await self.store.append_turn(thread_key, "assistant", reply, message_id)
            return out

        reply_subject = _prepend_re(subject)
        send_kwargs: dict[str, Any] = {
            "to": [sender],
            "subject": reply_subject,
            "text_body": reply,
        }
        if cfg and cfg.get("reply_from_domain_id"):
            send_kwargs["from_domain_id"] = cfg["reply_from_domain_id"]
        if cfg and cfg.get("reply_from_address"):
            send_kwargs["from_address"] = cfg["reply_from_address"]

        sent = await self.mail.send_email(**send_kwargs)
        out.update({"sent": sent, "to": sender, "subject": reply_subject})
        await self.store.append_turn(thread_key, "assistant", reply, message_id)
        return out

    async def _address_config(self, address_id: int) -> dict[str, Any] | None:
        """The address's auto-reply config from Mail-API (the single source of
        truth). On network failures we fall back to the local defaults rather
        than drop the message."""
        try:
            cfg = await self.mail.get_agent_config(address_id)
        except Exception:
            logger.exception("agent config fetch failed for address %s; using defaults", address_id)
            return None
        if cfg is None:
            logger.info("address %s: no agent config saved yet; using defaults", address_id)
        return cfg

    @staticmethod
    def _is_no_reply_target(msg: dict[str, Any]) -> bool:
        sender = _extract_email(msg.get("from_addr") or "")
        local, _, domain = sender.partition("@")
        if (local or "").lower() in _SKIP_SENDERS or (domain or "").lower().startswith(
            "mailer-daemon"
        ):
            return True
        headers = msg.get("headers") or {}
        raw = ""
        if isinstance(headers, dict):
            raw = "\n".join(f"{k}: {v}" for k, v in headers.items())
        elif isinstance(headers, str):
            raw = headers
        lowered = raw.lower()
        if "list-unsubscribe" in lowered:
            return True
        for marker in _AUTO_REPLY_HEADERS:
            if marker in lowered:
                return True
        return False


def _extract_email(value: str) -> str:
    match = _EMAIL_RE.search(value or "")
    return match.group(0) if match else ""


def _prepend_re(subject: str) -> str:
    if re.match(r"^\s*re\s*:", subject, flags=re.IGNORECASE):
        return subject
    return f"Re: {subject}"
