"""Tests for weekly eval digest generation."""

from __future__ import annotations

from pathlib import Path

from sre_agent.evals.weekly_digest import render_weekly_digest


def test_weekly_digest_includes_gate_sections(tmp_path: Path):
    db_path = tmp_path / "fix_history.db"
    digest = render_weekly_digest(db_path=str(db_path), current_days=7, baseline_days=7)
    assert "# Pulse Agent Weekly Eval Digest" in digest
    assert "## Gate status" in digest
    assert "`release`" in digest
    assert "`outcomes`" in digest
    assert "## Top failing categories" in digest
