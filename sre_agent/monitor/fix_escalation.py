"""After a fix that did not hold: stop, and tell a person why in plain English.

Measured on a live cluster at trust 3: 440 auto-fix actions in a week, mostly
the same fix repeated on an ImagePullBackOff app with a bad image and a
crash-looping app with a code or config bug. ``fix_breaker`` stops the loop;
this module decides what the person is told when it does.

One inbox item per condition (the breaker's escalation key, which is category
+ namespace + workload with generated pod suffixes stripped). It says:

- what Pulse tried, how many times, and how each attempt ended (from the
  actions table: measured, not remembered);
- why restarting cannot fix it, by cause, from the pods as they are now: the
  image and the registry's pull error, the container's exit code and the last
  error line of its previous run (credentials masked), the Secret or
  ConfigMap that does not exist, the memory limit against the measured peak;
- who probably owns the workload, from labels and annotations already on it
  or on its namespace, naming where each one came from. Nothing found is
  "unknown", never a guess;
- the next step for that cause, pointing at a git change when Argo CD or Helm
  manages the workload, since a direct edit would be reverted.

Raised in two places: when verification says an applied fix did not hold
(``still_failing``, or ``verified_then_recurred`` inside the recurrence window),
and when the breaker trips on the attempt count. While the item is open the
breaker keeps auto-fix off the target (``fix_breaker.check``).

The item resolves itself once the condition has stopped being reported and
stayed gone for the recurrence window (``resolve_held``): a condition that
clears for one scan and returns is the same incident, not a fix.

Everything here is read-only. Nothing in this module changes the cluster.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..k8s_client import get_apps_client, get_core_client

logger = logging.getLogger("pulse_agent.monitor")

CAUSE_IMAGE_PULL = "image_pull"
CAUSE_CRASHLOOP = "crashloop"
CAUSE_CONFIG = "config_missing"
CAUSE_OOM = "oom"
CAUSE_UNKNOWN = "unknown"

# Labels or annotations that name a person or team, on the workload or its
# namespace. Exact keys only: a near miss is not evidence of ownership.
OWNER_KEYS = ("owner", "team")
MANAGED_BY_KEY = "app.kubernetes.io/managed-by"
REQUESTER_ANNOTATION = "openshift.io/requester"

_PULL_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"})
_CONFIG_REASONS = frozenset({"CreateContainerConfigError"})
_AUTH_PULL = re.compile(r"unauthori[sz]ed|authentication required|access denied|denied|forbidden|403", re.I)
_MISSING_OBJECT = re.compile(r'(secret|configmap)s?\s+"([^"]+)"\s+not found', re.I)
_MISSING_KEY = re.compile(r"couldn't find key (\S+) in (Secret|ConfigMap) ([^\s/]+)/(\S+)", re.I)
_ERROR_LINE = re.compile(
    r"\b(error|exception|fatal|panic|traceback|failed|failure|cannot|could not|unable to|refused|denied|"
    r"not found|no such|invalid|missing|undefined)\b",
    re.I,
)
_CONTROL = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


# ── Reading the workload ───────────────────────────────────────────────────


@dataclass
class Workload:
    """The thing a person would change, and its pods as they are now."""

    kind: str
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    service_account: str = ""
    pods: list[Any] = field(default_factory=list)
    found: bool = False
    read_error: str = ""

    @property
    def ref(self) -> str:
        return f"{self.kind} {self.namespace}/{self.name}"


_READERS = {
    "Deployment": "read_namespaced_deployment",
    "StatefulSet": "read_namespaced_stateful_set",
    "DaemonSet": "read_namespaced_daemon_set",
}


def _meta(obj) -> tuple[dict[str, str], dict[str, str]]:
    md = getattr(obj, "metadata", None)
    return dict(getattr(md, "labels", None) or {}), dict(getattr(md, "annotations", None) or {})


def _controller(obj):
    for ref in getattr(getattr(obj, "metadata", None), "owner_references", None) or []:
        if getattr(ref, "controller", None) is True:
            return ref
    return None


def _pods_of(obj, namespace: str) -> list[Any]:
    selector = getattr(getattr(getattr(obj, "spec", None), "selector", None), "match_labels", None) or {}
    if not selector:
        return []
    label_selector = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    return list(
        getattr(get_core_client().list_namespaced_pod(namespace, label_selector=label_selector), "items", None) or []
    )


def _workload_from(kind: str, obj, namespace: str) -> Workload:
    labels, annotations = _meta(obj)
    tmpl_spec = getattr(getattr(getattr(obj, "spec", None), "template", None), "spec", None)
    w = Workload(
        kind=kind,
        name=getattr(obj.metadata, "name", "") or "",
        namespace=namespace,
        labels=labels,
        annotations=annotations,
        service_account=getattr(tmpl_spec, "service_account_name", "") or "",
        found=True,
    )
    try:
        w.pods = _pods_of(obj, namespace)
    except Exception as e:
        w.read_error = f"its pods could not be listed ({type(e).__name__})"
    return w


def _read_workload(kind: str, name: str, namespace: str):
    reader = getattr(get_apps_client(), _READERS[kind])
    return reader(name, namespace)


def resolve_workload(resource: dict) -> Workload:
    """The owning workload of the finding's resource, and its current pods. Never raises."""
    kind = str(resource.get("kind") or "")
    name = str(resource.get("name") or "")
    ns = str(resource.get("namespace") or "")
    if not name or not ns:
        return Workload(kind=kind or "resource", name=name, namespace=ns, read_error="the finding names no resource")
    try:
        if kind in _READERS:
            return _workload_from(kind, _read_workload(kind, name, ns), ns)
        if kind != "Pod":
            return Workload(kind=kind, name=name, namespace=ns, read_error=f"{kind} is not a workload Pulse can read")
        try:
            pod = get_core_client().read_namespaced_pod(name, ns)
        except Exception as e:
            if getattr(e, "status", None) != 404:
                raise
            pod = None
        if pod is not None:
            owner = _controller(pod)
            if owner is not None and owner.kind == "ReplicaSet":
                rs = get_apps_client().read_namespaced_replica_set(owner.name, ns)
                dep = _controller(rs)
                if dep is not None and dep.kind == "Deployment":
                    return _workload_from("Deployment", _read_workload("Deployment", dep.name, ns), ns)
            elif owner is not None and owner.kind in _READERS:
                return _workload_from(owner.kind, _read_workload(owner.kind, owner.name, ns), ns)
            labels, annotations = _meta(pod)
            return Workload(
                kind="Pod",
                name=name,
                namespace=ns,
                labels=labels,
                annotations=annotations,
                service_account=getattr(getattr(pod, "spec", None), "service_account_name", "") or "",
                pods=[pod],
                found=True,
            )
        # The pod a fix deleted is gone; its replacement has a new name. The
        # stripped name is the controller's name for Deployments and DaemonSets.
        from .confidence import _strip_pod_hash

        base = _strip_pod_hash(name)
        for candidate in ("Deployment", "StatefulSet", "DaemonSet"):
            try:
                return _workload_from(candidate, _read_workload(candidate, base, ns), ns)
            except Exception as e:
                if getattr(e, "status", None) != 404:
                    raise
        return Workload(kind="Pod", name=name, namespace=ns, read_error="the pod and its workload no longer exist")
    except Exception as e:
        return Workload(
            kind=kind or "resource", name=name, namespace=ns, read_error=f"could not be read ({type(e).__name__})"
        )


