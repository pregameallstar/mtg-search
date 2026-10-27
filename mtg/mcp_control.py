"""MCP server process management helpers.

ponytail: subprocess + socket checks, no Flask dependency.
"""

import os
import socket
import signal
import time
import subprocess
import sys

# Config
MCP_SSE_PORT = int(os.environ.get("MCP_SSE_PORT", "8765"))
MCPO_PORT = int(os.environ.get("MCPO_PORT", "8000"))
# Bind to loopback unless the operator explicitly opts into LAN exposure.
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# ponytail: 0.0.0.0 is a bind address — users connect to 127.0.0.1 (local)
# or the machine's actual IP (remote). Display the loopback when unconfigured.
MCP_DISPLAY_HOST = "127.0.0.1" if MCP_HOST == "0.0.0.0" else MCP_HOST


def port_alive(port):
    """Check if a TCP port is accepting connections on localhost."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def restart_mcp(transport, port, pid_file, log_file, host=MCP_HOST):
    """Kill existing MCP process by PID file, then re-launch.

    The child is launched as ``python -m mtg.mcp_server`` from the project root.
    Running ``mtg/mcp_server.py`` directly (the old behavior) put ``mtg/`` on
    sys.path and made ``import mtg`` fail; running from ``mtg/`` also made the
    relative database/deck paths point at the wrong directory.

    Returns the new process PID. Raises RuntimeError if the child exits
    immediately, so the config route does not report a false success.
    """
    try:
        with open(pid_file) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        time.sleep(0.5)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass

    with open(log_file, "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mtg.mcp_server", "--transport", transport,
             "--port", str(port), "--host", host],
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Give an import/config failure a moment to surface before claiming success.
    time.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None:
        tail = ""
        try:
            with open(log_file, "r", errors="replace") as f:
                tail = "".join(f.readlines()[-8:]).strip()
        except OSError:
            pass
        raise RuntimeError(
            f"MCP process exited immediately (code {exit_code})."
            + (f" Log tail: {tail}" if tail else "")
        )

    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    return proc.pid
