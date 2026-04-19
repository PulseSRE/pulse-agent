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
from dataclasses import dataclass

from ..k8s_client import get_apps_client, get_core_client

logger = logging.getLogger("pulse_agent.monitor")


def _get_db():
    from ..db import get_database

    return get_database()


def get_investigation_for_finding(finding_id: str) -> dict | None:
    """Look up the latest completed investigation for a finding."""
    try:
        db = _get_db()
        return db.fetchone(
            "SELECT suspected_cause, recommended_fix, confidence "
            "FROM investigations "
            "WHERE finding_id = %s AND status = 'completed' "
            "ORDER BY timestamp DESC LIMIT 1",
            (finding_id,),
        )
    except Exception:
        logger.debug("Failed to look up investigation for %s", finding_id, exc_info=True)
        return None


# Root cause categories with keyword patterns
_CAUSE_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "bad_image",
        [
            "image",
            "does not exist",
            "not found in registry",
            "imagepullbackoff",
            "pull access denied",
            "manifest unknown",
        ],
    ),
    ("missing_config", ["configmap", "not found", "missing", "secret.*not found"]),
    ("oom", ["oom", "out of memory", "memory limit", "oomkilled", "exceeded memory"]),
    ("probe_failure", ["readiness probe", "liveness probe", "probe failed", "connection refused"]),
    ("quota_exceeded", ["quota", "exceeded", "forbidden", "limit reached"]),
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
    ("dependency", ["connection refused", "connection timed out", "no such host", "dns", "service unavailable"]),
]


def classify_root_cause(suspected_cause: str) -> str:
    """Classify a suspected cause string into a root cause category."""
    if not suspected_cause:
        return "unknown"

    lower = suspected_cause.lower()
    for category, patterns in _CAUSE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lower):
                return category

    return "unknown"


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
    suspected_cause = investigation.get("suspectedCause", "") or investigation.get("suspected_cause", "")
    recommended_fix = investigation.get("recommendedFix", "") or investigation.get("recommended_fix", "")
    confidence = float(investigation.get("confidence", 0))

    if confidence < _MIN_TARGETED_CONFIDENCE:
        return None

    cause_category = classify_root_cause(suspected_cause)
    strategy = _STRATEGY_MAP.get(cause_category)

    if not strategy:
        return None

    return FixPlan(
        strategy=strategy,
        cause_category=cause_category,
        confidence=confidence,
        description=f"{strategy}: {recommended_fix[:200]}",
        params={
            "suspected_cause": suspected_cause,
            "recommended_fix": recommended_fix,
            "resources": finding.get("resources", []),
        },
    )


# Categories where a default fix strategy is known without investigation.
# Allows auto-fix to act immediately instead of waiting for async LLM diagnosis.
_FAST_PATH_STRATEGIES: dict[str, tuple[str, str]] = {
    "crashloop": ("restart_controller", "Delete crashlooping pod — controller recreates"),
    "image_pull": ("patch_image", "Rollback deployment to previous working image"),
    "workloads": ("restart_controller", "Rolling restart of failed deployment"),
}


def default_fix_plan(category: str, finding: dict) -> FixPlan | None:
    """Create a default fix plan for known categories without an investigation.

    Used as a fast-path when the async investigation hasn't completed yet
    but the category has a well-known remediation strategy.
    """
    entry = _FAST_PATH_STRATEGIES.get(category)
    if not entry:
        return None

    strategy, description = entry
    return FixPlan(
        strategy=strategy,
        cause_category=category,
        confidence=0.6,  # lower than investigation-backed plans
        description=description,
        params={
            "suspected_cause": f"Fast-path: {category} detected by scanner",
            "recommended_fix": description,
            "resources": finding.get("resources", []),
        },
    )


def execute_fix(plan: FixPlan) -> tuple[str, str, str]:
    """Execute a targeted fix plan. Returns (tool_name, before_state, after_state).

    Raises ValueError for unknown strategies.
    """
    executor = _EXECUTORS.get(plan.strategy)
    if not executor:
        raise ValueError(f"No executor for strategy: {plan.strategy}")

    logger.info(
        "Intelligent fix: strategy=%s cause=%s confidence=%.2f",
        plan.strategy,
        plan.cause_category,
        plan.confidence,
    )
    return executor(plan)