# ── Why restarting cannot fix it ───────────────────────────────────────────


@dataclass
class Cause:
    kind: str
    headline: str
    why: str
    next_step: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _statuses(pod) -> list[Any]:
    status = getattr(pod, "status", None)
    return list(getattr(status, "init_container_statuses", None) or []) + list(
        getattr(status, "container_statuses", None) or []
    )


def _spec_container(pod, name: str):
    spec = getattr(pod, "spec", None)
    for c in list(getattr(spec, "init_containers", None) or []) + list(getattr(spec, "containers", None) or []):
        if getattr(c, "name", None) == name:
            return c
    return None


def _clean(text: str, limit: int = 300) -> str:
    from ..credential_redaction import redact_credentials

    out = _CONTROL.sub("", str(text or "")).strip()
    out = redact_credentials(out)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def last_error_line(log_text: str) -> str:
    """The last line that reads like an error, else the last non-empty line, sanitized."""
    lines = [ln for ln in (_CONTROL.sub("", raw).strip() for raw in (log_text or "").splitlines()) if ln]
    if not lines:
        return ""
    for ln in reversed(lines):
        if _ERROR_LINE.search(ln):
            return _clean(ln)
    return _clean(lines[-1])


def _previous_log_line(pod_name: str, namespace: str, container: str) -> tuple[str, str]:
    """``(line, problem)``: the last error line of the previous run, or why it is missing."""
    try:
        text = get_core_client().read_namespaced_pod_log(
            pod_name, namespace, container=container, previous=True, tail_lines=100
        )
    except Exception as e:
        status = getattr(e, "status", None)
        return "", f"its previous logs could not be read ({status or type(e).__name__})"
    line = last_error_line(text if isinstance(text, str) else "")
    return (line, "") if line else ("", "its previous run wrote no log lines")


