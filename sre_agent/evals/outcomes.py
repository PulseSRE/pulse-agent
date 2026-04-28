"""Outcome-based evaluation and regression analysis from fix history."""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import Database

logger = logging.getLogger("pulse_agent.evals")

DEFAULT_FIX_DB_PATH = os.environ.get("PULSE_AGENT_DATABASE_URL", "")
DEFAULT_POLICY_PATH = str(Path(__file__).resolve().parent / "policies" / "outcome_regression_policy.yaml")


@dataclass
class OutcomeMetrics:
    total_actions: int
    completed_actions: int
    failed_actions: int
    rolled_back_actions: int
    success_rate: float
    rollback_rate: float
    p50_duration_ms: float
    p95_duration_ms: float
    avg_duration_ms: float
    confidence_coverage: float
    confidence_calibration_error: float | None


@dataclass
class OutcomeRegressionPolicy:
    version: int
    success_rate_delta_min: float
    rollback_rate_delta_max: float
    p95_duration_ms_delta_max: float


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_policy() -> OutcomeRegressionPolicy:
    return OutcomeRegressionPolicy(
        version=1,
        success_rate_delta_min=-0.05,
        rollback_rate_delta_max=0.03,
        p95_duration_ms_delta_max=500.0,
    )


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    if val == "":
        return ""
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        if "." in val:
            return float(val)
        return int(val)
    except (ValueError, TypeError) as e:
        logger.debug("outcome scalar parse fell back to string for %r: %s", val, e)
        return val.strip("'\"")


def load_outcome_policy(policy_path: str = DEFAULT_POLICY_PATH) -> OutcomeRegressionPolicy:
    """Load regression thresholds from a simple YAML policy file."""
    default = _default_policy()
    path = Path(policy_path)
    if not path.exists():
        return default

    content = path.read_text(encoding="utf-8").splitlines()
    root: dict[str, Any] = {}
    current_map: dict[str, Any] | None = None
    for line in content:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  "):
            if current_map is None or ":" not in line:
                continue
            key, raw_val = line.strip().split(":", 1)
            current_map[key.strip()] = _parse_scalar(raw_val)
            continue
        if ":" not in line:
            continue
        key, raw_val = line.split(":", 1)
        key = key.strip()
        raw_val = raw_val.strip()
        if raw_val == "":
            child: dict[str, Any] = {}
            root[key] = child
            current_map = child
        else:
            root[key] = _parse_scalar(raw_val)
            current_map = None

    thresholds = root.get("thresholds", {})
    if not isinstance(thresholds, dict):
        thresholds = {}
    return OutcomeRegressionPolicy(
        version=int(root.get("version", default.version)),
        success_rate_delta_min=float(thresholds.get("success_rate_delta_min", default.success_rate_delta_min)),
        rollback_rate_delta_max=float(thresholds.get("rollback_rate_delta_max", default.rollback_rate_delta_max)),
        p95_duration_ms_delta_max=float(thresholds.get("p95_duration_ms_delta_max", default.p95_duration_ms_delta_max)),
    )


def _open_db(db_path: str) -> Database:
    if not db_path.startswith("postgres"):
        # Use the global database if no PostgreSQL URL provided
        from ..db import get_database

        return get_database()
    return Database(db_path)


