"""A plan diagnosis that names its culprit without examining it is not reported at 0.7+.

In seven live replay runs after #223, ``plan_node_disk_pressure`` named the
wrong cause in 18 of 28 attempts, 8 of them at confidence >= 0.7 (the bar at
which the monitor stores a diagnosis in memory). Every wrong attempt said the
kubelet's image garbage collection had failed and blamed "container images";
none of them read the log-shipper pod that was filling /var. Their evidence was
the node's conditions (DiskPressure=True), its disk metric (94%) and its events
(an eviction, FreeDiskSpaceFailed): what every disk-pressure cause has in
common. Three or four such items, each at the default 0.6, put the derived
ceiling at 0.71-0.81, so the model's own 0.75 went through unchanged.

The node-pressure template already required ``top_consumer`` for this reason,
but the phase judge only checked that it was filled in. A ``measured`` field
must now name something one of the phase's calls examined; otherwise the phase
is asked again with the gap named, then capped below the memory bar and
treated as an unmet contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sre_agent.evals import investigation_replay as ir
from sre_agent.evidence import UNMEASURED_CONFIDENCE_CAP
from sre_agent.monitor.investigation_pipeline import MEMORY_CONFIDENCE
from sre_agent.phase_judge import judge_phase, should_retry, unmeasured_fields


@pytest.fixture(autouse=True)
def _offline_model(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_HARNESS", "0")


_NODE = ("describe_resource", {"kind": "Node", "name": "worker-3", "namespace": "_"})
_EVENTS = ("get_events", {"namespace": "ALL", "resource_kind": "Node", "resource_name": "worker-3"})
_METRICS = ("get_node_metrics", {})
_PODS = ("list_pods", {"namespace": "ALL", "field_selector": "spec.nodeName=worker-3"})
_LOGS = ("get_pod_logs", {"namespace": "batch", "pod_name": "log-shipper-7d9f-q2x"})

# The node-level evidence of live run 41278123000 attempt 2 (wrong, reported at 0.75).
_NODE_EVIDENCE = [
    {"observation": "worker-3 Disk(/var)=94% vs 48-51% on worker-1/worker-2", "kind": "metric", "call": "c3"},
    {
        "observation": "FreeDiskSpaceFailed: failed to garbage collect required amount of images (25m ago)",
        "kind": "event",
        "call": "c2",
    },
    {
        "observation": "Evicted report-gen-55f: node low on ephemeral-storage, container was using only 120Mi",
        "kind": "event",
        "call": "c2",
    },
    {
        "observation": "DiskPressure=True (KubeletHasDiskPressure), NoSchedule taint present",
        "kind": "resource",
        "call": "c1",
    },
]
_PODS_CONTEXT = {
    "observation": "Only 2 pods on worker-3: log-shipper-7d9f-q2x (Running) and report-gen-55f (Evicted)",
    "kind": "resource",
    "call": "c4",
    "stance": "context",
}


def _supports(items):
    return [{"stance": "supports", **item} for item in items]


def _run(answer: dict, call_tools: list) -> tuple[dict, ir.ScriptedClient]:
    fixture = ir.load_fixture("plan_node_disk_pressure")
    client = ir.ScriptedClient(fixture["recorded_responses"], answer=answer, call_tools=call_tools)
    with ir.investigation_isolation():
        outcome = asyncio.run(ir.run_investigation(fixture, client))
    return outcome, client


def _phase_prompts(client: ir.ScriptedClient, phase_id: str) -> list[str]:
    prompts = []
    for request in client.requests:
        first = next((m for m in request.get("messages") or [] if m.get("role") == "user"), None)
        content = first.get("content") if first else ""
        if isinstance(content, str) and f"## Current Phase: {phase_id}" in content and content not in prompts:
            prompts.append(content)
    return prompts


def test_wrong_image_gc_answer_from_node_level_evidence_is_not_remembered():
    """The real wrong answer's shape: node-only evidence, a culprit no call examined."""
    outcome, client = _run(
        answer={
            "root_cause": "kubelet failed to garbage-collect container images, filling /var to 94%",
            "findings": {"top_consumer": "container images (kubelet image GC failing to reclaim space)"},
            "confidence": 0.75,
            "evidence": _supports(_NODE_EVIDENCE) + [_PODS_CONTEXT],
        },
        call_tools=[_NODE, _EVENTS, _METRICS, _PODS],
    )

    phase = outcome["plan"]["phases"]["node_diagnostics"]
    assert outcome["report"]["confidence"] < MEMORY_CONFIDENCE
    assert outcome["report"]["confidence"] == UNMEASURED_CONFIDENCE_CAP
    assert phase["confidence"] == UNMEASURED_CONFIDENCE_CAP
    assert phase["status"] == "partial"
    assert "top_consumer" in phase["contract_missing"]
    # The phase was sent back once, told what it had not examined.
    prompts = _phase_prompts(client, "node_diagnostics")
    assert len(prompts) == 2
    assert "named top_consumer without examining it" in prompts[1]
    # The guess is still reported; it just is not trusted.
    assert "garbage-collect" in outcome["report"]["suspected_cause"]


