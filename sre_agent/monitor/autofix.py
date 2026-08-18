"""Auto-fix handlers for autonomous remediation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC

from ..k8s_client import get_apps_client, get_core_client

logger = logging.getLogger("pulse_agent.monitor")

# ── Auto-fix kill switch ───────────────────────────────────────────────────
#
# Backed by the operational_flags table rather than a module global. A global
# had two failure modes that both silently re-armed autonomous remediation:
# it reset to "not paused" on every pod restart (an OOMKill or reschedule
# during an incident would quietly resume deleting pods), and it was
# process-local, so pausing one replica left the others acting.
#
# Read on every auto_fix() call — once per scan cycle, so the query cost is
# irrelevant next to what it guards.

_AUTOFIX_PAUSED_KEY = "autofix_paused"


def set_autofix_paused(paused: bool, updated_by: str = "") -> None:
    """Persist the auto-fix pause flag. Raises if it cannot be stored.

    Deliberately not best-effort: an operator who hits pause and gets a success
    response must not be left with remediation still running.
    """
    from ..db import get_database

    get_database().execute(
        "INSERT INTO operational_flags (key, value, updated_at, updated_by) "
        "VALUES (%s, %s, NOW(), %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by",
        (_AUTOFIX_PAUSED_KEY, paused, updated_by or None),
    )
    get_database().commit()
    logger.info("Auto-fix %s (persisted)", "paused" if paused else "resumed")


def is_autofix_paused() -> bool:
    """Return the persisted pause state, defaulting to PAUSED on any read error.

    Fails closed on purpose. If the flag cannot be read we cannot know whether
    an operator has halted remediation, and continuing to delete pods and patch
    workloads on that assumption is the more damaging of the two mistakes.
    """
    from ..db import get_database

    try:
        row = get_database().fetchone("SELECT value FROM operational_flags WHERE key = %s", (_AUTOFIX_PAUSED_KEY,))
    except Exception:
        logger.exception("Could not read the auto-fix pause flag — treating auto-fix as PAUSED")
        return True
    if row is None:
        return False
    return bool(row["value"])


# ── Auto-fix functions ────────────────────────────────────────────────────


def _fix_crashloop(finding: dict) -> tuple[str, str, str]:
    """Delete crashlooping pod. Returns (tool, before_state, after_state) or raises."""
    resources = finding.get("resources", [])
    if not resources:
        raise ValueError("No resources to fix")
    r = resources[0]
    core = get_core_client()
    # Get current state
    pod = core.read_namespaced_pod(r["name"], r["namespace"])
    restart_count = 0
    if pod.status.container_statuses:
        restart_count = pod.status.container_statuses[0].restart_count
    before = f"Pod {r['name']} in {r['namespace']}: restarts={restart_count}"
    # Delete it — controller will recreate
    core.delete_namespaced_pod(r["name"], r["namespace"])
    return ("delete_pod", before, f"Pod {r['name']} deleted — controller will recreate")


def _fix_workloads(finding: dict) -> tuple[str, str, str]:
    """Restart a failed deployment. Returns (tool, before_state, after_state) or raises."""
    resources = finding.get("resources", [])
    if not resources:
        raise ValueError("No resources to fix")
    r = resources[0]
    apps = get_apps_client()
    # Get current state
    dep = apps.read_namespaced_deployment(r["name"], r["namespace"])
    desired = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0
    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "")
    before = f"Deployment {r['name']} in {r['namespace']}: revision={revision}, available={available}/{desired}"
    # Trigger rolling restart
    from datetime import datetime as _dt

    now = _dt.now(UTC).isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    apps.patch_namespaced_deployment(r["name"], r["namespace"], body=body)
    # Stash rollback metadata on the finding so save_action can persist it
    finding["_rollback_meta"] = {
        "name": r["name"],
        "namespace": r["namespace"],
        "revision": revision,
    }
    return ("restart_deployment", before, f"Deployment {r['name']} rolling restart triggered")


def _fix_image_pull(finding: dict) -> tuple[str, str, str]:
    """Restart deployment/statefulset/daemonset for ImagePullBackOff pods — clears the backoff timer."""
    resources = finding.get("resources", [])
    if not resources:
        raise ValueError("No resources to fix")
    r = resources[0]
    ns = r.get("namespace", "default")
    core = get_core_client()
    pod = core.read_namespaced_pod(r["name"], ns)
    before = f"Pod {r['name']} in {ns}: ImagePullBackOff"

    # Check for bare pod before attempting any fix
    if not pod.metadata.owner_references:
        return ("skip", "", "Skipped: bare pod with no controller — deletion would be permanent")

    # Find the owning controller via ownerReferences
    owner_refs = pod.metadata.owner_references or []
    for ref in owner_refs:
        if ref.kind == "ReplicaSet":
            # ReplicaSet -> find parent Deployment
            apps = get_apps_client()
            rs = apps.read_namespaced_replica_set(ref.name, ns)
            for rs_ref in rs.metadata.owner_references or []:
                if rs_ref.kind == "Deployment":
                    from datetime import datetime as _dt

                    now = _dt.now(UTC).isoformat()
                    body = {
                        "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}
                    }
                    apps.patch_namespaced_deployment(rs_ref.name, ns, body=body)
                    dep = apps.read_namespaced_deployment(rs_ref.name, ns)
                    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "")
                    finding["_rollback_meta"] = {"name": rs_ref.name, "namespace": ns, "revision": revision}
                    return ("restart_deployment", before, f"Deployment {rs_ref.name} rolling restart triggered")

        elif ref.kind == "StatefulSet":
            apps = get_apps_client()
            from datetime import datetime as _dt

            now = _dt.now(UTC).isoformat()
            body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
            apps.patch_namespaced_stateful_set(ref.name, ns, body=body)
            return ("restart_statefulset", before, f"StatefulSet {ref.name} rolling restart triggered")

        elif ref.kind == "DaemonSet":
            apps = get_apps_client()
            from datetime import datetime as _dt

            now = _dt.now(UTC).isoformat()
            body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
            apps.patch_namespaced_daemon_set(ref.name, ns, body=body)
            return ("restart_daemonset", before, f"DaemonSet {ref.name} rolling restart triggered")

        elif ref.kind == "Job":
            return ("skip", "", "Skipped: Job-owned pod — restart won't help")

    # Fallback: delete the pod directly (has owner but not a recognized type)
    core.delete_namespaced_pod(r["name"], ns)
    return ("delete_pod", before, f"Pod {r['name']} deleted — controller will recreate")


AUTO_FIX_HANDLERS: dict[str, Callable] = {
    "crashloop": _fix_crashloop,
    "workloads": _fix_workloads,
    "image_pull": _fix_image_pull,
}
