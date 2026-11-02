"""What changed around one resource, for the ``GET /changes`` endpoint.

The episode view asks "what changed before this broke" for a whole episode
(``change_correlation.ranked_changes``). A resource page asks the same question
for one object: its own rollouts, those of what owns it (Pod -> ReplicaSet ->
Deployment, StatefulSet, DaemonSet, Job -> CronJob), the ConfigMaps and
Secrets it mounts or references, Argo CD syncs into its namespace, and cluster
upgrades. The readers are the ones ``change_correlation`` already uses; this
module only works out the scope and filters to it.

Reads run as the caller (token forwarding), so the answer is what the caller's
RBAC lets them see. A read that is refused is not "nothing changed":

- the resource, its owner chain, rollouts and ConfigMap edits are required —
  a refused read raises ``ChangeReadError`` (the endpoint answers 403);
- Secret metadata, Argo CD Applications and ClusterVersion are optional —
  many users may not read them. A refused read there is named in
  ``unreadable`` instead of failing the whole answer; a 404 (Argo CD not
  installed, not OpenShift) is simply nothing to report.

Secrets are read as metadata only; no value is ever fetched or returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..errors import ToolError
from ..k8s_client import get_apps_client, get_batch_client, get_core_client, safe
from .change_correlation import (
    _argo_syncs,
    _cluster_upgrades,
    _configmap_edits,
    _controller_revisions,
    _deployment_rollouts,
    _epoch,
    _last_edit,
    _secret_edits,
    referenced_config,
)

# Canonical kind names accepted by the endpoint (matched case-insensitively).
SUPPORTED_KINDS = (
    "Pod",
    "ReplicaSet",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "ConfigMap",
    "Secret",
    "ClusterVersion",
)
CLUSTER_SCOPED_KINDS = frozenset({"ClusterVersion"})

# change_correlation's category -> the ``source`` the API reports.
SOURCE = {
    "rollout": "rollout",
    "config": "configmap",
    "secret": "secret",
    "gitops": "argo",
    "upgrade": "clusterversion",
    "workload": "workload",
}


class ChangeReadError(Exception):
    """A required read failed; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _status_for(err: ToolError) -> int:
    code = err.status_code or 0
    if code in (401, 403, 404):
        return code
    return 502


def _require(fn: Any, what: str) -> Any:
    result = safe(fn)
    if isinstance(result, ToolError):
        raise ChangeReadError(_status_for(result), f"Could not read {what}: {result.message}")
    return result


def _optional_owner(fn: Any, what: str) -> Any | None:
    """Read an owner further up the chain; one that has since been deleted ends the chain."""
    result = safe(fn)
    if isinstance(result, ToolError):
        if result.status_code == 404:
            return None
        raise ChangeReadError(_status_for(result), f"Could not read {what}: {result.message}")
    return result


def _owner(obj: Any, kind: str) -> str | None:
    for ref in getattr(obj.metadata, "owner_references", None) or []:
        if ref.kind == kind:
            return str(ref.name)
    return None


@dataclass
class Scope:
    namespace: str
    namespace_wide: bool = False
    deployments: set[str] = field(default_factory=set)
    revisioned: set[tuple[str, str]] = field(default_factory=set)  # (StatefulSet|DaemonSet, name)
    jobs: list[Any] = field(default_factory=list)
    cronjobs: list[Any] = field(default_factory=list)
    configmaps: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)

    def add_refs(self, pod_spec: Any) -> None:
        cms, secs = referenced_config(pod_spec)
        self.configmaps |= cms
        self.secrets |= secs


def _template_spec(obj: Any) -> Any:
    try:
        return obj.spec.template.spec
    except AttributeError:
        return None


def _add_job_chain(scope: Scope, job: Any) -> None:
    ns = scope.namespace
    scope.jobs.append(job)
    cj_name = _owner(job, "CronJob")
    if cj_name:
        cj = _optional_owner(
            lambda: get_batch_client().read_namespaced_cron_job(cj_name, ns), f"CronJob {ns}/{cj_name}"
        )
        if cj is not None:
            scope.cronjobs.append(cj)


def _add_replicaset_chain(scope: Scope, rs: Any) -> None:
    dep = _owner(rs, "Deployment")
    if dep:
        scope.deployments.add(dep)


