"""Auto-discovery of tool modules.

Imports all tool-defining modules to trigger @beta_tool registration in
the central TOOL_REGISTRY. Replaces the manual import list in agent.py.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

_logger = logging.getLogger("pulse_agent.tool_discovery")

_TOOL_MODULES = [
    "sre_agent.k8s_tools",
    "sre_agent.fleet_tools",
    "sre_agent.git_tools",
    "sre_agent.gitops_tools",
    "sre_agent.security_tools",
    "sre_agent.view_tools",
    "sre_agent.view_mutations",
    "sre_agent.self_tools",
    "sre_agent.timeline_tools",
    "sre_agent.predict_tools",
    "sre_agent.handoff_tools",
    "sre_agent.inbox",
]

_discovered = False


def discover_tools() -> dict[str, Any]:
    """Import all tool modules and return the populated registry.

    Safe to call multiple times — modules are only imported once.
    """
    global _discovered
    if not _discovered:
        for mod_name in _TOOL_MODULES:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                _logger.warning("Tool module %s not available", mod_name)
        _discovered = True

    from .tool_registry import get_tool_map

    return get_tool_map()
