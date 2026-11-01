"""A correct, well-evidenced plan diagnosis must not be reported at confidence 0.0.

Diagnose phases declare ``confidence`` in ``produces``, so the phase output
template asks for it in ``findings`` and again at the top level, where the
example value is ``0.0``. In the live replay after PR #216 the model began
filling only ``findings.confidence`` and leaving the top-level placeholder; the
parser read the placeholder as the asserted ceiling, so ``derive_confidence``
returned 0.0 for diagnoses with three grounded, supporting evidence items.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from sre_agent.evidence import asserted_phase_confidence
from sre_agent.plan_runtime import PlanRuntime


def _diagnose_phase():
    return SimpleNamespace(
        id="diagnose",
        skill_name="sre",
        produces=["root_cause", "scheduler_reason", "confidence"],
        timeout_seconds=60,
    )


# Shaped like the diagnose phase of plan_scheduling_cpu in live run 41271853940.
_EVIDENCE = [
    {
        "observation": "PodScheduled=False, reason Unschedulable: 0/3 nodes available",
        "kind": "resource",
        "call": "c1",
        "source": "describe_pod",
        "stance": "supports",
    },
    {
        "observation": "worker-1 ~500m CPU free; worker-2 ~400m free; master-1 tainted",
        "kind": "resource",
        "call": "c2",
        "source": "list_resources",
        "stance": "supports",
    },
    {
        "observation": "FailedScheduling event repeating the insufficient-cpu message",
        "kind": "event",
        "call": "c3",
        "source": "get_events",
        "stance": "supports",
    },
]


def _answer(**top_level) -> str:
    body = {
        "findings": {
            "root_cause": "Pod requests 2 CPU but no schedulable node has that much free",
            "scheduler_reason": "Insufficient cpu",
            "confidence": 0.85,
        },
        "evidence_summary": "describe_pod and the FailedScheduling event agree.",
        "evidence": _EVIDENCE,
        "alternatives_considered": ["scheduler glitch: ruled out, preemption found no victims"],
        **top_level,
        "open_questions": [],
        "security_notes": [],
    }
    return "```json\n" + json.dumps(body) + "\n```"


_CALLED = ["describe_pod", "list_resources", "get_events"]


@pytest.mark.parametrize(
    "top_level",
    [{"confidence": 0.0}, {}],
    ids=["template-placeholder-left-at-0.0", "top-level-omitted"],
)
def test_findings_confidence_is_the_ceiling_when_top_level_is_placeholder(top_level):
    out = PlanRuntime()._parse_skill_output(_answer(**top_level), _diagnose_phase(), _CALLED)
    assert out.confidence > 0.5
    assert out.confidence <= 0.85


def test_explicit_top_level_confidence_still_wins():
    out = PlanRuntime()._parse_skill_output(_answer(confidence=0.4), _diagnose_phase(), _CALLED)
    assert out.confidence == 0.4


def test_no_figure_anywhere_still_means_no_asserted_confidence():
    assert asserted_phase_confidence({"findings": {}}, {}) == 0.0
    assert asserted_phase_confidence({"confidence": "high"}, {"confidence": None}) == 0.0
    assert asserted_phase_confidence({"confidence": 0.0}, {"confidence": "0.7"}) == 0.7
    assert asserted_phase_confidence({"confidence": 2}, {}) == 1.0


@pytest.mark.parametrize("top_level", [0.0, None], ids=["placeholder-0.0", "omitted"])
def test_plan_replay_reports_correct_diagnosis_with_nonzero_confidence(monkeypatch, top_level):
    """Through the real plan pipeline (phases, parsing, outcome_from_plan, report)."""
    monkeypatch.setenv("PULSE_AGENT_HARNESS", "0")
    from sre_agent.evals import investigation_replay as ir

    fixture = ir.load_fixture("plan_scheduling_cpu")
    client = ir.ScriptedClient(
        fixture["recorded_responses"],
        answer={
            "root_cause": "insufficient cpu on every schedulable node",
            "confidence": 0.85,
            "top_level_confidence": top_level,
        },
    )
    with ir.investigation_isolation():
        outcome = asyncio.run(ir.run_investigation(fixture, client))

    assert outcome["path"] == "plan"
    assert outcome["plan"]["phases"]["diagnose"]["confidence"] > 0.0
    assert outcome["report"]["confidence"] > 0.0
