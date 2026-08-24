"""Verification contracts for interactive write tools.

The monitor's auto-fix path earned its trust piecewise: RBAC preflight before
proposing, a restorable snapshot before writing (migration 034), and an
affirmative health gate after (health_gate.py). The interactive path had none
of it — a write tool called from chat validated its inputs, mutated the
cluster, and returned a sentence. This module gives the most-used mutating
tools the same contract, as one model instead of four scattered mechanisms:

    precondition -> snapshot -> action -> postcondition probe -> restore

- **Precondition**: read the target before writing. A missing or unreadable
  target refuses the write with a plain reason instead of letting the mutation
  discover the problem — and the read runs under the caller's forwarded token,
  so a permission gap surfaces here, before anything changed.
- **Snapshot**: `snapshot.capture` where the kind supports it, persisted on the
  action row exactly as auto-fix does, so fix history can offer a real undo.
- **Postcondition probe**: tool-specific, not generic health. "Scale to 0"
  verifies as 0 ready replicas; the generic gate would call that a failure,
  and calling it a pass by absence is the bug this system exists to kill.
  Probes run on the monitor's next scans via the existing verification
  pipeline, with a short grace window because rollouts are not instant.
- **Evidence**: every contracted write becomes an action row, so
  `/fix-history` shows the probe's reading — a measurement, not a claim.

The contract never invents success: a probe that cannot read the cluster
reports UNVERIFIABLE, and a tool result that does not affirmatively look like
the tool's success message is passed through unrecorded rather than recorded
as a completed action.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .monitor import health_gate

logger = logging.getLogger("pulse_agent.tool_contracts")

#: Scans a failing probe may wait for a rollout to settle before the verdict
#: is recorded. Applies only to contract probes, never to auto-fix semantics.
PROBE_GRACE_SCANS = 3


class ContractPreconditionError(Exception):
    """The write must not happen; the message says why in operator terms."""


# Replay evals score recorded trajectories with every live dependency patched
# out; a contract precondition reaching for the cluster there would fail
# closed and replace the recorded response. Suspension is explicit and scoped
# (replay_config.offline_context enters it), never ambient configuration.
_SUSPENDED = False


@contextmanager
def suspended():
    """Disable contracts for the duration — offline replay only."""
    global _SUSPENDED
    previous = _SUSPENDED
    _SUSPENDED = True
    try:
        yield
    finally:
        _SUSPENDED = previous


def _clamp_replicas(replicas: int) -> int:
    # Mirrors scale_deployment's own clamp so the probe asserts what the tool
    # actually requested, not what the caller typed.
    return min(max(0, int(replicas)), 100)


def _read_deployment(namespace: str, name: str):
    from .k8s_client import get_apps_client

    return get_apps_client().read_namespaced_deployment(name, namespace)


def _revision_of(dep: Any) -> str:
    return ((dep.metadata.annotations or {}) if dep.metadata else {}).get("deployment.kubernetes.io/revision", "")


def _refuse_missing(kind: str, namespace: str, name: str, exc: Exception) -> ContractPreconditionError:
    if getattr(exc, "status", None) == 404:
        ref = f"{kind} {namespace}/{name}" if namespace else f"{kind} {name}"
        return ContractPreconditionError(f"{ref} was not found — there is nothing to change.")
    return ContractPreconditionError(f"could not read the target before writing: {exc}")


# ── Preconditions ──────────────────────────────────────────────────────────
# Each returns a JSON-able dict of pre-facts the probe will need, or raises
# ContractPreconditionError to refuse the write.


def _pre_restart_deployment(args: dict) -> dict:
    ns, name = args.get("namespace", ""), args.get("name", "")
    try:
        dep = _read_deployment(ns, name)
    except Exception as e:
        raise _refuse_missing("Deployment", ns, name, e) from e
    return {"revision": _revision_of(dep), "desired": dep.spec.replicas or 0}


def _pre_scale_deployment(args: dict) -> dict:
    ns, name = args.get("namespace", ""), args.get("name", "")
    try:
        dep = _read_deployment(ns, name)
    except Exception as e:
        raise _refuse_missing("Deployment", ns, name, e) from e
    return {"previous_replicas": dep.spec.replicas or 0}


def _pre_rollback_deployment(args: dict) -> dict:
    ns, name = args.get("namespace", ""), args.get("name", "")
    try:
        dep = _read_deployment(ns, name)
    except Exception as e:
        raise _refuse_missing("Deployment", ns, name, e) from e
    return {"revision_before": _revision_of(dep)}


def _pre_delete_pod(args: dict) -> dict:
    from .k8s_client import get_apps_client, get_core_client

    ns, name = args.get("namespace", ""), args.get("pod_name", "")
    try:
        pod = get_core_client().read_namespaced_pod(name, ns)
    except Exception as e:
        raise _refuse_missing("Pod", ns, name, e) from e

    owner: dict | None = None
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            try:
                rs = get_apps_client().read_namespaced_replica_set(ref.name, ns)
                dep_ref = next((r for r in rs.metadata.owner_references or [] if r.kind == "Deployment"), None)
                owner = (
                    {"kind": "Deployment", "name": dep_ref.name, "namespace": ns}
                    if dep_ref
                    else {"kind": "ReplicaSet", "name": ref.name, "namespace": ns}
                )
            except Exception:
                owner = {"kind": "ReplicaSet", "name": ref.name, "namespace": ns}
            break
        if ref.kind in ("StatefulSet", "DaemonSet", "Job"):
            owner = {"kind": ref.kind, "name": ref.name, "namespace": ns}
            break
    return {"owner": owner}


def _pre_cordon_node(args: dict) -> dict:
    from .k8s_client import get_core_client

    name = args.get("node_name", "")
    try:
        node = get_core_client().read_node(name)
    except Exception as e:
        raise _refuse_missing("Node", "", name, e) from e
    return {"was_unschedulable": bool(node.spec.unschedulable)}


# ── Postcondition probes ───────────────────────────────────────────────────
# Each returns (status, evidence) in health_gate vocabulary. A probe reads the
# live cluster and states a fact; it never infers success from silence.


def _probe_restart_deployment(args: dict, pre: dict) -> tuple[str, str]:
    result = health_gate.check_resource("Deployment", args.get("name", ""), args.get("namespace", ""))
    return result.status, result.detail


def _probe_scale_deployment(args: dict, pre: dict) -> tuple[str, str]:
    ns, name = args.get("namespace", ""), args.get("name", "")
    requested = _clamp_replicas(args.get("replicas", 0))
    ref = f"Deployment {ns}/{name}"
    try:
        dep = _read_deployment(ns, name)
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return health_gate.FAIL, f"{ref} no longer exists — it was deleted, not scaled"
        return health_gate.UNVERIFIABLE, f"could not read {ref}: {e}"

    desired = dep.spec.replicas or 0
    ready = (dep.status.ready_replicas if dep.status else 0) or 0
    if desired != requested:
        return (
            health_gate.FAIL,
            f"{ref} spec shows {desired} replicas, but the scale requested {requested} — the change did not hold",
        )
    if ready == requested:
        return health_gate.PASS, f"{ref} scaled to {requested} as requested ({ready} ready)"
    return health_gate.FAIL, f"{ref} requested {requested} replicas but {ready} are ready"


def _probe_delete_pod(args: dict, pre: dict) -> tuple[str, str]:
    owner = pre.get("owner")
    pod_ref = f"Pod {args.get('namespace', '')}/{args.get('pod_name', '')}"
    if not owner:
        return (
            health_gate.UNVERIFIABLE,
            f"{pod_ref} had no controller — the deletion is permanent and there is no owner to verify",
        )
    if owner.get("kind") == "Job":
        return health_gate.UNVERIFIABLE, f"{pod_ref} was owned by a Job — completion state, not health, applies"
    result = health_gate.check_resource(owner["kind"], owner["name"], owner["namespace"])
    return result.status, f"verified through owner after deleting {pod_ref}: {result.detail}"


def _probe_rollback_deployment(args: dict, pre: dict) -> tuple[str, str]:
    ns, name = args.get("namespace", ""), args.get("name", "")
    ref = f"Deployment {ns}/{name}"
    gate = health_gate.check_resource("Deployment", name, ns)
    if gate.status != health_gate.PASS:
        return gate.status, gate.detail
    try:
        revision_now = _revision_of(_read_deployment(ns, name))
    except Exception as e:
        return health_gate.UNVERIFIABLE, f"healthy, but could not read revision of {ref}: {e}"
    before = pre.get("revision_before", "")
    if before and revision_now == before:
        return (
            health_gate.FAIL,
            f"{ref} is healthy but still at revision {revision_now} — the rollback did not take effect",
        )
    return health_gate.PASS, f"{gate.detail}; revision moved {before or '?'} -> {revision_now}"


def _probe_cordon_node(args: dict, pre: dict) -> tuple[str, str]:
    from .k8s_client import get_core_client

    name = args.get("node_name", "")
    try:
        node = get_core_client().read_node(name)
    except Exception as e:
        return health_gate.UNVERIFIABLE, f"could not read Node {name}: {e}"
    if bool(node.spec.unschedulable):
        return health_gate.PASS, f"Node {name} is marked unschedulable"
    return health_gate.FAIL, f"Node {name} is still schedulable — the cordon did not hold"


# ── Contract registry ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolContract:
    tool: str
    precheck: Callable[[dict], dict]
    probe: Callable[[dict, dict], tuple[str, str]]
    #: Prefix of the tool's own success message. Anything else (validator
    #: errors, ToolError strings) passes through without an action row.
    success_prefix: str
    #: Resource the probe watches, for the action row and the health gate.
    verify_target: Callable[[dict, dict], dict | None]
    #: Kind/name/namespace to snapshot before the write, or None.
    snapshot_target: Callable[[dict, dict], tuple[str, str, str] | None]


def _deployment_target(args: dict, pre: dict) -> dict:
    return {"kind": "Deployment", "name": args.get("name", ""), "namespace": args.get("namespace", "")}


def _deployment_snapshot(args: dict, pre: dict) -> tuple[str, str, str]:
    return ("Deployment", args.get("name", ""), args.get("namespace", ""))


TOOL_CONTRACTS: dict[str, ToolContract] = {
    "restart_deployment": ToolContract(
        tool="restart_deployment",
        precheck=_pre_restart_deployment,
        probe=_probe_restart_deployment,
        success_prefix="Rolling restart triggered",
        verify_target=_deployment_target,
        snapshot_target=_deployment_snapshot,
    ),
    "scale_deployment": ToolContract(
        tool="scale_deployment",
        precheck=_pre_scale_deployment,
        probe=_probe_scale_deployment,
        success_prefix="Scaled ",
        verify_target=_deployment_target,
        snapshot_target=_deployment_snapshot,
    ),
    "delete_pod": ToolContract(
        tool="delete_pod",
        precheck=_pre_delete_pod,
        probe=_probe_delete_pod,
        success_prefix="Pod ",
        verify_target=lambda args, pre: (
            pre.get("owner")
            or {"kind": "Pod", "name": args.get("pod_name", ""), "namespace": args.get("namespace", "")}
        ),
        snapshot_target=lambda args, pre: None,
    ),
    "rollback_deployment": ToolContract(
        tool="rollback_deployment",
        precheck=_pre_rollback_deployment,
        probe=_probe_rollback_deployment,
        success_prefix="Rolled back ",
        verify_target=_deployment_target,
        snapshot_target=_deployment_snapshot,
    ),
    "cordon_node": ToolContract(
        tool="cordon_node",
        precheck=_pre_cordon_node,
        probe=_probe_cordon_node,
        success_prefix="Node ",
        verify_target=lambda args, pre: {"kind": "Node", "name": args.get("node_name", ""), "namespace": ""},
        snapshot_target=lambda args, pre: None,
    ),
}


def run_probe(probe_payload: dict) -> tuple[str, str]:
    """Execute a stored probe. Called by the verification pipeline on scan.

    Unknown or malformed payloads are UNVERIFIABLE — the pipeline must treat
    that as "not verified", never as a pass.
    """
    contract = TOOL_CONTRACTS.get(str(probe_payload.get("tool", "")))
    if contract is None:
        return health_gate.UNVERIFIABLE, f"no contract for tool {probe_payload.get('tool')!r}"
    try:
        return contract.probe(dict(probe_payload.get("args") or {}), dict(probe_payload.get("pre") or {}))
    except Exception as e:  # a crashed probe is a fact about the check, not the fix
        logger.warning("Contract probe for %s crashed", contract.tool, exc_info=True)
        return health_gate.UNVERIFIABLE, f"probe crashed: {e}"


# ── Execution wrapper ──────────────────────────────────────────────────────


def _record_and_schedule(
    contract: ToolContract,
    args: dict,
    pre: dict,
    snapshot_blob: str,
    result_text: str,
    duration_ms: int,
) -> tuple[str, str]:
    """Persist the action row and register the probe. Returns (action_id, note)."""
    from .monitor.actions import save_action
    from .monitor.findings import _ts

    action_id = f"a-{uuid.uuid4().hex[:12]}"
    target = contract.verify_target(args, pre)
    action_report = {
        "id": action_id,
        "type": "action",
        "findingId": "",
        "tool": contract.tool,
        "input": args,
        "status": "completed",
        "beforeState": _describe_pre(contract.tool, args, pre),
        "afterState": result_text[:500],
        "reasoning": "Interactive write executed under a verification contract",
        "durationMs": duration_ms,
        "timestamp": _ts(),
        "verificationStatus": "pending",
    }
    if snapshot_blob:
        action_report["beforeSnapshot"] = snapshot_blob
    save_action(action_report, category="chat_action", resources=[target] if target else [])

    from .monitor.cluster_monitor import get_cluster_monitor_sync

    monitor = get_cluster_monitor_sync()
    if monitor is not None:
        monitor.schedule_contract_verification(
            action_id,
            resources=[target] if target else [],
            probe={"tool": contract.tool, "args": args, "pre": pre},
        )
        note = f"postcondition probe scheduled on the next monitor scan (action {action_id})"
    else:
        # No monitor loop in this process (CLI mode). Saying "pending" forever
        # would be a lie; say plainly that nothing will come back.
        from .monitor.actions import update_action_verification

        update_action_verification(
            action_id, "unverifiable", "monitor loop not running in this process — no probe will run"
        )
        note = f"recorded as action {action_id}; no monitor loop is running, so the postcondition was not scheduled"
    return action_id, note


def _describe_pre(tool: str, args: dict, pre: dict) -> str:
    ns, name = args.get("namespace", ""), args.get("name", "")
    if tool == "scale_deployment":
        return f"Deployment {ns}/{name}: replicas={pre.get('previous_replicas')}"
    if tool == "restart_deployment":
        return f"Deployment {ns}/{name}: revision={pre.get('revision')}, desired={pre.get('desired')}"
    if tool == "rollback_deployment":
        return f"Deployment {ns}/{name}: revision={pre.get('revision_before')}"
    if tool == "delete_pod":
        owner = pre.get("owner")
        owned = f"owned by {owner['kind']} {owner['name']}" if owner else "no controller (bare pod)"
        return f"Pod {ns}/{args.get('pod_name', '')}: {owned}"
    if tool == "cordon_node":
        return f"Node {args.get('node_name', '')}: unschedulable={pre.get('was_unschedulable')}"
    return ""


def execute_with_contract(name: str, input_data: dict, call: Callable[[], Any]) -> Any:
    """Run a tool call under its verification contract, if it has one.

    Tools without a contract are called unchanged. For contracted tools the
    order is: precondition read (refuses the write with a reason), snapshot,
    the tool's own execution, then an action row + a scheduled postcondition
    probe. The tool's return shape (str or (str, component)) is preserved,
    with a contract note appended to the text.
    """
    contract = TOOL_CONTRACTS.get(name)
    if contract is None or _SUSPENDED:
        return call()

    args = dict(input_data or {})

    try:
        pre = contract.precheck(args)
    except ContractPreconditionError as e:
        return f"Precondition failed — {e} The write was not attempted."
    except Exception as e:
        # Fail closed: mutating a cluster we could not read is the worse error.
        logger.warning("Contract precheck for %s crashed", name, exc_info=True)
        return f"Precondition check for {name} could not complete ({e}). The write was not attempted."

    snapshot_blob = ""
    target = contract.snapshot_target(args, pre)
    if target is not None:
        from . import snapshot as snap_mod

        snapshot_blob = snap_mod.to_json(snap_mod.capture(*target))

    start = time.monotonic()
    result = call()
    duration_ms = int((time.monotonic() - start) * 1000)

    text, component = result if isinstance(result, tuple) and len(result) == 2 else (result, None)
    if not isinstance(text, str) or not text.startswith(contract.success_prefix):
        # Validator message, ToolError string, or anything else that is not the
        # tool's own success sentence: pass it through, record nothing.
        return result

    try:
        _action_id, note = _record_and_schedule(contract, args, pre, snapshot_blob, text, duration_ms)
    except Exception:
        logger.exception("Contract recording for %s failed; returning the tool result unannotated", name)
        return result

    undo = "snapshot captured for rollback" if snapshot_blob else "no snapshot for this kind"
    annotated = f"{text}\n\nVerification contract: {undo}; {note}."
    return (annotated, component) if component is not None else annotated
