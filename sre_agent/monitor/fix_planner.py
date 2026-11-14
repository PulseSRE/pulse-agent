"""Intelligent auto-fix planning — maps investigation diagnosis to targeted fixes.

Sits between the investigation result and auto-fix execution:
1. Query latest investigation for the finding
2. Classify the root cause from suspected_cause text
3. Select a targeted fix strategy
4. Fall back to blunt handlers if no strategy matches
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from ..k8s_client import get_apps_client, get_core_client

logger = logging.getLogger("pulse_agent.monitor")


def get_investigation_for_finding(finding_id: str) -> dict | None:
    """Look up the latest completed investigation for a finding."""
    try:
        from ..repositories.monitor_repo import get_monitor_repo

        return get_monitor_repo().get_investigation_for_finding(finding_id)
    except Exception:
        logger.debug("Failed to look up investigation for %s", finding_id, exc_info=True)
        return None


# Root cause categories. Specific phrases decide; generic words only break a
# tie when nothing specific matched.
#
# This used to be one first-match-wins list with generic words early: "image"
# and "does not exist" sat in bad_image ahead of every other category, so
# 'secret "db-creds" does not exist' and "image pull secret not found" were
# both classified as a bad image and got an image patch.
_SPECIFIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("oom", [r"\boom", "out of memory", "exceeded memory", "memory limit", "exit code 137"]),
    (
        "missing_config",
        [
            r"configmap\b.*\b(not found|does not exist|missing)",
            r"secret\b.*\b(not found|does not exist|missing)",
            "createcontainerconfigerror",
            "couldn't find key",
        ],
    ),
    (
        "bad_image",
        [
            "imagepullbackoff",
            "errimagepull",
            "invalidimagename",
            "pull access denied",
            "manifest unknown",
            "not found in registry",
            r"\bimage\b.*\b(does not exist|not found|unknown)",
        ],
    ),
    ("quota_exceeded", ["quota", "resourcequota", "limit reached"]),
    ("probe_failure", ["readiness probe", "liveness probe", "startup probe", "probe failed"]),
    ("dependency", ["connection timed out", "no such host", r"\bdns\b", "service unavailable"]),
    (
        "crash_exit",
        [
            "exit code",
            "fatal",
            "panic",
            "segfault",
            "error code",
            "exits immediately",
            "short-lived",
            "no persistent",
            "no long-running",
            "no foreground process",
        ],
    ),
]
_GENERIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("probe_failure", ["connection refused"]),
    ("missing_config", ["configmap", "not found", "missing"]),
    ("bad_image", ["image", "does not exist"]),
    ("quota_exceeded", ["exceeded", "forbidden"]),
]


def _matches(patterns: list[tuple[str, list[str]]], text: str) -> list[str]:
    return [category for category, pats in patterns if any(re.search(p, text) for p in pats)]


def classify_root_cause(suspected_cause: str) -> str:
    """Classify a suspected cause string into a root cause category.

    Returns "unknown" when the text points two specific ways at once (an image
    pull failing *because* its pull secret is missing): the targeted fixes are
    writes, and guessing between two of them is how the wrong one gets applied.
    "unknown" falls back to the category's reviewed default plan.
    """
    if not suspected_cause:
        return "unknown"

    lower = suspected_cause.lower()
    specific = _matches(_SPECIFIC_PATTERNS, lower)
    # A crash with an exit code is what OOM, a missing config or a failed
    # dependency look like from outside; it only decides on its own.
    if len(specific) > 1 and "crash_exit" in specific:
        specific.remove("crash_exit")
    if len(specific) == 1:
        return specific[0]
    if len(specific) > 1:
        return "unknown"

    generic = _matches(_GENERIC_PATTERNS, lower)
    return generic[0] if generic else "unknown"


@dataclass
class FixPlan:
    """A targeted fix plan produced by the fix planner."""

    strategy: str  # e.g., "patch_image", "patch_resources", "create_configmap"
    cause_category: str  # from classify_root_cause
    confidence: float  # from investigation
    description: str  # human-readable description of what will be done
    params: dict  # strategy-specific parameters


# Minimum confidence to attempt a targeted fix
_MIN_TARGETED_CONFIDENCE = 0.5

# Map root cause category to fix strategy
_STRATEGY_MAP: dict[str, str] = {
    "bad_image": "patch_image",
    "oom": "patch_resources",
    "missing_config": "create_configmap",
    "probe_failure": "patch_probe",
    "quota_exceeded": "suggest_quota_increase",
    "crash_exit": "require_human_review",
    "dependency": "require_human_review",
}


def plan_fix(investigation: dict, finding: dict) -> FixPlan | None:
    """Plan a targeted fix based on investigation results.

    Returns a FixPlan if a targeted strategy is available and confidence
    is sufficient. Returns None to fall back to blunt handlers.
    """
    suspected_cause = investigation.get("suspected_cause", "") or investigation.get("suspectedCause", "")
    recommended_fix = investigation.get("recommended_fix", "") or investigation.get("recommendedFix", "")
    confidence = float(investigation.get("confidence", 0))

    if confidence < _MIN_TARGETED_CONFIDENCE:
        return None

    cause_category = classify_root_cause(suspected_cause)
    strategy = _STRATEGY_MAP.get(cause_category)

    if not strategy:
        return None

    return _steer(
        gate_by_cause(
            FixPlan(
                strategy=strategy,
                cause_category=cause_category,
                confidence=confidence,
                description=f"{strategy}: {recommended_fix[:200]}",
                params={
                    "suspected_cause": suspected_cause,
                    "recommended_fix": recommended_fix,
                    "resources": finding.get("resources", []),
                },
            ),
            finding,
        )
    )


def _steer(plan: FixPlan) -> FixPlan:
    """Let what has held for this cause before adjust the plan (fix_learning)."""
    try:
        from ..fix_learning import steer

        return steer(plan)
    except Exception:
        logger.debug("Fix track-record steering failed", exc_info=True)
        return plan


# Categories where a default fix strategy is known without investigation.
# Allows auto-fix to act immediately instead of waiting for async LLM diagnosis.
_FAST_PATH_STRATEGIES: dict[str, tuple[str, str]] = {
    "crashloop": ("restart_controller", "Delete crashlooping pod — controller recreates"),
    "image_pull": ("patch_image", "Rollback deployment to previous working image"),
    # The workloads scanner reports the Deployment, not a pod. This used to be
    # restart_controller, which reads the resource as a pod: every attempt
    # 404'd (140 of 140 failed on one cluster) and was retried each scan.
    "workloads": ("rollout_restart", "Rolling restart of the degraded Deployment"),
}


# Firing alerts whose remedy is known and narrow. Deliberately a short list:
# an alert says something is wrong, not what to do about it, and guessing at a
# remedy from an alert name is how an automated fixer earns its reputation.
#
# The OLM entries come from a real, recurring failure on the reference cluster.
# The operator gets stuck re-running an install strategy for a CSV that never
# leaves 'Installing', which starves its own /healthz, times out its probes and
# takes the packageserver pods with it. Restarting the operator breaks the loop
# and the Deployment recreates it immediately. It has been firing for 30 hours
# with nothing able to act on it.
_ALERT_STRATEGIES: dict[str, tuple[str, str]] = {
    "CsvAbnormalFailedOver2Min": (
        "restart_controller",
        "Restart the OLM operator — it is stuck re-running an install strategy and starving its own probes",
    ),
    "CsvAbnormalOver30Min": (
        "restart_controller",
        "Restart the OLM operator — a CSV has been mid-install for over 30 minutes",
    ),
}


def alert_fix_plan(finding: dict) -> FixPlan | None:
    """A fix plan for a firing alert, where one is known.

    Requires the alert to name a real pod. Alerts that carry no pod label get a
    placeholder ``Alert`` resource instead, and there is nothing to restart.
    """
    strategy_entry = _ALERT_STRATEGIES.get(finding.get("title", ""))
    if not strategy_entry:
        return None
    resources = finding.get("resources") or []
    if not resources or resources[0].get("kind") != "Pod":
        return None

    strategy, description = strategy_entry
    return FixPlan(
        strategy=strategy,
        cause_category="alerts",
        confidence=0.6,
        description=description,
        params={
            "suspected_cause": f"Alert {finding.get('title', '')} firing",
            "recommended_fix": description,
            "resources": resources,
        },
    )


def default_fix_plan(category: str, finding: dict) -> FixPlan | None:
    """Create a default fix plan for known categories without an investigation.

    Used as a fast-path when the async investigation hasn't completed yet
    but the category has a well-known remediation strategy.
    """
    if category == "alerts":
        # Alerts are not one category of problem, so they cannot share one
        # strategy. Dispatch on the alert itself.
        alert_plan = alert_fix_plan(finding)
        return _steer(alert_plan) if alert_plan else None

    entry = _FAST_PATH_STRATEGIES.get(category)
    if not entry:
        return None

    strategy, description = entry
    return _steer(
        gate_by_cause(
            FixPlan(
                strategy=strategy,
                cause_category=category,
                confidence=0.6,  # lower than investigation-backed plans
                description=description,
                params={
                    "suspected_cause": f"Fast-path: {category} detected by scanner",
                    "recommended_fix": description,
                    "resources": finding.get("resources", []),
                },
            ),
            finding,
        )
    )


# ── Can this fix work at all? ────────────────────────────────────────────
#
# Deleting a pod re-pulls the same image reference with the same credentials.
# That fixes a pull that failed because the registry timed out or rate-limited
# us, and nothing else: a wrong tag, a missing repository or a missing pull
# secret fails identically on the new pod. Measured on one cluster: 143 pod
# deletions for one bad image, each recorded "completed". Likewise a rolling
# restart of a Deployment whose pods cannot pull, cannot find their config or
# crash on start recreates the same failure.

_PULL_WAITING_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"})

# Checked first: when both appear (an old timeout, then "not found"), the
# permanent cause wins — acting on the wrong reading repeats a useless write.
_PERMANENT_PULL_PATTERNS = [
    r"not found",
    r"manifest unknown",
    r"unauthori[sz]ed",
    r"authentication required",
    r"denied",
    r"forbidden",
    r"invalidimagename",
    r"invalid reference format",
    r"errimageneverpull",
    r"does not exist",
    r"no such image",
]
_TRANSIENT_PULL_PATTERNS = [
    r"i/o timeout",
    r"timed? ?out",
    r"deadline exceeded",
    r"tls handshake",
    r"toomanyrequests",
    r"too many requests",
    r"rate limit",
    r"\b429\b",
    r"\b50[234]\b",
    r"service unavailable",
    r"bad gateway",
    r"connection reset",
    r"connection refused",
    r"unexpected eof",
    r"temporary failure",
    r"server misbehaving",
]

TRANSIENT = "transient"
PERMANENT = "permanent"
UNKNOWN = "unknown"


def classify_pull_error(text: str) -> str:
    """``transient`` only when the pull error reads as a passing registry problem."""
    lower = (text or "").lower()
    if any(re.search(p, lower) for p in _PERMANENT_PULL_PATTERNS):
        return PERMANENT
    if any(re.search(p, lower) for p in _TRANSIENT_PULL_PATTERNS):
        return TRANSIENT
    return UNKNOWN


def image_pull_cause(resource: dict) -> tuple[str, str]:
    """Why this pod cannot pull: ``(transient|permanent|unknown, evidence)``.

    The waiting message of an ``ImagePullBackOff`` container says only
    "Back-off pulling image"; the reason is in the ``ErrImagePull`` message
    and the pod's ``Failed`` events, so both are read.
    """
    if resource.get("kind") != "Pod" or not resource.get("name"):
        return UNKNOWN, "the finding does not name a pod"
    name, ns = resource["name"], resource.get("namespace", "default")
    texts: list[str] = []
    try:
        core = get_core_client()
        pod = core.read_namespaced_pod(name, ns)
        status = getattr(pod, "status", None)
        statuses = list(getattr(status, "init_container_statuses", None) or []) + list(
            getattr(status, "container_statuses", None) or []
        )
        for cs in statuses:
            waiting = getattr(getattr(cs, "state", None), "waiting", None)
            reason = getattr(waiting, "reason", "") or ""
            if reason in _PULL_WAITING_REASONS:
                texts.append(f"{reason}: {getattr(waiting, 'message', '') or ''}".strip())
        try:
            events = core.list_namespaced_event(ns, field_selector=f"involvedObject.name={name}")
            for ev in (getattr(events, "items", None) or [])[-10:]:
                message = getattr(ev, "message", "") or ""
                if getattr(ev, "reason", "") in ("Failed", "ErrImagePull", "InspectFailed") and message:
                    texts.append(message)
        except Exception:
            logger.debug("Could not read pull events for %s/%s", ns, name, exc_info=True)
    except Exception as e:
        return UNKNOWN, f"could not read the pod's pull error ({type(e).__name__})"
    evidence = " | ".join(t for t in texts if t)
    if not evidence:
        return UNKNOWN, "no pull error message on the pod"
    return classify_pull_error(evidence), evidence[:400]


_UNRESTARTABLE_REASONS = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "CrashLoopBackOff",
        "RunContainerError",
    }
)


def deployment_restart_blocker(resource: dict) -> str:
    """Why a rolling restart cannot help this Deployment, or "" if it might."""
    if resource.get("kind") != "Deployment" or not resource.get("name"):
        return f"a rolling restart applies to Deployments, not {resource.get('kind') or 'this resource'}"
    name, ns = resource["name"], resource.get("namespace", "default")
    try:
        apps = get_apps_client()
        dep = apps.read_namespaced_deployment(name, ns)
        labels = (dep.spec.selector.match_labels or {}) if dep.spec and dep.spec.selector else {}
        if not labels:
            return "the Deployment's pods could not be identified (no matchLabels)"
        pods = get_core_client().list_namespaced_pod(ns, label_selector=",".join(f"{k}={v}" for k, v in labels.items()))
    except Exception as e:
        return f"the Deployment's pods could not be inspected ({type(e).__name__})"
    for pod in getattr(pods, "items", None) or []:
        pod_name = getattr(pod.metadata, "name", "?")
        status = pod.status
        for cs in list(getattr(status, "init_container_statuses", None) or []) + list(
            getattr(status, "container_statuses", None) or []
        ):
            waiting = getattr(getattr(cs, "state", None), "waiting", None)
            reason = getattr(waiting, "reason", "") or ""
            if reason in _UNRESTARTABLE_REASONS:
                message = (getattr(waiting, "message", "") or "")[:200]
                return f"pod {pod_name} is {reason}{': ' + message if message else ''}"
        for cond in getattr(status, "conditions", None) or []:
            if getattr(cond, "type", "") == "PodScheduled" and getattr(cond, "status", "") == "False":
                return f"pod {pod_name} cannot be scheduled: {(getattr(cond, 'message', '') or '')[:200]}"
    return ""


def _to_human_review(plan: FixPlan, why: str) -> FixPlan:
    plan.strategy = "require_human_review"
    plan.description = why
    plan.params["recommended_fix"] = why
    return plan


def gate_by_cause(plan: FixPlan, finding: dict) -> FixPlan:
    """Turn a plan that cannot work for this finding into a request for a person.

    Runs inside planning, so the auto-fix loop and an approval re-deriving the
    plan reach the same strategy.
    """
    if plan.strategy == "require_human_review":
        return plan
    category = finding.get("category", "")
    resources = finding.get("resources") or []
    r0 = resources[0] if resources else {}
    if category == "image_pull":
        verdict, evidence = image_pull_cause(r0)
        plan.params["pull_error"] = evidence
        if verdict == TRANSIENT:
            # A passing registry problem: retrying the pull now is the fix. A
            # rollback would revert a good image over a timeout.
            plan.strategy = "restart_controller"
            plan.description = (
                f"Delete the pod so the pull is retried now — the failure looks transient: {evidence[:200]}"
            )
            plan.params["recommended_fix"] = plan.description
            return plan
        return _to_human_review(
            plan,
            "The image cannot be pulled and retrying will not change that "
            f"({'not a transient registry error' if verdict == PERMANENT else 'cause could not be confirmed as transient'}): "
            f"{evidence[:240]}. Fix the image reference or the pull secret.",
        )
    if plan.strategy == "rollout_restart":
        blocker = deployment_restart_blocker(r0)
        if blocker:
            return _to_human_review(
                plan,
                f"A rolling restart would recreate the same failure: {blocker}. Fix the cause, not the pods.",
            )
    return plan


# The tool each mutating strategy is equivalent to, for deny-policy purposes.
# The auto-fix path does not go through agent._execute_tool — it dispatches
# here and calls the K8s API directly — so without this mapping the harness
# deny policy would cover the supervised chat path while leaving the
# *unsupervised* one wide open, which is the wrong way round.
_STRATEGY_POLICY_TOOLS: dict[str, str] = {
    "restart_controller": "delete_pod",
    "rollout_restart": "restart_deployment",
    "patch_image": "restart_deployment",
    "patch_resources": "restart_deployment",
}


class FixPreconditionError(ValueError):
    """A proven refusal before any mutation was attempted."""


def execute_fix(plan: FixPlan) -> tuple[str, str, str]:
    """Execute a targeted fix plan. Returns (tool_name, before_state, after_state).

    Raises ValueError for unknown strategies. Returns a ``blocked`` result when
    the harness deny policy refuses the equivalent tool.
    """
    _last_snapshot.set(None)
    _verify_resources.set([])
    _expected_deployment_uid.set(None)
    executor = _EXECUTORS.get(plan.strategy)
    if not executor:
        raise ValueError(f"No executor for strategy: {plan.strategy}")

    policy_tool = _STRATEGY_POLICY_TOOLS.get(plan.strategy)
    if policy_tool:
        from ..policy import check_write_policy

        resource, namespace = _get_first_resource(plan)
        denied = check_write_policy(policy_tool, {"namespace": namespace, "name": resource.get("name", "")})
        if denied is not None:
            logger.warning(
                "Auto-fix blocked by deny policy: strategy=%s tool=%s namespace=%s",
                plan.strategy,
                policy_tool,
                namespace,
            )
            return (
                "blocked",
                f"{policy_tool} in {namespace}",
                f"Blocked by policy — {denied.message}",
            )

    logger.info(
        "Intelligent fix: strategy=%s cause=%s confidence=%.2f",
        plan.strategy,
        plan.cause_category,
        plan.confidence,
    )
    result = executor(plan)
    # The snapshot the executor captured travels with the result via
    # take_last_snapshot(); callers that persist the action read it there.
    return result


# Worker/context-local so concurrent actions never borrow compensation data.
_last_snapshot: ContextVar[dict | None] = ContextVar("fix_snapshot", default=None)
_expected_deployment_uid: ContextVar[str | None] = ContextVar("fix_deployment_uid", default=None)
_verify_resources: ContextVar[list[dict] | None] = ContextVar("fix_verify_resources", default=None)


_APPLIED_TOOLS = frozenset({"delete_pod", "rollback_deployment", "patch_resources", "rollout_restart"})


@dataclass(frozen=True)
class FixExecution:
    result: tuple[str, str, str]
    snapshot: dict | None
    verify_resources: list[dict]

    @property
    def applied(self) -> bool:
        return self.result[0] in _APPLIED_TOOLS


def execute_fix_with_snapshot(plan: FixPlan) -> FixExecution:
    """Consume snapshot in the same worker as execution, preserving triple API."""
    token = _last_snapshot.set(None)
    target_token = _verify_resources.set([])
    uid_token = _expected_deployment_uid.set(None)
    try:
        result = execute_fix(plan)
        snapshot = take_last_snapshot()
        if snapshot is not None and result[0] in _APPLIED_TOOLS:
            from ..snapshot import mark_applied

            snapshot = mark_applied(snapshot)
        return FixExecution(result, snapshot, list(_verify_resources.get() or []))
    finally:
        _last_snapshot.reset(token)
        _verify_resources.reset(target_token)
        _expected_deployment_uid.reset(uid_token)


def take_last_snapshot() -> dict | None:
    """Return and clear only this worker's last snapshot."""
    snap = _last_snapshot.get()
    _last_snapshot.set(None)
    return snap


