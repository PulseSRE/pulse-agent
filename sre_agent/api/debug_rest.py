"""Debug endpoints for memory introspection and diagnostics."""

from __future__ import annotations

import gc
import logging
import os
import sys

from fastapi import APIRouter, Depends

from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[Depends(verify_token)])


def _get_rss_mb() -> float:
    """Get current RSS on Linux, peak RSS on other platforms."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except FileNotFoundError:
        pass
    # Fallback: ru_maxrss is peak RSS (bytes on macOS, KB on Linux)
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform == "darwin" else 1)
    except Exception:
        return 0.0


@router.get("/memory")
async def debug_memory():
    """Report memory usage, cache sizes, and session state for diagnostics."""
    result: dict = {
        "rss_mb": round(_get_rss_mb(), 1),
        "python": {
            "gc_counts": gc.get_count(),
            "gc_stats": gc.get_stats(),
        },
    }

    try:
        from ..dependency_graph import get_dependency_graph

        result["dependency_graph"] = get_dependency_graph().memory_stats()
    except Exception as e:
        result["dependency_graph"] = {"error": str(e)}

    try:
        from .ws_endpoints import _active_monitor_sessions

        sessions = {ws_id: s.memory_stats() for ws_id, s in _active_monitor_sessions.items()}
        result["monitor_sessions"] = sessions
        result["monitor_session_count"] = len(sessions)
    except Exception as e:
        result["monitor_sessions"] = {"error": str(e)}

    try:
        from ..tool_registry import TOOL_REGISTRY

        result["tool_registry_count"] = len(TOOL_REGISTRY)
    except Exception as e:
        result["tool_registry_count"] = {"error": str(e)}

    try:
        from ..db import get_database

        db = get_database()
        row = db.fetchone("SELECT COUNT(*) AS cnt FROM context_entries")
        result["context_bus_entries"] = row["cnt"] if row else 0
    except Exception as e:
        result["context_bus_entries"] = {"error": str(e)}

    result["pid"] = os.getpid()

    return result
