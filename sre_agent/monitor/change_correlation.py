"""What changed before an incident, read from the cluster and ranked by relevance.

``episodes.changes_around`` answered "what changed" from inbox rows filed by the
audit scanners. That only sees changes those scanners happened to file, in the
namespaces they watch, and lists them by time — a ConfigMap edit in an unrelated
namespace ranked level with the rollout of the very Deployment that broke.

This reads the cluster itself for the window before an episode's onset:

    Deployment rollouts         new ReplicaSet revisions
    StatefulSet / DaemonSet     new ControllerRevisions
    ConfigMap edits             managedFields timestamps
    Argo CD syncs               Application operationState.finishedAt
    Cluster upgrades            ClusterVersion history

and ranks each change by how close it sits to what broke: the same workload,
a ConfigMap an affected workload mounts, the same namespace, or the cluster as
a whole. Still no claim of causation — the order says where to look first.

Rollouts carry a ``rollback`` suggestion (the revision before), which the UI
offers as an action routed through the agent's confirmation gate.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..errors import ToolError
from ..k8s_client import get_apps_client, get_core_client, get_custom_client, safe
from .confidence import _strip_pod_hash

logger = logging.getLogger("pulse_agent.monitor")

LOOKBACK_SECONDS = 60 * 60
MAX_CHANGES = 15
_MAX_NAMESPACES = 10

RELEVANCE = {"same workload": 3.0, "used by an affected workload": 3.0, "same namespace": 2.0, "cluster-wide": 1.0}


def _epoch(ts: Any) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return int(ts.timestamp())
    except (AttributeError, OSError, ValueError):
        return None


def affected_scope(correlation_keys: list[str], namespaces: list[str]) -> tuple[set[str], set[tuple[str, str]]]:
    """Namespaces and (namespace, workload) pairs an episode touches.

    Correlation keys are ``category:namespace:Kind/name``.
    """
    ns_set = {n for n in namespaces if n}
    workloads: set[tuple[str, str]] = set()
    for key in correlation_keys:
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        _, ns, ref = parts
        if ns:
            ns_set.add(ns)
        if "/" in ref and ns:
            kind, name = ref.split("/", 1)
            workloads.add((ns, _strip_pod_hash(name) if kind == "Pod" else name))
    return ns_set, workloads


def _owner_name(obj: Any, kind: str) -> str | None:
    for ref in obj.metadata.owner_references or []:
        if ref.kind == kind:
            return ref.name
    return None


def _image(template: Any) -> str:
    try:
        return ", ".join(c.image for c in template.spec.containers)
    except AttributeError:
        return ""


def _deployment_rollouts(ns: str, start: int, onset: int) -> list[dict]:
    rs_list = safe(lambda: get_apps_client().list_namespaced_replica_set(ns))
    if isinstance(rs_list, ToolError):
        return []
    by_deploy: dict[str, list[Any]] = {}
    for rs in rs_list.items:
        owner = _owner_name(rs, "Deployment")
        if owner:
            by_deploy.setdefault(owner, []).append(rs)
    out = []
    for name, sets in by_deploy.items():
        sets.sort(key=lambda r: int((r.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")))
        for i, rs in enumerate(sets):
            at = _epoch(rs.metadata.creation_timestamp)
            if at is None or not (start <= at <= onset):
                continue
            rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
            # The previous ReplicaSet may already be pruned (revisionHistoryLimit);
            # the rollout still happened, it just cannot say what it replaced.
            prev = sets[i - 1] if i > 0 else None
            if prev is None and rev == "1":
                title = f"Deployment {name} created"
            else:
                before = _image(prev.spec.template) if prev is not None else ""
                after = _image(rs.spec.template)
                detail = f" ({before} → {after})" if before and after and before != after else ""
                title = f"Deployment {name} rolled out revision {rev}{detail}"
            change = {
                "category": "rollout",
                "kind": "Deployment",
                "name": name,
                "namespace": ns,
                "title": title,
                "at": at,
            }
            prev_rev = (prev.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "") if prev else ""
            if prev_rev.isdigit():
                change["rollback"] = {
                    "tool": "rollback_deployment",
                    "namespace": ns,
                    "name": name,
                    "revision": int(prev_rev),
                }
            out.append(change)
    return out


def _controller_revisions(ns: str, start: int, onset: int) -> list[dict]:
    revs = safe(lambda: get_apps_client().list_namespaced_controller_revision(ns))
    if isinstance(revs, ToolError):
        return []
    out = []
    for rev in revs.items:
        at = _epoch(rev.metadata.creation_timestamp)
        if at is None or not (start <= at <= onset) or (rev.revision or 0) <= 1:
            continue
        for kind in ("StatefulSet", "DaemonSet"):
            owner = _owner_name(rev, kind)
            if owner:
                out.append(
                    {
                        "category": "rollout",
                        "kind": kind,
                        "name": owner,
                        "namespace": ns,
                        "title": f"{kind} {owner} rolled out revision {rev.revision}",
                        "at": at,
                    }
                )
    return out


def _last_edit(obj: Any) -> tuple[int | None, str]:
    latest, manager = None, ""
    for mf in obj.metadata.managed_fields or []:
        t = _epoch(getattr(mf, "time", None))
        if t is not None and (latest is None or t > latest):
            latest, manager = t, mf.manager or ""
    return latest, manager


def _configmap_edits(ns: str, start: int, onset: int) -> list[dict]:
    cms = safe(lambda: get_core_client().list_namespaced_config_map(ns))
    if isinstance(cms, ToolError):
        return []
    out = []
    for cm in cms.items:
        name = cm.metadata.name
        if name in ("kube-root-ca.crt", "openshift-service-ca.crt"):
            continue
        at, manager = _last_edit(cm)
        created = _epoch(cm.metadata.creation_timestamp)
        if at is None or not (start <= at <= onset):
            continue
        verb = "created" if created is not None and abs(at - created) < 5 else "edited"
        by = f" by {manager}" if manager else ""
        out.append(
            {
                "category": "config",
                "kind": "ConfigMap",
                "name": name,
                "namespace": ns,
                "title": f"ConfigMap {name} {verb}{by}",
                "at": at,
            }
        )
    return out


def _mounted_configmaps(ns: str) -> dict[str, set[str]]:
    """ConfigMap name -> workloads in ``ns`` whose pod template references it."""
    deps = safe(lambda: get_apps_client().list_namespaced_deployment(ns))
    if isinstance(deps, ToolError):
        return {}
    refs: dict[str, set[str]] = {}
    for d in deps.items:
        spec = d.spec.template.spec
        names = {v.config_map.name for v in (spec.volumes or []) if getattr(v, "config_map", None)}
        for c in list(spec.containers or []) + list(spec.init_containers or []):
            for ef in c.env_from or []:
                if getattr(ef, "config_map_ref", None):
                    names.add(ef.config_map_ref.name)
            for e in c.env or []:
                vf = getattr(e, "value_from", None)
                if vf is not None and getattr(vf, "config_map_key_ref", None):
                    names.add(vf.config_map_key_ref.name)
        for n in names:
            refs.setdefault(n, set()).add(d.metadata.name)
    return refs


def _argo_syncs(namespaces: set[str], start: int, onset: int) -> list[dict]:
    apps = safe(lambda: get_custom_client().list_cluster_custom_object("argoproj.io", "v1alpha1", "applications"))
    if isinstance(apps, ToolError) or not isinstance(apps, dict):
        return []
    out = []
    for app in apps.get("items", []):
        dest = (app.get("spec", {}).get("destination") or {}).get("namespace", "")
        op = (app.get("status") or {}).get("operationState") or {}
        at = _epoch(op.get("finishedAt"))
        if dest not in namespaces or at is None or not (start <= at <= onset):
            continue
        rev = ((op.get("syncResult") or {}).get("revision") or "")[:8]
        name = app.get("metadata", {}).get("name", "")
        out.append(
            {
                "category": "gitops",
                "kind": "Application",
                "name": name,
                "namespace": dest,
                "title": f"Argo CD synced {name}" + (f" to {rev}" if rev else ""),
                "at": at,
            }
        )
    return out


def _cluster_upgrades(start: int, onset: int) -> list[dict]:
    cv = safe(
        lambda: get_custom_client().get_cluster_custom_object("config.openshift.io", "v1", "clusterversions", "version")
    )
    if isinstance(cv, ToolError) or not isinstance(cv, dict):
        return []
    out = []
    for h in (cv.get("status") or {}).get("history", []) or []:
        at = _epoch(h.get("startedTime"))
        if at is not None and start <= at <= onset:
            out.append(
                {
                    "category": "upgrade",
                    "kind": "ClusterVersion",
                    "name": "version",
                    "namespace": "",
                    "title": f"Cluster upgrade to {h.get('version', '?')} started",
                    "at": at,
                }
            )
    return out


def _relevance(change: dict, workloads: set[tuple[str, str]], mounts: dict[str, dict[str, set[str]]]) -> str:
    ns, name = change.get("namespace", ""), change.get("name", "")
    if not ns:
        return "cluster-wide"
    if (ns, name) in workloads:
        return "same workload"
    if change.get("kind") == "ConfigMap":
        users = mounts.get(ns, {}).get(name, set())
        if any((ns, w) in workloads for w in users):
            return "used by an affected workload"
    return "same namespace"


def ranked_changes(
    correlation_keys: list[str],
    namespaces: list[str],
    onset: int,
    lookback: int = LOOKBACK_SECONDS,
) -> list[dict]:
    """Changes in the ``lookback`` before ``onset``, most relevant first."""
    ns_set, workloads = affected_scope(correlation_keys, namespaces)
    start = onset - lookback
    changes: list[dict] = []
    mounts: dict[str, dict[str, set[str]]] = {}
    for ns in sorted(ns_set)[:_MAX_NAMESPACES]:
        try:
            changes += _deployment_rollouts(ns, start, onset)
            changes += _controller_revisions(ns, start, onset)
            changes += _configmap_edits(ns, start, onset)
            mounts[ns] = _mounted_configmaps(ns)
        except Exception:
            logger.debug("Change scan failed for namespace %s", ns, exc_info=True)
    try:
        changes += _argo_syncs(ns_set, start, onset)
        changes += _cluster_upgrades(start, onset)
    except Exception:
        logger.debug("Cluster-scope change scan failed", exc_info=True)

    for c in changes:
        c["relevance"] = _relevance(c, workloads, mounts)
        c["seconds_before"] = onset - c["at"]
        # Relevance first; within a tier, the change nearest the onset.
        c["score"] = round(RELEVANCE[c["relevance"]] + (1 - c["seconds_before"] / lookback), 3)
    changes.sort(key=lambda c: (-c["score"], c["seconds_before"]))
    return changes[:MAX_CHANGES]