def _get_first_resource(plan: FixPlan) -> tuple[dict, str]:
    """Extract the first resource and namespace from a fix plan."""
    resources = plan.params.get("resources", [])
    if not resources:
        raise ValueError("No resources in fix plan")
    r = resources[0]
    return r, r.get("namespace", "default")


def _find_owning_deployment(pod_name: str, ns: str) -> str | None:
    """Walk Pod → ReplicaSet → Deployment owner chain. Returns deployment name or None."""
    core = get_core_client()
    apps = get_apps_client()
    pod = core.read_namespaced_pod(pod_name, ns)
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            rs = apps.read_namespaced_replica_set(ref.name, ns)
            for rs_ref in rs.metadata.owner_references or []:
                if rs_ref.kind == "Deployment":
                    return rs_ref.name
    return None


def _execute_patch_image(plan: FixPlan) -> tuple[str, str, str]:
    """Fix bad image by rolling back to the previous deployment revision."""
    r, ns = _get_first_resource(plan)
    core = get_core_client()
    apps = get_apps_client()

    pod = core.read_namespaced_pod(r["name"], ns)
    bad_image = pod.spec.containers[0].image if pod.spec.containers else "unknown"

    dep_name = _find_owning_deployment(r["name"], ns)

    if not dep_name:
        # Fallback: delete the pod
        core.delete_namespaced_pod(r["name"], ns)
        return (
            "delete_pod",
            f"Pod {r['name']} in {ns}: image={bad_image}",
            f"Pod {r['name']} deleted — could not find owning Deployment",
        )

    dep = apps.read_namespaced_deployment(dep_name, ns)
    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
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
            body = {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": good_image}]}}}}
            apps.patch_namespaced_deployment(dep_name, ns, body=body)
            return (
                "rollback_deployment",
                before,
                f"Deployment {dep_name} patched: image={good_image} (rolled back from rev {revision})",
            )

    # Fallback: delete pod if previous revision not found
    core.delete_namespaced_pod(r["name"], ns)
    return ("delete_pod", before, f"Pod {r['name']} deleted — previous revision not found")


def _execute_patch_resources(plan: FixPlan) -> tuple[str, str, str]:
    """Fix OOM by doubling the memory limit on the deployment."""
    r, ns = _get_first_resource(plan)
    name = r.get("name", "")
    kind = r.get("kind", "")
    apps = get_apps_client()

    # If resource is a Pod, find the owning Deployment
    if kind == "Pod":
        dep_name = _find_owning_deployment(name, ns)
        if dep_name:
            name = dep_name
            kind = "Deployment"

    if kind != "Deployment":
        raise ValueError(f"Cannot patch resources on {kind}/{name} — only Deployments supported")

    dep = apps.read_namespaced_deployment(name, ns)
    container = dep.spec.template.spec.containers[0]

    current_limit = "256Mi"
    if container.resources and container.resources.limits:
        current_limit = container.resources.limits.get("memory", "256Mi")

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

    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container.name,
                            "resources": {"limits": {"memory": new_limit}},
                        }
                    ]
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name, ns, body=body)

    return ("patch_resources", before, f"Deployment {name} patched: memory limit {current_limit} -> {new_limit}")


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
    core.delete_namespaced_pod(pod_name, ns, grace_period_seconds=0)

    return (
        "delete_pod",
        before,
        f"Pod {pod_name} deleted — controller will recreate",
    )


_EXECUTORS: dict[str, Callable] = {
    "patch_image": _execute_patch_image,
    "patch_resources": _execute_patch_resources,
    "restart_controller": _execute_restart_controller,
    "create_configmap": _execute_noop,
    "patch_probe": _execute_noop,
    "suggest_quota_increase": _execute_noop,
    "require_human_review": _execute_require_human_review,
}
