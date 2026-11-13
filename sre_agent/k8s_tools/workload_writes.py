"""Purpose-built, confirm-gated writes on Deployments, StatefulSets and DaemonSets.

``patch_workload_container`` changes one container's resources, probes, or the
pod template's imagePullSecrets; ``scale_workload``, ``restart_workload`` and
``rollback_workload`` extend the Deployment-only verbs to StatefulSets and
DaemonSets. They follow the silence tools' pattern:

* Each is a write tool (registered in ``WRITE_TOOLS``), so the agent loop asks
  the person to confirm, and ``confirm_preview`` attaches a server-built
  preview computed as the caller from the same planning code the tool runs.
* Every write is first sent with ``dryRun=All``; the preview shows the field
  diff between the object as read and what the API server would store, and the
  tool refuses when the dry run fails.
* Patches that depend on what was read carry it: JSON Patch ``test`` ops on
  ``/metadata/resourceVersion`` (and on the container's name), so a concurrent
  change is refused instead of overwritten. They are the same requests the UI's
  guided fixes send (pulse-ui ``engine/podFixes.ts``) and its Rollout History
  rollback (``views/detail/RolloutHistory.tsx``).
* They run as the person: with token forwarding off, or without the caller's
  token, they refuse rather than write as Pulse's service account.
* A GitOps-managed object is called out in the preview with a recommendation to
  change it in Git (``propose_git_change``); one whose namespace an Argo CD
  application syncs automatically is refused, as ``apply_yaml`` refuses it.

Secrets are never read: a pull secret is checked for existence with a
metadata-only GET (PartialObjectMetadata), which carries no ``data``.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError, classify_api_error
from .validators import MAX_REPLICAS, _validate_k8s_name, _validate_k8s_namespace

logger = logging.getLogger("pulse_agent.k8s_tools")

JSON_PATCH = "application/json-patch+json"
MERGE_PATCH = "application/merge-patch+json"
STRATEGIC_PATCH = "application/strategic-merge-patch+json"

WORKLOAD_PLURAL = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}
_KIND_ALIASES = {
    "deployment": "Deployment",
    "deploy": "Deployment",
    "statefulset": "StatefulSet",
    "sts": "StatefulSet",
    "daemonset": "DaemonSet",
    "ds": "DaemonSet",
}

PROBE_FIELDS = ("readinessProbe", "livenessProbe", "startupProbe")
_PROBE_TIMING_BOUNDS = {
    # field: (min, max). Positive ints except the initial delay, which may be 0.
    "initialDelaySeconds": (0, 3600),
    "periodSeconds": (1, 300),
    "timeoutSeconds": (1, 300),
    "failureThreshold": (1, 120),
    "successThreshold": (1, 10),
}
# A startup budget past this hides a hang. The UI never proposes more than 600s.
MAX_STARTUP_BUDGET_SECONDS = 3600
_NEW_HANDLERS = ("httpGet", "tcpSocket", "grpc")
_ALL_HANDLERS = ("httpGet", "tcpSocket", "grpc", "exec")

_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")
_MIN_MEMORY = 4 * 1024 * 1024  # 4Mi: below this a value is almost always a missing unit.
_MAX_MEMORY = 4 * 1024**4  # 4Ti
_MIN_CPU = Decimal("0.001")
_MAX_CPU = Decimal(512)

GITOPS_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
GITOPS_INSTANCE_LABELS = ("argocd.argoproj.io/instance", "app.kubernetes.io/instance")

_HASH_LABELS = ("pod-template-hash", "controller-revision-hash")


class WorkloadWriteError(ValueError):
    """Input or state that makes the write refuse before anything is sent."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def normalize_kind(kind: str) -> str:
    k = _KIND_ALIASES.get((kind or "").strip().lower())
    if not k:
        raise WorkloadWriteError("kind must be Deployment, StatefulSet or DaemonSet.")
    return k


