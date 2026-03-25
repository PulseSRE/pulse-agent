"""Entrypoint for the Pulse Agent API server."""

import os
import uvicorn


def main():
    host = os.environ.get("PULSE_AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("PULSE_AGENT_PORT", "8080"))
    uvicorn.run("sre_agent.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
