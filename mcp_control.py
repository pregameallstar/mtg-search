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
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
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

    Returns the new process PID.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # ponytail: sys.executable works in Docker (system python) and venv
    venv_python = os.path.join(script_dir, "venv", "bin", "python3")
    if not os.path.isfile(venv_python):
        venv_python = sys.executable
    mcp_script = os.path.join(script_dir, "mcp_server.py")

    try:
        with open(pid_file) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        time.sleep(0.5)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass

    proc = subprocess.Popen(
        [venv_python, mcp_script, "--transport", transport,
         "--port", str(port), "--host", host],
        cwd=script_dir,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    return proc.pid
