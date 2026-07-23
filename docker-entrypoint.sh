#!/bin/bash
set -e

# ponytail: start MCP SSE server + MCPO proxy alongside Flask app.
# Docker runs all three in one container — no systemd, no run.sh.

cleanup() {
    echo "Shutting down…"
    kill $mcp_pid $mcpo_pid $flask_pid 2>/dev/null
    wait 2>/dev/null
    exit 0
}
trap cleanup TERM INT

echo "Starting MCP SSE server on :${MCP_SSE_PORT:-8765}…"
python mcp_server.py --transport sse --port "${MCP_SSE_PORT:-8765}" --host "${MCP_HOST:-0.0.0.0}" &
mcp_pid=$!

echo "Starting MCPO proxy on :${MCPO_PORT:-8000}…"
mcpo --type sse --port "${MCPO_PORT:-8000}" --name mtg-search -- "http://127.0.0.1:${MCP_SSE_PORT:-8765}/sse" &
mcpo_pid=$!

echo "Starting Flask app on :${PORT:-5000}…"
python app.py &
flask_pid=$!

# ponytail: wait for all three; restart any that die.
# Flask is the primary — if it exits, shut everything down.
while true; do
    wait -n $mcp_pid $mcpo_pid $flask_pid
    dead=$?
    if ! kill -0 $flask_pid 2>/dev/null; then
        echo "Flask died — shutting down."
        cleanup
    fi
    if ! kill -0 $mcp_pid 2>/dev/null; then
        echo "MCP SSE died — restarting…"
        python mcp_server.py --transport sse --port "${MCP_SSE_PORT:-8765}" --host "${MCP_HOST:-0.0.0.0}" &
        mcp_pid=$!
    fi
    if ! kill -0 $mcpo_pid 2>/dev/null; then
        echo "MCPO proxy died — restarting…"
        mcpo --type sse --port "${MCPO_PORT:-8000}" --name mtg-search -- "http://127.0.0.1:${MCP_SSE_PORT:-8765}/sse" &
        mcpo_pid=$!
    fi
done