def workload_path(kind: str, namespace: str, name: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/{WORKLOAD_PLURAL[kind]}/{name}"


def _validate_target(kind: str, namespace: str, name: str) -> str:
    k = normalize_kind(kind)
    if not namespace:
        raise WorkloadWriteError("namespace is required.")
    err = _validate_k8s_namespace(namespace) or _validate_k8s_name(name)
    if err:
        raise WorkloadWriteError(err.removeprefix("Error: "))
    return k


def require_user_writes() -> None:
    """Workload writes run as the person, never as the agent's service account."""
    from ..config import get_settings

    if not get_settings().agent.token_forwarding:
        raise WorkloadWriteError(
            "Workload changes are not made from chat on this deployment: token forwarding is off "
            "(PULSE_AGENT_TOKEN_FORWARDING=false), so the write would run as Pulse's service account instead of "
            "as you. Make the change from the workload's page, which writes with your own permissions."
        )
    if not _kc.get_current_user_token():
        raise WorkloadWriteError(
            "Your access token did not reach Pulse with this request, and workloads are never changed as "
            "Pulse's service account. Sign in again and retry."
        )


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def rv_test(obj: dict) -> list[dict]:
    rv = (obj.get("metadata") or {}).get("resourceVersion")
    return [{"op": "test", "path": "/metadata/resourceVersion", "value": rv}] if rv else []


def change_cause_op(obj: dict, cause: str) -> dict:
    """kubectl's change-cause annotation, so Rollout History says why (as the UI's withChangeCause)."""
    if (obj.get("metadata") or {}).get("annotations"):
        return {"op": "add", "path": "/metadata/annotations/kubernetes.io~1change-cause", "value": cause}
    return {"op": "add", "path": "/metadata/annotations", "value": {"kubernetes.io/change-cause": cause}}


def _pointer_parts(path: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]


def apply_json_patch(doc: dict, ops: list[dict]) -> dict:
    """A local JSON Patch (RFC 6902 add/remove/replace/test) for previews and tests."""
    out = copy.deepcopy(doc)
    for op in ops:
        parts = _pointer_parts(op["path"])
        parent: Any = out
        for p in parts[:-1]:
            parent = parent[int(p)] if isinstance(parent, list) else parent[p]
        last = parts[-1]
        kind = op["op"]
        if kind == "test":
            cur = parent[int(last)] if isinstance(parent, list) else parent.get(last)
            if cur != op["value"]:
                raise WorkloadWriteError(f"test failed at {op['path']}")
        elif kind in ("add", "replace"):
            if isinstance(parent, list):
                if last == "-":
                    parent.append(copy.deepcopy(op["value"]))
                elif kind == "add":
                    parent.insert(int(last), copy.deepcopy(op["value"]))
                else:
                    parent[int(last)] = copy.deepcopy(op["value"])
            else:
                parent[last] = copy.deepcopy(op["value"])
        elif kind == "remove":
            if isinstance(parent, list):
                parent.pop(int(last))
            else:
                parent.pop(last, None)
    return out


def _flatten(value: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        if not value:
            out[prefix] = "{}"
        for k in sorted(value):
            _flatten(value[k], f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(value, list):
        if all(not isinstance(v, dict | list) for v in value):
            out[prefix] = ", ".join(str(v) for v in value) if value else "[]"
        else:
            for i, v in enumerate(value):
                _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = "" if value is None else str(value)


def field_diff(before: Any, after: Any, prefix: str = "", limit: int = 60) -> list[dict]:
    """Every leaf that differs, as {field, before, after}; 'none' where absent."""
    b: dict[str, str] = {}
    a: dict[str, str] = {}
    _flatten(before, prefix, b)
    _flatten(after, prefix, a)
    lines = []
    for key in sorted(set(b) | set(a)):
        if b.get(key) != a.get(key):
            lines.append({"field": key, "before": b.get(key, "none"), "after": a.get(key, "none")})
    return lines[:limit]


def read_workload(kind: str, namespace: str, name: str) -> dict:
    got = _kc.get_raw_json(workload_path(kind, namespace, name), f"get {kind} {namespace}/{name}")
    if isinstance(got, ToolError):
        raise WorkloadWriteError(f"{kind} {namespace}/{name} could not be read: {got}")
    if not isinstance(got, dict):
        raise WorkloadWriteError(f"{kind} {namespace}/{name} could not be read.")
    return got


def send_patch(path: str, body: Any, content_type: str, *, dry_run: bool, operation: str) -> dict | ToolError:
    """PATCH ``path`` as the caller; ``dry_run`` sends ``dryRun=All``. Returns the stored object."""
    api = _kc.get_apps_client().api_client
    query = [("fieldManager", "pulse-agent")] + ([("dryRun", "All")] if dry_run else [])
    try:
        resp = api.call_api(
            path,
            "PATCH",
            body=body,
            header_params={"Content-Type": content_type, "Accept": "application/json"},
            query_params=query,
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
    except ApiException as e:
        return classify_api_error(e, operation)
    raw = resp[0] if isinstance(resp, tuple) else resp
    data = raw.data if hasattr(raw, "data") else raw.read()
    try:
        out = json.loads(data)
    except ValueError:
        return ToolError(message="The API server returned an unreadable response.", category="server")
    return out if isinstance(out, dict) else {}


def _template(obj: dict) -> dict:
    return ((obj.get("spec") or {}).get("template")) or {}


def replicas_of(obj: dict) -> int:
    if obj.get("kind") == "DaemonSet":
        return int((obj.get("status") or {}).get("desiredNumberScheduled") or 0)
    spec = obj.get("spec") or {}
    replicas = spec.get("replicas")
    return int(replicas) if replicas is not None else 1


def rolls_out(obj: dict) -> bool:
    """Whether a template change replaces pods by itself (OnDelete waits for each pod to be deleted)."""
    if obj.get("kind") in ("StatefulSet", "DaemonSet"):
        return ((obj.get("spec") or {}).get("updateStrategy") or {}).get("type") != "OnDelete"
    return True


def rollout_impact(obj: dict) -> str:
    """The UI's rolloutImpact: who restarts, and how."""
    kind = obj.get("kind", "")
    n = replicas_of(obj)
    pods = f"{n} pod{'' if n == 1 else 's'}"
    spec = obj.get("spec") or {}
    if kind == "Deployment":
        strategy = spec.get("strategy") or {}
        if strategy.get("type") == "Recreate":
            return f"All {pods} are stopped, then recreated (strategy Recreate): the app is down meanwhile"
        ru = strategy.get("rollingUpdate") or {}
        return (
            f"Rolling restart of {pods} (maxUnavailable {ru.get('maxUnavailable', '25%')}, "
            f"maxSurge {ru.get('maxSurge', '25%')})"
        )
    us = spec.get("updateStrategy") or {}
    if us.get("type") == "OnDelete":
        return (
            f"Nothing restarts by itself (updateStrategy OnDelete): each of the {pods} picks the change up only "
            "when it is deleted"
        )
    if kind == "StatefulSet":
        return f"Rolling restart of {pods}, one at a time from the highest ordinal"
    return f"Rolling restart of {pods}, one per node (maxUnavailable {(us.get('rollingUpdate') or {}).get('maxUnavailable', 1)})"


def undo_text(obj: dict) -> str:
    kind = obj.get("kind", "")
    name = (obj.get("metadata") or {}).get("name", "")
    rev = ((obj.get("metadata") or {}).get("annotations") or {}).get("deployment.kubernetes.io/revision")
    if kind == "Deployment" and rev:
        return (
            f"Roll back to the revision before this change (revision {rev}) in Rollout History on {kind} {name}, "
            f"or ask Pulse to rollback_workload it"
        )
    return f"Roll back to the revision before this change in Rollout History on {kind} {name}, or ask Pulse to rollback_workload it"


def gitops_owner(obj: dict) -> dict | None:
    """Who else writes this object's spec: Argo CD (tracking annotation or instance label) or Helm."""
    meta = obj.get("metadata") or {}
    ann = meta.get("annotations") or {}
    labels = meta.get("labels") or {}
    tracking = ann.get(GITOPS_TRACKING_ANNOTATION)
    if tracking:
        return {
            "tool": "Argo CD",
            "app": str(tracking).split(":", 1)[0],
            "detail": f"annotation {GITOPS_TRACKING_ANNOTATION}",
        }
    helm = labels.get("app.kubernetes.io/managed-by") == "Helm" or "meta.helm.sh/release-name" in ann
    for label in GITOPS_INSTANCE_LABELS:
        if labels.get(label) and not (helm and label == "app.kubernetes.io/instance"):
            return {"tool": "Argo CD", "app": str(labels[label]), "detail": f"label {label}"}
    if helm:
        return {
            "tool": "Helm",
            "app": str(ann.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/instance") or ""),
            "detail": "label app.kubernetes.io/managed-by=Helm",
        }
    return None


def gitops_note(owner: dict | None) -> str:
    if not owner:
        return ""
    app = f" '{owner['app']}'" if owner.get("app") else ""
    if owner["tool"] == "Helm":
        return (
            f"Managed by Helm release{app} ({owner['detail']}): the next helm upgrade overwrites this change. "
            "Make it in the chart's values as well."
        )
    return (
        f"Managed by Argo CD application{app} ({owner['detail']}): the next sync can revert this change. "
        "Prefer a pull request to the application's source (propose_git_change); a direct edit is drift."
    )


def _argo_autosync_refusal(namespace: str, kind: str, name: str) -> str | None:
    from ..gitops_tools import check_argo_auto_sync

    try:
        return check_argo_auto_sync(namespace, kind, name)
    except Exception:
        logger.debug("Argo auto-sync check failed for %s/%s", namespace, name, exc_info=True)
        return None


def _protected(namespace: str) -> bool:
    from ..policy import _is_protected

    try:
        return _is_protected(namespace)
    except Exception:
        return False


def _cluster_text(text: Any, limit: int = 300) -> str:
    from ..monitor.confidence import sanitize_block

    return sanitize_block(str(text if text is not None else ""))[:limit].replace("\n", " ")


@dataclass
class WritePlan:
    """What a workload write will send, computed identically for the preview and the tool."""

    tool: str
    kind: str
    namespace: str
    name: str
    summary: str = ""
    container: str = ""
    path: str = ""
    patch_type: str = JSON_PATCH
    body: Any = None
    diff_header: str = ""
    diff: list[dict] = field(default_factory=list)
    impact: str = ""
    undo: str = ""
    gitops: dict | None = None
    warnings: list[str] = field(default_factory=list)
    dry_run_ok: bool = False
    dry_run_message: str = ""
    error: str = ""
    before: Any = None

    @property
    def valid(self) -> bool:
        return not self.error and self.dry_run_ok

    def to_preview(self) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "kind": "workload_change",
            "tool": self.tool,
            "valid": self.valid,
            "target": {"kind": self.kind, "namespace": self.namespace, "name": self.name},
            "summary": self.summary,
            "diffHeader": self.diff_header,
            "diff": self.diff,
            "dryRun": {
                "state": "passed" if self.dry_run_ok else ("failed" if self.body is not None else "unavailable"),
                "message": self.dry_run_message,
            },
            "impact": self.impact,
            "undo": self.undo,
            "gitops": self.gitops,
            "warnings": self.warnings,
            "protectedNamespace": _protected(self.namespace) if self.namespace else False,
        }
        if self.container:
            preview["target"]["container"] = self.container
        if self.body is not None:
            preview["patch"] = {"method": "PATCH", "path": self.path, "type": self.patch_type, "body": self.body}
        if self.error:
            preview["error"] = self.error
        return preview

    def diff_text(self) -> str:
        if not self.diff:
            return "(no field changes)"
        return "\n".join(
            f"  {_cluster_text(d['field'], 120)}: {_cluster_text(d['before'])} -> {_cluster_text(d['after'])}"
            for d in self.diff
        )


def _dry_run(plan: WritePlan, before: Any, diff_of) -> dict | None:
    """Send the plan with dryRun=All; fill the diff from what the server would store."""
    got = send_patch(plan.path, plan.body, plan.patch_type, dry_run=True, operation=plan.tool)
    if isinstance(got, ToolError):
        plan.dry_run_ok = False
        plan.dry_run_message = f"Dry run refused by the API server: {got}"
        return None
    plan.dry_run_ok = True
    plan.dry_run_message = "Passed: the API server accepts this change (dryRun=All)"
    plan.diff = diff_of(before, got)
    return got


def _execute(plan: WritePlan) -> dict | str:
    """The real write, after a fresh dry run. Returns the stored object or a refusal."""
    if plan.error:
        return plan.error
    if not plan.dry_run_ok:
        return f"{plan.dry_run_message}. Nothing was changed."
    got = send_patch(plan.path, plan.body, plan.patch_type, dry_run=False, operation=plan.tool)
    if isinstance(got, ToolError):
        msg = got.message.lower()
        # A failed JSON Patch `test` (the resourceVersion moved) is a 422 saying
        # "testing value ... failed"; a stale merge-patch resourceVersion is a 409.
        conflict = "test operation" in msg or "testing value" in msg or got.category == "conflict"
        hint = " The object changed since it was read; read it again and retry." if conflict else ""
        return f"{plan.kind} {plan.namespace}/{plan.name} was not changed: {got}.{hint}"
    return got


def _footer(plan: WritePlan) -> list[str]:
    lines = []
    if plan.gitops:
        lines.append(gitops_note(plan.gitops))
    lines += [f"Note: {w}" for w in plan.warnings]
    lines.append(f"Impact: {plan.impact}")
    lines.append(f"Undo: {plan.undo}")
    return lines


# ---------------------------------------------------------------------------
# patch_workload_container
# ---------------------------------------------------------------------------


def parse_memory(value: str, label: str) -> int:
    v = value.strip()
    m = _MEMORY_RE.fullmatch(v)
    if not m:
        hint = ""
        low = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ki|mi|gi|ti|mb|gb|kb)", v, re.IGNORECASE)
        if low:
            unit = low.group(2)[0].upper() + "i"
            hint = f" Did you mean {low.group(1)}{unit}?"
        raise WorkloadWriteError(f"{label} '{value}' is not a memory quantity like 512Mi or 2Gi.{hint}")
    from kubernetes.utils import parse_quantity

    try:
        q = parse_quantity(v)
    except ValueError as e:
        raise WorkloadWriteError(f"{label} '{value}' does not parse: {e}") from e
    if q < _MIN_MEMORY:
        raise WorkloadWriteError(
            f"{label} '{value}' is under 4Mi; a quantity without a unit is bytes. Did you mean {v}Mi?"
        )
    if q > _MAX_MEMORY:
        raise WorkloadWriteError(f"{label} '{value}' is over 4Ti.")
    return int(q)


def parse_cpu(value: str, label: str) -> Decimal:
    v = value.strip()
    if not _CPU_RE.fullmatch(v):
        raise WorkloadWriteError(f"{label} '{value}' is not a CPU quantity like 250m or 1.5.")
    from kubernetes.utils import parse_quantity

    try:
        q = Decimal(parse_quantity(v))
    except ValueError as e:
        raise WorkloadWriteError(f"{label} '{value}' does not parse: {e}") from e
    if q < _MIN_CPU:
        raise WorkloadWriteError(f"{label} '{value}' is under 1m.")
    if q > _MAX_CPU:
        raise WorkloadWriteError(f"{label} '{value}' is over 512 cores.")
    return q


def _port_ok(port: Any) -> bool:
    if isinstance(port, bool):
        return False
    if isinstance(port, int):
        return 1 <= port <= 65535
    return isinstance(port, str) and bool(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,13}[a-z0-9])?", port))


def _validate_handler(kind: str, h: Any, label: str) -> dict:
    if not isinstance(h, dict):
        raise WorkloadWriteError(f"{label}.{kind} must be an object.")
    if kind == "httpGet":
        allowed = {"path", "port", "scheme", "host", "httpHeaders"}
        if set(h) - allowed:
            raise WorkloadWriteError(f"{label}.httpGet takes only {', '.join(sorted(allowed))}.")
        path = h.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
            raise WorkloadWriteError(f"{label}.httpGet.path must start with '/'.")
        if h.get("scheme", "HTTP") not in ("HTTP", "HTTPS"):
            raise WorkloadWriteError(f"{label}.httpGet.scheme must be HTTP or HTTPS.")
        if "host" in h:
            raise WorkloadWriteError(f"{label}.httpGet.host is not set from chat; leave it to default to the pod IP.")
        headers = h.get("httpHeaders", [])
        if not isinstance(headers, list) or not all(
            isinstance(x, dict) and set(x) == {"name", "value"} and all(isinstance(v, str) for v in x.values())
            for x in headers
        ):
            raise WorkloadWriteError(f"{label}.httpGet.httpHeaders must be a list of {{name, value}}.")
    elif kind == "tcpSocket":
        if set(h) - {"port"}:
            raise WorkloadWriteError(f"{label}.tcpSocket takes only port.")
    elif kind == "grpc":
        if set(h) - {"port", "service"}:
            raise WorkloadWriteError(f"{label}.grpc takes only port and service.")
        if not isinstance(h.get("port"), int) or isinstance(h.get("port"), bool):
            raise WorkloadWriteError(f"{label}.grpc.port must be a number.")
    if not _port_ok(h.get("port")):
        raise WorkloadWriteError(f"{label}.{kind}.port must be 1-65535 or a named container port.")
    return dict(h)


def parse_probe(raw: Any, probe: str) -> dict:
    """A probe change: timings, optionally a new handler or one copied from another probe."""
    if isinstance(raw, str):
        try:
            spec = json.loads(raw)
        except ValueError as e:
            raise WorkloadWriteError(
                f'{probe} must be a JSON object, e.g. {{"periodSeconds": 10, "failureThreshold": 30}}.'
            ) from e
    else:
        spec = raw
    if not isinstance(spec, dict) or not spec:
        raise WorkloadWriteError(f"{probe} must be a non-empty JSON object.")
    allowed = set(_PROBE_TIMING_BOUNDS) | set(_ALL_HANDLERS) | {"copyHandlerFrom"}
    unknown = set(spec) - allowed
    if unknown:
        raise WorkloadWriteError(
            f"{probe} has unknown field(s) {sorted(unknown)}. Allowed: {', '.join(sorted(allowed - {'exec'}))}."
        )
    if "exec" in spec:
        raise WorkloadWriteError(
            f"{probe}: an exec command is not set from chat (it runs inside the container). Copy the handler of an "
            "existing probe with copyHandlerFrom, or change the manifest through review."
        )
    out: dict[str, Any] = {}
    for key, (lo, hi) in _PROBE_TIMING_BOUNDS.items():
        if key not in spec:
            continue
        v = spec[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise WorkloadWriteError(
                f"{probe}.{key} must be a whole number of {'seconds' if 'Seconds' in key else 'checks'}."
            )
        if not lo <= v <= hi:
            raise WorkloadWriteError(f"{probe}.{key} must be between {lo} and {hi}, got {v}.")
        out[key] = v
    if probe != "readinessProbe" and out.get("successThreshold", 1) != 1:
        raise WorkloadWriteError(f"{probe}.successThreshold must be 1 (Kubernetes requires it for {probe}).")
    handlers = [h for h in _NEW_HANDLERS if h in spec]
    if len(handlers) + ("copyHandlerFrom" in spec) > 1:
        raise WorkloadWriteError(
            f"{probe}: give one handler (httpGet, tcpSocket, grpc) or copyHandlerFrom, not several."
        )
    if handlers:
        out["_handler"] = (handlers[0], _validate_handler(handlers[0], spec[handlers[0]], probe))
    if "copyHandlerFrom" in spec:
        src = spec["copyHandlerFrom"]
        if src not in PROBE_FIELDS or src == probe:
            raise WorkloadWriteError(f"{probe}.copyHandlerFrom must be another of {', '.join(PROBE_FIELDS)}.")
        out["_copy"] = src
    return out


@dataclass
class ContainerChange:
    container: str = ""
    resources: dict[tuple[str, str], str] = field(default_factory=dict)  # (tier, resource) -> quantity
    probes: dict[str, dict] = field(default_factory=dict)
    pull_secrets: list[str] = field(default_factory=list)
    reason: str = ""


_RESOURCE_ORDER = (("limits", "memory"), ("requests", "memory"), ("limits", "cpu"), ("requests", "cpu"))


def parse_container_change(args: dict) -> ContainerChange:
    """Validate everything that does not need the cluster."""
    change = ContainerChange(
        container=str(args.get("container") or "").strip(), reason=str(args.get("reason") or "").strip()
    )
    if change.container and (err := _validate_k8s_name(change.container, "container")):
        raise WorkloadWriteError(err.removeprefix("Error: "))
    inputs = {
        ("limits", "memory"): args.get("memory_limit"),
        ("requests", "memory"): args.get("memory_request"),
        ("limits", "cpu"): args.get("cpu_limit"),
        ("requests", "cpu"): args.get("cpu_request"),
    }
    for (tier, res), raw in inputs.items():
        if raw in (None, ""):
            continue
        label = f"{res}_{tier[:-1] if tier == 'limits' else 'request'}"
        if res == "memory":
            parse_memory(str(raw), label)
        else:
            parse_cpu(str(raw), label)
        change.resources[(tier, res)] = str(raw).strip()
    for probe, arg in (
        ("readinessProbe", "readiness_probe"),
        ("livenessProbe", "liveness_probe"),
        ("startupProbe", "startup_probe"),
    ):
        raw = args.get(arg)
        if raw in (None, ""):
            continue
        change.probes[probe] = parse_probe(raw, probe)
    secrets = args.get("add_image_pull_secrets") or ""
    names = [s.strip() for s in (secrets if isinstance(secrets, list) else str(secrets).split(",")) if s.strip()]
    for n in names:
        if err := _validate_k8s_name(n, "image pull secret"):
            raise WorkloadWriteError(err.removeprefix("Error: "))
    change.pull_secrets = list(dict.fromkeys(names))
    if len(change.pull_secrets) > 5:
        raise WorkloadWriteError("Add at most 5 image pull secrets at a time.")
    if not change.resources and not change.probes and not change.pull_secrets:
        raise WorkloadWriteError(
            "Nothing to change: give memory_limit/memory_request/cpu_limit/cpu_request, a probe, or add_image_pull_secrets."
        )
    if len(change.reason) > 200:
        change.reason = change.reason[:200]
    return change


def _pick_container(tpl_spec: dict, wanted: str, kind: str, name: str) -> tuple[int, dict]:
    containers = [c for c in (tpl_spec.get("containers") or []) if isinstance(c, dict)]
    if wanted:
        for i, c in enumerate(containers):
            if c.get("name") == wanted:
                return i, c
        names = ", ".join(str(c.get("name")) for c in containers)
        raise WorkloadWriteError(f"{kind} {name} has no container '{wanted}'. Containers: {names}.")
    if len(containers) == 1:
        return 0, containers[0]
    names = ", ".join(str(c.get("name")) for c in containers)
    raise WorkloadWriteError(f"{kind} {name} has {len(containers)} containers ({names}); say which one with container.")


def _check_requests_within_limits(resources: dict) -> None:
    limits = resources.get("limits") or {}
    requests = resources.get("requests") or {}
    for res, parse in (("memory", parse_memory), ("cpu", parse_cpu)):
        if res in limits and res in requests:
            try:
                lim = parse(str(limits[res]), f"limits.{res}")
                req = parse(str(requests[res]), f"requests.{res}")
            except WorkloadWriteError:
                continue  # an existing value Pulse would not write itself; the API server judges it
            if req > lim:
                raise WorkloadWriteError(
                    f"requests.{res} ({requests[res]}) would be above limits.{res} ({limits[res]}); "
                    f"raise the limit too, or lower the request."
                )


def _secret_exists(namespace: str, secret: str) -> bool | str:
    """Existence only, from a metadata-only GET: the Secret's data never leaves the API server."""
    from ..monitor.change_correlation import _METADATA_ONE

    got = _kc.get_raw_json(
        f"/api/v1/namespaces/{namespace}/secrets/{secret}",
        f"get Secret {namespace}/{secret} (metadata only)",
        accept=_METADATA_ONE,
    )
    if isinstance(got, ToolError):
        if got.status_code == 404:
            return False
        return str(got)
    return True


def _service_account_pull_secrets(namespace: str, sa: str) -> list[str] | None:
    got = _kc.get_raw_json(
        f"/api/v1/namespaces/{namespace}/serviceaccounts/{sa}", f"get ServiceAccount {namespace}/{sa}"
    )
    if isinstance(got, ToolError) or not isinstance(got, dict):
        return None
    return [str(s.get("name")) for s in (got.get("imagePullSecrets") or []) if isinstance(s, dict) and s.get("name")]


def container_ops(obj: dict, change: ContainerChange) -> tuple[list[dict], str, list[str]]:
    """The JSON Patch for a change, the container it applies to, and warnings.

    Mirrors pulse-ui ``podFixes.ts``: a resourceVersion test, a test of the
    container's name at its index, then ``add`` ops that create parents as
    needed (``setResourceOps``), ``.../startupProbe`` copied from liveness, and
    ``imagePullSecrets/-``.
    """
    kind = obj.get("kind", "")
    name = (obj.get("metadata") or {}).get("name", "")
    tpl_spec = _template(obj).get("spec") or {}
    i, c = _pick_container(tpl_spec, change.container, kind, name)
    cname = str(c.get("name"))
    base = f"/spec/template/spec/containers/{i}"
    ops: list[dict] = [*rv_test(obj), {"op": "test", "path": f"{base}/name", "value": cname}]
    warnings: list[str] = []
    sim = copy.deepcopy(c)

    for tier, res in _RESOURCE_ORDER:
        value = change.resources.get((tier, res))
        if value is None:
            continue
        if not sim.get("resources"):
            ops.append({"op": "add", "path": f"{base}/resources", "value": {tier: {res: value}}})
            sim["resources"] = {tier: {res: value}}
        elif not sim["resources"].get(tier):
            ops.append({"op": "add", "path": f"{base}/resources/{tier}", "value": {res: value}})
            sim["resources"][tier] = {res: value}
        else:
            ops.append({"op": "add", "path": f"{base}/resources/{tier}/{res}", "value": value})
            sim["resources"][tier][res] = value
    if change.resources:
        _check_requests_within_limits(sim.get("resources") or {})
        if ("limits", "memory") in change.resources and ("requests", "memory") not in change.resources:
            req = ((c.get("resources") or {}).get("requests") or {}).get("memory")
            if not req:
                warnings.append("The container has no memory request, so Kubernetes sets the request to the new limit.")

    for probe, spec in change.probes.items():
        timings = {k: v for k, v in spec.items() if not k.startswith("_")}
        existing = c.get(probe)
        handler: tuple[str, dict] | None = spec.get("_handler")
        if "_copy" in spec:
            src = c.get(spec["_copy"])
            if not isinstance(src, dict):
                raise WorkloadWriteError(f"{probe}.copyHandlerFrom: container {cname} has no {spec['_copy']}.")
            found = [h for h in _ALL_HANDLERS if h in src]
            if not found:
                raise WorkloadWriteError(f"{spec['_copy']} of container {cname} has no handler to copy.")
            handler = (found[0], copy.deepcopy(src[found[0]]))
        if isinstance(existing, dict):
            if handler:
                for h in _ALL_HANDLERS:
                    if h in existing and h != handler[0]:
                        ops.append({"op": "remove", "path": f"{base}/{probe}/{h}"})
                ops.append({"op": "add", "path": f"{base}/{probe}/{handler[0]}", "value": handler[1]})
            for k, v in timings.items():
                ops.append({"op": "add", "path": f"{base}/{probe}/{k}", "value": v})
        else:
            if not handler:
                raise WorkloadWriteError(
                    f"Container {cname} has no {probe}. To add one, give its check (httpGet, tcpSocket or grpc) or "
                    'copyHandlerFrom another probe, e.g. {"copyHandlerFrom": "livenessProbe", "periodSeconds": 10, '
                    '"failureThreshold": 30}.'
                )
            new_probe: dict[str, Any] = {handler[0]: handler[1]}
            if probe == "startupProbe":
                new_probe["periodSeconds"] = 10  # as the UI's startup-probe fix
            new_probe.update(timings)
            ops.append({"op": "add", "path": f"{base}/{probe}", "value": new_probe})
        merged = {**(existing if isinstance(existing, dict) else {}), **timings}
        if handler:
            merged = {k: v for k, v in merged.items() if k not in _ALL_HANDLERS}
            merged[handler[0]] = handler[1]
        if probe == "startupProbe" and not isinstance(existing, dict):
            merged.setdefault("periodSeconds", 10)
        if probe == "startupProbe":
            budget = int(merged.get("periodSeconds", 10)) * int(merged.get("failureThreshold", 3))
            if budget > MAX_STARTUP_BUDGET_SECONDS:
                raise WorkloadWriteError(
                    f"startupProbe would allow {budget}s to start (periodSeconds x failureThreshold); the cap is "
                    f"{MAX_STARTUP_BUDGET_SECONDS}s, so a real hang is still caught."
                )

    if change.pull_secrets:
        current = [str(s.get("name")) for s in (tpl_spec.get("imagePullSecrets") or []) if isinstance(s, dict)]
        new = [s for s in change.pull_secrets if s not in current]
        if not new:
            if not change.resources and not change.probes:
                raise WorkloadWriteError(
                    f"{kind} {name} already lists {', '.join(change.pull_secrets)}; nothing to change."
                )
        elif current:
            for s in new:
                ops.append({"op": "add", "path": "/spec/template/spec/imagePullSecrets/-", "value": {"name": s}})
        else:
            # A pod that lists imagePullSecrets never gets its ServiceAccount's
            # (admission fills them in only when the pod has none), so keep the
            # ServiceAccount's first: adding one must not drop the others.
            sa = str(tpl_spec.get("serviceAccountName") or "default")
            sa_secrets = _service_account_pull_secrets(obj["metadata"]["namespace"], sa)
            if sa_secrets is None:
                warnings.append(
                    f"ServiceAccount {sa} could not be read: if it lists pull secrets, these pods stop using them "
                    "once the template lists its own."
                )
                sa_secrets = []
            elif sa_secrets:
                warnings.append(
                    f"Also lists ServiceAccount {sa}'s pull secrets ({', '.join(sa_secrets)}) in the template: a pod "
                    "that lists its own imagePullSecrets no longer gets the ServiceAccount's."
                )
            listed = list(dict.fromkeys([*sa_secrets, *new]))
            ops.append(
                {"op": "add", "path": "/spec/template/spec/imagePullSecrets", "value": [{"name": s} for s in listed]}
            )
    return ops, cname, warnings


def _container_summary(change: ContainerChange, kind: str, name: str, cname: str) -> str:
    parts = []
    for (tier, res), v in change.resources.items():
        parts.append(f"{res} {tier[:-1] if tier == 'limits' else 'request'} {v}")
    for probe in change.probes:
        parts.append(probe)
    if change.pull_secrets:
        parts.append("pull secret " + ", ".join(change.pull_secrets))
    return f"Set {', '.join(parts)} on {kind} {name} (container {cname})"


def _container_view(obj: dict, cname: str) -> dict:
    spec = _template(obj).get("spec") or {}
    c = next((x for x in spec.get("containers") or [] if isinstance(x, dict) and x.get("name") == cname), {})
    view: dict[str, Any] = {k: c.get(k) for k in ("resources", *PROBE_FIELDS) if c.get(k) is not None}
    view["imagePullSecrets"] = [s.get("name") for s in spec.get("imagePullSecrets") or [] if isinstance(s, dict)]
    return view


def plan_patch_workload_container(args: dict) -> WritePlan:
    """Read, validate and dry-run a container patch; the preview and the tool both use this."""
    plan = WritePlan(
        tool="patch_workload_container",
        kind=str(args.get("kind") or ""),
        namespace=str(args.get("namespace") or ""),
        name=str(args.get("name") or ""),
    )
    try:
        plan.kind = _validate_target(plan.kind, plan.namespace, plan.name)
        change = parse_container_change(args)
        require_user_writes()
        obj = read_workload(plan.kind, plan.namespace, plan.name)
        plan.gitops = gitops_owner(obj)
        if refusal := _argo_autosync_refusal(plan.namespace, plan.kind, plan.name):
            raise WorkloadWriteError(refusal)
        for secret in change.pull_secrets:
            exists = _secret_exists(plan.namespace, secret)
            if exists is False:
                raise WorkloadWriteError(f"Secret '{secret}' does not exist in namespace {plan.namespace}.")
            if exists is not True:
                raise WorkloadWriteError(f"Pulse could not check that Secret '{secret}' exists: {exists}")
        ops, cname, warnings = container_ops(obj, change)
    except WorkloadWriteError as e:
        plan.error = str(e)
        return plan
    plan.container = cname
    plan.before = obj
    plan.warnings = warnings
    plan.summary = _container_summary(change, plan.kind, plan.name, cname)
    cause = f"Pulse: {plan.summary}" + (f" ({change.reason})" if change.reason else "")
    plan.body = [*ops, change_cause_op(obj, cause)]
    plan.path = workload_path(plan.kind, plan.namespace, plan.name)
    plan.diff_header = f"{plan.kind} {plan.name} · containers[{cname}]"
    plan.impact = rollout_impact(obj)
    plan.undo = undo_text(obj)
    if not rolls_out(obj):
        plan.warnings.append("updateStrategy is OnDelete: running pods keep the old values until they are deleted.")
    local = apply_json_patch(obj, plan.body)
    plan.diff = field_diff(_container_view(obj, cname), _container_view(local, cname))
    _dry_run(plan, obj, lambda before, after: field_diff(_container_view(before, cname), _container_view(after, cname)))
    return plan


@beta_tool
def patch_workload_container(
    kind: str,
    namespace: str,
    name: str,
    container: str = "",
    memory_limit: str = "",
    memory_request: str = "",
    cpu_limit: str = "",
    cpu_request: str = "",
    readiness_probe: str = "",
    liveness_probe: str = "",
    startup_probe: str = "",
    add_image_pull_secrets: str = "",
    reason: str = "",
):
    """Change one container's resources or probes, or add image pull secrets, on a Deployment, StatefulSet or DaemonSet. REQUIRES USER CONFIRMATION.

    Use this instead of apply_yaml to raise a memory/CPU limit (OOMKilled, CPU throttling), right-size
    requests, loosen or add a readiness/liveness/startup probe (killed while starting, flapping readiness),
    or attach a registry pull secret (ImagePullBackOff with an auth error). Only the named fields change:
    a JSON Patch guarded by the object's resourceVersion, dry-run first; the user sees the field diff.
    The pods roll (unless updateStrategy is OnDelete).

    Args:
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Namespace of the workload.
        name: Workload name.
        container: Container to change; may be omitted when the pod template has exactly one.
        memory_limit: New memory limit, e.g. '768Mi' or '2Gi'.
        memory_request: New memory request, e.g. '512Mi'. Must not exceed the limit.
        cpu_limit: New CPU limit, e.g. '500m' or '2'.
        cpu_request: New CPU request, e.g. '250m'.
        readiness_probe: JSON object of fields to set on the readiness probe, e.g. '{"periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 6}'. Timings: initialDelaySeconds, periodSeconds, timeoutSeconds, failureThreshold, successThreshold. A probe the container lacks needs a check: httpGet {path, port}, tcpSocket {port}, grpc {port}, or copyHandlerFrom ('livenessProbe').
        liveness_probe: Same as readiness_probe, for the liveness probe.
        startup_probe: Same, for the startup probe, e.g. '{"copyHandlerFrom": "livenessProbe", "failureThreshold": 30}' to give a slow starter 300s. The start budget (periodSeconds x failureThreshold) is capped at 3600s.
        add_image_pull_secrets: Comma-separated Secret names to add to the pod template's imagePullSecrets. Each must exist in the namespace.
        reason: Why, in a few words; recorded in the kubernetes.io/change-cause annotation.
    """
    args = {
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "container": container,
        "memory_limit": memory_limit,
        "memory_request": memory_request,
        "cpu_limit": cpu_limit,
        "cpu_request": cpu_request,
        "readiness_probe": readiness_probe,
        "liveness_probe": liveness_probe,
        "startup_probe": startup_probe,
        "add_image_pull_secrets": add_image_pull_secrets,
        "reason": reason,
    }
    plan = plan_patch_workload_container(args)
    if plan.error:
        return f"Not changed: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    if plan.before is not None:
        plan.diff = field_diff(_container_view(plan.before, plan.container), _container_view(got, plan.container))
    logger.info("patch_workload_container %s %s/%s container %s", plan.kind, plan.namespace, plan.name, plan.container)
    lines = [
        f"{plan.summary}. Applied.",
        "--- BEGIN CLUSTER DATA ---",
        f"{plan.diff_header}:",
        plan.diff_text(),
        "--- END CLUSTER DATA ---",
        *_footer(plan),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# scale / restart / rollback for all three kinds
# ---------------------------------------------------------------------------


def _hpa_warning(namespace: str, kind: str, name: str) -> str | None:
    got = _kc.get_raw_json(
        f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers", f"list HPAs in {namespace}"
    )
    if isinstance(got, ToolError) or not isinstance(got, dict):
        return None
    for hpa in got.get("items") or []:
        ref = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            spec = hpa.get("spec") or {}
            return (
                f"HorizontalPodAutoscaler {hpa['metadata']['name']} manages the replicas "
                f"({spec.get('minReplicas', 1)}-{spec.get('maxReplicas')}) and will change them back; "
                "change the HPA's bounds instead."
            )
    return None


def plan_scale_workload(args: dict) -> WritePlan:
    plan = WritePlan(
        tool="scale_workload",
        kind=str(args.get("kind") or ""),
        namespace=str(args.get("namespace") or ""),
        name=str(args.get("name") or ""),
    )
    try:
        plan.kind = _validate_target(plan.kind, plan.namespace, plan.name)
        if plan.kind == "DaemonSet":
            raise WorkloadWriteError(
                "A DaemonSet has no replica count: it runs one pod per eligible node. Change its nodeSelector, "
                "affinity or tolerations to change where it runs."
            )
        replicas = args.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            raise WorkloadWriteError("replicas must be a whole number.")
        if not 0 <= replicas <= MAX_REPLICAS:
            raise WorkloadWriteError(f"replicas must be between 0 and {MAX_REPLICAS}, got {replicas}.")
        require_user_writes()
        obj = read_workload(plan.kind, plan.namespace, plan.name)
        plan.gitops = gitops_owner(obj)
        if refusal := _argo_autosync_refusal(plan.namespace, plan.kind, plan.name):
            raise WorkloadWriteError(refusal)
    except WorkloadWriteError as e:
        plan.error = str(e)
        return plan
    current = (obj.get("spec") or {}).get("replicas", 1)
    plan.summary = f"Scale {plan.kind} {plan.name} from {current} to {replicas} replicas"
    plan.path = workload_path(plan.kind, plan.namespace, plan.name) + "/scale"
    plan.patch_type = MERGE_PATCH
    rv = (obj.get("metadata") or {}).get("resourceVersion")
    plan.body = {**({"metadata": {"resourceVersion": rv}} if rv else {}), "spec": {"replicas": replicas}}
    plan.diff_header = f"{plan.kind} {plan.name} · spec.replicas"
    plan.diff = [{"field": "replicas", "before": str(current), "after": str(replicas)}]
    if replicas == 0:
        plan.impact = f"All {current} pod(s) are terminated: the workload stops serving"
    elif replicas > current:
        plan.impact = f"{replicas - current} pod(s) are added" + (
            ", one at a time in ordinal order" if plan.kind == "StatefulSet" else ""
        )
    elif replicas < current:
        plan.impact = f"{current - replicas} pod(s) are terminated" + (
            ", highest ordinal first; their PersistentVolumeClaims are kept" if plan.kind == "StatefulSet" else ""
        )
    else:
        plan.impact = "No change: the workload already has that many replicas"
    plan.undo = f"Scale {plan.kind} {plan.name} back to {current} replicas"
    if hpa := _hpa_warning(plan.namespace, plan.kind, plan.name):
        plan.warnings.append(hpa)
    _dry_run(
        plan,
        current,
        lambda before, after: [
            {"field": "replicas", "before": str(before), "after": str((after.get("spec") or {}).get("replicas"))}
        ],
    )
    return plan


@beta_tool
def scale_workload(kind: str, namespace: str, name: str, replicas: int):
    """Scale a Deployment or StatefulSet to a number of replicas. REQUIRES USER CONFIRMATION.

    DaemonSets have no replica count (one pod per node) and are refused. For a StatefulSet, pods are
    added or removed one at a time by ordinal and removed pods' PVCs are kept.

    Args:
        kind: Deployment or StatefulSet.
        namespace: Namespace of the workload.
        name: Workload name.
        replicas: Desired number of replicas (0-100).
    """
    plan = plan_scale_workload({"kind": kind, "namespace": namespace, "name": name, "replicas": replicas})
    if plan.error:
        return f"Not changed: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    return "\n".join([f"{plan.summary}. Applied.", *_footer(plan)])


def plan_restart_workload(args: dict) -> WritePlan:
    plan = WritePlan(
        tool="restart_workload",
        kind=str(args.get("kind") or ""),
        namespace=str(args.get("namespace") or ""),
        name=str(args.get("name") or ""),
    )
    try:
        plan.kind = _validate_target(plan.kind, plan.namespace, plan.name)
        require_user_writes()
        obj = read_workload(plan.kind, plan.namespace, plan.name)
        if plan.kind == "Deployment" and (obj.get("spec") or {}).get("paused"):
            raise WorkloadWriteError(
                f"Deployment {plan.name} is paused, so a restart would not roll out. Resume it first."
            )
    except WorkloadWriteError as e:
        plan.error = str(e)
        return plan
    plan.gitops = gitops_owner(obj)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = ((_template(obj).get("metadata") or {}).get("annotations") or {}).get("kubectl.kubernetes.io/restartedAt")
    plan.summary = f"Rolling restart of {plan.kind} {plan.name}"
    plan.path = workload_path(plan.kind, plan.namespace, plan.name)
    plan.patch_type = STRATEGIC_PATCH
    # `kubectl rollout restart`, as the UI's rolloutRestart. The annotation is
    # not reverted by Argo CD's default diff, so no auto-sync refusal here.
    plan.body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    plan.diff_header = f"{plan.kind} {plan.name} · spec.template.metadata.annotations"
    plan.diff = [{"field": "kubectl.kubernetes.io/restartedAt", "before": before or "none", "after": now}]
    plan.impact = rollout_impact(obj)
    plan.undo = "Nothing to undo: pods are replaced with the same spec"
    if not rolls_out(obj):
        plan.warnings.append(
            "updateStrategy is OnDelete: no pod restarts until it is deleted; delete pods one at a time to restart."
        )
    _dry_run(plan, before, lambda b, after: plan.diff)
    return plan


@beta_tool
def restart_workload(kind: str, namespace: str, name: str):
    """Rolling restart of a Deployment, StatefulSet or DaemonSet (kubectl rollout restart). REQUIRES USER CONFIRMATION.

    Pods are replaced with the same spec: a Deployment by its strategy, a StatefulSet one at a time from
    the highest ordinal, a DaemonSet one node at a time. A paused Deployment is refused.

    Args:
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Namespace of the workload.
        name: Workload name.
    """
    plan = plan_restart_workload({"kind": kind, "namespace": namespace, "name": name})
    if plan.error:
        return f"Not changed: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    return "\n".join([f"{plan.summary} triggered.", *_footer(plan)])


def _strip_hash_labels(template: dict) -> dict:
    t = copy.deepcopy(template)
    labels = (t.get("metadata") or {}).get("labels")
    if isinstance(labels, dict):
        for k in _HASH_LABELS:
            labels.pop(k, None)
    t.pop("$patch", None)
    return t


def _owned(item: dict, uid: str, kind: str) -> bool:
    return any(
        ref.get("kind") == kind and ref.get("uid") == uid
        for ref in (item.get("metadata") or {}).get("ownerReferences") or []
    )


def _selector(obj: dict) -> str:
    labels = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def workload_revisions(obj: dict) -> list[dict]:
    """[{revision, name, template, current}] oldest first: ReplicaSets or ControllerRevisions owned by uid."""
    kind = obj["kind"]
    ns = obj["metadata"]["namespace"]
    uid = obj["metadata"].get("uid", "")
    query = {"labelSelector": sel} if (sel := _selector(obj)) else None
    out: list[dict] = []
    if kind == "Deployment":
        got = _kc.get_raw_json(f"/apis/apps/v1/namespaces/{ns}/replicasets", f"list ReplicaSets in {ns}", query=query)
        if isinstance(got, ToolError):
            raise WorkloadWriteError(f"Revisions could not be read: {got}")
        cur = ((obj.get("metadata") or {}).get("annotations") or {}).get("deployment.kubernetes.io/revision")
        for rs in got.get("items") or []:
            if not _owned(rs, uid, kind):
                continue
            rev = ((rs.get("metadata") or {}).get("annotations") or {}).get("deployment.kubernetes.io/revision", "0")
            out.append(
                {
                    "revision": int(rev) if str(rev).isdigit() else 0,
                    "name": rs["metadata"]["name"],
                    "template": _template(rs),
                    "current": rev == cur,
                }
            )
    else:
        got = _kc.get_raw_json(
            f"/apis/apps/v1/namespaces/{ns}/controllerrevisions", f"list ControllerRevisions in {ns}", query=query
        )
        if isinstance(got, ToolError):
            raise WorkloadWriteError(f"Revisions could not be read: {got}")
        owned = [cr for cr in got.get("items") or [] if _owned(cr, uid, kind)]
        if kind == "StatefulSet":
            current_name = (obj.get("status") or {}).get("updateRevision") or ""
        else:
            top = max((int(cr.get("revision") or 0) for cr in owned), default=0)
            current_name = next((cr["metadata"]["name"] for cr in owned if int(cr.get("revision") or 0) == top), "")
        for cr in owned:
            data = cr.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            out.append(
                {
                    "revision": int(cr.get("revision") or 0),
                    "name": cr["metadata"]["name"],
                    "template": ((data.get("spec") or {}).get("template")) or {},
                    "current": cr["metadata"]["name"] == current_name,
                }
            )
    out.sort(key=lambda r: r["revision"])
    return out


def template_summary_diff(current: dict, target: dict) -> list[dict]:
    """What a rollback changes in the running template: images first, then any other field."""
    cur = _strip_hash_labels(current)
    tgt = _strip_hash_labels(target)
    # restartedAt differs on every restart and says nothing about the change.
    for t in (cur, tgt):
        ann = (t.get("metadata") or {}).get("annotations")
        if isinstance(ann, dict):
            ann.pop("kubectl.kubernetes.io/restartedAt", None)
    lines = field_diff(cur, tgt, "template", limit=40)
    lines.sort(key=lambda d: (".image" not in d["field"], d["field"]))
    return lines


def plan_rollback_workload(args: dict) -> WritePlan:
    plan = WritePlan(
        tool="rollback_workload",
        kind=str(args.get("kind") or ""),
        namespace=str(args.get("namespace") or ""),
        name=str(args.get("name") or ""),
    )
    try:
        plan.kind = _validate_target(plan.kind, plan.namespace, plan.name)
        revision = args.get("revision", 0) or 0
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise WorkloadWriteError("revision must be a revision number, or 0 for the one before the current.")
        require_user_writes()
        obj = read_workload(plan.kind, plan.namespace, plan.name)
        if plan.kind == "Deployment" and (obj.get("spec") or {}).get("paused"):
            raise WorkloadWriteError(
                f"Deployment {plan.name} is paused: resume it before rolling back, or the template would not roll out."
            )
        plan.gitops = gitops_owner(obj)
        if refusal := _argo_autosync_refusal(plan.namespace, plan.kind, plan.name):
            raise WorkloadWriteError(refusal)
        revs = workload_revisions(obj)
        current = next((r for r in revs if r["current"]), revs[-1] if revs else None)
        if not current:
            raise WorkloadWriteError(f"No revisions found for {plan.kind} {plan.namespace}/{plan.name}.")
        if revision:
            target = next((r for r in revs if r["revision"] == revision), None)
            if not target:
                avail = ", ".join(str(r["revision"]) for r in revs)
                raise WorkloadWriteError(f"Revision {revision} not found. Available: {avail}.")
            if target is current:
                raise WorkloadWriteError(f"Revision {revision} is the current one; nothing to roll back to.")
        else:
            older = [r for r in revs if r["revision"] < current["revision"] and r["template"]]
            if not older:
                raise WorkloadWriteError(f"No revision before {current['revision']} to roll back to.")
            target = older[-1]
        if not target["template"]:
            raise WorkloadWriteError(f"Revision {target['revision']} has no pod template to restore.")
    except WorkloadWriteError as e:
        plan.error = str(e)
        return plan
    template = _strip_hash_labels(target["template"])
    plan.summary = f"Roll back {plan.kind} {plan.name} to revision {target['revision']}"
    plan.path = workload_path(plan.kind, plan.namespace, plan.name)
    cause = f"Pulse: {plan.summary}"
    # As the UI's Rollout History: replace the template exactly, guarded by resourceVersion.
    plan.body = [
        *rv_test(obj),
        {"op": "replace", "path": "/spec/template", "value": template},
        change_cause_op(obj, cause),
    ]
    plan.diff_header = (
        f"{plan.kind} {plan.name} · spec.template (revision {current['revision']} -> {target['revision']})"
    )
    plan.diff = template_summary_diff(_template(obj), template)
    plan.impact = rollout_impact(obj)
    plan.undo = f"Roll forward again: rollback_workload to revision {current['revision']}'s template (it becomes a new revision)"
    if not rolls_out(obj):
        plan.warnings.append("updateStrategy is OnDelete: running pods keep the current template until deleted.")
    _dry_run(plan, obj, lambda before, after: template_summary_diff(_template(before), _template(after)))
    return plan


@beta_tool
def rollback_workload(kind: str, namespace: str, name: str, revision: int = 0):
    """Roll a Deployment, StatefulSet or DaemonSet back to an earlier revision's pod template. REQUIRES USER CONFIRMATION.

    Uses the Deployment's ReplicaSets or the StatefulSet's/DaemonSet's ControllerRevisions (see
    get_rollout_history for the revisions and their images). The template is replaced exactly, guarded by
    the object's resourceVersion; the user sees what changes (images first).

    Args:
        kind: Deployment, StatefulSet or DaemonSet.
        namespace: Namespace of the workload.
        name: Workload name.
        revision: Revision to restore; 0 means the one before the current revision.
    """
    plan = plan_rollback_workload({"kind": kind, "namespace": namespace, "name": name, "revision": revision})
    if plan.error:
        return f"Not changed: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    return "\n".join(
        [
            f"{plan.summary}. Applied.",
            "--- BEGIN CLUSTER DATA ---",
            f"{plan.diff_header}:",
            plan.diff_text(),
            "--- END CLUSTER DATA ---",
            *_footer(plan),
        ]
    )


# ---------------------------------------------------------------------------
# Confirm previews
# ---------------------------------------------------------------------------


def _preview(planner):
    def build(args: dict) -> dict[str, Any]:
        return planner(args).to_preview()

    return build


PREVIEW_BUILDERS = {
    "patch_workload_container": _preview(plan_patch_workload_container),
    "scale_workload": _preview(plan_scale_workload),
    "restart_workload": _preview(plan_restart_workload),
    "rollback_workload": _preview(plan_rollback_workload),
}

WORKLOAD_WRITE_TOOLS = [patch_workload_container, scale_workload, restart_workload, rollback_workload]