def _snapshot_before(kind: str, name: str, namespace: str, observed=None) -> None:
    """Missing undo is explicit; a mismatched snapshot refuses the mutation."""
    snapshot = None
    _verify_resources.set([{"kind": kind, "name": name, "namespace": namespace}])
    try:
        from ..snapshot import capture

        snapshot = capture(kind, name, namespace)
        _last_snapshot.set(snapshot)
        _verify_resources.set([{"kind": kind, "name": name, "namespace": namespace}])
        if snapshot is None:
            logger.warning("No snapshot for %s %s/%s — this fix will not be undoable", kind, namespace, name)
    except Exception:
        logger.warning("Snapshot failed for %s %s/%s", kind, namespace, name, exc_info=True)
        _last_snapshot.set(None)

    if observed is not None and snapshot is not None:
        uid, version = _identity(observed)
        if snapshot.get("uid") != uid or snapshot.get("resourceVersion") != version:
            _last_snapshot.set(None)
            raise FixPreconditionError("Target changed while snapshot was captured; re-investigate before mutation")


def _identity(obj) -> tuple[str, str]:
    metadata = getattr(obj, "metadata", None)
    uid = getattr(metadata, "uid", None)
    version = getattr(metadata, "resource_version", None)
    if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
        raise FixPreconditionError(
            "Target UID/resourceVersion missing; refusing mutation without identity preconditions"
        )
    return uid, version