def resolve_scope(namespace: str, kind: str, name: str) -> Scope:
    """The resource, its owner chain and the config it references."""
    scope = Scope(namespace=namespace)
    if not kind:
        scope.namespace_wide = bool(namespace)
        return scope
    if kind in CLUSTER_SCOPED_KINDS:
        return scope
    ns = namespace
    apps, batch = get_apps_client, get_batch_client
    if kind == "Pod":
        pod = _require(lambda: get_core_client().read_namespaced_pod(name, ns), f"Pod {ns}/{name}")
        scope.add_refs(pod.spec)
        rs_name = _owner(pod, "ReplicaSet")
        if rs_name:
            rs = _optional_owner(lambda: apps().read_namespaced_replica_set(rs_name, ns), f"ReplicaSet {ns}/{rs_name}")
            if rs is not None:
                _add_replicaset_chain(scope, rs)
        for owner_kind in ("StatefulSet", "DaemonSet"):
            owner_name = _owner(pod, owner_kind)
            if owner_name:
                scope.revisioned.add((owner_kind, owner_name))
        job_name = _owner(pod, "Job")
        if job_name:
            job = _optional_owner(lambda: batch().read_namespaced_job(job_name, ns), f"Job {ns}/{job_name}")
            if job is not None:
                _add_job_chain(scope, job)
    elif kind == "ReplicaSet":
        rs = _require(lambda: apps().read_namespaced_replica_set(name, ns), f"ReplicaSet {ns}/{name}")
        scope.add_refs(_template_spec(rs))
        _add_replicaset_chain(scope, rs)
    elif kind == "Deployment":
        dep = _require(lambda: apps().read_namespaced_deployment(name, ns), f"Deployment {ns}/{name}")
        scope.add_refs(_template_spec(dep))
        scope.deployments.add(name)
    elif kind == "StatefulSet":
        sts = _require(lambda: apps().read_namespaced_stateful_set(name, ns), f"StatefulSet {ns}/{name}")
        scope.add_refs(_template_spec(sts))
        scope.revisioned.add(("StatefulSet", name))
    elif kind == "DaemonSet":
        ds = _require(lambda: apps().read_namespaced_daemon_set(name, ns), f"DaemonSet {ns}/{name}")
        scope.add_refs(_template_spec(ds))
        scope.revisioned.add(("DaemonSet", name))
    elif kind == "Job":
        job = _require(lambda: batch().read_namespaced_job(name, ns), f"Job {ns}/{name}")
        scope.add_refs(_template_spec(job))
        _add_job_chain(scope, job)
    elif kind == "CronJob":
        cj = _require(lambda: batch().read_namespaced_cron_job(name, ns), f"CronJob {ns}/{name}")
        try:
            scope.add_refs(cj.spec.job_template.spec.template.spec)
        except AttributeError:
            pass
        scope.cronjobs.append(cj)
    elif kind == "ConfigMap":
        scope.configmaps.add(name)
    elif kind == "Secret":
        scope.secrets.add(name)
    return scope


def _job_changes(scope: Scope, start: int, end: int) -> list[dict]:
    out = []
    for job in scope.jobs:
        at = _epoch(job.metadata.creation_timestamp)
        if at is None or not (start <= at <= end):
            continue
        cj = _owner(job, "CronJob")
        out.append(
            {
                "category": "workload",
                "kind": "Job",
                "name": job.metadata.name,
                "namespace": scope.namespace,
                "title": f"Job {job.metadata.name} created" + (f" by CronJob {cj}" if cj else ""),
                "at": at,
                "actor": None,
            }
        )
    for cj in scope.cronjobs:
        at, manager = _last_edit(cj)
        if at is None or not (start <= at <= end):
            continue
        by = f" by {manager}" if manager else ""
        out.append(
            {
                "category": "workload",
                "kind": "CronJob",
                "name": cj.metadata.name,
                "namespace": scope.namespace,
                "title": f"CronJob {cj.metadata.name} edited{by}",
                "at": at,
                "actor": manager or None,
            }
        )
    return out


def _raise_required(errors: list[ToolError], what: str) -> None:
    if errors:
        err = errors[0]
        raise ChangeReadError(_status_for(err), f"Could not read {what}: {err.message}")


def _note_optional(errors: list[ToolError], what: str, unreadable: list[str]) -> None:
    for err in errors:
        if err.status_code == 404:
            continue  # not served here (no Argo CD, not OpenShift): nothing to report
        if err.status_code == 401:
            raise ChangeReadError(401, f"Could not read {what}: {err.message}")
        unreadable.append(f"{what}: {err.message}")
        return


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def collect_changes(namespace: str, kind: str, name: str, start: int, end: int) -> dict[str, Any]:
    """``{"changes": [...], "available": True}`` (+ ``unreadable`` when an optional source was refused).

    Raises ``ChangeReadError`` when a required read fails.
    """
    scope = resolve_scope(namespace, kind, name)
    ns = scope.namespace
    raw: list[dict] = []
    unreadable: list[str] = []

    if ns and (scope.namespace_wide or scope.deployments):
        errs: list[ToolError] = []
        rollouts = _deployment_rollouts(ns, start, end, errs)
        _raise_required(errs, f"ReplicaSets in {ns}")
        raw += [c for c in rollouts if scope.namespace_wide or c["name"] in scope.deployments]
    if ns and (scope.namespace_wide or scope.revisioned):
        errs = []
        revs = _controller_revisions(ns, start, end, errs)
        _raise_required(errs, f"ControllerRevisions in {ns}")
        raw += [c for c in revs if scope.namespace_wide or (c["kind"], c["name"]) in scope.revisioned]
    if ns and (scope.namespace_wide or scope.configmaps):
        errs = []
        cms = _configmap_edits(ns, start, end, errs)
        _raise_required(errs, f"ConfigMaps in {ns}")
        raw += [c for c in cms if scope.namespace_wide or c["name"] in scope.configmaps]
    if ns:
        raw += _job_changes(scope, start, end)
    if ns and scope.secrets:
        errs = []
        raw += _secret_edits(ns, scope.secrets, start, end, errs)
        _note_optional(errs, f"Secret edits in {ns}", unreadable)
    if ns:
        errs = []
        raw += _argo_syncs({ns}, start, end, errs)
        _note_optional(errs, "Argo CD Applications", unreadable)
    errs = []
    raw += _cluster_upgrades(start, end, errs)
    _note_optional(errs, "ClusterVersion", unreadable)

    changes = [
        {
            "at": _iso(c["at"]),
            "kind": c.get("kind", ""),
            "namespace": c.get("namespace", ""),
            "name": c.get("name", ""),
            "summary": c.get("title", ""),
            "actor": c.get("actor"),
            "source": SOURCE.get(c.get("category", ""), c.get("category", "")),
        }
        for c in raw
    ]
    changes.sort(key=lambda c: (c["at"], c["summary"]), reverse=True)
    result: dict[str, Any] = {"changes": changes, "available": True}
    if unreadable:
        result["unreadable"] = unreadable
    return result
