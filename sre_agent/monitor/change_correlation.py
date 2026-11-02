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

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..errors import ToolError
from ..k8s_client import (
    get_apps_client,
    get_current_user_token,
    get_custom_client,
    get_raw_json,
    safe,
)
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


def _failed(result: Any, errors: list[ToolError] | None) -> bool:
    """True when ``result`` is a read error; the error is kept in ``errors`` when given.

    The ranking callers treat an unreadable source as "nothing to add"; the
    ``/changes`` endpoint passes ``errors`` so a forbidden read becomes a 403
    rather than an empty list that looks like "nothing changed".
    """
    if isinstance(result, ToolError):
        if errors is not None:
            errors.append(result)
        return True
    return False


# Lists are read in pages; past MAX_LIST_ITEMS the rest is not read and the
# source is named in ``incomplete`` so a caller can say the answer is partial.
PAGE_SIZE = 500
MAX_LIST_ITEMS = 5000


def _list_typed(
    fn: Callable[..., Any], what: str, errors: list[ToolError] | None, incomplete: list[str] | None
) -> list[Any] | None:
    """All items of a typed-client list call, paged with ``limit``/``_continue``; None on a read error."""
    items: list[Any] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"limit": PAGE_SIZE}
        if token:
            kwargs["_continue"] = token
        page = safe(lambda: fn(**kwargs))
        if _failed(page, errors):
            return None
        items.extend(getattr(page, "items", None) or [])
        nxt = getattr(getattr(page, "metadata", None), "_continue", None)
        if not isinstance(nxt, str) or not nxt or nxt == token:
            return items
        if len(items) >= MAX_LIST_ITEMS:
            if incomplete is not None:
                incomplete.append(what)
            return items
        token = nxt


def _list_dicts(
    fetch: Callable[[str | None], Any], what: str, errors: list[ToolError] | None, incomplete: list[str] | None
) -> list[dict] | None:
    """All items of a JSON list (``metadata.continue`` paging); None on a read error."""
    items: list[dict] = []
    token: str | None = None
    while True:
        page = fetch(token)
        if _failed(page, errors) or not isinstance(page, dict):
            return None if isinstance(page, ToolError) else items
        items.extend(page.get("items") or [])
        nxt = (page.get("metadata") or {}).get("continue")
        if not isinstance(nxt, str) or not nxt or nxt == token:
            return items
        if len(items) >= MAX_LIST_ITEMS:
            if incomplete is not None:
                incomplete.append(what)
            return items
        token = nxt


def _list_metadata(
    path: str, what: str, errors: list[ToolError] | None, incomplete: list[str] | None
) -> list[dict] | None:
    """A core list as PartialObjectMetadata: names, timestamps, managedFields — never ``data``."""

    def fetch(token: str | None) -> Any:
        query = {"limit": str(PAGE_SIZE)}
        if token:
            query["continue"] = token
        return get_raw_json(path, what, query=query, accept=_METADATA_ONLY)

    return _list_dicts(fetch, what, errors, incomplete)


def _deployment_rollouts(
    ns: str,
    start: int,
    onset: int,
    errors: list[ToolError] | None = None,
    incomplete: list[str] | None = None,
) -> list[dict]:
    rs_items = _list_typed(
        lambda **kw: get_apps_client().list_namespaced_replica_set(ns, **kw), f"ReplicaSets in {ns}", errors, incomplete
    )
    if rs_items is None:
        return []
    by_deploy: dict[str, list[Any]] = {}
    for rs in rs_items:
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
                "actor": None,
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


def _controller_revisions(
    ns: str,
    start: int,
    onset: int,
    errors: list[ToolError] | None = None,
    incomplete: list[str] | None = None,
) -> list[dict]:
    revs = _list_typed(
        lambda **kw: get_apps_client().list_namespaced_controller_revision(ns, **kw),
        f"ControllerRevisions in {ns}",
        errors,
        incomplete,
    )
    if revs is None:
        return []
    out = []
    for rev in revs:
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
                        "actor": None,
                    }
                )
    return out


def _last_edit(obj: Any) -> tuple[int | None, str]:
    latest, manager = None, ""
    for mf in obj.metadata.managed_fields or []:
        # A status write (a controller recording progress) is not an edit.
        if getattr(mf, "subresource", None) == "status":
            continue
        t = _epoch(getattr(mf, "time", None))
        if t is not None and (latest is None or t > latest):
            latest, manager = t, mf.manager or ""
    return latest, manager


_SKIP_CONFIGMAPS = ("kube-root-ca.crt", "openshift-service-ca.crt")


