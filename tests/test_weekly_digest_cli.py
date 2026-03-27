"""CLI smoke tests for weekly digest output."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sre_agent.evals.weekly_digest_cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_weekly_digest_cli_writes_markdown(tmp_path: Path):
    out = tmp_path / "weekly-digest.md"
    db_path = tmp_path / "fix_history.db"
    proc = _run("--db-path", str(db_path), "--output", str(out))
    assert proc.returncode == 0
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "# Pulse Agent Weekly Eval Digest" in content
