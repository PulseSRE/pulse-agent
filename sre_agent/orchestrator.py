"""Agent Orchestrator — classifies queries and routes to SRE or Security agent."""

import logging
from typing import Literal

logger = logging.getLogger("pulse_agent.orchestrator")

AgentMode = Literal["sre", "security", "both"]

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


def _keyword_score(query_lower: str, keywords: list[str]) -> float:
    """Score query against keywords, weighting longer (more specific) keywords higher."""
    return sum(len(kw) for kw in keywords if kw in query_lower)


def classify_intent(query: str) -> AgentMode:
    """Classify a user query as sre, security, or both."""
    q = query.lower()

    # Check "both" first (explicit full-audit requests)
    if any(kw in q for kw in BOTH_KEYWORDS):
        return "both"

    sre_score = _keyword_score(q, SRE_KEYWORDS)
    sec_score = _keyword_score(q, SECURITY_KEYWORDS)

    # If both domains match and scores are close, route to both
    if sre_score > 0 and sec_score > 0:
        ratio = min(sre_score, sec_score) / max(sre_score, sec_score)
        if ratio >= 0.7:
            return "both"

    if sec_score > sre_score and sec_score > 0:
        return "security"
    return "sre"  # default


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

    if mode == "security":
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
