"""Entrypoint for the Pulse Agent API server with socket cleanup."""

import atexit
import logging
import os
import signal
import sys

logger = logging.getLogger("pulse_agent.serve")

import uvicorn


def _cleanup_socket():
    """Remove any leftover Unix socket file to prevent zombie locks on restart."""
    from .config import get_settings

    socket_path = get_settings().socket
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            logger.debug("Failed to unlink socket %s", socket_path, exc_info=True)

    # Also clean up any uvicorn PID file
    pid_file = "/tmp/pulse_agent.pid"
    if os.path.exists(pid_file):
        try:
            os.unlink(pid_file)
        except OSError:
            logger.debug("Failed to unlink PID file %s", pid_file, exc_info=True)


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

    from .config import get_settings

    settings = get_settings()
    host = settings.host
    port = settings.port
    socket_path = settings.socket

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
