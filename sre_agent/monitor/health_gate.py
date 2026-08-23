"""Affirmative post-fix health check.

Pulse decided a fix worked by looking for the finding and not seeing it:

    else:
        status = "verified"
        evidence = f"No active {category} findings for affected resources..."

Absence is not evidence. A finding also stops appearing when the scanner
errored, when the namespace was drained, or when the workload was deleted
outright — so deleting a broken Deployment scored as a *verified fix*. That
verdict is what promotes a diagnosis into a reusable skill and what an
operator reads when deciding whether to raise the trust level, which makes
it the worst place in the system to accept a silent default.

Hermes runs deterministic quality gates before its judge is allowed to say
"done", and feeds a failed gate's own output back as the next prompt. This is
the cluster equivalent: read the live object and require it to affirmatively
look healthy. The gate's output becomes the verification evidence, so what an
operator reads is a measurement rather than a restatement of the fallback.

The gate never invents success. It returns UNVERIFIABLE — distinct from both
pass and fail — whenever it cannot get a clear answer, and the caller must
treat that as "not verified".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("pulse_agent.monitor.health_gate")

PASS = "pass"
FAIL = "fail"
UNVERIFIABLE = "unverifiable"

# Kinds whose readiness we can state as a fact. Anything else is reported
# UNVERIFIABLE rather than guessed at.
# ReplicaSet is here because a Deployment's pods are owned by one, and a
# deleted pod is verified through its owner.
CHECKABLE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Pod")


@dataclass
class GateResult:
    """One resource's health verdict plus the reading that produced it."""

    status: str
    resource: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == PASS


def _fmt(kind: str, namespace: str, name: str) -> str:
    return f"{kind} {namespace}/{name}"


def _check_workload(kind: str, name: str, namespace: str) -> GateResult:
    ref = _fmt(kind, namespace, name)
    try:
        from ..k8s_client import get_apps_client

        apps = get_apps_client()
        reader = {
            "Deployment": apps.read_namespaced_deployment,
            "StatefulSet": apps.read_namespaced_stateful_set,
            "DaemonSet": apps.read_namespaced_daemon_set,
            "ReplicaSet": apps.read_namespaced_replica_set,
        }[kind]
        obj = reader(name, namespace)
    except Exception as e:
        # A 404 here is the case that matters most: the workload the fix was
        # meant to repair is gone. That is emphatically not a verified fix,
        # but the old absence-based check scored it as one.
        if getattr(e, "status", None) == 404:
            return GateResult(FAIL, ref, f"{ref} no longer exists — it was deleted, not repaired")
        logger.warning("Health gate could not read %s", ref, exc_info=True)
        return GateResult(UNVERIFIABLE, ref, f"could not read {ref}: {e}")

    status = getattr(obj, "status", None)
    if status is None:
        return GateResult(UNVERIFIABLE, ref, f"{ref} reported no status")

    if kind == "DaemonSet":
        desired = getattr(status, "desired_number_scheduled", None)
        ready = getattr(status, "number_ready", None)
    else:
        spec = getattr(obj, "spec", None)
        desired = getattr(spec, "replicas", None)
        ready = getattr(status, "ready_replicas", None)

    if desired is None:
        return GateResult(UNVERIFIABLE, ref, f"{ref} did not report a desired replica count")

    ready = ready or 0

    # A workload scaled to zero is not healthy, it is switched off. Reporting
    # 0/0 as a pass would let "scale it to zero" verify as a fix.
    if desired == 0:
        return GateResult(FAIL, ref, f"{ref} is scaled to 0 replicas — not running, so not repaired")

    if ready < desired:
        return GateResult(FAIL, ref, f"{ref} has {ready}/{desired} replicas ready")

    return GateResult(PASS, ref, f"{ref} has {ready}/{desired} replicas ready")


def _check_pod(name: str, namespace: str) -> GateResult:
    ref = _fmt("Pod", namespace, name)
    try:
        from ..k8s_client import get_core_client

        pod = get_core_client().read_namespaced_pod(name, namespace)
    except Exception as e:
        if getattr(e, "status", None) == 404:
            # Pods are cattle: one disappearing is normal and says nothing
            # either way about the fix, so this is unverifiable, not a failure.
            return GateResult(UNVERIFIABLE, ref, f"{ref} no longer exists (pods are replaced routinely)")
        logger.warning("Health gate could not read %s", ref, exc_info=True)
        return GateResult(UNVERIFIABLE, ref, f"could not read {ref}: {e}")

    phase = getattr(getattr(pod, "status", None), "phase", None)
    if phase in ("Running", "Succeeded"):
        statuses = getattr(pod.status, "container_statuses", None) or []
        restarts = sum(int(getattr(cs, "restart_count", 0) or 0) for cs in statuses)
        not_ready = [getattr(cs, "name", "?") for cs in statuses if not getattr(cs, "ready", False)]
        if not_ready and phase == "Running":
            return GateResult(FAIL, ref, f"{ref} is Running but containers not ready: {', '.join(not_ready)}")
        return GateResult(PASS, ref, f"{ref} is {phase} with {restarts} restarts")
    return GateResult(FAIL, ref, f"{ref} is in phase {phase}")


def check_resource(kind: str, name: str, namespace: str) -> GateResult:
    """Read one live resource and say whether it is affirmatively healthy."""
    if not name or kind not in CHECKABLE_KINDS:
        return GateResult(UNVERIFIABLE, _fmt(kind or "?", namespace, name or "?"), f"no health check for kind {kind!r}")
    if kind == "Pod":
        return _check_pod(name, namespace)
    return _check_workload(kind, name, namespace)


def check_resources(resources: list[dict[str, Any]]) -> tuple[str, str]:
    """Run the gate over every resource a fix touched.

    Returns (status, evidence). The verdict is deliberately conservative:
    any failure fails the gate, and a gate that could not read anything at
    all is UNVERIFIABLE rather than a pass. Only a reading that affirmatively
    shows healthy resources returns PASS.
    """
    if not resources:
        return UNVERIFIABLE, "no resources recorded for this action — nothing to check"

    results = [
        check_resource(str(r.get("kind", "")), str(r.get("name", "")), str(r.get("namespace", ""))) for r in resources
    ]

    failed = [r for r in results if r.status == FAIL]
    passed = [r for r in results if r.status == PASS]

    if failed:
        return FAIL, "; ".join(r.detail for r in failed)
    if not passed:
        return UNVERIFIABLE, "; ".join(r.detail for r in results)
    return PASS, "; ".join(r.detail for r in passed)
