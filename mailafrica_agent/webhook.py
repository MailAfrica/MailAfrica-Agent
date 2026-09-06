from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Settings
from .mcp_server import Runtime

logger = logging.getLogger("mailafrica_agent.webhook")

MAILAFRICA_SIGNATURE_HEADERS = ("X-Webhook-Signature", "X-Signature")


class ChatRequest(BaseModel):
    messages: list[dict[str, str]]


def create_app(settings: Settings) -> FastAPI:
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.connect()
        yield
        await runtime.aclose()

    app = FastAPI(title="MailAfrica Agent", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat")
    @app.post("/v1/support/chat")
    async def chat(req: ChatRequest) -> JSONResponse:
        try:
            reply = await runtime.agent.chat(req.messages)
            return JSONResponse({"reply": reply})
        except Exception:
            logger.exception("chat endpoint error")
            return JSONResponse(
                {"reply": "I encountered an error processing your query. Please try again."},
                status_code=500,
            )

    @app.post("/webhooks/mailafrica")
    async def mailafrica_webhook(request: Request) -> JSONResponse:
        if settings.agent_webhook_secret:
            body = await request.body()
            if not _verify_signature(body, request, settings.agent_webhook_secret):
                return JSONResponse({"status": "unauthorized"}, status_code=401)
        else:
            body = await request.body()

        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"status": "invalid_json"}, status_code=400)

        if payload.get("event") != "inbound.message_received":
            return JSONResponse({"status": "ignored"})

        message_id = payload.get("message_id")
        address_id = payload.get("address_id")
        if not message_id or not address_id:
            return JSONResponse({"status": "missing_fields"}, status_code=400)

        logger.info("webhook: message %s at address %s", message_id, address_id)

        # Run the LLM pipeline in the background so MailAfrica sees a fast 2xx
        # and won't retry; the store records every turn so no state is lost.
        import asyncio

        async def _run() -> None:
            try:
                await runtime.agent.handle_message(message_id, address_id)
            except Exception:
                logger.exception("agent pipeline failed for message %s", message_id)

        asyncio.create_task(_run())
        return JSONResponse({"status": "queued", "message_id": message_id})

    return app


def _verify_signature(body: bytes, request: Request, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for header in MAILAFRICA_SIGNATURE_HEADERS:
        provided = request.headers.get(header)
        if provided and hmac.compare_digest(provided, expected):
            return True
    return False
