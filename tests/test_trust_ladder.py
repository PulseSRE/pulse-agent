"""What each trust level actually does.

The UI ladder described a different agent than the one that ships. Level 1 was
labelled "Confirm — every action requires your explicit approval" for a level
that never enters ``auto_fix`` at all, so nothing is ever proposed and there is
nothing to approve. Level 2, the level that genuinely asks, was labelled
"Batch — low-risk auto-approved". Levels 3 and 4 promised LOW/MEDIUM/HIGH risk
tiers that do not exist anywhere in the fix path.

An operator who wanted supervised remediation read that ladder, chose 1, and
got an agent that did nothing — the ``total_actions: 0`` symptom, sitting in
plain sight in the control's own name.

The labels were corrected against the table below. This file exists so the
table cannot quietly stop being true: if the gate moves, these tests fail and
the labels have to move with it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre_agent.monitor.cluster_monitor import ClusterMonitor

_PLAN = MagicMock(
    strategy="restart_pod",
    cause_category="crashloop",
    description="Delete the pod so its controller recreates it",
    confidence=0.9,
)

FINDING = {
    "id": "f-1",
    "category": "crashloop",
    "title": "Pod api-7f9 restarting (12x)",
    "autoFixable": True,
    "resources": [{"kind": "Pod", "name": "api-7f9", "namespace": "prod"}],
}


async def _run_at(level: int) -> dict:
    """Drive one scan cycle's remediation path at the given trust level."""
    monitor = ClusterMonitor()
    monitor._subscribers = []
    monitor._broadcast_raw = AsyncMock()
    monitor.broadcast = AsyncMock()

    saved: list[dict] = []
    executed: list = []
    handler = MagicMock(return_value={"success": True, "message": "ok"})
    repo = MagicMock()
    repo.check_pending_proposal.return_value = None
    repo.check_existing_human_review.return_value = None

    with (
        patch("sre_agent.monitor.cluster_monitor.is_autofix_paused", return_value=False),
        patch("sre_agent.monitor.cluster_monitor.get_settings") as settings,
        patch("sre_agent.monitor.cluster_monitor.get_monitor_repo", return_value=repo),
        # Snapshot: the report dict is mutated after it is saved, so appending it
        # by reference would report the *final* status rather than the one the
        # trust level chose.
        patch("sre_agent.monitor.cluster_monitor.save_action", side_effect=lambda r, **kw: saved.append(dict(r))),
        patch("sre_agent.monitor.cluster_monitor.get_core_client") as core,
        patch("sre_agent.monitor.cluster_monitor._estimate_auto_fix_confidence", return_value=0.9),
        patch("sre_agent.monitor.fix_planner.get_investigation_for_finding", return_value=None),
        patch("sre_agent.monitor.fix_planner.default_fix_plan", return_value=_PLAN),
        patch("sre_agent.monitor.autofix.AUTO_FIX_HANDLERS", {"crashloop": handler}),
        # The real executor. Status strings alone cannot tell "asked and
        # waited" apart from "ran it" — only whether this was called can.
        patch(
            "sre_agent.monitor.fix_planner.execute_fix",
            side_effect=lambda plan: executed.append(plan) or ("delete_pod", "before", "after"),
        ),
    ):
        settings.return_value.monitor.autofix_enabled = True
        settings.return_value.monitor.max_trust_level = level
        core.return_value.read_namespaced_pod.return_value = MagicMock(
            metadata=MagicMock(owner_references=[MagicMock(kind="ReplicaSet", name="api")])
        )

        # Ask the monitor, do not restate the rule — a test that hardcodes
        # >= 2 keeps passing when the real gate moves.
        entered = monitor.remediation_enabled
        if entered:
            await monitor.auto_fix([dict(FINDING)])

    return {
        "entered": entered,
        "statuses": [r.get("status") for r in saved],
        "executed": bool(executed),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [0, 1])
async def test_the_low_levels_never_remediate(level):
    """0 and 1 do not enter auto_fix at all.

    This is why "Confirm" was the wrong name for 1: an approval prompt cannot
    appear for an action that is never proposed.
    """
    result = await _run_at(level)
    assert result["entered"] is False, f"level {level} must not enter auto_fix"
    assert result["statuses"] == [], f"level {level} must record no action at all"
    assert result["executed"] is False, f"level {level} must not run a fix"


@pytest.mark.asyncio
async def test_level_two_is_the_one_that_asks():
    """Every fix is proposed and waits for a human. Nothing is auto-approved."""
    result = await _run_at(2)
    assert result["entered"] is True
    assert result["statuses"] == ["proposed"]
    assert result["executed"] is False, "level 2 asks — it must not run the fix itself"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [3, 4])
async def test_the_high_levels_act_without_asking(level):
    """3 and 4 do not propose — they go straight to executing.

    The status is whatever the execution came to; what matters for the ladder
    is that it is never "proposed", because nobody is being asked.
    """
    result = await _run_at(level)
    assert result["entered"] is True
    assert result["executed"] is True, f"level {level} must run the fix without asking"
    assert "proposed" not in result["statuses"], f"level {level} must not ask first"


def test_no_risk_tiering_exists_in_the_fix_path():
    """The old labels promised LOW/MEDIUM/HIGH tiers. Nothing implements them.

    ``risk_level`` appears only in LLM investigation output. If auto_fix ever
    grows real risk tiering, this test should fail and the labels can honestly
    promise it.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "sre_agent" / "monitor" / "cluster_monitor.py"
    body = source.read_text()
    start = body.index("async def auto_fix")
    end = body.index("\n    async def ", start + 1)
    assert "risk" not in body[start:end].lower(), "auto_fix now mentions risk — revisit the trust labels"