def _load_actions(db: Database, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
    try:
        rows = db.fetchall(
            """
            SELECT timestamp, status, duration_ms, input
            FROM actions
            WHERE timestamp >= ? AND timestamp < ?
            ORDER BY timestamp ASC
            """,
            (since_ms, until_ms),
        )
    except Exception:
        logger.debug("Failed to load actions from DB", exc_info=True)
        return []
    actions: list[dict[str, Any]] = []
    for row in rows:
        raw_input = row["input"] or "{}"
        try:
            inp = json.loads(raw_input)
        except (ValueError, TypeError):
            inp = {}
        actions.append(
            {
                "timestamp": int(row["timestamp"] or 0),
                "status": str(row["status"] or ""),
                "duration_ms": int(row["duration_ms"] or 0),
                "input": inp if isinstance(inp, dict) else {},
            }
        )
    return actions


def _confidence_values(actions: list[dict[str, Any]]) -> list[tuple[float, int]]:
    """Return (confidence, outcome) pairs where confidence is present."""
    pairs: list[tuple[float, int]] = []
    for a in actions:
        conf = a["input"].get("confidence")
        if conf is None:
            continue
        try:
            c = float(conf)
        except (ValueError, TypeError) as e:
            logger.debug("confidence value parse failed for %r: %s", conf, e)
            continue
        c = max(0.0, min(1.0, c))
        outcome = 1 if a["status"] == "completed" else 0
        pairs.append((c, outcome))
    return pairs


def _calibration_error_brier(pairs: list[tuple[float, int]]) -> float | None:
    """Lower is better. Returns None if no confidence data."""
    if not pairs:
        return None
    vals = [(p - y) ** 2 for p, y in pairs]
    return sum(vals) / len(vals)


def _percentile(sorted_vals: list[int], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (len(sorted_vals) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def compute_metrics(actions: list[dict[str, Any]]) -> OutcomeMetrics:
    total = len(actions)
    completed = sum(1 for a in actions if a["status"] == "completed")
    failed = sum(1 for a in actions if a["status"] == "failed")
    rolled_back = sum(1 for a in actions if a["status"] == "rolled_back")
    success_rate = (completed / total) if total else 0.0
    rollback_rate = (rolled_back / total) if total else 0.0

    durations = sorted(a["duration_ms"] for a in actions if a["duration_ms"] > 0)
    p50 = _percentile(durations, 0.50)
    p95 = _percentile(durations, 0.95)
    avg = statistics.mean(durations) if durations else 0.0

    conf_pairs = _confidence_values(actions)
    conf_coverage = (len(conf_pairs) / total) if total else 0.0
    brier = _calibration_error_brier(conf_pairs)

    return OutcomeMetrics(
        total_actions=total,
        completed_actions=completed,
        failed_actions=failed,
        rolled_back_actions=rolled_back,
        success_rate=round(success_rate, 4),
        rollback_rate=round(rollback_rate, 4),
        p50_duration_ms=round(p50, 2),
        p95_duration_ms=round(p95, 2),
        avg_duration_ms=round(avg, 2),
        confidence_coverage=round(conf_coverage, 4),
        confidence_calibration_error=round(brier, 6) if brier is not None else None,
    )


def analyze_windows(
    db_path: str = DEFAULT_FIX_DB_PATH,
    current_days: int = 7,
    baseline_days: int = 7,
    policy_path: str = DEFAULT_POLICY_PATH,
) -> dict[str, Any]:
    """Compare current window against previous baseline window."""
    now = _now_ms()
    day_ms = 86_400_000
    current_start = now - current_days * day_ms
    baseline_start = current_start - baseline_days * day_ms
    baseline_end = current_start

    db = _open_db(db_path)
    try:
        current_actions = _load_actions(db, current_start, now)
        baseline_actions = _load_actions(db, baseline_start, baseline_end)
    finally:
        db.close()

    current = compute_metrics(current_actions)
    baseline = compute_metrics(baseline_actions)
    policy = load_outcome_policy(policy_path)

    deltas = {
        "success_rate_delta": round(current.success_rate - baseline.success_rate, 4),
        "rollback_rate_delta": round(current.rollback_rate - baseline.rollback_rate, 4),
        "p95_duration_ms_delta": round(current.p95_duration_ms - baseline.p95_duration_ms, 2),
    }
    regressions = {
        "success_drop": deltas["success_rate_delta"] < policy.success_rate_delta_min,
        "rollback_increase": deltas["rollback_rate_delta"] > policy.rollback_rate_delta_max,
        "latency_increase": deltas["p95_duration_ms_delta"] > policy.p95_duration_ms_delta_max,
    }

    return {
        "generated_at_ms": now,
        "db_path": db_path,
        "windows": {
            "current_days": current_days,
            "baseline_days": baseline_days,
        },
        "current": current.__dict__,
        "baseline": baseline.__dict__,
        "policy": {
            "version": policy.version,
            "thresholds": {
                "success_rate_delta_min": policy.success_rate_delta_min,
                "rollback_rate_delta_max": policy.rollback_rate_delta_max,
                "p95_duration_ms_delta_max": policy.p95_duration_ms_delta_max,
            },
        },
        "deltas": deltas,
        "regressions": regressions,
        "gate_passed": not any(regressions.values()),
    }


def render_text_report(report: dict[str, Any]) -> str:
    cur = report["current"]
    base = report["baseline"]
    policy = report.get("policy", {})
    thresholds = policy.get("thresholds", {})
    d = report["deltas"]
    r = report["regressions"]
    lines = []
    lines.append("Pulse Agent Outcome Eval Report")
    lines.append(f"Gate: {'PASS' if report['gate_passed'] else 'FAIL'}")
    if thresholds:
        lines.append(
            "Policy thresholds: "
            f"success_rate_delta_min={thresholds.get('success_rate_delta_min')} "
            f"rollback_rate_delta_max={thresholds.get('rollback_rate_delta_max')} "
            f"p95_duration_ms_delta_max={thresholds.get('p95_duration_ms_delta_max')}"
        )
    lines.append("")
    lines.append("Current window:")
    lines.append(
        f"- total={cur['total_actions']} success={cur['success_rate']:.3f} rollback={cur['rollback_rate']:.3f} p95={cur['p95_duration_ms']:.1f}ms"
    )
    lines.append("Baseline window:")
    lines.append(
        f"- total={base['total_actions']} success={base['success_rate']:.3f} rollback={base['rollback_rate']:.3f} p95={base['p95_duration_ms']:.1f}ms"
    )
    lines.append("Deltas:")
    lines.append(f"- success_rate_delta={d['success_rate_delta']:+.3f}")
    lines.append(f"- rollback_rate_delta={d['rollback_rate_delta']:+.3f}")
    lines.append(f"- p95_duration_ms_delta={d['p95_duration_ms_delta']:+.1f}")
    if cur["confidence_calibration_error"] is not None:
        lines.append(
            f"- confidence_brier={cur['confidence_calibration_error']:.6f} coverage={cur['confidence_coverage']:.3f}"
        )
    else:
        lines.append("- confidence_brier=n/a (no confidence telemetry)")
    lines.append("Regressions:")
    lines.append(f"- success_drop={r['success_drop']}")
    lines.append(f"- rollback_increase={r['rollback_increase']}")
    lines.append(f"- latency_increase={r['latency_increase']}")
    return "\n".join(lines)
