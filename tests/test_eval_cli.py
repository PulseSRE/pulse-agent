"""CLI smoke tests for eval framework."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sre_agent.evals.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_json_output_writes_file(tmp_path: Path):
    out = tmp_path / "eval.json"
    proc = _run("--suite", "release", "--format", "json", "--output", str(out))
    assert proc.returncode == 0
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["suite_name"] == "release"
    assert payload["scenario_count"] >= 1


def test_cli_fail_on_gate_for_core():
    proc = _run("--suite", "core", "--fail-on-gate")
    assert proc.returncode != 0