def _patch_tests(obj) -> list[dict]:
    uid, version = _identity(obj)
    return [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": version},
    ]


def _delete_observed_pod(pod, name: str, namespace: str, *, force=False) -> str | None:
    from kubernetes.client import V1DeleteOptions, V1Preconditions

    from ..policy import check_write_policy

    denied = check_write_policy("delete_pod", {"namespace": namespace, "pod_name": name})
    if denied is not None:
        return f"blocked: {denied.message}"
    target = _controller_target(pod, namespace)
    if target is None:
        return "skip: pod has no supported controlling owner; recreation cannot be confirmed"
    uid, version = _identity(pod)
    _verify_resources.set([target])
    # Pod deletion has no restorable spec. Never attach a snapshot of an unmodified owner.
    _last_snapshot.set(None)
    options = V1DeleteOptions(preconditions=V1Preconditions(uid=uid, resource_version=version))
    get_core_client().delete_namespaced_pod(
        name, namespace, body=options, **({"grace_period_seconds": 0} if force else {})
    )
    return None


def _get_first_resource(plan: FixPlan) -> tuple[dict, str]:
    """Extract the first resource and namespace from a fix plan."""
    resources = plan.params.get("resources", [])
    if not resources:
        raise ValueError("No resources in fix plan")
    r = resources[0]
    return r, r.get("namespace", "default")


