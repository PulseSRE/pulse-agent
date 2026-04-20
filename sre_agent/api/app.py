"""FastAPI app instance and lifespan management.

Protocol Version: 2 (see API_CONTRACT.md for full specification)

Exposes the SRE and Security agents over WebSocket for integration
with the OpenShift Pulse web UI. V2 adds /ws/monitor for autonomous scanning.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.metadata import version as pkg_version

from fastapi import Depends, FastAPI

from ..agent import ALL_TOOLS as SRE_ALL_TOOLS
from ..config import get_settings
from ..monitor import get_investigation_stats, is_autofix_paused
from ..security_agent import ALL_TOOLS as SEC_ALL_TOOLS
from .analytics_rest import recommendations_router
from .analytics_rest import router as analytics_router
from .auth import verify_token
from .chat_rest import router as chat_router
from .eval_rest import router as eval_router
from .fix_rest import router as fix_router
from .memory_rest import router as memory_router
from .monitor_rest import router as monitor_router
from .scanner_rest import router as scanner_router
from .skill_rest import router as skill_router
from .tools_rest import router as tools_router
from .views import router as views_router
from .ws_endpoints import websocket_auto_agent, websocket_monitor

logger = logging.getLogger("pulse_agent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify k8s connectivity and auth config on startup."""
    from ..logging_config import configure_logging

    configure_logging()
    # Ensure pulse_agent loggers are at INFO so monitor scan output is visible
    logging.getLogger("pulse_agent").setLevel(logging.INFO)

    if not get_settings().ws_token:
        logger.critical(
            "PULSE_AGENT_WS_TOKEN is not set. WebSocket endpoint is UNAUTHENTICATED. "
            "Set this variable or connections will be rejected."
        )
    try:
        from ..k8s_client import get_core_client

        get_core_client().list_namespace(limit=1)
        logger.info("Connected to cluster")
    except Exception:
        logger.warning("Cannot connect to cluster -- tools may fail")
    # Load skill packages
    try:
        from ..skill_loader import load_skills

        skills = load_skills()
        logger.info("Loaded %d skill packages", len(skills))

        # Connect MCP servers for skills that have mcp.yaml
        from ..mcp_client import connect_skill_mcp

        for skill in skills.values():
            if (skill.path / "mcp.yaml").exists():
                try:
                    conn = connect_skill_mcp(skill.name, skill.path)
                    if conn and conn.connected:
                        logger.info("MCP connected for skill '%s': %d tools", skill.name, len(conn.tools))
                    elif conn:
                        logger.warning("MCP failed for skill '%s': %s", skill.name, conn.error)
                except Exception as e:
                    logger.warning("MCP init failed for skill '%s': %s", skill.name, e)
        # Re-validate skills now that TOOL_REGISTRY is populated
        from ..skill_loader import revalidate_skills

        revalidate_skills()
    except Exception as e:
        logger.warning("Skill loading failed: %s", e)

    # Initialize memory system if enabled
    if get_settings().memory:
        try:
            from ..memory import MemoryManager, set_manager

            manager = MemoryManager()
            set_manager(manager)
            logger.info("Memory system initialized")
        except Exception as e:
            logger.warning("Memory system init failed: %s", e)
    yield

    # Cleanup MCP connections on shutdown
    try:
        from ..mcp_client import disconnect_all

        disconnect_all()
    except Exception:
        pass


def _get_agent_version() -> str:
    try:
        return pkg_version("openshift-sre-agent")
    except Exception:
        return "dev"


app = FastAPI(title="Pulse Agent API", version=_get_agent_version(), lifespan=lifespan)

PROTOCOL_VERSION = "2"

# Include REST routers
app.include_router(tools_router)
app.include_router(monitor_router)
app.include_router(scanner_router)
app.include_router(fix_router)
app.include_router(memory_router)
app.include_router(eval_router)
app.include_router(views_router)
app.include_router(chat_router)
app.include_router(skill_router)
app.include_router(analytics_router)
app.include_router(recommendations_router)

# Register WebSocket endpoints
# /ws/agent — ORCA-routed chat (routes to any of 7 skills)
# /ws/monitor — background scanner push (findings, predictions, actions)
app.websocket("/ws/agent")(websocket_auto_agent)
app.websocket("/ws/monitor")(websocket_monitor)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/version")
async def version():
    """API protocol version. UI checks this on connect to detect mismatches."""
    from ..skill_loader import list_skills

    return {
        "protocol": PROTOCOL_VERSION,
        "agent": _get_agent_version(),
        "tools": len(__import__("sre_agent.tool_registry", fromlist=["TOOL_REGISTRY"]).TOOL_REGISTRY)
        or len(SRE_ALL_TOOLS) + len(SEC_ALL_TOOLS),
        "skills": len(list_skills()),
        "features": ["component_specs", "ws_token_auth", "rate_limiting", "monitor", "fix_history", "predictions"],
    }


@app.get("/health")
async def health(_auth=Depends(verify_token)):
    from ..agent import _circuit_breaker
    from ..error_tracker import get_tracker

    tracker = get_tracker()
    summary = tracker.get_summary()
    return {
        "status": "degraded" if _circuit_breaker.is_open else "ok",
        "circuit_breaker": {
            "state": _circuit_breaker.state,
            "failure_count": _circuit_breaker.failure_count,
            "recovery_timeout": _circuit_breaker.recovery_timeout,
        },
        "errors": {
            "total": summary["total"],
            "by_category": summary["by_category"],
            "recent": tracker.get_recent(limit=5),
        },
        "investigations": get_investigation_stats(),
        "autofix_paused": is_autofix_paused(),
    }