_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def _quantity_bytes(q: str) -> float | None:
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT]i?)?\s*", str(q or ""))
    if not m:
        return None
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2) or "", 1)
    except ValueError:
        return None


def _mib(n: float) -> str:
    return f"{n / 1024**2:.0f} MiB"


def _peak_memory(namespace: str, pod_prefix: str, container: str) -> tuple[float | None, str]:
    """Highest working set over 24h for this workload's container, from Prometheus."""
    query = (
        f'max(max_over_time(container_memory_working_set_bytes{{namespace="{namespace}",'
        f'pod=~"{re.escape(pod_prefix)}-.*",container="{container}"}}[24h]))'
    )
    try:
        from ..prometheus import prometheus_request

        data = prometheus_request("api/v1/query", {"query": query}, timeout=10)
        result = (data.get("data") or {}).get("result") or []
        if not result:
            return None, "Prometheus has no memory samples for it"
        return float(result[0]["value"][1]), ""
    except Exception as e:
        return None, f"Prometheus could not be read ({type(e).__name__})"


def _image_pull_cause(pod, cs, namespace: str) -> Cause:
    from .fix_planner import image_pull_cause

    pod_name = pod.metadata.name
    image = getattr(cs, "image", "") or getattr(_spec_container(pod, cs.name), "image", "") or "?"
    verdict, pull_error = image_pull_cause({"kind": "Pod", "name": pod_name, "namespace": namespace})
    pull_error = _clean(pull_error, 400)
    evidence = {"image": image, "container": cs.name, "pod": pod_name, "pull_error": pull_error, "verdict": verdict}
    if _AUTH_PULL.search(pull_error):
        evidence["subtype"] = "registry_auth"
        return Cause(
            CAUSE_IMAGE_PULL,
            "the registry refuses the pull credentials",
            f"The registry refused to serve image {image} to this workload: {pull_error}. "
            "A restarted pod presents the same pull secret (or none), so it is refused the same way.",
            "Create or fix the registry pull secret and reference it from the workload's imagePullSecrets "
            "(or link it to its service account).",
            evidence,
        )
    evidence["subtype"] = "image_reference"
    return Cause(
        CAUSE_IMAGE_PULL,
        "the image cannot be pulled",
        f"Image {image} cannot be pulled: {pull_error}. A restarted pod asks for the same image reference "
        "and fails the same way until the reference or the registry changes.",
        f"Fix the image reference (tag or repository) of container {cs.name}, or push the missing image to the registry.",
        evidence,
    )


