#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] starting mailafrica-agent webhook + mcp-http"

mailafrica-agent webhook &
WEBHOOK_PID=$!

mailafrica-agent mcp-http &
MCP_PID=$!

_term() {
    kill -TERM "$WEBHOOK_PID" "$MCP_PID" 2>/dev/null || true
}
trap _term SIGTERM SIGINT

wait
