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
    layer: int | None = None,
    posture: bool = False,
    started_at: int | None = None,
) -> dict:
    """Build a finding.

    ``layer``, ``posture`` and ``started_at`` are for scanners that know more
    about their own output than the category does. They exist for firing
    alerts: one scanner emits facts about nodes, about operators and about the
    monitoring stack, all under ``category="alerts"``, and only it is holding
    the alert name and the moment Prometheus started firing.
    """
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
    if layer is not None:
        finding["layer"] = layer
    if posture:
        finding["posture"] = True
    if started_at is not None:
        # When the condition itself began, as distinct from when Pulse noticed
        # it. Episodes correlate on this: Pulse's own first-sight is lost on
        # every restart, so without it a redeploy makes every standing problem
        # look like it started at the same second.
        finding["startedAt"] = started_at
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
    """Build rollback availability flag and action JSON.

    A captured snapshot is the strongest form of undo and is not tied to a
    particular tool: it restores whatever the resource looked like before the
    change. It takes precedence over the revision-based path below, which only
    ever covered the three restart tools.
    """
    snapshot_blob = action.get("beforeSnapshot")
    if snapshot_blob and action.get("status") in ("completed", "applied"):
        return 1, json.dumps({"tool": "restore_snapshot", "input": {"snapshot": snapshot_blob}})

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