def _config_cause(pod, cs, waiting) -> Cause:
    message = _clean(getattr(waiting, "message", "") or "", 300)
    missing = ""
    m = _MISSING_OBJECT.search(message)
    if m:
        missing = f"{'Secret' if m.group(1).lower() == 'secret' else 'ConfigMap'} {m.group(2)}"
    else:
        k = _MISSING_KEY.search(message)
        if k:
            missing = f"key {k.group(1)} in {k.group(2)} {k.group(4)}"
    what = missing or "configuration it references"
    return Cause(
        CAUSE_CONFIG,
        f"{missing or 'a Secret or ConfigMap'} it needs does not exist",
        f"Kubernetes cannot create container {cs.name}: {message or 'CreateContainerConfigError'}. "
        "Restarting does not create the missing object.",
        f"Create the {what}{' in namespace ' + pod.metadata.namespace if getattr(pod.metadata, 'namespace', '') else ''}, "
        "or fix the reference in the workload.",
        {"container": cs.name, "pod": pod.metadata.name, "message": message, "missing": missing},
    )


def _oom_cause(pod, cs, workload: Workload) -> Cause:
    spec = _spec_container(pod, cs.name)
    limits = getattr(getattr(spec, "resources", None), "limits", None) or {}
    limit_q = str(limits.get("memory") or "")
    limit_b = _quantity_bytes(limit_q) if limit_q else None
    prefix = workload.name if workload.kind != "Pod" else pod.metadata.name
    peak, peak_problem = _peak_memory(workload.namespace, prefix, cs.name)
    evidence: dict[str, Any] = {"container": cs.name, "pod": pod.metadata.name, "limit": limit_q or None}
    if peak is not None:
        evidence["peak_bytes"] = int(peak)
        evidence["peak"] = _mib(peak)
    else:
        evidence["peak"] = None
        evidence["peak_unknown_because"] = peak_problem
    peak_text = (
        f"its highest measured use in 24h is {_mib(peak)}"
        if peak is not None
        else f"its peak is unknown: {peak_problem}"
    )
    if limit_b:
        limit_text = f"its memory limit is {limit_q} ({_mib(limit_b)})"
        why = (
            f"Container {cs.name} was killed for using more memory than its limit (OOMKilled): {limit_text}, {peak_text}. "
            "A restart gives it the same limit, so it is killed again under the same load."
        )
    else:
        why = (
            f"Container {cs.name} was OOMKilled with no memory limit set, so the node ran short of memory; {peak_text}. "
            "A restart does not change how much it uses."
        )
    target = f"{workload.kind} {workload.namespace}/{workload.name}"
    return Cause(
        CAUSE_OOM,
        "it runs out of memory",
        why,
        f"Raise the memory limit of container {cs.name} above the measured peak: ask Pulse in chat to "
        f"patch_workload_container on {target} (it shows a dry-run diff and asks before changing anything), "
        "or find out why it uses that much memory.",
        evidence,
    )


def _crash_cause(pod, cs, terminated, namespace: str) -> Cause:
    exit_code = getattr(terminated, "exit_code", None)
    reason = getattr(terminated, "reason", "") or ""
    line, problem = _previous_log_line(pod.metadata.name, namespace, cs.name)
    if not line and getattr(terminated, "message", None):
        line, problem = _clean(terminated.message), ""
    evidence = {
        "container": cs.name,
        "pod": pod.metadata.name,
        "exit_code": exit_code,
        "reason": reason,
        "last_error": line or None,
    }
    if problem:
        evidence["last_error_unknown_because"] = problem
    said = f'Its last error line was: "{line}".' if line else f"The error line is unknown: {problem}."
    return Cause(
        CAUSE_CRASHLOOP,
        f"container {cs.name} exits with code {exit_code}",
        f"Container {cs.name} exits with code {exit_code}{f' ({reason})' if reason else ''} each time it starts. {said} "
        "A restart runs the same code with the same configuration, so it exits the same way.",
        f"Fix the code or configuration that makes container {cs.name} exit; its previous logs show why "
        f"(ask Pulse in chat to show them for pod {pod.metadata.name}).",
        evidence,
    )