def _controller_target(pod, namespace: str) -> dict | None:
    owners = pod.metadata.owner_references or []
    owner = next(
        (
            ref
            for ref in owners
            if getattr(ref, "controller", None) is True and ref.kind in {"ReplicaSet", "StatefulSet", "DaemonSet"}
        ),
        None,
    )
    if owner is None:
        return None
    apps = get_apps_client()
    reader = {
        "ReplicaSet": apps.read_namespaced_replica_set,
        "StatefulSet": apps.read_namespaced_stateful_set,
        "DaemonSet": apps.read_namespaced_daemon_set,
    }[owner.kind]
    obj = reader(owner.name, namespace)
    expected_uid = getattr(owner, "uid", None)
    if not isinstance(expected_uid, str) or not expected_uid or getattr(obj.metadata, "uid", None) != expected_uid:
        raise FixPreconditionError("Controlling owner identity changed; re-investigate before mutation")
    target = {"kind": owner.kind, "name": owner.name, "namespace": namespace}
    if owner.kind == "ReplicaSet":
        dep_ref = next(
            (
                ref
                for ref in (obj.metadata.owner_references or [])
                if getattr(ref, "controller", None) is True and ref.kind == "Deployment"
            ),
            None,
        )
        if dep_ref:
            dep = apps.read_namespaced_deployment(dep_ref.name, namespace)
            expected_dep_uid = getattr(dep_ref, "uid", None)
            if (
                not isinstance(expected_dep_uid, str)
                or not expected_dep_uid
                or getattr(dep.metadata, "uid", None) != expected_dep_uid
            ):
                raise FixPreconditionError("Deployment owner identity changed; re-investigate before mutation")
            _expected_deployment_uid.set(expected_dep_uid)
            target = {"kind": "Deployment", "name": dep_ref.name, "namespace": namespace}
    return target