def test_right_answer_that_read_the_consumer_keeps_its_confidence():
    """The real right answer's shape: the same node evidence plus the log-shipper's logs."""
    logs = {
        "observation": "log-shipper-7d9f-q2x logs show LOG_LEVEL=trace, one payload line repeated ~4.1M times in 6h",
        "kind": "log",
        "call": "c5",
    }
    outcome, client = _run(
        answer={
            "root_cause": "log-shipper-7d9f-q2x at LOG_LEVEL=trace filled /var/log/pods on worker-3",
            "findings": {"top_consumer": "batch/log-shipper-7d9f-q2x container logs (LOG_LEVEL=trace)"},
            "confidence": 0.75,
            "evidence": _supports(_NODE_EVIDENCE + [logs]) + [_PODS_CONTEXT],
        },
        call_tools=[_NODE, _EVENTS, _METRICS, _PODS, _LOGS],
    )

    phase = outcome["plan"]["phases"]["node_diagnostics"]
    assert phase["contract_missing"] == []
    assert phase["status"] == "complete"
    assert outcome["report"]["confidence"] == 0.75
    assert len(_phase_prompts(client, "node_diagnostics")) == 1


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

_INCIDENT = {"resources": [{"kind": "Node", "name": "worker-3"}]}
_PHASE = SimpleNamespace(id="node_diagnostics", produces=["top_consumer"], measured=["top_consumer"])


def _output(top_consumer, calls, evidence=()):
    return SimpleNamespace(
        status="complete",
        findings={"top_consumer": top_consumer},
        confidence=0.75,
        branch_signal=None,
        tool_calls=[{"ref": f"c{i}", "tool": t, "status": s, "input": inp} for i, (t, s, inp) in enumerate(calls, 1)],
        evidence=list(evidence),
    )


_NODE_CALLS = [
    ("describe_resource", "success", "kind=Node, name=worker-3, namespace=_"),
    ("get_events", "success", "namespace=ALL, resource_kind=Node, resource_name=worker-3"),
    ("get_node_metrics", "success", ""),
]


@pytest.mark.parametrize(
    "value",
    [
        "container images",
        "unreclaimed image layers (kubelet GC failing)",
        "ephemeral-storage on worker-3",  # the finding's own node, and a resource type
        "worker-3 /var",
    ],
)
def test_a_category_or_the_finding_itself_is_not_a_measured_culprit(value):
    assert unmeasured_fields(_PHASE, _output(value, _NODE_CALLS), _INCIDENT) == ["top_consumer"]


def test_a_workload_the_phase_read_is_measured():
    calls = [*_NODE_CALLS, ("get_pod_logs", "success", "namespace=batch, pod_name=log-shipper-7d9f-q2x")]
    for value in ("log-shipper-7d9f-q2x", "log-shipper pod", "batch/log-shipper-7d9f-q2x"):
        assert unmeasured_fields(_PHASE, _output(value, calls), _INCIDENT) == [], value


def test_a_workload_only_listed_or_read_by_a_failed_call_is_not_measured():
    listed = [*_NODE_CALLS, ("list_pods", "success", "namespace=ALL, field_selector=spec.nodeName=worker-3")]
    assert unmeasured_fields(_PHASE, _output("log-shipper-7d9f-q2x", listed), _INCIDENT) == ["top_consumer"]
    failed = [*_NODE_CALLS, ("get_pod_logs", "error", "namespace=batch, pod_name=log-shipper-7d9f-q2x")]
    assert unmeasured_fields(_PHASE, _output("log-shipper-7d9f-q2x", failed), _INCIDENT) == ["top_consumer"]