def explain_cause(finding: dict, workload: Workload) -> Cause:
    """Why restarting cannot fix this, from the workload's pods as they are now."""
    ns = workload.namespace
    found: dict[str, Cause] = {}
    for pod in workload.pods:
        for cs in _statuses(pod):
            state = getattr(cs, "state", None)
            waiting = getattr(state, "waiting", None)
            reason = getattr(waiting, "reason", "") or ""
            if reason in _PULL_REASONS and CAUSE_IMAGE_PULL not in found:
                found[CAUSE_IMAGE_PULL] = _image_pull_cause(pod, cs, ns)
            elif reason in _CONFIG_REASONS and CAUSE_CONFIG not in found:
                found[CAUSE_CONFIG] = _config_cause(pod, cs, waiting)
            terminated = getattr(getattr(cs, "last_state", None), "terminated", None) or getattr(
                state, "terminated", None
            )
            if terminated is None:
                continue
            if getattr(terminated, "reason", "") == "OOMKilled":
                if CAUSE_OOM not in found:
                    found[CAUSE_OOM] = _oom_cause(pod, cs, workload)
            elif (getattr(terminated, "exit_code", 0) or 0) != 0 and CAUSE_CRASHLOOP not in found:
                found[CAUSE_CRASHLOOP] = _crash_cause(pod, cs, terminated, ns)
    # The most specific explanation first: a pod that cannot start never gets
    # to crash, and an OOM kill is also a non-zero exit.
    for kind in (CAUSE_IMAGE_PULL, CAUSE_CONFIG, CAUSE_OOM, CAUSE_CRASHLOOP):
        if kind in found:
            return found[kind]

    category = finding.get("category", "")
    if workload.read_error:
        seen = f"Pulse could not read the current state of {workload.ref}: {workload.read_error}"
    elif not workload.pods:
        seen = f"{workload.ref} has no pods Pulse could inspect"
    else:
        seen = f"the pods of {workload.ref} show no pull, configuration, memory or exit error Pulse can name"
    return Cause(
        CAUSE_UNKNOWN,
        "the cause is not one Pulse can name",
        f"The same {category or 'fix'} condition came back after every fix, and {seen}. "
        "Repeating a fix that has not held will not make it hold.",
        f"Look at the events and logs of {workload.ref}, or ask Pulse in chat to investigate it.",
        {"read_error": workload.read_error or None, "pods_seen": len(workload.pods)},
    )


# ── Who owns it ─────────────────────────────────────────────────────────────


def _argo_source(app: str) -> dict[str, str]:
    """Repo, path and revision of an Argo CD Application, via the git tool's reader."""
    from ..git_tools import get_argo_app_source

    candidates = [(app, "openshift-gitops")]
    if "_" in app:
        # Apps in any namespace are tracked as "<namespace>_<name>".
        app_ns, app_name = app.split("_", 1)
        candidates.append((app_name, app_ns))
    for name, ns in candidates:
        try:
            raw = get_argo_app_source.func(name, ns)
            data = json.loads(raw) if isinstance(raw, str) and raw.lstrip().startswith("{") else None
        except Exception:
            data = None
        if data:
            return {k: str(data.get(k) or "") for k in ("repo_url", "path", "target_revision")} | {
                "app_namespace": ns,
                "app_name": name,
            }
    return {}


