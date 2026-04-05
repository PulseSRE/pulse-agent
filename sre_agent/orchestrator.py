"""Agent Orchestrator — classifies queries and routes to SRE or Security agent."""

import logging
from typing import Literal

logger = logging.getLogger("pulse_agent.orchestrator")

AgentMode = Literal["sre", "security", "both", "view_designer"]

SRE_KEYWORDS = [
    "crash",
    "restart",
    "pod",
    "deploy",
    "node",
    "scale",
    "log",
    "event",
    "health",
    "capacity",
    "oom",
    "pending",
    "drain",
    "cordon",
    "prometheus",
    "alert",
    "metric",
    "resource",
    "quota",
    "memory",
    "cpu",
    "disk",
    "image",
    "pull",
    "schedule",
    "replica",
    "rollout",
    "ingress",
    "route",
    "service",
    "endpoint",
    "pvc",
    "volume",
    "operator",
    "update",
    "certificate",
    "cert",
    "tls",
    "expir",
]

SECURITY_KEYWORDS = [
    "rbac",
    "role",
    "permission",
    "scc",
    "network policy",
    "networkpolicy",
    "secret",
    "privilege",
    "root",
    "audit",
    "compliance",
    "vulnerability",
    "tls",
    "certificate",
    "access control",
    "service account",
    "cluster-admin",
    "wildcard",
    "overly permissive",
    "security context",
    "capability",
]

BOTH_KEYWORDS = [
    "scan the cluster",
    "full assessment",
    "production readiness",
    "audit everything",
    "check everything",
    "full audit",
    "cluster audit",
]

VIEW_DESIGNER_KEYWORDS = [
    "dashboard",
    "create a view",
    "build a view",
    "build me a view",
    "design a view",
    "build me a dashboard",
    "create a dashboard",
    "customize the view",
    "edit the view",
    "edit view",
    "update view",
    "update the view",
    "change layout",
    "update layout",
    "edit layout",
    "change the layout",
    "add widget",
    "add a widget",
    "remove widget",
    "show me a dashboard",
    "make a view",
    "make me a view",
    "new view",
    "modify the view",
    "redesign",
    "rearrange",
    "build me an overview",
    "create an overview",
    "build an overview",
    "overview dashboard",
    "monitoring dashboard",
    "monitoring view",
    "delete dashboard",
    "delete the dashboard",
    "delete my dashboard",
    "clone dashboard",
    "clone my dashboard",
    "duplicate dashboard",
    "copy dashboard",
]


_VIEW_TRIGGER_WORDS = {"widget", "sparkline", "metric card", "overview", "page"}
# Patterns where "view" means "dashboard", not "look at"
_VIEW_TRIGGER_PHRASES = {"a view", "the view", "my view", "this view", "new view"}


def _keyword_score(query_lower: str, keywords: list[str]) -> float:
    """Score query against keywords, weighting longer (more specific) keywords higher."""
    return sum(len(kw) for kw in keywords if kw in query_lower)


def classify_intent(query: str) -> tuple[AgentMode, bool]:
    """Classify a user query as sre, security, both, or view_designer.

    Returns:
        (mode, is_strong) — is_strong is True when the classification is based
        on explicit keyword matches, False when it's the default sre fallback.
        Callers should only switch session mode on strong signals.
    """
    q = query.lower()

    # Check "both" first (explicit full-audit requests)
    if any(kw in q for kw in BOTH_KEYWORDS):
        return "both", True

    # Check view designer (dashboard/view creation requests)
    if (
        any(kw in q for kw in VIEW_DESIGNER_KEYWORDS)
        or any(w in q for w in _VIEW_TRIGGER_WORDS)
        or any(p in q for p in _VIEW_TRIGGER_PHRASES)
    ):
        return "view_designer", True

    sre_score = _keyword_score(q, SRE_KEYWORDS)
    sec_score = _keyword_score(q, SECURITY_KEYWORDS)

    # If both domains match and scores are close, route to both
    if sre_score > 0 and sec_score > 0:
        ratio = min(sre_score, sec_score) / max(sre_score, sec_score)
        if ratio >= 0.7:
            return "both", True

    if sec_score > sre_score and sec_score > 0:
        return "security", True

    # SRE is the default — only strong if there were actual keyword matches
    return "sre", sre_score > 0


def build_orchestrated_config(mode: AgentMode) -> dict:
    """Return tool_defs, tool_map, system_prompt, write_tools for the given mode."""
    from .agent import (
        SYSTEM_PROMPT as SRE_PROMPT,
    )
    from .agent import (
        TOOL_DEFS as SRE_TOOL_DEFS,
    )
    from .agent import (
        TOOL_MAP as SRE_TOOL_MAP,
    )
    from .agent import (
        WRITE_TOOLS as SRE_WRITE_TOOLS,
    )
    from .security_agent import (
        SECURITY_SYSTEM_PROMPT,
    )
    from .security_agent import (
        TOOL_DEFS as SEC_TOOL_DEFS,
    )
    from .security_agent import (
        TOOL_MAP as SEC_TOOL_MAP,
    )

    if mode == "view_designer":
        from .view_designer import (
            TOOL_DEFS as VD_TOOL_DEFS,
        )
        from .view_designer import (
            TOOL_MAP as VD_TOOL_MAP,
        )
        from .view_designer import (
            VIEW_DESIGNER_SYSTEM_PROMPT,
        )

        return {
            "system_prompt": VIEW_DESIGNER_SYSTEM_PROMPT,
            "tool_defs": VD_TOOL_DEFS,
            "tool_map": VD_TOOL_MAP,
            "write_tools": set(),  # View designer never modifies cluster state
        }
    elif mode == "security":
        return {
            "system_prompt": SECURITY_SYSTEM_PROMPT,
            "tool_defs": SEC_TOOL_DEFS,
            "tool_map": SEC_TOOL_MAP,
            "write_tools": set(),
        }
    elif mode == "both":
        # Merge both tool sets, use SRE prompt with security addendum
        merged_map = {**SRE_TOOL_MAP, **SEC_TOOL_MAP}
        merged_defs = SRE_TOOL_DEFS + [d for d in SEC_TOOL_DEFS if d.get("name") not in SRE_TOOL_MAP]
        combined_prompt = (
            SRE_PROMPT
            + "\n\n"
            + (
                "You also have security scanning tools available. "
                "After diagnosing operational issues, check for related security concerns."
            )
        )
        return {
            "system_prompt": combined_prompt,
            "tool_defs": merged_defs,
            "tool_map": merged_map,
            "write_tools": SRE_WRITE_TOOLS,
        }
    else:  # sre
        return {
            "system_prompt": SRE_PROMPT,
            "tool_defs": SRE_TOOL_DEFS,
            "tool_map": SRE_TOOL_MAP,
            "write_tools": SRE_WRITE_TOOLS,
        }
