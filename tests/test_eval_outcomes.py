"""Tests for outcome-based eval analysis."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sre_agent.evals.outcomes import analyze_windows


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE actions (
            id TEXT PRIMARY KEY,
            timestamp INTEGER,
            status TEXT,
            duration_ms INTEGER,
            input TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_action(path: Path, action_id: str, timestamp_ms: int, status: str, duration_ms: int, inp: dict):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO actions (id, timestamp, status, duration_ms, input) VALUES (?, ?, ?, ?, ?)",
        (action_id, timestamp_ms, status, duration_ms, json.dumps(inp)),
    )
    conn.commit()
    conn.close()


def test_outcome_report_detects_regression(tmp_path: Path):
    db = tmp_path / "fix.db"
    _init_db(db)

    # Use deterministic relative timestamps around "now" used in analyze_windows.
    # Two windows of 1 day each with current worse than baseline.
    now_ms = 1_700_000_000_000
    day = 86_400_000

    # baseline window: [now-2d, now-1d)
    _insert_action(db, "b1", now_ms - int(1.8 * day), "completed", 100, {"confidence": 0.8})
    _insert_action(db, "b2", now_ms - int(1.7 * day), "completed", 120, {"confidence": 0.9})

    # current window: [now-1d, now)
    _insert_action(db, "c1", now_ms - int(0.8 * day), "failed", 1200, {"confidence": 0.9})
    _insert_action(db, "c2", now_ms - int(0.7 * day), "rolled_back", 1500, {"confidence": 0.7})

    # Monkeypatch time via direct arguments by shifting windows around db data:
    # emulate now by selecting current_days=36500 then using old timestamps would be out-of-range.
    # Instead, update rows near real now:
    # reset with relative to actual now for portable test
    import time

    real_now = int(time.time() * 1000)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE actions SET timestamp = ? WHERE id = 'b1'", (real_now - int(1.8 * day),))
    conn.execute("UPDATE actions SET timestamp = ? WHERE id = 'b2'", (real_now - int(1.7 * day),))
    conn.execute("UPDATE actions SET timestamp = ? WHERE id = 'c1'", (real_now - int(0.8 * day),))
    conn.execute("UPDATE actions SET timestamp = ? WHERE id = 'c2'", (real_now - int(0.7 * day),))
    conn.commit()
    conn.close()

    report = analyze_windows(db_path=str(db), current_days=1, baseline_days=1)
    assert report["current"]["total_actions"] == 2
    assert report["baseline"]["total_actions"] == 2
    assert report["regressions"]["success_drop"] is True
    assert report["gate_passed"] is False


def test_outcome_report_handles_empty_db(tmp_path: Path):
    db = tmp_path / "empty.db"
    _init_db(db)
    report = analyze_windows(db_path=str(db), current_days=7, baseline_days=7)
    assert report["current"]["total_actions"] == 0
    assert report["baseline"]["total_actions"] == 0


def test_outcome_report_uses_policy_file_thresholds(tmp_path: Path):
    db = tmp_path / "policy.db"
    _init_db(db)
    day = 86_400_000

    import time

    now = int(time.time() * 1000)
    _insert_action(db, "b1", now - int(1.8 * day), "completed", 100, {"confidence": 0.8})
    _insert_action(db, "c1", now - int(0.8 * day), "completed", 180, {"confidence": 0.8})

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "\n".join(
            [
                "version: 2",
                "thresholds:",
                "  success_rate_delta_min: -0.10",
                "  rollback_rate_delta_max: 0.10",
                "  p95_duration_ms_delta_max: 10.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = analyze_windows(db_path=str(db), current_days=1, baseline_days=1, policy_path=str(policy))
    assert report["policy"]["version"] == 2
    assert report["regressions"]["latency_increase"] is True