def _find_owning_deployment(pod_name: str, ns: str, pod=None) -> str | None:
    pod = pod if pod is not None else get_core_client().read_namespaced_pod(pod_name, ns)
    target = _controller_target(pod, ns)
    return target["name"] if target and target["kind"] == "Deployment" else None


def _check_deployment_identity(dep) -> None:
    uid, _version = _identity(dep)
    expected = _expected_deployment_uid.get()
    if expected is not None and uid != expected:
        raise FixPreconditionError("Deployment was recreated after owner resolution; mutation refused")


def _execute_patch_image(plan: FixPlan) -> tuple[str, str, str]:
    """Fix bad image by rolling back to the previous deployment revision."""
    r, ns = _get_first_resource(plan)
    core = get_core_client()
    apps = get_apps_client()

    pod = core.read_namespaced_pod(r["name"], ns)
    bad_image = pod.spec.containers[0].image if pod.spec.containers else "unknown"

    dep_name = _find_owning_deployment(r["name"], ns, pod)

    if not dep_name:
        # Deleting the pod is not a fallback for a bad image: the replacement
        # pulls the same reference and fails the same way.
        return (
            "skip",
            f"Pod {r['name']} in {ns}: image={bad_image}",
            "Not changed: no owning Deployment to roll back, and deleting the pod would pull the same image — "
            "needs a person.",
        )

    dep = apps.read_namespaced_deployment(dep_name, ns)
    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
    _check_deployment_identity(dep)
    _snapshot_before("Deployment", dep_name, ns, dep)
    before = f"Deployment {dep_name} in {ns}: image={bad_image}, revision={revision}"

    # Find previous revision's ReplicaSet
    rollback_revision = max(int(revision) - 1, 0)
    rs_list = apps.list_namespaced_replica_set(
        ns, label_selector=",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
    )

    for rs in rs_list.items:
        rs_rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "")
        if rs_rev == str(rollback_revision) and rs.spec.template.spec.containers:
            good_image = rs.spec.template.spec.containers[0].image
            container_name = rs.spec.template.spec.containers[0].name
            index = next(
                (
                    i
                    for i, container in enumerate(dep.spec.template.spec.containers)
                    if container.name == container_name
                ),
                None,
            )
            if index is None:
                raise FixPreconditionError(
                    "Rollback container is not present in current deployment; human review required"
                )
            body = _patch_tests(dep) + [
                {"op": "replace", "path": f"/spec/template/spec/containers/{index}/image", "value": good_image}
            ]
            apps.patch_namespaced_deployment(dep_name, ns, body=body, _content_type="application/json-patch+json")
            return (
                "rollback_deployment",
                before,
                f"Deployment {dep_name} patched: image={good_image} (rolled back from rev {revision})",
            )

    # No previous revision to return to. Deleting the pod used to be the
    # fallback here and was recorded "completed" every time, though the new pod
    # pulls the same image and fails the same way.
    return (
        "skip",
        before,
        "Not changed: no previous revision to roll back to, and deleting the pod would pull the same image — "
        "needs a person.",
    )


