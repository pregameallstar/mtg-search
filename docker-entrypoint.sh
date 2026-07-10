#!/bin/bash
set -e

# ponytail: start MCP SSE server + MCPO proxy alongside Flask app.
# Docker runs all three in one container — no systemd, no run.sh.

echo "Starting MCP SSE server on :${MCP_SSE_PORT:-8765}…"
python mcp_server.py --transport sse --port "${MCP_SSE_PORT:-8765}" --host 0.0.0.0 &
mcp_pid=$!

echo "Starting MCPO proxy on :${MCPO_PORT:-8000}…"
mcpo --type sse --port "${MCPO_PORT:-8000}" --name mtg-search -- "http://127.0.0.1:${MCP_SSE_PORT:-8765}/sse" &
mcpo_pid=$!

echo "Starting Flask app on :${PORT:-5000}…"
trap "kill $mcp_pid $mcpo_pid 2>/dev/null; exit 0" TERM INT
python app.py &
flask_pid=$!

wait -n
