"""RBAC, SCC and admission-webhook change history, kept by the agent.

The kube-apiserver audit log is the real record of who changed what, but
Pulse cannot read it (it lives on the control-plane nodes). Pulse's "Recent
RBAC changes" therefore sorted bindings by creationTimestamp, which shows
new bindings only: an edit to an existing Role, an SCC change or a webhook
change never appeared, and there was no previous version to restore.

So the agent keeps its own observed history. On each audit scan it lists the
watched kinds (as its service account) and stores a row in
``rbac_object_versions`` when an object appears, its content changes, or it
disappears. A change is shown with the version before it, which is what
"Restore previous" applies. What this cannot know:

- who made a change, unless it was made through Pulse (see the ledger
  below). ``managedFields`` gives the client (``kubectl-edit``, an operator's
  controller name), not the person.
- intermediate versions between two scans: several edits inside one scan
  interval collapse into one.
- anything before the first observation (rows marked ``baseline``).

Writes made through Pulse are recorded in ``pulse_write_ledger`` with the
authenticated actor: the UI posts its own writes, and the agent records the
write tools it ran.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from typing import Any

from kubernetes.client.rest import ApiException

logger = logging.getLogger("pulse_agent")

# kind → (group, version, plural, namespaced)
WATCHED_KINDS: dict[str, tuple[str, str, str, bool]] = {
    "Role": ("rbac.authorization.k8s.io", "v1", "roles", True),
    "ClusterRole": ("rbac.authorization.k8s.io", "v1", "clusterroles", False),
    "RoleBinding": ("rbac.authorization.k8s.io", "v1", "rolebindings", True),
    "ClusterRoleBinding": ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", False),
    "SecurityContextConstraints": ("security.openshift.io", "v1", "securitycontextconstraints", False),
    "ValidatingWebhookConfiguration": ("admissionregistration.k8s.io", "v1", "validatingwebhookconfigurations", False),
    "MutatingWebhookConfiguration": ("admissionregistration.k8s.io", "v1", "mutatingwebhookconfigurations", False),
}

# Server-owned metadata: replaying it would conflict (resourceVersion), point
# at a dead object (uid) or say nothing about the object (managedFields).
_SERVER_FIELDS = (
    "managedFields",
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "selfLink",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
)

RETENTION_DAYS = 30
_PRUNE_EVERY_SECONDS = 3600

_lock = threading.Lock()
_latest: dict[tuple[str, str, str], tuple[str, str, str]] | None = (
    None  # key → (resource_version, change, content hash)
)
_observed_kinds: set[str] = set()
_last_prune = 0.0
_status: dict[str, Any] = {"last_observed_at": None, "last_error": None, "unwatched": {}}


def strip(obj: dict) -> dict:
    """The object as it can be stored and replayed: no status, no server-owned metadata."""
    out = copy.deepcopy(obj)
    meta = out.get("metadata") or {}
    for f in _SERVER_FIELDS:
        meta.pop(f, None)
    out["metadata"] = meta
    out.pop("status", None)
    return out


def content_hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def newest_manager(obj: dict) -> tuple[str, str, str | None]:
    """(manager, operation, time) of the newest managedFields entry."""
    entries = (obj.get("metadata") or {}).get("managedFields") or []
    best: dict | None = None
    for e in entries:
        if best is None or str(e.get("time") or "") >= str(best.get("time") or ""):
            best = e
    if not best:
        return "", "", None
    return str(best.get("manager") or ""), str(best.get("operation") or ""), best.get("time")


def _list_kind(kind: str) -> list[dict]:
    from .k8s_client import get_custom_client

    group, version, plural, _ = WATCHED_KINDS[kind]
    # GET /apis/<group>/<version>/<plural> lists every namespace for namespaced kinds too.
    result = get_custom_client().list_cluster_custom_object(group, version, plural)
    items = result.get("items", []) if isinstance(result, dict) else []
    for it in items:
        it.setdefault("kind", kind)
        it.setdefault("apiVersion", f"{group}/{version}")
    return items


def _key(kind: str, obj: dict) -> tuple[str, str, str]:
    meta = obj.get("metadata") or {}
    ns = (meta.get("namespace") or "") if WATCHED_KINDS[kind][3] else ""
    return kind, ns, meta.get("name") or ""


def _load_latest(repo) -> None:
    global _latest, _observed_kinds
    latest: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for r in repo.latest_per_object():
        content = r.get("content")
        latest[(r["kind"], r["namespace"], r["name"])] = (
            r.get("resource_version") or "",
            r["change"],
            content_hash(content) if content is not None else "",
        )
    _latest = latest
    _observed_kinds = repo.observed_kinds()


def observe(now_ms: int | None = None, lister=None) -> dict:
    """List every watched kind and store what appeared, changed or disappeared.

    Returns ``{"recorded": n, "unwatched": {kind: reason}}``. A kind the
    service account may not list is reported unwatched and its objects are
    left alone (never recorded as deleted).
    """
    from .repositories import get_rbac_history_repo

    global _last_prune
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    list_fn = lister or _list_kind
    repo = get_rbac_history_repo()
    with _lock:
        if _latest is None:
            _load_latest(repo)
        assert _latest is not None
        rows: list[dict] = []
        unwatched: dict[str, str] = {}
        updates: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        for kind in WATCHED_KINDS:
            try:
                items = list_fn(kind)
            except ApiException as e:
                unwatched[kind] = f"{e.status} {e.reason}"
                continue
            except Exception as e:
                unwatched[kind] = str(e)[:200]
                continue
            first_time = kind not in _observed_kinds
            seen: set[tuple[str, str, str]] = set()
            for obj in items:
                key = _key(kind, obj)
                if not key[2]:
                    continue
                seen.add(key)
                meta = obj.get("metadata") or {}
                rv = str(meta.get("resourceVersion") or "")
                prev = _latest.get(key)
                if prev and prev[1] != "deleted" and prev[0] == rv:
                    continue
                content = strip(obj)
                h = content_hash(content)
                if prev and prev[1] != "deleted" and prev[2] == h:
                    # Only the resourceVersion moved (e.g. a status write); nothing to show.
                    updates[key] = (rv, prev[1], h)
                    continue
                change = "baseline" if first_time else ("created" if not prev or prev[1] == "deleted" else "updated")
                manager, operation, mtime = newest_manager(obj)
                rows.append(
                    {
                        "kind": kind,
                        "namespace": key[1],
                        "name": key[2],
                        "uid": str(meta.get("uid") or ""),
                        "resource_version": rv,
                        "change": change,
                        "observed_at": now_ms,
                        "manager": manager,
                        "operation": operation,
                        "manager_time": mtime,
                        "content": content,
                    }
                )
                updates[key] = (rv, change, h)
            # Gone since the last observation: deleted (its last content is the row before).
            for key, (rv, change, _h) in list(_latest.items()):
                if key[0] != kind or key in seen or change == "deleted":
                    continue
                rows.append(
                    {
                        "kind": kind,
                        "namespace": key[1],
                        "name": key[2],
                        "resource_version": rv,
                        "change": "deleted",
                        "observed_at": now_ms,
                        "content": None,
                    }
                )
                updates[key] = (rv, "deleted", "")
        repo.insert_versions(rows)
        # Only after the rows are stored: a failed insert retries next scan.
        _latest.update(updates)
        _observed_kinds.update(k for k in WATCHED_KINDS if k not in unwatched)
        _status.update({"last_observed_at": now_ms, "last_error": None, "unwatched": unwatched})
        if time.monotonic() - _last_prune > _PRUNE_EVERY_SECONDS:
            _last_prune = time.monotonic()
            try:
                repo.prune(now_ms - RETENTION_DAYS * 86400 * 1000)
            except Exception:
                logger.warning("RBAC history prune failed", exc_info=True)
        return {"recorded": len(rows), "unwatched": unwatched}


def observe_safely() -> None:
    """``observe()`` for the audit scan: a failure is logged and reported, never raised."""
    try:
        observe()
    except Exception as e:
        logger.warning("RBAC history observation failed", exc_info=True)
        _status["last_error"] = str(e)[:300]


def status() -> dict:
    return {
        "last_observed_at": _status["last_observed_at"],
        "last_error": _status["last_error"],
        "unwatched": dict(_status["unwatched"]),
    }


def reset_for_tests() -> None:
    global _latest, _observed_kinds, _last_prune
    with _lock:
        _latest = None
        _observed_kinds = set()
        _last_prune = 0.0
        _status.update({"last_observed_at": None, "last_error": None, "unwatched": {}})


# -- Ledger -----------------------------------------------------------------


# Which object each write tool acts on: tool → (kind, name input, namespace input).
# Only these fields are stored for an agent write; free-form arguments (exec
# commands, manifests, skill text, git content) never are.
_TOOL_TARGETS: dict[str, tuple[str, str, str]] = {
    "scale_deployment": ("Deployment", "name", "namespace"),
    "restart_deployment": ("Deployment", "name", "namespace"),
    "rollback_deployment": ("Deployment", "name", "namespace"),
    "patch_workload_container": ("", "name", "namespace"),
    "scale_workload": ("", "name", "namespace"),
    "restart_workload": ("", "name", "namespace"),
    "rollback_workload": ("", "name", "namespace"),
    "delete_pod": ("Pod", "pod_name", "namespace"),
    "exec_command": ("Pod", "pod_name", "namespace"),
    "cordon_node": ("Node", "node_name", ""),
    "uncordon_node": ("Node", "node_name", ""),
    "drain_node": ("Node", "node_name", ""),
    "create_network_policy": ("NetworkPolicy", "name", "namespace"),
    "create_argo_application": ("Application", "name", ""),
    "remove_finalizer": ("", "name", "namespace"),
    "create_skill": ("Skill", "name", ""),
    "create_skill_from_template": ("Skill", "name", ""),
    "edit_skill": ("Skill", "name", ""),
    "delete_skill": ("Skill", "name", ""),
}


# Write tools whose target kind is one of their inputs.
_KIND_INPUT_TOOLS = frozenset(
    {"remove_finalizer", "patch_workload_container", "scale_workload", "restart_workload", "rollback_workload"}
)


def agent_write_target(tool: str, tool_input: dict) -> tuple[str, str, str]:
    """(kind, namespace, name) a write tool acts on, from its inputs; '' where unknown."""
    if tool == "apply_yaml":
        # The manifest itself is never stored: only the first object's identity.
        try:
            import yaml

            docs = [d for d in yaml.safe_load_all(str(tool_input.get("yaml_content") or "")) if isinstance(d, dict)]
        except Exception:
            docs = []
        if not docs:
            return "", str(tool_input.get("namespace") or ""), ""
        meta = docs[0].get("metadata") or {}
        return (
            str(docs[0].get("kind") or ""),
            str(meta.get("namespace") or tool_input.get("namespace") or ""),
            str(meta.get("name") or "") + (f" (+{len(docs) - 1} more)" if len(docs) > 1 else ""),
        )
    kind, name_key, ns_key = _TOOL_TARGETS.get(tool, ("", "name", "namespace"))
    if tool in _KIND_INPUT_TOOLS:
        kind = str(tool_input.get("kind") or "")
    name = tool_input.get(name_key)
    if tool == "create_network_policy" and not name:
        from .k8s_tools.advanced import default_network_policy_name

        name = default_network_policy_name(
            str(tool_input.get("policy_type") or "deny-all-ingress"), bool(tool_input.get("strict"))
        )
    ns = tool_input.get(ns_key) if ns_key else ""
    return kind, str(ns or "") if isinstance(ns, str) else "", str(name) if isinstance(name, str) else ""


def record_agent_write(tool: str, tool_input: dict, status_: str, error: str | None = None) -> None:
    """Record a write tool the agent ran: who, which tool, the target object and the
    outcome. Nothing else from the input is kept. Best effort: never raises."""
    try:
        from .repositories import get_rbac_history_repo
        from .request_context import get_current_user

        actor = get_current_user() or "pulse-agent (no signed-in user)"
        kind, ns, name = agent_write_target(tool, tool_input)
        result = "ok" if status_ == "success" else f"failed: {(error or '')[:200]}"
        get_rbac_history_repo().record_write(
            {
                "at": int(time.time() * 1000),
                "actor": str(actor),
                # The agent knows the user from the session it serves, not from a token review.
                "actor_verified": False,
                "source": "agent",
                "verb": tool,
                "kind": kind,
                "namespace": ns,
                "name": name or "(unknown)",
                "outcome": "ok" if status_ == "success" else "failed",
                "detail": {"tool": tool, "result": result},
            }
        )
    except Exception:
        logger.debug("Could not record agent write %s in the ledger", tool, exc_info=True)
