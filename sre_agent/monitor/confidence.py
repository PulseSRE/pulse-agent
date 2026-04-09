"""Confidence estimation and finding deduplication utilities."""

from __future__ import annotations

import json
import re
from typing import Any

from .registry import SEVERITY_CRITICAL, SEVERITY_INFO


def _estimate_finding_confidence(finding: dict) -> float:
    """Estimate confidence that a finding is a real issue (not noise)."""
    severity = str(finding.get("severity", "warning"))
    category = str(finding.get("category", ""))
    # High-signal scanners get higher base confidence
    base_by_category = {
        "crashloop": 0.95,
        "oom": 0.93,
        "alerts": 0.90,
        "workloads": 0.88,
        "nodes": 0.92,
        "operators": 0.90,
        "image_pull": 0.85,
        "cert_expiry": 0.88,
        "pending": 0.80,
        "daemonsets": 0.82,
        "hpa": 0.75,
    }
    base = base_by_category.get(category, 0.80)
    if severity == SEVERITY_CRITICAL:
        base = min(1.0, base + 0.05)
    elif severity == SEVERITY_INFO:
        base = max(0.0, base - 0.10)
    return round(base, 2)


def _estimate_auto_fix_confidence(finding: dict, recent_fixes: dict[str, float] | None = None) -> float:
    """Estimate confidence for autonomous fixes for outcome calibration.

    Confidence is reduced when the same resource was recently fixed,
    indicating a recurring issue that auto-fix may not resolve.
    """
    category = str(finding.get("category", ""))
    severity = str(finding.get("severity", "warning"))
    base_by_category = {
        "crashloop": 0.84,
        "workloads": 0.78,
        "image_pull": 0.72,
    }
    base = base_by_category.get(category, 0.65)
    if severity == SEVERITY_CRITICAL:
        base -= 0.1
    elif severity == SEVERITY_INFO:
        base += 0.05

    # Reduce confidence for recurring issues on the same resource
    if recent_fixes:
        resources = finding.get("resources", [])
        if resources:
            r = resources[0]
            resource_key = f"{r.get('kind', '')}:{r.get('namespace', '')}:{r.get('name', '')}"
            if resource_key in recent_fixes:
                base *= 0.7  # 30% reduction for recurring issues

    return max(0.1, min(1.0, round(base, 2)))


def _finding_key(finding: dict) -> str:
    resources = finding.get("resources", [])
    resource_part = "_"
    if resources:
        r = resources[0]
        resource_part = f"{r.get('kind', '')}:{r.get('namespace', '')}:{r.get('name', '')}"
    return f"{finding.get('category', '')}:{finding.get('title', '')}:{resource_part}"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object from text."""
    for i, ch in enumerate(text):
        if ch == "{":
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i : j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
    return None


def _sanitize_for_prompt(text: str) -> str:
    """Strip potential prompt injection from cluster-sourced text."""
    patterns = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now",
        r"system:\s*",
        r"assistant:\s*",
        r"<\/?system>",
    ]
    result = text
    for pattern in patterns:
        result = re.sub(pattern, "[REDACTED]", result, flags=re.IGNORECASE)
    return result[:500]  # Cap length
