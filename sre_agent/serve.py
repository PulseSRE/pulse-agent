"""Entrypoint for the Pulse Agent API server with socket cleanup."""

import atexit
import os
import signal
import sys

import uvicorn


def _cleanup_socket():
    """Remove any leftover Unix socket file to prevent zombie locks on restart."""
    socket_path = os.environ.get("PULSE_AGENT_SOCKET", "")
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    # Also clean up any uvicorn PID file
    pid_file = "/tmp/pulse_agent.pid"
    if os.path.exists(pid_file):
        try:
            os.unlink(pid_file)
        except OSError:
            pass


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT gracefully with cleanup."""
    _cleanup_socket()
    sys.exit(0)


def main():
    from .config import validate_config
    validate_config()

    # Register cleanup for normal exit, SIGTERM (k8s pod shutdown), and SIGINT
    atexit.register(_cleanup_socket)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Clean up any stale socket from a previous crash
    _cleanup_socket()

    host = os.environ.get("PULSE_AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("PULSE_AGENT_PORT", "8080"))
    socket_path = os.environ.get("PULSE_AGENT_SOCKET", "")

    # Write PID for process management
    with open("/tmp/pulse_agent.pid", "w") as f:
        f.write(str(os.getpid()))

    if socket_path:
        # Unix socket mode (for sidecar/local communication)
        uvicorn.run("sre_agent.api:app", uds=socket_path, log_level="info")
    else:
        # TCP mode (default)
        uvicorn.run("sre_agent.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
