#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_FILE="$SCRIPT_DIR/.server.log"
PORT=5000

usage() {
    cat <<EOF
Usage: $0 {start|stop|status|restart}

  start    Start the MTG Search server
  stop     Stop the server
  status   Show whether the server is running
  restart  Stop then start the server
EOF
    exit 0
}

start() {
    if running; then
        echo "Server is already running on port $PORT (pid $(cat "$PID_FILE"))."
        return 0
    fi

    echo "Starting MTG Search server…"
    cd "$SCRIPT_DIR"
    source venv/bin/activate
    nohup python app.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (pid $!). Log: $LOG_FILE"
}

stop() {
    if ! running; then
        echo "Server is not running."
        rm -f "$PID_FILE"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo "Stopping server (pid $pid)…"
    kill "$pid" 2>/dev/null || true
    # Wait for graceful shutdown
    for i in {1..10}; do
        if ! running; then
            rm -f "$PID_FILE"
            echo "Stopped."
            return 0
        fi
        sleep 0.3
    done
    echo "Graceful shutdown timed out — force-killing."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "Stopped."
}

status() {
    if running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo "Server is running (pid $pid, port $PORT)."
    else
        echo "Server is not running."
    fi
}

running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    local pid
    pid=$(cat "$PID_FILE")
    kill -0 "$pid" 2>/dev/null
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
