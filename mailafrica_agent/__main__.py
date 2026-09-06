from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from .config import get_settings
from .mcp_server import Runtime, build_server
from .webhook import create_app


def _cmd_mcp() -> int:
    from mcp.server.fastmcp import FastMCP

    settings = get_settings()
    runtime = Runtime(settings)
    server: FastMCP = build_server(runtime)
    # FastMCP.run() starts the stdio transport loop (the default for MCP
    # clients like Claude). Lifespan connects the store and closes on exit.
    server.run()
    return 0


def _cmd_mcp_http() -> int:
    """Run the multi-tenant OAuth MCP server over streamable HTTP."""
    from .mcp_oauth import OAuthGroup, build_http_app

    settings = get_settings()
    runtime = Runtime(settings)
    oauth = OAuthGroup(runtime, settings)
    app = build_http_app(build_server(runtime, oauth=oauth), runtime)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")
    return 0


def _cmd_webhook() -> int:
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.agent_host, port=settings.agent_port, log_level="info")
    return 0


def _cmd_check() -> int:
    """Validate configuration and connectivity without running a server."""
    settings = get_settings()
    if not settings.mailafrica_api_key:
        print("MAILAFRICA_API_KEY is not set", file=sys.stderr)
        return 1
    if not settings.ngamia_api_key:
        print("NGAMIA_API_KEY is not set", file=sys.stderr)
        return 1

    async def _probe() -> None:
        runtime = Runtime(settings)
        await runtime.connect()
        try:
            balance = await runtime.mail.balance()
            print(f"MailAfrica OK — balance: {balance.get('balance_tzs')} TZS")
            models = await runtime.ngamia.list_models()
            print(f"Ngamia OK — {len(models)} models, e.g. {models[:3]}")
            print(f"configured model: {settings.ngamia_model}")
        finally:
            await runtime.aclose()

    try:
        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001 - CLI surface, report plainly
        print(f"check failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="mailafrica-agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="run the MCP server over stdio")
    sub.add_parser("mcp-http", help="run the multi-tenant OAuth MCP server over streamable HTTP")
    sub.add_parser("webhook", help="run the webhook HTTP server (uvicorn)")
    sub.add_parser("check", help="verify config + API connectivity")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "mcp":
        return _cmd_mcp()
    if args.command == "mcp-http":
        return _cmd_mcp_http()
    if args.command == "webhook":
        return _cmd_webhook()
    if args.command == "check":
        return _cmd_check()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