def test_a_path_named_in_a_sourced_usage_breakdown_is_measured():
    """A per-path breakdown names the path in its output, not in its query."""
    calls = [
        *_NODE_CALLS,
        ("get_prometheus_query", "success", 'query=node_filesystem_avail_bytes{instance="worker-3"}'),
    ]
    breakdown = {
        "observation": "Largest growth on /var: /var/log/pods/batch_log-shipper-* (+29GiB in 6h)",
        "source": "get_prometheus_query",
        "stance": "supports",
    }
    value = "/var/log/pods/batch_log-shipper-*"
    assert unmeasured_fields(_PHASE, _output(value, calls, [breakdown]), _INCIDENT) == []
    # The same claim from an observation whose source the run never read does not count.
    unsourced = {**breakdown, "source": ""}
    assert unmeasured_fields(_PHASE, _output(value, calls, [unsourced]), _INCIDENT) == ["top_consumer"]


def test_verdict_retries_once_and_names_the_gap():
    verdict = judge_phase(_PHASE, _output("container images", _NODE_CALLS), _INCIDENT)
    assert not verdict.satisfied
    assert verdict.missing == []
    assert verdict.unmeasured == ["top_consumer"]
    assert should_retry(verdict, 1)
    assert not should_retry(verdict, 2)
    assert "without examining it" in verdict.as_retry_hint()


def test_phases_without_measured_fields_are_judged_as_before():
    phase = SimpleNamespace(id="diagnose", produces=["top_consumer"])
    assert judge_phase(phase, _output("container images", _NODE_CALLS), _INCIDENT).satisfied


def test_node_pressure_template_measures_top_consumer_and_temporal_keeps_it():
    from sre_agent.plan_templates import match_template
    from sre_agent.temporal.activities import _phase_from_dict, plan_to_dict

    template = match_template(category="nodes")
    phase = next(p for p in template.phases if p.id == "node_diagnostics")
    assert phase.measured == ["top_consumer"]
    restored = _phase_from_dict(next(p for p in plan_to_dict(template)["phases"] if p["id"] == "node_diagnostics"))
    assert restored.measured == ["top_consumer"]


# ---------------------------------------------------------------------------
# freeform_pvc_storageclass: right answers the matcher scored as wrong
# ---------------------------------------------------------------------------
#
# Its only two "wrong" attempts in the same seven runs (both at 0.85) named the
# missing StorageClass correctly, in words the negation list did not have.


@pytest.mark.parametrize(
    "cause",
    [
        # Live run 41276500541, attempt 3.
        "The PVC's storageClassName is set to 'fast-ssd', but no StorageClass by that name exists on the "
        "cluster (only 'gp3-csi' [default] and 'gp2-csi' are present). The provisioner cannot create a "
        "volume, so the claim never binds.",
        # Live run 41282849215, attempt 3.
        "The PVC spec references storageClassName 'fast-ssd', but no StorageClass by that name exists in "
        "the cluster (only 'gp3-csi' (default) and 'gp2-csi' are present). The provisioner cannot create "
        "a PV, so the claim stays Pending indefinitely.",
    ],
)
def test_pvc_fixture_accepts_the_missing_storageclass_however_it_is_worded(cause):
    expected = ir.load_fixture("freeform_pvc_storageclass")["expected"]
    score = ir.score_investigation({"report": {"suspected_cause": cause}}, expected)
    assert score["cause_correct"]


@pytest.mark.parametrize(
    "cause",
    [
        "The PVC has no storageClassName set and no default StorageClass applies",
        "The fast-ssd StorageClass exists but its provisioner is crashlooping",
        "WaitForFirstConsumer: the claim is waiting for its pod to be scheduled",
    ],
)
def test_pvc_fixture_still_rejects_other_causes(cause):
    expected = ir.load_fixture("freeform_pvc_storageclass")["expected"]
    score = ir.score_investigation({"report": {"suspected_cause": cause}}, expected)
    assert not score["cause_correct"]
