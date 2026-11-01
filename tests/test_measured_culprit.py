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
must now name something a call in the phase targeted (an object argument) or
returned (the tool's own output), which the phase's root cause also names.
Otherwise the phase is asked again with the gap named, then capped below the
memory bar and treated as an unmet contract.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from sre_agent.evals import investigation_replay as ir
from sre_agent.evals.replay_config import no_recording_message
from sre_agent.evidence import UNMEASURED_CONFIDENCE_CAP, CallLedger
from sre_agent.monitor.investigation_pipeline import MEMORY_CONFIDENCE
from sre_agent.phase_judge import judge_phase, should_retry, unmeasured_fields


@pytest.fixture(autouse=True)
def _offline_model(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_HARNESS", "0")


_FIXTURE = ir.load_fixture("plan_node_disk_pressure")
_RECORDED = _FIXTURE["recorded_responses"]
_FINDING = _FIXTURE["finding"]

_NODE = ("describe_resource", {"kind": "Node", "name": "worker-3", "namespace": "_"})
_EVENTS = ("get_events", {"namespace": "ALL", "resource_kind": "Node", "resource_name": "worker-3"})
_METRICS = ("get_node_metrics", {})
_PODS = ("list_pods", {"namespace": "ALL", "field_selector": "spec.nodeName=worker-3"})
_LOGS = ("get_pod_logs", {"namespace": "batch", "pod_name": "log-shipper-7d9f-q2x"})
_DESCRIBE_SHIPPER = ("describe_pod", {"namespace": "batch", "pod_name": "log-shipper-7d9f-q2x"})

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
_IMAGE_GC_CAUSE = "kubelet failed to garbage-collect container images, filling /var to 94%"


def _supports(items):
    return [{"stance": "supports", **item} for item in items]


def _run(answer: dict, call_tools: list) -> tuple[dict, ir.ScriptedClient]:
    client = ir.ScriptedClient(_RECORDED, answer=answer, call_tools=call_tools)
    with ir.investigation_isolation():
        outcome = asyncio.run(ir.run_investigation(_FIXTURE, client))
    return outcome, client


def _phase_prompts(client: ir.ScriptedClient, phase_id: str) -> list[str]:
    prompts = []
    for request in client.requests:
        first = next((m for m in request.get("messages") or [] if m.get("role") == "user"), None)
        content = first.get("content") if first else ""
        if isinstance(content, str) and f"## Current Phase: {phase_id}" in content and content not in prompts:
            prompts.append(content)
    return prompts


def _assert_capped(outcome: dict) -> dict:
    phase = outcome["plan"]["phases"]["node_diagnostics"]
    assert outcome["report"]["confidence"] < MEMORY_CONFIDENCE
    assert outcome["report"]["confidence"] == UNMEASURED_CONFIDENCE_CAP
    assert phase["confidence"] == UNMEASURED_CONFIDENCE_CAP
    assert phase["status"] == "partial"
    assert "top_consumer" in phase["contract_missing"]
    return phase


# ---------------------------------------------------------------------------
# Through the real plan pipeline
# ---------------------------------------------------------------------------


def test_wrong_image_gc_answer_from_node_level_evidence_is_not_remembered():
    """The real wrong answer's shape: node-only evidence, a culprit no call examined."""
    outcome, client = _run(
        answer={
            "root_cause": _IMAGE_GC_CAUSE,
            "findings": {"top_consumer": "container images (kubelet image GC failing to reclaim space)"},
            "confidence": 0.75,
            "evidence": _supports(_NODE_EVIDENCE) + [_PODS_CONTEXT],
        },
        call_tools=[_NODE, _EVENTS, _METRICS, _PODS],
    )

    _assert_capped(outcome)
    # The phase was sent back once, told what it had not examined.
    prompts = _phase_prompts(client, "node_diagnostics")
    assert len(prompts) == 2
    assert "named top_consumer without examining it" in prompts[1]
    # The guess is still reported; it just is not trusted.
    assert "garbage-collect" in outcome["report"]["suspected_cause"]


def test_retry_that_names_a_path_only_its_own_observation_mentions_is_still_capped():
    """Review blocker: the retry hint asks for a path; a path in prose is not a measurement.

    The retry returns the image-GC answer reshaped around a path, with a
    supporting observation that cites get_node_metrics. That tool's output
    never mentions the path; only the model's description of it does.
    """
    path_answer = {
        "findings": {"top_consumer": "container image layers in /var/lib/containers/storage (image GC failing)"},
        "evidence": _supports(
            [{"observation": "/var/lib/containers/storage at 94% on worker-3", "kind": "metric", "call": "c3"}]
            + _NODE_EVIDENCE[1:]
        ),
    }
    outcome, client = _run(
        answer={
            "root_cause": _IMAGE_GC_CAUSE + " in /var/lib/containers/storage",
            "findings": {"top_consumer": "container images"},
            "confidence": 0.75,
            "evidence": _supports(_NODE_EVIDENCE),
            "retry_answer": path_answer,
        },
        call_tools=[_NODE, _EVENTS, _METRICS],
    )

    phase = _assert_capped(outcome)
    assert len(_phase_prompts(client, "node_diagnostics")) == 2
    assert "/var/lib/containers/storage" in phase["findings"]["top_consumer"]


def test_live_hedge_naming_a_described_pod_as_unconfirmed_suspect_is_capped():
    """Live repeat run 41322819635, attempt 6: wrong at 0.71 past the first version of this check.

    The phase described the log-shipper pod (the replay had no recording, as
    a real cluster might time out), then named it in top_consumer only as an
    unverified suspect while its root cause blamed image GC. The pod name
    matched a call argument, so the field counted as examined.
    """
    hedge = (
        "Not conclusively isolated — likely orphaned/unreclaimed container images (GC failure), not the "
        "evicted pod itself (which only used 120Mi); log-shipper-7d9f-q2x (6h running) is the remaining "
        "suspect but unverified since describe_pod was unavailable"
    )
    outcome, _ = _run(
        answer={
            "root_cause": (
                "Kubelet image garbage collection failed to reclaim space on worker-3's /var filesystem (94% "
                "used), causing sustained DiskPressure=True and triggering eviction of report-gen-55f"
            ),
            "findings": {"top_consumer": hedge},
            "confidence": 0.71,
            "evidence": _supports(_NODE_EVIDENCE) + [_PODS_CONTEXT],
        },
        call_tools=[_NODE, _EVENTS, _METRICS, _PODS, _DESCRIBE_SHIPPER],
    )
    _assert_capped(outcome)


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


def test_a_retry_that_runs_out_of_time_keeps_the_first_answer(monkeypatch):
    """Review: the retry shared the first attempt's timeout, so a slow retry failed the phase at 0."""
    import sre_agent.plan_runtime as plan_runtime
    from sre_agent.plan_templates import match_template

    template = match_template(category="nodes")
    node = next(p for p in template.phases if p.id == "node_diagnostics")
    monkeypatch.setattr(node, "timeout_seconds", 4)
    monkeypatch.setattr(plan_runtime, "_RETRY_TIMEOUT_MARGIN", 0.5)

    outcome, client = _run(
        answer={
            "root_cause": _IMAGE_GC_CAUSE,
            "findings": {"top_consumer": "container images"},
            "confidence": 0.75,
            "evidence": _supports(_NODE_EVIDENCE),
            "retry_answer": {"findings": {"top_consumer": "log-shipper-7d9f-q2x"}},
            "delay_retry": 30,
        },
        call_tools=[_NODE, _EVENTS, _METRICS],
    )

    phase = _assert_capped(outcome)
    assert len(_phase_prompts(client, "node_diagnostics")) == 2
    # The first answer, kept: a cause, not a timed-out phase with none.
    assert phase["findings"]["root_cause"] == _IMAGE_GC_CAUSE
    assert outcome["report"]["suspected_cause"] == _IMAGE_GC_CAUSE


def test_write_gate_says_the_culprit_was_not_examined():
    """Review nit: the gate said "did not produce top_consumer" when it was produced."""
    from sre_agent.plan_runtime import _contract_gaps
    from sre_agent.skill_plan import SkillOutput

    unexamined = SkillOutput("sre", "node_diagnostics", contract_missing=["top_consumer"], unexamined=["top_consumer"])
    missing = SkillOutput("sre", "diagnose", contract_missing=["root_cause"])
    assert _contract_gaps([unexamined]) == "named top_consumer without examining it"
    assert _contract_gaps([missing, unexamined]) == (
        "did not produce root_cause and named top_consumer without examining it"
    )


# ---------------------------------------------------------------------------
# The rule itself, against a ledger of calls and what they returned
# ---------------------------------------------------------------------------

_PHASE = SimpleNamespace(id="node_diagnostics", produces=["root_cause", "top_consumer"], measured=["top_consumer"])


def _ledger(*calls) -> CallLedger:
    """Calls as (tool, input, result) or (tool, input, result, status)."""
    ledger = CallLedger()
    for call in calls:
        tool, args, result, *status = call
        ledger.record({"tool_name": tool, "input": args, "result_text": result, "status": (status or ["success"])[0]})
    return ledger


def _gaps(top_consumer, cause, *calls, incident=_FINDING):
    output = SimpleNamespace(findings={"top_consumer": top_consumer, "root_cause": cause})
    return unmeasured_fields(_PHASE, output, incident, _ledger(*calls))


_NODE_CALLS = [
    (*_NODE, _RECORDED["describe_resource"]),
    (*_EVENTS, _RECORDED["get_events"]),
    (*_METRICS, _RECORDED["get_node_metrics"]),
]


@pytest.mark.parametrize(
    "value",
    [
        "container images",
        "unreclaimed image layers (kubelet GC failing)",
        "ephemeral-storage on worker-3",  # a resource type, on the finding's own node
        "worker-3 /var",
        "node.kubernetes.io/disk-pressure taint",
    ],
)
def test_a_category_or_the_finding_itself_is_not_a_measured_culprit(value):
    assert _gaps(value, f"{value} filled the disk", *_NODE_CALLS) == ["top_consumer"]


@pytest.mark.parametrize(
    "value",
    [
        "Not conclusively identified at pod level — log-shipper-7d9f-q2x plausible but unconfirmed",
        "unknown; log-shipper-7d9f-q2x was not readable",
        "Unverified: log-shipper-7d9f-q2x",
    ],
)
def test_a_value_that_says_it_was_not_determined_is_not_measured(value):
    cause = "log-shipper-7d9f-q2x filled /var"
    assert _gaps(value, cause, *_NODE_CALLS, (*_LOGS, _RECORDED["get_pod_logs"])) == ["top_consumer"]


def test_a_workload_the_phase_read_is_measured():
    calls = [*_NODE_CALLS, (*_LOGS, _RECORDED["get_pod_logs"])]
    for value in ("log-shipper-7d9f-q2x", "batch/log-shipper-7d9f-q2x (trace logging)"):
        assert _gaps(value, "log-shipper-7d9f-q2x at trace level filled /var", *calls) == [], value


def test_a_read_workload_the_root_cause_does_not_blame_is_not_the_measured_culprit():
    calls = [*_NODE_CALLS, (*_LOGS, _RECORDED["get_pod_logs"])]
    assert _gaps("log-shipper-7d9f-q2x", _IMAGE_GC_CAUSE, *calls) == ["top_consumer"]


def test_a_call_that_failed_examined_nothing():
    calls = [*_NODE_CALLS, (*_LOGS, "Error: pods not found", "error")]
    assert _gaps("log-shipper-7d9f-q2x", "log-shipper-7d9f-q2x filled /var", *calls) == ["top_consumer"]


def test_a_path_counts_only_when_a_tool_returned_it():
    """A per-path breakdown names the path in its output, not in its query."""
    cause = "/var/log/pods/batch_log-shipper-* grew 29GiB in 6h"
    breakdown = (
        "get_prometheus_query",
        {"query": 'node_filesystem_avail_bytes{instance="worker-3"}'},
        _RECORDED["get_prometheus_query"],
    )
    assert _gaps("/var/log/pods/batch_log-shipper-*", cause, *_NODE_CALLS, breakdown) == []
    # The same path when no tool returned it: the model's word only.
    assert _gaps("/var/lib/containers/storage", "/var/lib/containers/storage filled", *_NODE_CALLS) == ["top_consumer"]


@pytest.mark.parametrize(
    ("value", "args"),
    [
        # Review: a namespace or selector names a set, not the object examined.
        ("openshift-monitoring/prometheus-k8s-0", {"namespace": "openshift-monitoring"}),
        ("log-shipper-7d9f-q2x", {"namespace": "batch", "label_selector": "app=log-shipper"}),
        ("fluentd-x7k2p", {"namespace": "openshift-logging"}),
    ],
)
def test_namespace_and_selector_arguments_do_not_examine_a_pod(value, args):
    listing = ("list_pods", args, "3 pods: Running")
    assert _gaps(value, f"{value} filled /var", *_NODE_CALLS, listing) == ["top_consumer"]


@pytest.mark.parametrize(
    ("value", "args"),
    [
        ("fluentd", {"namespace": "openshift-logging", "pod_name": "fluentd-x7k2p"}),  # a pod by its owner
        ("es-0", {"namespace": "logging", "pod_name": "es-0"}),  # short, no shape to match
        ("collector container", {"pod_name": "vector-8k2l9", "container": "collector"}),
        # A name past the 120-character summary of the call's input.
        ("a-" + "x" * 130, {"namespace": "batch", "label_selector": "", "pod_name": "a-" + "x" * 130}),
    ],
)
def test_the_object_a_call_targeted_is_examined_whatever_its_shape(value, args):
    calls = [*_NODE_CALLS, ("get_pod_logs", args, "line 1\nline 2")]
    assert _gaps(value, f"{value} filled /var with logs", *calls) == []


def test_memory_pressure_culprit_found_by_a_topk_query_is_examined():
    """Review: the pod name is only in the query's result, not its arguments."""
    incident = {"resources": [{"kind": "Node", "name": "worker-7"}]}
    topk = (
        "get_prometheus_query",
        {"query": 'topk(3, container_memory_working_set_bytes{node="worker-7"})'},
        "pod=analytics-7f9c4-x2kqp namespace=data 11.8GiB\npod=api-5d8b-q9w2z namespace=shop 1.2GiB",
    )
    describe = ("describe_resource", {"kind": "Node", "name": "worker-7"}, "MemoryPressure=True")
    assert (
        _gaps(
            "data/analytics-7f9c4-x2kqp",
            "analytics-7f9c4-x2kqp leaked memory to 11.8GiB",
            describe,
            topk,
            incident=incident,
        )
        == []
    )
    # Naming a pod the query never returned is still a guess.
    assert _gaps("data/etl-runner-1", "etl-runner-1 leaked memory", describe, topk, incident=incident) == [
        "top_consumer"
    ]


def test_a_tool_with_no_data_returns_nothing_to_match():
    describe = (*_DESCRIBE_SHIPPER, no_recording_message("describe_pod"))
    # Targeted by argument, so examined, as a real describe_pod would be...
    assert _gaps("log-shipper-7d9f-q2x", "log-shipper-7d9f-q2x filled /var", *_NODE_CALLS, describe) == []
    # ...but a path it never returned is not.
    assert _gaps("/var/log/pods/batch_x", "/var/log/pods/batch_x filled /var", *_NODE_CALLS, describe) == [
        "top_consumer"
    ]


def test_ledger_keeps_results_out_of_what_is_serialized():
    ledger = _ledger((*_LOGS, "secret-ish log line"))
    assert "secret-ish" not in json.dumps(ledger.as_list())
    call = next(iter(ledger.calls.values()))
    assert call.object_args == {"pod_name": "log-shipper-7d9f-q2x"}
    assert call.result_text == "secret-ish log line"


def test_verdict_retries_once_and_names_the_gap():
    output = SimpleNamespace(
        status="complete",
        findings={"top_consumer": "container images", "root_cause": _IMAGE_GC_CAUSE},
        confidence=0.75,
        branch_signal=None,
        unexamined=["top_consumer"],
    )
    verdict = judge_phase(_PHASE, output)
    assert not verdict.satisfied
    assert verdict.missing == []
    assert verdict.unmeasured == ["top_consumer"]
    assert should_retry(verdict, 1)
    assert not should_retry(verdict, 2)
    assert "without examining it" in verdict.as_retry_hint()


def test_phases_without_measured_fields_are_judged_as_before():
    phase = SimpleNamespace(id="diagnose", produces=["top_consumer"])
    output = SimpleNamespace(findings={"top_consumer": "container images"})
    assert unmeasured_fields(phase, output, _FINDING, _ledger(*_NODE_CALLS)) == []


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
        "The claim asks for a class the cluster lacks: there is no StorageClass named 'fast-ssd'.",
        "There is no fast-ssd StorageClass on this cluster, so the PVC cannot be provisioned.",
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
        # Review: accepted by the first, unanchored version of the new phrases.
        "The cluster has no StorageClass by default, so the claim cannot be provisioned",
        "There is no fast-ssd capacity left in the zone",
        "No fast-ssd PersistentVolume is available to bind the claim",
        "The PVC has no storage class named in its spec, so nothing provisions it",
        # Review: accepted on main too ("storageclass" inside "storageclassname").
        "The PVC is missing a storageClassName",
    ],
)
def test_pvc_fixture_still_rejects_other_causes(cause):
    expected = ir.load_fixture("freeform_pvc_storageclass")["expected"]
    score = ir.score_investigation({"report": {"suspected_cause": cause}}, expected)
    assert not score["cause_correct"]