def _execute_patch_resources(plan: FixPlan) -> tuple[str, str, str]:
    """Fix OOM by doubling the memory limit on the deployment."""
    r, ns = _get_first_resource(plan)
    name = r.get("name", "")
    kind = r.get("kind", "")
    apps = get_apps_client()

    # If resource is a Pod, find the owning Deployment, and which of its
    # containers was the one OOM-killed.
    oom_container = None
    if kind == "Pod":
        oom_container = _oom_killed_container(name, ns)
        dep_name = _find_owning_deployment(name, ns)
        if dep_name:
            name = dep_name
            kind = "Deployment"

    if kind != "Deployment":
        raise ValueError(f"Cannot patch resources on {kind}/{name} — only Deployments supported")

    dep = apps.read_namespaced_deployment(name, ns)
    containers = dep.spec.template.spec.containers
    if oom_container is not None:
        matches = [i for i, c in enumerate(containers) if c.name == oom_container]
    else:
        # Without a record of which container was killed, only an unambiguous
        # single-container pod is safe to patch: the first container is often
        # not the one that ran out (a sidecar, or the app behind one).
        matches = [0] if len(containers) == 1 else []
    if not matches:
        return (
            "skip",
            f"Deployment {name} in {ns}",
            "Not changed: could not tell which container ran out of memory — needs a person to choose.",
        )
    index = matches[0]
    container = containers[index]

    limits_now = (container.resources.limits or {}) if container.resources else {}
    if "memory" not in limits_now:
        # It used to assume 256Mi here and "double" it to 512Mi — which, for a
        # container with no limit using 2Gi, imposes a limit far below its use.
        return (
            "skip",
            f"Deployment {name} in {ns}: container {container.name} has no memory limit",
            "Not changed: with no existing limit there is nothing safe to double — needs a person to size it.",
        )
    current_limit = limits_now["memory"]

    from ..units import parse_memory_bytes

    current_bytes = parse_memory_bytes(current_limit)
    new_bytes = current_bytes * 2
    max_bytes = parse_memory_bytes("8Gi")
    if new_bytes > max_bytes:
        raise ValueError(
            f"Cannot auto-fix OOM on {kind}/{name}: doubling {current_limit} would exceed 8Gi cap. "
            "Route to human review."
        )
    new_limit = f"{new_bytes // (1024 * 1024)}Mi"

    before = f"Deployment {name} in {ns}: memory limit={current_limit}"

    _check_deployment_identity(dep)
    _snapshot_before("Deployment", name, ns, dep)
    limits = dict(container.resources.limits or {}) if container.resources else {}
    limits["memory"] = new_limit
    operation = {
        "op": "add",
        "path": f"/spec/template/spec/containers/{index}/resources/limits",
        "value": limits,
    }
    body = _patch_tests(dep) + [operation]
    apps.patch_namespaced_deployment(name, ns, body=body, _content_type="application/json-patch+json")

    return ("patch_resources", before, f"Deployment {name} patched: memory limit {current_limit} -> {new_limit}")