def find_owners(workload: Workload) -> dict[str, Any]:
    """Who probably owns the workload, from metadata already on it or its namespace.

    ``people``: owner/team/requester values with where each was read.
    ``managed_by``: the tool that writes the spec (Argo CD, Helm, a label).
    ``unknown``: true when no person or team is recorded anywhere.
    """
    people: list[dict[str, str]] = []
    managed: list[dict[str, str]] = []
    problems: list[str] = []
    where = workload.ref

    if workload.found:
        for key in OWNER_KEYS:
            if workload.labels.get(key):
                people.append({"key": key, "value": workload.labels[key], "source": f"label {key} on {where}"})
            if workload.annotations.get(key):
                people.append(
                    {"key": key, "value": workload.annotations[key], "source": f"annotation {key} on {where}"}
                )
        from ..k8s_tools.workload_writes import gitops_owner

        gitops = gitops_owner({"metadata": {"labels": workload.labels, "annotations": workload.annotations}})
        if gitops:
            entry = {
                "tool": gitops["tool"],
                "app": gitops.get("app", ""),
                "source": f"{gitops['detail']} on {where}",
            }
            if gitops["tool"] == "Argo CD" and gitops.get("app"):
                entry.update(_argo_source(gitops["app"]))
            managed.append(entry)
        elif workload.labels.get(MANAGED_BY_KEY):
            managed.append(
                {"tool": workload.labels[MANAGED_BY_KEY], "app": "", "source": f"label {MANAGED_BY_KEY} on {where}"}
            )
    else:
        problems.append(f"{where} could not be read")

    if workload.namespace:
        try:
            ns_obj = get_core_client().read_namespace(workload.namespace)
            ns_labels, ns_ann = _meta(ns_obj)
            ns_where = f"namespace {workload.namespace}"
            requester = ns_ann.get(REQUESTER_ANNOTATION)
            if requester:
                people.append(
                    {
                        "key": "requester",
                        "value": requester,
                        "source": f"annotation {REQUESTER_ANNOTATION} on {ns_where}",
                    }
                )
            for key in OWNER_KEYS:
                if ns_labels.get(key):
                    people.append({"key": key, "value": ns_labels[key], "source": f"label {key} on {ns_where}"})
                if ns_ann.get(key):
                    people.append({"key": key, "value": ns_ann[key], "source": f"annotation {key} on {ns_where}"})
        except Exception as e:
            problems.append(f"namespace {workload.namespace} could not be read ({type(e).__name__})")

    return {"people": people, "managed_by": managed, "unknown": not people, "unreadable": problems}


# ── The item ──────────────────────────────────────────────────────────────