def metadata_edit(meta: dict, kind: str, category: str, ns: str, start: int, onset: int) -> dict | None:
    """The last edit of an object from its (metadata-only) ``metadata``, if inside the window."""
    latest, manager = None, ""
    for mf in meta.get("managedFields") or []:
        if mf.get("subresource") == "status":
            continue
        t = _epoch(mf.get("time"))
        if t is not None and (latest is None or t > latest):
            latest, manager = t, mf.get("manager") or ""
    if latest is None or not (start <= latest <= onset):
        return None
    name = meta.get("name", "")
    created = _epoch(meta.get("creationTimestamp"))
    verb = "created" if created is not None and abs(latest - created) < 5 else "edited"
    by = f" by {manager}" if manager else ""
    return {
        "category": category,
        "kind": kind,
        "name": name,
        "namespace": ns,
        "title": f"{kind} {name} {verb}{by}",
        "at": latest,
        "actor": manager or None,
    }


def _configmap_edits(
    ns: str,
    start: int,
    onset: int,
    errors: list[ToolError] | None = None,
    incomplete: list[str] | None = None,
) -> list[dict]:
    """ConfigMap edits in ``ns``, listed as metadata only (no ``data`` is read)."""
    items = _list_metadata(f"/api/v1/namespaces/{ns}/configmaps", f"ConfigMaps in {ns}", errors, incomplete)
    out = []
    for item in items or []:
        meta = item.get("metadata") or {}
        if meta.get("name") in _SKIP_CONFIGMAPS:
            continue
        change = metadata_edit(meta, "ConfigMap", "config", ns, start, onset)
        if change:
            out.append(change)
    return out


def referenced_config(pod_spec: Any) -> tuple[set[str], set[str]]:
    """Names of the ConfigMaps and Secrets a pod spec mounts or references.

    Volumes (including projected sources and a CSI ``nodePublishSecretRef``),
    ``envFrom`` and ``env.valueFrom`` key refs of containers, init containers
    and ephemeral containers, and image pull secrets. Names only: nothing here
    reads a value.
    """
    configmaps: set[str] = set()
    secrets: set[str] = set()
    if pod_spec is None:
        return configmaps, secrets
    for v in getattr(pod_spec, "volumes", None) or []:
        cm = getattr(v, "config_map", None)
        if cm is not None and getattr(cm, "name", None):
            configmaps.add(cm.name)
        sec = getattr(v, "secret", None)
        if sec is not None and getattr(sec, "secret_name", None):
            secrets.add(sec.secret_name)
        csi = getattr(v, "csi", None)
        csi_ref = getattr(csi, "node_publish_secret_ref", None) if csi is not None else None
        if csi_ref is not None and getattr(csi_ref, "name", None):
            secrets.add(csi_ref.name)
        projected = getattr(v, "projected", None)
        for src in (getattr(projected, "sources", None) or []) if projected is not None else []:
            pcm = getattr(src, "config_map", None)
            if pcm is not None and getattr(pcm, "name", None):
                configmaps.add(pcm.name)
            psec = getattr(src, "secret", None)
            if psec is not None and getattr(psec, "name", None):
                secrets.add(psec.name)
    containers = [
        c
        for attr in ("containers", "init_containers", "ephemeral_containers")
        for c in (getattr(pod_spec, attr, None) or [])
    ]
    for c in containers:
        for ef in getattr(c, "env_from", None) or []:
            if getattr(ef, "config_map_ref", None):
                configmaps.add(ef.config_map_ref.name)
            if getattr(ef, "secret_ref", None):
                secrets.add(ef.secret_ref.name)
        for e in getattr(c, "env", None) or []:
            vf = getattr(e, "value_from", None)
            if vf is None:
                continue
            if getattr(vf, "config_map_key_ref", None):
                configmaps.add(vf.config_map_key_ref.name)
            if getattr(vf, "secret_key_ref", None):
                secrets.add(vf.secret_key_ref.name)
    for ips in getattr(pod_spec, "image_pull_secrets", None) or []:
        if getattr(ips, "name", None):
            secrets.add(ips.name)
    configmaps.discard("")
    secrets.discard("")
    return configmaps, secrets


def _mounted_configmaps(ns: str) -> dict[str, set[str]]:
    """ConfigMap name -> workloads in ``ns`` whose pod template references it."""
    deps = safe(lambda: get_apps_client().list_namespaced_deployment(ns))
    if isinstance(deps, ToolError):
        return {}
    refs: dict[str, set[str]] = {}
    for d in deps.items:
        names, _secrets = referenced_config(d.spec.template.spec)
        for n in names:
            refs.setdefault(n, set()).add(d.metadata.name)
    return refs


# Metadata only: the API server sends no ``data`` for PartialObjectMetadata, so
# no ConfigMap or Secret payload is read. Annotations still are (and
# ``kubectl.kubernetes.io/last-applied-configuration`` can hold a Secret's
# data), so callers take names, timestamps and managers only; no Secret value is
# returned.
_METADATA_ONLY = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
_METADATA_ONE = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"

_PLURAL = {"ConfigMap": "configmaps", "Secret": "secrets"}
_CATEGORY = {"ConfigMap": "config", "Secret": "secret"}