def _oom_killed_container(pod_name: str, namespace: str) -> str | None:
    """The name of the container in this pod whose last termination was OOMKilled."""
    try:
        pod = get_core_client().read_namespaced_pod(pod_name, namespace)
    except Exception:
        return None
    for status in (pod.status.container_statuses or []) if pod.status else []:
        for state in (status.last_state, status.state):
            terminated = getattr(state, "terminated", None) if state else None
            if terminated is not None and getattr(terminated, "reason", "") == "OOMKilled":
                return status.name
    return None


def _execute_noop(plan: FixPlan) -> tuple[str, str, str]:
    """Strategies that can't be auto-fixed yet."""
    return ("skip", "", f"Strategy {plan.strategy} requires manual intervention: {plan.description}")


def _execute_require_human_review(plan: FixPlan) -> tuple[str, str, str]:
    """Route to Approvals queue — restart won't fix this, human needs to decide."""
    r, ns = _get_first_resource(plan)
    cause = plan.params.get("suspected_cause", "Unknown root cause")
    fix = plan.params.get("recommended_fix", "Review and fix manually")
    return (
        "require_human_review",
        f"{r.get('kind', 'Resource')} {r.get('name', '?')} in {ns}: {cause[:100]}",
        f"Manual fix required: {fix[:200]}",
    )