def _when(ts_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return "?"


def _attempt_line(a: dict) -> str:
    verdict = a.get("verification_status") or "not verified"
    line = f"{_when(a.get('timestamp'))}: {a.get('tool') or '?'} — {a.get('status') or '?'}, {verdict}"
    detail = a.get("error") or a.get("verification_evidence") or ""
    if detail:
        line += f" ({_clean(str(detail), 140)})"
    return line


def _tried_sentence(attempts: list[dict]) -> str:
    if not attempts:
        return "Pulse has no record of the fix attempts (the fix history could not be read)."
    tools: dict[str, int] = {}
    for a in attempts:
        tools[str(a.get("tool") or "a fix")] = tools.get(str(a.get("tool") or "a fix"), 0) + 1
    tried = ", ".join(f"{t} {n} time{'s' if n != 1 else ''}" for t, n in tools.items())
    held = sum(1 for a in attempts if a.get("verification_status") == "verified")
    n = len(attempts)
    if held:
        return f"Pulse tried {tried}. {held} of {n} looked healthy at first and the condition came back."
    return f"Pulse tried {tried}. {'It' if n == 1 else 'None of them'} did not hold."


def _gitops_step(owners: dict[str, Any], cause: Cause) -> str:
    for m in owners.get("managed_by") or []:
        if m.get("tool") == "Argo CD":
            where = ""
            if m.get("repo_url"):
                where = f" ({m['repo_url']}{', path ' + m['path'] if m.get('path') else ''})"
            return (
                f"This workload is managed by Argo CD application '{m.get('app', '')}'{where}: make the change in git "
                "(Pulse can draft it with propose_git_change in chat). A direct edit would be reverted on the next sync."
            )
        if m.get("tool") == "Helm":
            return (
                f"This workload is managed by Helm release '{m.get('app', '')}': make the change in the release's "
                "values, or the next helm upgrade undoes it."
            )
    return ""


def build_item(corr_key: str, finding: dict, attempts: list[dict], reason: str, workload: Workload) -> dict:
    """The inbox item for one condition whose fix did not hold."""
    from .fix_breaker import ESCALATION_CREATOR, escalation_key, limits

    cause = explain_cause(finding, workload)
    owners = find_owners(workload)
    gitops_step = _gitops_step(owners, cause)
    next_step = cause.next_step + (f" {gitops_step}" if gitops_step else "")

    if owners["people"]:
        owner_lines = [f"{p['value']} ({p['source']})" for p in owners["people"]]
    else:
        searched = f"no owner or team label/annotation on {workload.ref}, and no {REQUESTER_ANNOTATION} on namespace {workload.namespace}"
        if owners["unreadable"]:
            searched += f"; {'; '.join(owners['unreadable'])}"
        owner_lines = [f"unknown owner — {searched}"]
    for m in owners["managed_by"]:
        owner_lines.append(f"managed by {m['tool']}{' ' + repr(m['app']) if m.get('app') else ''} ({m['source']})")

    resources = finding.get("resources") or []
    r0 = resources[0] if resources else {}
    _, window_s = limits()
    target = workload.ref
    summary = (
        f"{_tried_sentence(attempts)} Auto-fix is off for {target} while this item is open ({reason}).\n\n"
        f"Why restarting can't fix it: {cause.why}\n\n"
        f"Likely owner:\n- " + "\n- ".join(owner_lines) + "\n\n"
        f"Next step: {next_step}\n\n"
        "This item resolves itself once the condition has stayed gone for the recurrence window."
    )
    if attempts:
        summary += "\n\nAttempts (newest first):\n- " + "\n- ".join(_attempt_line(a) for a in attempts)

    return {
        "item_type": "task",
        "title": f"Auto-fix stopped on {target}: {cause.headline}",
        "summary": summary,
        "severity": finding.get("severity") or "warning",
        "confidence": 1.0,
        "noise_score": 0,
        "namespace": r0.get("namespace") or workload.namespace or None,
        "resources": resources,
        "correlation_key": escalation_key(corr_key),
        # Not system:monitor: inbox reconciliation resolves monitor items whose
        # key is not a live finding key, and untouched-item expiry archives
        # them after 48h. This one resolves by resolve_held below.
        "created_by": ESCALATION_CREATOR,
        "metadata": {
            "source": "fix_breaker",
            "target": corr_key,
            "workload": {"kind": workload.kind, "name": workload.name, "namespace": workload.namespace},
            "finding_title": finding.get("title", ""),
            "window_seconds": window_s,
            "reason": reason,
            "cause": cause.kind,
            "cause_evidence": cause.evidence,
            "why": cause.why,
            "next_step": next_step,
            "owners": owners,
            "attempt_count": len(attempts),
            "attempts": [
                {
                    "action_id": a.get("id"),
                    "timestamp": a.get("timestamp"),
                    "tool": a.get("tool"),
                    "status": a.get("status"),
                    "verification_status": a.get("verification_status"),
                    "error": _clean(str(a.get("error") or ""), 300),
                }
                for a in attempts
            ],
            # Set by resolve_held while the condition is not being reported.
            "cleared_at": None,
        },
    }


def raise_item(item: dict) -> str:
    """Create the escalation, or refresh the one already open. Returns its id, or ""."""
    from ..inbox import create_inbox_item, upsert_inbox_item
    from ..repositories.inbox_repo import get_inbox_repo

    repo = get_inbox_repo()
    key = item["correlation_key"]
    now = int(time.time())
    if repo.is_muted(key, now):
        return ""
    if repo.find_active_by_correlation(key, item["item_type"]):
        item_id = upsert_inbox_item(item)
        if item_id:
            repo.merge_metadata(item_id, item["metadata"], now)
        return item_id
    # Not upsert_inbox_item's "recently resolved" shortcut: that returns a
    # resolved item's id, and with nothing open the breaker would let auto-fix
    # start over on a condition it just failed to fix.
    return create_inbox_item(item)


def escalate(corr_key: str, finding: dict, attempts: list[dict] | None, reason: str) -> str:
    """Raise (or refresh) the one item for a condition whose fix did not hold. Never raises."""
    if not corr_key:
        return ""
    if attempts is None:
        try:
            from ..repositories.monitor_repo import get_monitor_repo
            from .fix_breaker import limits

            _, window_s = limits()
            attempts = get_monitor_repo().recent_fix_attempts(corr_key, int(time.time() * 1000) - window_s * 1000)
        except Exception:
            logger.warning("Fix escalation: could not read the fix history for %s", corr_key, exc_info=True)
            attempts = []
    resources = finding.get("resources") or []
    try:
        workload = resolve_workload(resources[0] if resources else {})
        item = build_item(corr_key, finding, attempts, reason, workload)
        item_id = raise_item(item)
    except Exception:
        logger.warning("Fix escalation: could not raise the inbox item for %s", corr_key, exc_info=True)
        return ""
    logger.warning("Fix escalated for %s: %s (inbox %s)", corr_key, reason, item_id or "-")
    return item_id


def resolve_held(live_keys: set[str], now: int | None = None) -> int:
    """Resolve escalations whose condition has stayed gone for the recurrence window.

    ``live_keys`` are the correlation keys of every finding still standing,
    including those of scanners that skipped or failed this scan (only the
    owning scanner resolves a finding), so absence is meaningful. A condition
    that comes back before the window ends resets the clock. Returns the
    number resolved.
    """
    from ..config import get_settings
    from ..inbox import _publish_event
    from ..repositories.inbox_repo import get_inbox_repo
    from .fix_breaker import ESCALATION_CREATOR, ESCALATION_PREFIX

    now = int(now if now is not None else time.time())
    window = max(0, int(get_settings().monitor.recurrence_window))
    repo = get_inbox_repo()
    resolved = 0
    for row in repo.fetch_open_by_creator(ESCALATION_CREATOR):
        key = str(row.get("correlation_key") or "")
        if not key.startswith(ESCALATION_PREFIX):
            continue
        target = key[len(ESCALATION_PREFIX) :]
        md = row.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md) if md else {}
            except ValueError:
                md = {}
        md = md if isinstance(md, dict) else {}
        cleared_at = md.get("cleared_at")
        if target in live_keys:
            if cleared_at:
                repo.merge_metadata(row["id"], {"cleared_at": None}, now)
            continue
        if not cleared_at:
            cleared_at = now
            if window > 0:
                repo.merge_metadata(row["id"], {"cleared_at": now}, now)
                continue
        if now - int(cleared_at) < window:
            continue
        minutes = max(1, window // 60) if window else 0
        md.update(
            cleared_at=cleared_at,
            resolved_reason=(
                f"the condition stopped being reported and stayed gone for {minutes} min"
                if window
                else "the condition stopped being reported"
            ),
        )
        repo.resolve_item(row["id"], now, md)
        _publish_event("inbox_item_resolved", row["id"], {"resolved_at": now})
        resolved += 1
        logger.info("Fix escalation %s resolved: %s held", row["id"], target)
    return resolved
