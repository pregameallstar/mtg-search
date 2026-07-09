#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.server.pid"
MCP_PID_FILE="$SCRIPT_DIR/.mcp_server.pid"
MCPO_PID_FILE="$SCRIPT_DIR/.mcpo.pid"
LOG_FILE="$SCRIPT_DIR/.server.log"
MCP_LOG_FILE="$SCRIPT_DIR/.mcp_server.log"
MCPO_LOG_FILE="$SCRIPT_DIR/.mcpo.log"
PORT=5000
MCP_SSE_PORT="${MCP_SSE_PORT:-8765}"
MCPO_PORT="${MCPO_PORT:-8000}"

usage() {
    cat <<EOF
Usage: $0 {start|stop|status|restart}

  start    Start Flask app + MCP SSE server + MCPO proxy
  stop     Stop all servers
  status   Show whether all servers are running
  restart  Stop then start all servers

OpenWebUI: connect to http://<host>:${MCPO_PORT}/openapi.json
MCPO proxy (port ${MCPO_PORT}) bridges MCP SSE (port ${MCP_SSE_PORT}) to OpenAPI.
EOF
    exit 0
}

_pid_alive() {
    local pid="$1"
    local stat
    stat=$(cut -d' ' -f3 /proc/"$pid"/stat 2>/dev/null) || return 1
    [ "$stat" != "Z" ]
}

flask_running() {
    if [ ! -f "$PID_FILE" ]; then return 1; fi
    _pid_alive "$(cat "$PID_FILE")"
}

mcp_sse_running() {
    if [ ! -f "$MCP_PID_FILE" ]; then return 1; fi
    _pid_alive "$(cat "$MCP_PID_FILE")"
}

mcpo_running() {
    if [ ! -f "$MCPO_PID_FILE" ]; then return 1; fi
    _pid_alive "$(cat "$MCPO_PID_FILE")"
}

start() {
    cd "$SCRIPT_DIR"
    source venv/bin/activate

    # ── Flask app ──
    if flask_running; then
        echo "Flask server already running on port $PORT (pid $(cat "$PID_FILE"))."
    else
        echo "Starting Flask server…"
        nohup python app.py >> "$LOG_FILE" 2>&1 &
        local flask_pid=$!
        echo $flask_pid > "$PID_FILE"
        sleep 0.5
        if _pid_alive "$flask_pid"; then
            echo "Flask started (pid $flask_pid). Log: $LOG_FILE"
        else
            rm -f "$PID_FILE"
            echo "Flask failed to start — check $LOG_FILE"
            return 1
        fi
    fi

    # ── MCP SSE server (internal, feeds MCPO proxy) ──
    if mcp_sse_running; then
        echo "MCP SSE server already running on port $MCP_SSE_PORT (pid $(cat "$MCP_PID_FILE"))."
    else
        echo "Starting MCP SSE server on port $MCP_SSE_PORT…"
        nohup python mcp_server.py --transport sse --port "$MCP_SSE_PORT" \
            >> "$MCP_LOG_FILE" 2>&1 &
        local mcp_pid=$!
        echo $mcp_pid > "$MCP_PID_FILE"
        sleep 1.5
        if _pid_alive "$mcp_pid"; then
            echo "MCP SSE started (pid $mcp_pid). Log: $MCP_LOG_FILE"
        else
            rm -f "$MCP_PID_FILE"
            echo "MCP SSE failed to start — check $MCP_LOG_FILE"
            return 1
        fi
    fi

    # ── MCPO proxy (bridges MCP SSE → OpenAPI for OpenWebUI) ──
    if mcpo_running; then
        echo "MCPO proxy already running on port $MCPO_PORT (pid $(cat "$MCPO_PID_FILE"))."
    else
        echo "Starting MCPO proxy on port $MCPO_PORT → MCP SSE :$MCP_SSE_PORT…"
        nohup mcpo --type sse --port "$MCPO_PORT" --name "mtg-search" \
            -- "http://127.0.0.1:${MCP_SSE_PORT}/sse" \
            >> "$MCPO_LOG_FILE" 2>&1 &
        local mcpo_pid=$!
        echo $mcpo_pid > "$MCPO_PID_FILE"
        sleep 2
        if _pid_alive "$mcpo_pid"; then
            echo "MCPO proxy started (pid $mcpo_pid). URL: http://0.0.0.0:${MCPO_PORT}/openapi.json"
        else
            rm -f "$MCPO_PID_FILE"
            echo "MCPO proxy failed to start — check $MCPO_LOG_FILE"
            return 1
        fi
    fi
}

_stop_one() {
    local label="$1" pid_file="$2" pid="$3"
    echo "Stopping $label (pid $pid)…"
    kill "$pid" 2>/dev/null || true
    for i in {1..10}; do
        if ! _pid_alive "$pid"; then
            rm -f "$pid_file"
            echo "$label stopped."
            return 0
        fi
        sleep 0.3
    done
    echo "$label graceful shutdown timed out — force-killing."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pid_file"
    echo "$label stopped."
}

stop() {
    # ── MCPO ──
    if mcpo_running; then
        _stop_one "MCPO proxy" "$MCPO_PID_FILE" "$(cat "$MCPO_PID_FILE")"
    else
        echo "MCPO proxy is not running."
        rm -f "$MCPO_PID_FILE"
    fi

    # ── MCP SSE ──
    if mcp_sse_running; then
        _stop_one "MCP SSE" "$MCP_PID_FILE" "$(cat "$MCP_PID_FILE")"
    else
        echo "MCP SSE server is not running."
        rm -f "$MCP_PID_FILE"
    fi

    # ── Flask ──
    if flask_running; then
        _stop_one "Flask server" "$PID_FILE" "$(cat "$PID_FILE")"
    else
        echo "Flask server is not running."
        rm -f "$PID_FILE"
    fi
}

status() {
    if flask_running; then
        echo "Flask server is running (pid $(cat "$PID_FILE"), port $PORT)."
    else
        echo "Flask server is not running."
    fi

    if mcp_sse_running; then
        echo "MCP SSE server is running (pid $(cat "$MCP_PID_FILE"), port $MCP_SSE_PORT)."
    else
        echo "MCP SSE server is not running."
    fi

    if mcpo_running; then
        echo "MCPO proxy is running (pid $(cat "$MCPO_PID_FILE"), port $MCPO_PORT)."
    else
        echo "MCPO proxy is not running."
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    status)  status ;;
    restart) stop; start ;;
    -h|--help|help) usage ;;
    *)
        echo "Error: unknown command '${1:-}'"
        usage
        ;;
esac