def _execute_restart_controller(plan: FixPlan) -> tuple[str, str, str]:
    """Delete the pod — its controller (Deployment/RS/SS) recreates it."""
    r, ns = _get_first_resource(plan)
    core = get_core_client()

    pod_name = r.get("name", "")
    pod = core.read_namespaced_pod(pod_name, ns)

    # Safety: only delete pods with ownerReferences (controller-managed)
    if not pod.metadata.owner_references:
        return (
            "skip",
            f"Pod {pod_name} in {ns}: no ownerReferences",
            "Skipped — bare pod without controller (would not be recreated)",
        )

    before = f"Pod {pod_name} in {ns}: phase={pod.status.phase}, restarts={pod.status.container_statuses[0].restart_count if pod.status.container_statuses else 0}"
    # Normal grace period. A crash-looping container is restarting anyway, so
    # skipping graceful shutdown buys nothing — and grace 0 lets a replacement
    # start before the old process is gone (two writers on one volume, or two
    # StatefulSet pods with one identity).
    refused = _delete_observed_pod(pod, pod_name, ns)
    if refused:
        return (refused.split(":", 1)[0], before, refused)

    return (
        "delete_pod",
        before,
        f"Pod {pod_name} deleted — controller will recreate",
    )


def _execute_rollout_restart(plan: FixPlan) -> tuple[str, str, str]:
    """Rolling restart of a Deployment (what ``oc rollout restart`` does)."""
    r, ns = _get_first_resource(plan)
    name = r.get("name", "")
    if r.get("kind") != "Deployment" or not name:
        raise FixPreconditionError(f"rollout_restart needs a Deployment, got {r.get('kind') or 'nothing'}")
    apps = get_apps_client()
    dep = apps.read_namespaced_deployment(name, ns)
    desired = dep.spec.replicas if dep.spec and dep.spec.replicas is not None else 0
    available = (dep.status.available_replicas or 0) if dep.status else 0
    before = f"Deployment {name} in {ns}: available={available}/{desired}"
    _check_deployment_identity(dep)
    _snapshot_before("Deployment", name, ns, dep)
    from datetime import UTC, datetime

    template_meta = dep.spec.template.metadata
    annotations = dict((template_meta.annotations or {}) if template_meta else {})
    annotations["kubectl.kubernetes.io/restartedAt"] = datetime.now(UTC).isoformat()
    if template_meta is None:
        op = {"op": "add", "path": "/spec/template/metadata", "value": {"annotations": annotations}}
    else:
        op = {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations}
    apps.patch_namespaced_deployment(
        name, ns, body=_patch_tests(dep) + [op], _content_type="application/json-patch+json"
    )
    return ("rollout_restart", before, f"Deployment {name} rolling restart triggered")


_EXECUTORS: dict[str, Callable] = {
    "rollout_restart": _execute_rollout_restart,
    "patch_image": _execute_patch_image,
    "patch_resources": _execute_patch_resources,
    "restart_controller": _execute_restart_controller,
    "create_configmap": _execute_noop,
    "patch_probe": _execute_noop,
    "suggest_quota_increase": _execute_noop,
    "require_human_review": _execute_require_human_review,
}
