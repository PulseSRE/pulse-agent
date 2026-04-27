"""Finding/prediction/action report constructors and helper utilities."""

from __future__ import annotations

import json
import time
import uuid

from .registry import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_WARNING  # noqa: F401 — re-export convenience


def _ts() -> int:
    return int(time.time() * 1000)


def _make_finding(
    severity: str,
    category: str,
    title: str,
    summary: str,
    resources: list[dict],
    auto_fixable: bool = False,
    runbook_id: str | None = None,
    confidence: float | None = None,
    finding_type: str = "current",
) -> dict:
    finding: dict = {
        "type": "finding",
        "id": f"f-{uuid.uuid4().hex[:12]}",
        "severity": severity,
        "category": category,
        "title": title,
        "summary": summary,
        "resources": resources,
        "autoFixable": auto_fixable,
        "runbookId": runbook_id,
        "timestamp": _ts(),
        "findingType": finding_type,
    }
    if confidence is not None:
        finding["confidence"] = round(max(0.0, min(1.0, confidence)), 2)
    return finding


def _make_prediction(
    category: str,
    title: str,
    detail: str,
    eta: str,
    confidence: float,
    resources: list[dict],
    recommended_action: str | None = None,
) -> dict:
    return {
        "type": "prediction",
        "id": f"p-{uuid.uuid4().hex[:12]}",
        "category": category,
        "title": title,
        "detail": detail,
        "eta": eta,
        "confidence": confidence,
        "resources": resources,
        "recommendedAction": recommended_action,
        "timestamp": _ts(),
    }


def _make_action_report(
    finding_id: str,
    tool: str,
    inp: dict,
    status: str,
    action_id: str | None = None,
    before_state: str = "",
    after_state: str = "",
    error: str | None = None,
    reasoning: str = "",
    duration_ms: int = 0,
    confidence: float | None = None,
) -> dict:
    report: dict = {
        "type": "action_report",
        "id": action_id or f"a-{uuid.uuid4().hex[:12]}",
        "findingId": finding_id,
        "tool": tool,
        "input": inp,
        "status": status,
        "beforeState": before_state,
        "afterState": after_state,
        "error": error,
        "timestamp": _ts(),
        "reasoning": reasoning,
        "durationMs": duration_ms,
    }
    if confidence is not None:
        report["confidence"] = round(max(0.0, min(1.0, confidence)), 2)
    return report


def _make_rollback_info(action: dict, finding: dict | None) -> tuple[int, str]:
    """Build rollback availability flag and action JSON from finding metadata."""
    rollback_meta = (finding or {}).get("_rollback_meta")
    if not rollback_meta or action.get("status") != "completed":
        return 0, ""
    tool = action.get("tool", "")
    if tool not in ("restart_deployment", "restart_statefulset", "restart_daemonset"):
        return 0, ""
    return 1, json.dumps(
        {
            "tool": "rollback_deployment",
            "input": {
                "name": rollback_meta["name"],
                "namespace": rollback_meta["namespace"],
                "revision": rollback_meta.get("revision", ""),
            },
        }
    )


# ── Fix History (Database abstraction) ────────────────────────────────────

_tables_ensured = False


def _ensure_tables() -> None:
    """Create actions and investigations tables if they don't exist."""
    global _tables_ensured
    if _tables_ensured:
        return
    from ..repositories.monitor_repo import get_monitor_repo

    get_monitor_repo().ensure_tables()
    _tables_ensured = True


def _skip_namespace(ns: str) -> bool:
    """Return True for system namespaces that scanners should ignore."""
    return ns.startswith("openshift-") or ns.startswith("kube-") or ns == "openshift"
