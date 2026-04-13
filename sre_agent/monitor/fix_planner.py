"""Intelligent auto-fix planning — maps investigation diagnosis to targeted fixes.

Sits between the investigation result and auto-fix execution:
1. Query latest investigation for the finding
2. Classify the root cause from suspected_cause text
3. Select a targeted fix strategy
4. Fall back to blunt handlers if no strategy matches
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("pulse_agent.monitor")

# Root cause categories with keyword patterns
_CAUSE_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "bad_image",
        [
            "image",
            "does not exist",
            "not found in registry",
            "imagepullbackoff",
            "pull access denied",
            "manifest unknown",
        ],
    ),
    ("missing_config", ["configmap", "not found", "missing", "secret.*not found"]),
    ("oom", ["oom", "out of memory", "memory limit", "oomkilled", "exceeded memory"]),
    ("probe_failure", ["readiness probe", "liveness probe", "probe failed", "connection refused"]),
    ("quota_exceeded", ["quota", "exceeded", "forbidden", "limit reached"]),
    ("crash_exit", ["exit code", "fatal", "panic", "segfault", "error code"]),
    ("dependency", ["connection refused", "connection timed out", "no such host", "dns", "service unavailable"]),
]


def classify_root_cause(suspected_cause: str) -> str:
    """Classify a suspected cause string into a root cause category."""
    if not suspected_cause:
        return "unknown"

    lower = suspected_cause.lower()
    for category, patterns in _CAUSE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lower):
                return category

    return "unknown"


@dataclass
class FixPlan:
    """A targeted fix plan produced by the fix planner."""

    strategy: str  # e.g., "patch_image", "patch_resources", "create_configmap"
    cause_category: str  # from classify_root_cause
    confidence: float  # from investigation
    description: str  # human-readable description of what will be done
    params: dict  # strategy-specific parameters


# Minimum confidence to attempt a targeted fix
_MIN_TARGETED_CONFIDENCE = 0.5

# Map root cause category to fix strategy
_STRATEGY_MAP: dict[str, str] = {
    "bad_image": "patch_image",
    "oom": "patch_resources",
    "missing_config": "create_configmap",
    "probe_failure": "patch_probe",
    "quota_exceeded": "suggest_quota_increase",
}


def plan_fix(investigation: dict, finding: dict) -> FixPlan | None:
    """Plan a targeted fix based on investigation results.

    Returns a FixPlan if a targeted strategy is available and confidence
    is sufficient. Returns None to fall back to blunt handlers.
    """
    suspected_cause = investigation.get("suspectedCause", "") or investigation.get("suspected_cause", "")
    recommended_fix = investigation.get("recommendedFix", "") or investigation.get("recommended_fix", "")
    confidence = float(investigation.get("confidence", 0))

    if confidence < _MIN_TARGETED_CONFIDENCE:
        return None

    cause_category = classify_root_cause(suspected_cause)
    strategy = _STRATEGY_MAP.get(cause_category)

    if not strategy:
        return None

    return FixPlan(
        strategy=strategy,
        cause_category=cause_category,
        confidence=confidence,
        description=f"{strategy}: {recommended_fix[:200]}",
        params={
            "suspected_cause": suspected_cause,
            "recommended_fix": recommended_fix,
            "resources": finding.get("resources", []),
        },
    )