def object_metadata(kind: str, ns: str, name: str) -> Any:
    """``metadata`` of one ConfigMap or Secret (metadata only), or a ToolError."""
    got = get_raw_json(
        f"/api/v1/namespaces/{ns}/{_PLURAL[kind]}/{name}",
        f"get {kind} {ns}/{name} (metadata only)",
        accept=_METADATA_ONE,
    )
    if isinstance(got, ToolError) or not isinstance(got, dict):
        return got
    return got.get("metadata") or {}


def named_edits(
    kind: str, ns: str, names: set[str], start: int, onset: int, errors: list[ToolError] | None = None
) -> list[dict]:
    """Edits to the named ConfigMaps or Secrets, one metadata-only read each.

    A referenced object that does not exist (404) has no edit to report; any
    other error is kept in ``errors``.
    """
    out = []
    for name in sorted(names):
        if kind == "ConfigMap" and name in _SKIP_CONFIGMAPS:
            continue
        meta = object_metadata(kind, ns, name)
        if isinstance(meta, ToolError):
            if meta.status_code != 404 and errors is not None:
                errors.append(meta)
            continue
        if not isinstance(meta, dict):
            continue
        change = metadata_edit(meta, kind, _CATEGORY[kind], ns, start, onset)
        if change:
            out.append(change)
    return out


# The Argo CD Application list is cluster-wide and can be large. One read per
# identity (forwarded token, or the service account) per minute serves every
# /changes call and episode ranking in that minute; only what is used is kept.
_ARGO_TTL_SECONDS = 60
_ARGO_CACHE_MAX = 128
_argo_cache: dict[str, tuple[float, list[dict], bool]] = {}
_argo_lock = threading.Lock()


def clear_argo_cache() -> None:
    with _argo_lock:
        _argo_cache.clear()


def _compact_app(app: dict) -> dict:
    op = (app.get("status") or {}).get("operationState") or {}
    initiated = (op.get("operation") or {}).get("initiatedBy") or {}
    return {
        "name": (app.get("metadata") or {}).get("name", ""),
        "dest": ((app.get("spec") or {}).get("destination") or {}).get("namespace", ""),
        "finishedAt": op.get("finishedAt"),
        "revision": ((op.get("syncResult") or {}).get("revision") or "")[:8],
        "username": initiated.get("username"),
        "automated": bool(initiated.get("automated")),
    }


def _argo_apps(errors: list[ToolError] | None, incomplete: list[str] | None) -> list[dict] | None:
    key = hashlib.sha256((get_current_user_token() or "").encode()).hexdigest()
    now = time.monotonic()
    with _argo_lock:
        hit = _argo_cache.get(key)
    if hit and now - hit[0] < _ARGO_TTL_SECONDS:
        if hit[2] and incomplete is not None:
            incomplete.append("Argo CD Applications")
        return hit[1]

    def fetch(token: str | None) -> Any:
        kwargs: dict[str, Any] = {"limit": PAGE_SIZE}
        if token:
            kwargs["_continue"] = token
        return safe(
            lambda: get_custom_client().list_cluster_custom_object("argoproj.io", "v1alpha1", "applications", **kwargs)
        )

    local_incomplete: list[str] = []
    items = _list_dicts(fetch, "Argo CD Applications", errors, local_incomplete)
    if items is None:
        return None
    apps = [_compact_app(a) for a in items]
    with _argo_lock:
        _argo_cache[key] = (now, apps, bool(local_incomplete))
        while len(_argo_cache) > _ARGO_CACHE_MAX:
            _argo_cache.pop(next(iter(_argo_cache)))
    if local_incomplete and incomplete is not None:
        incomplete.extend(local_incomplete)
    return apps


def _argo_syncs(
    namespaces: set[str],
    start: int,
    onset: int,
    errors: list[ToolError] | None = None,
    incomplete: list[str] | None = None,
) -> list[dict]:
    apps = _argo_apps(errors, incomplete)
    out = []
    for app in apps or []:
        at = _epoch(app["finishedAt"])
        if app["dest"] not in namespaces or at is None or not (start <= at <= onset):
            continue
        rev = app["revision"]
        actor = app["username"] or ("automated sync" if app["automated"] else None)
        out.append(
            {
                "category": "gitops",
                "kind": "Application",
                "name": app["name"],
                "namespace": app["dest"],
                "title": f"Argo CD synced {app['name']}" + (f" to {rev}" if rev else ""),
                "at": at,
                "actor": actor,
            }
        )
    return out


def _cluster_upgrades(start: int, onset: int, errors: list[ToolError] | None = None) -> list[dict]:
    cv = safe(
        lambda: get_custom_client().get_cluster_custom_object("config.openshift.io", "v1", "clusterversions", "version")
    )
    if _failed(cv, errors) or not isinstance(cv, dict):
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
                    "actor": None,
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
