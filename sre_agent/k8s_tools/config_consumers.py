"""Which workloads use a ConfigMap or Secret, and restarting the ones that need it.

The scan is the same as pulse-ui's "Used by" section
(``src/kubeview/engine/configConsumers.ts``), so the agent and the page give
the same answer: every Deployment, StatefulSet, DaemonSet, DeploymentConfig,
CronJob and Job pod template in the namespace (Jobs a CronJob created are
covered by the CronJob), matched through env ``valueFrom``, ``envFrom``,
volumes, projected volumes, image pull secrets (in the template or on the
pod's ServiceAccount) and volume driver credentials. A kind that could not be
listed is named as not checked; the answer is never presented as complete
when it is not.

Kubernetes facts it relies on:

- env and envFrom are resolved when a container starts: running pods see a
  change only after a restart. So does a volume mounted with subPath.
- Other ConfigMap/Secret volumes are refreshed by the kubelet (about a
  minute); whether the process re-reads the file is the application's.
- An image pull secret is used at the next pull; a volume driver credential
  when the volume is next mounted. A CronJob's next run, or a re-run Job,
  starts with the new values.

``restart_consumers`` restarts the Deployments, StatefulSets and DaemonSets
whose running pods keep the old values until restarted (the UI's "Restart
consumers" targets), with the template annotation ``oc rollout restart``
writes. Secrets: only names and key names are read from pod templates; no
Secret is ever read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from .validators import _validate_k8s_name, _validate_k8s_namespace

logger = logging.getLogger("pulse_agent.k8s_tools")

CONFIG_KINDS = ("ConfigMap", "Secret")
CONSUMER_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "DeploymentConfig", "CronJob", "Job")
_LIST_PATHS = {
    "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments",
    "StatefulSet": "/apis/apps/v1/namespaces/{ns}/statefulsets",
    "DaemonSet": "/apis/apps/v1/namespaces/{ns}/daemonsets",
    "DeploymentConfig": "/apis/apps.openshift.io/v1/namespaces/{ns}/deploymentconfigs",
    "CronJob": "/apis/batch/v1/namespaces/{ns}/cronjobs",
    "Job": "/apis/batch/v1/namespaces/{ns}/jobs",
}
_PLURAL = {
    "Deployment": "Deployments",
    "StatefulSet": "StatefulSets",
    "DaemonSet": "DaemonSets",
    "DeploymentConfig": "DeploymentConfigs",
    "CronJob": "CronJobs",
    "Job": "Jobs",
    "ServiceAccount": "ServiceAccounts (their image pull secrets)",
}
# OpenShift only: not served means there are none, not a failure.
OPTIONAL_KINDS = frozenset({"DeploymentConfig"})
RESTARTABLE_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})
NOT_SCANNED = "bare Pods, ReplicationControllers, Ingress/Route TLS, custom resources and operators, other namespaces"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
# Each restart is a dry run and a patch inside one tool call (30 s by default).
MAX_RESTARTS = 20

_PICK_UP_TEXT = {
    "restart": "only after a restart",
    "file": "file updates in about 60s (could not check if the app reloads it)",
    "next-pull": "at the next image pull (running pods are not affected)",
    "next-run": "at the next scheduled run",
    "rerun": "only if the Job is re-run",
    "mount": "when the volume is next mounted",
}


class ConsumerInputError(ValueError):
    """Input or setting that makes the tool refuse before reading anything."""


# ---------------------------------------------------------------------------
# Matching (a port of configConsumers.ts; keep the two in step)
# ---------------------------------------------------------------------------


def _volume_secret_refs(v: dict) -> list[tuple[str, str]]:
    """Secrets a volume's driver reads (not mounted as files), with the volume type."""
    out: list[tuple[str, str]] = []
    csi = v.get("csi") or {}
    if (csi.get("nodePublishSecretRef") or {}).get("name"):
        out.append((csi["nodePublishSecretRef"]["name"], f"csi {csi.get('driver') or ''}".strip()))
    for typ in ("cephfs", "rbd", "iscsi"):
        ref = ((v.get(typ) or {}).get("secretRef") or {}).get("name")
        if ref:
            out.append((ref, typ))
    flex = v.get("flexVolume") or {}
    if (flex.get("secretRef") or {}).get("name"):
        out.append((flex["secretRef"]["name"], f"flexVolume {flex.get('driver') or ''}".strip()))
    if (v.get("azureFile") or {}).get("secretName"):
        out.append((v["azureFile"]["secretName"], "azureFile"))
    return out


def service_account_of(spec: dict | None) -> str:
    spec = spec or {}
    return spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"


def pod_template_of(workload: dict) -> dict:
    spec = workload.get("spec") or {}
    if workload.get("kind") == "CronJob":
        return ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    return spec.get("template") or {}


def _containers(spec: dict) -> list[dict]:
    return [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]


def _mounts_of(spec: dict, volume_name: str) -> list[dict]:
    out = []
    for c in _containers(spec):
        for m in c.get("volumeMounts") or []:
            if m.get("name") == volume_name:
                out.append(
                    {
                        "container": c.get("name", ""),
                        "mountPath": m.get("mountPath", ""),
                        "subPath": bool(m.get("subPath") or m.get("subPathExpr")),
                    }
                )
    return out


def usages_in(spec: dict | None, kind: str, name: str) -> list[dict]:
    """Every way one pod spec uses ConfigMap/Secret ``name``."""
    if not spec:
        return []
    out: list[dict] = []
    for c in _containers(spec):
        keys: list[str] = []
        env_vars: list[str] = []
        optional = False
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            ref = vf.get("configMapKeyRef") if kind == "ConfigMap" else vf.get("secretKeyRef")
            if ref and ref.get("name") == name:
                if ref.get("key") and ref["key"] not in keys:
                    keys.append(ref["key"])
                env_vars.append(e.get("name", ""))
                optional = optional or bool(ref.get("optional"))
        if env_vars:
            out.append(
                {"how": "env", "container": c.get("name", ""), "keys": keys, "envVars": env_vars, "optional": optional}
            )
        for f in c.get("envFrom") or []:
            ref = f.get("configMapRef") if kind == "ConfigMap" else f.get("secretRef")
            if ref and ref.get("name") == name:
                out.append(
                    {
                        "how": "envFrom",
                        "container": c.get("name", ""),
                        "keys": [],
                        "optional": bool(ref.get("optional")),
                        "prefix": f.get("prefix") or "",
                    }
                )
    for v in spec.get("volumes") or []:
        if kind == "ConfigMap":
            direct = v.get("configMap") if (v.get("configMap") or {}).get("name") == name else None
        else:
            direct = v.get("secret") if (v.get("secret") or {}).get("secretName") == name else None
        if direct:
            out.append(
                {
                    "how": "volume",
                    "keys": [i.get("key", "") for i in direct.get("items") or []],
                    "mounts": _mounts_of(spec, v.get("name", "")),
                    "optional": bool(direct.get("optional")),
                }
            )
        for s in (v.get("projected") or {}).get("sources") or []:
            src = s.get("configMap") if kind == "ConfigMap" else s.get("secret")
            if src and src.get("name") == name:
                out.append(
                    {
                        "how": "projected",
                        "keys": [i.get("key", "") for i in src.get("items") or []],
                        "mounts": _mounts_of(spec, v.get("name", "")),
                        "optional": bool(src.get("optional")),
                    }
                )
        if kind == "Secret":
            for ref_name, driver in _volume_secret_refs(v):
                if ref_name == name:
                    out.append(
                        {
                            "how": "volumeSecretRef",
                            "keys": [],
                            "driver": driver,
                            "mounts": _mounts_of(spec, v.get("name", "")),
                        }
                    )
    if kind == "Secret" and any((s or {}).get("name") == name for s in spec.get("imagePullSecrets") or []):
        out.append({"how": "imagePullSecret", "keys": []})
    return out


def pick_up_of(workload_kind: str, uses: list[dict]) -> str:
    """When the running pods of a workload see a change, given how they use the object."""
    if workload_kind == "CronJob":
        return "next-run"
    if workload_kind == "Job":
        return "rerun"
    needs_restart = any(
        u["how"] in ("env", "envFrom")
        or (u["how"] in ("volume", "projected") and any(m["subPath"] for m in u.get("mounts") or []))
        for u in uses
    )
    if needs_restart:
        return "restart"
    if any(u["how"] in ("volume", "projected") for u in uses):
        return "file"
    if any(u["how"] == "volumeSecretRef" for u in uses):
        return "mount"
    return "next-pull"


def _owned_by_cronjob(w: dict) -> bool:
    return any(o.get("kind") == "CronJob" for o in (w.get("metadata") or {}).get("ownerReferences") or [])


def find_consumers(
    kind: str, name: str, workloads: list[dict], service_accounts: list[dict] | None = None
) -> list[dict]:
    """The workloads whose pod templates use ``kind/name``, in kind order then by name."""
    pulling: set[str] = set()
    if kind == "Secret":
        for sa in service_accounts or []:
            if any((s or {}).get("name") == name for s in sa.get("imagePullSecrets") or []):
                pulling.add((sa.get("metadata") or {}).get("name", ""))
    out = []
    for w in workloads:
        wkind = w.get("kind", "")
        if wkind not in CONSUMER_KINDS:
            continue
        if wkind == "Job" and _owned_by_cronjob(w):
            continue
        spec = pod_template_of(w).get("spec") or {}
        uses = usages_in(spec, kind, name)
        runs_as = service_account_of(spec)
        if runs_as in pulling:
            uses.append({"how": "serviceAccountPullSecret", "keys": [], "serviceAccount": runs_as})
        if not uses:
            continue
        out.append(
            {
                "workload": w,
                "kind": wkind,
                "name": (w.get("metadata") or {}).get("name", ""),
                "uses": uses,
                "pickUp": pick_up_of(wkind, uses),
            }
        )
    out.sort(key=lambda c: (CONSUMER_KINDS.index(c["kind"]), c["name"]))
    return out


def describe_usage(u: dict, kind: str) -> str:
    """One line for how a consumer uses the object (as the UI's describeUsage)."""
    opt = " (optional)" if u.get("optional") else ""
    ctr = f" in {u['container']}" if u.get("container") else ""
    how = u["how"]
    if how == "env":
        keys = u.get("keys") or []
        via = "configMapKeyRef" if kind == "ConfigMap" else "secretKeyRef"
        return (
            f"env {', '.join(u.get('envVars') or [])}{ctr} from key{'' if len(keys) == 1 else 's'} "
            f"{', '.join(keys)} via {via}{opt}"
        )
    if how == "envFrom":
        prefix = f" with prefix {u['prefix']}" if u.get("prefix") else ""
        return f"envFrom: all keys{prefix}{ctr}{opt}"
    if how in ("volume", "projected"):
        mounts = u.get("mounts") or []
        where = (
            ", ".join(f"{m['mountPath']}{' (subPath)' if m['subPath'] else ''}" for m in mounts)
            if mounts
            else "not mounted by any container"
        )
        items = f" (keys {', '.join(u['keys'])})" if u.get("keys") else ""
        label = "projected volume" if how == "projected" else "volume"
        return f"{label} mounted at {where}{items}{opt}"
    if how == "imagePullSecret":
        return "image pull secret"
    if how == "serviceAccountPullSecret":
        return f"image pull secret of its ServiceAccount {u.get('serviceAccount', '')}"
    mounts = u.get("mounts") or []
    at = f" mounted at {', '.join(m['mountPath'] for m in mounts)}" if mounts else ""
    return f"credentials of a {u.get('driver', '')} volume{at}"


# ---------------------------------------------------------------------------
# Restart plan (a port of restartPlan.ts: strategy, OnDelete/paused, order)
# ---------------------------------------------------------------------------


def _int_or_pct(v: Any, total: int, round_up: bool, fallback: int) -> int:
    if v is None or v == "":
        return fallback
    if isinstance(v, int):
        return v
    s = str(v)
    if s.endswith("%") and s[:-1].isdigit():
        raw = int(s[:-1]) / 100 * total
        return int(-(-raw // 1)) if round_up else int(raw // 1)
    try:
        return int(s)
    except ValueError:
        return fallback


def strategy_of(w: dict) -> dict:
    """Rollout strategy as the UI's restart plan states it; ``type`` is rolling, OnDelete or Recreate."""
    spec = w.get("spec") or {}
    kind = w.get("kind")
    if kind == "DaemonSet":
        desired = (w.get("status") or {}).get("desiredNumberScheduled") or 0
        us = spec.get("updateStrategy") or {}
        if us.get("type") == "OnDelete":
            return {"type": "OnDelete", "text": f"{desired} · OnDelete: pods are replaced only when deleted"}
        ru = us.get("rollingUpdate") or {}
        mu = _int_or_pct(ru.get("maxUnavailable"), desired, False, 1)
        ms = _int_or_pct(ru.get("maxSurge"), desired, True, 0)
        surge = f", maxSurge {ms}" if ms else ""
        return {"type": "rolling", "text": f"{desired} · rolling node by node, maxUnavailable {mu}{surge}"}
    replicas = spec.get("replicas", 1)
    if kind == "StatefulSet":
        us = spec.get("updateStrategy") or {}
        if us.get("type") == "OnDelete":
            return {"type": "OnDelete", "text": f"{replicas} · OnDelete: pods are replaced only when deleted"}
        partition = (us.get("rollingUpdate") or {}).get("partition")
        part = f", partition {partition} (pods below it are not restarted)" if partition else ""
        return {"type": "rolling", "text": f"{replicas} · rolling one ordinal at a time, highest first{part}"}
    st = spec.get("strategy") or {}
    if st.get("type") == "Recreate":
        return {
            "type": "Recreate",
            "text": f"{replicas} · Recreate: every pod stops before new ones start (downtime)",
        }
    ru = st.get("rollingUpdate") or {}
    ms = _int_or_pct(ru.get("maxSurge", "25%"), replicas, True, 1)
    mu = _int_or_pct(ru.get("maxUnavailable", "25%"), replicas, False, 0)
    if mu == 0 and ms == 0:
        mu = 1
    how = f"maxSurge {ms}: new pod ready before an old one stops" if mu == 0 else f"maxUnavailable {mu}, maxSurge {ms}"
    return {"type": "rolling", "text": f"{replicas} · rolling, {how}"}


def _serves(w: dict, services: list[dict] | None) -> list[str] | None:
    if services is None:
        return None
    labels = (pod_template_of(w).get("metadata") or {}).get("labels") or {}
    out = []
    for s in services:
        sel = (s.get("spec") or {}).get("selector") or {}
        if sel and all(labels.get(k) == v for k, v in sel.items()):
            out.append((s.get("metadata") or {}).get("name", ""))
    return out


def gitops_owner(w: dict) -> str | None:
    """The Argo CD application that appears to manage this object, if any."""
    meta = w.get("metadata") or {}
    ann = meta.get("annotations") or {}
    labels = meta.get("labels") or {}
    tracking = ann.get("argocd.argoproj.io/tracking-id")
    if tracking:
        return tracking.split(":", 1)[0]
    instance = labels.get("app.kubernetes.io/instance") or labels.get("argocd.argoproj.io/instance")
    if instance and labels.get("app.kubernetes.io/managed-by") != "Helm":
        return instance
    return None


# ---------------------------------------------------------------------------
# Cluster reads and writes
# ---------------------------------------------------------------------------


def _validate(kind: str, name: str, namespace: str) -> str:
    """The canonical config kind; raises ConsumerInputError for bad input."""
    canonical = {"configmap": "ConfigMap", "configmaps": "ConfigMap", "cm": "ConfigMap"}
    canonical.update({"secret": "Secret", "secrets": "Secret"})
    k = canonical.get((kind or "").strip().lower())
    if k is None:
        raise ConsumerInputError(f"kind must be ConfigMap or Secret, got {kind!r}.")
    if err := _validate_k8s_namespace(namespace):
        raise ConsumerInputError(err)
    if not namespace:
        raise ConsumerInputError("namespace is required: consumers are found in the object's own namespace.")
    if err := _validate_k8s_name(name):
        raise ConsumerInputError(err)
    return k


def _list(path: str) -> list[dict] | ToolError:
    result = _kc.get_raw_json(path, "list")
    if isinstance(result, ToolError):
        return result
    return list((result or {}).get("items") or [])


def scan(kind: str, name: str, namespace: str) -> dict:
    """List every consumer kind in ``namespace`` (as the caller) and find the ones that use ``kind/name``."""
    workloads: list[dict] = []
    failures: list[tuple[str, str]] = []
    kinds_read: list[str] = []
    for wkind in CONSUMER_KINDS:
        items = _list(_LIST_PATHS[wkind].format(ns=namespace))
        if isinstance(items, ToolError):
            if wkind in OPTIONAL_KINDS and items.status_code == 404:
                continue
            failures.append((wkind, items.message))
            continue
        kinds_read.append(wkind)
        workloads += [{**w, "kind": wkind} for w in items]
    sas: list[dict] | None = None
    if kind == "Secret":
        got = _list(f"/api/v1/namespaces/{namespace}/serviceaccounts")
        if isinstance(got, ToolError):
            failures.append(("ServiceAccount", got.message))
        else:
            sas = got
    scanned = sum(1 for w in workloads if not (w["kind"] == "Job" and _owned_by_cronjob(w)))
    return {
        "consumers": find_consumers(kind, name, workloads, sas),
        "failures": failures,
        "kindsRead": kinds_read,
        "scanned": scanned,
    }


def build_plan(kind: str, name: str, namespace: str) -> dict:
    """The scan, then the restart plan: targets (non-serving first), skipped ones, and the rest."""
    found = scan(kind, name, namespace)
    targets = [c for c in found["consumers"] if c["pickUp"] == "restart" and c["kind"] in RESTARTABLE_KINDS]
    services: list[dict] | None = None
    if targets:
        got = _list(f"/api/v1/namespaces/{namespace}/services")
        services = None if isinstance(got, ToolError) else got
    items = []
    for c in targets:
        w = c["workload"]
        strategy = strategy_of(w)
        skip = None
        if strategy["type"] == "OnDelete":
            skip = (
                f"{c['kind']} uses the OnDelete update strategy: a restart annotation replaces no pod. "
                "Delete its pods one by one to pick up the change."
            )
        elif c["kind"] == "Deployment" and (w.get("spec") or {}).get("paused"):
            skip = "The Deployment is paused: the restart would not roll out until it is resumed."
        items.append(
            {
                "kind": c["kind"],
                "name": c["name"],
                "strategy": strategy["text"],
                "services": _serves(w, services),
                "uses": [describe_usage(u, kind) for u in c["uses"]],
                "gitops": gitops_owner(w),
                "skip": skip,
            }
        )
    # Stable: non-serving first, traffic-serving last.
    items.sort(key=lambda i: 1 if i["services"] else 0)
    others = [c for c in found["consumers"] if c not in targets]
    return {**found, "items": items, "others": others, "orderUnknown": bool(targets) and services is None}


def restart_body(now: str) -> dict:
    return {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: now}}}}}


def _patch(kind: str, name: str, namespace: str, body: dict, dry_run: bool) -> Any:
    apps = _kc.get_apps_client()
    fn = {
        "Deployment": apps.patch_namespaced_deployment,
        "StatefulSet": apps.patch_namespaced_stateful_set,
        "DaemonSet": apps.patch_namespaced_daemon_set,
    }[kind]
    kwargs = {"dry_run": "All"} if dry_run else {}
    return _kc.safe(lambda: fn(name, namespace, body, **kwargs))


def require_user_writes(what: str = "Workloads") -> None:
    """Writes from chat run as the person, never as the agent's service account."""
    from ..config import get_settings

    if not get_settings().agent.token_forwarding:
        raise ConsumerInputError(
            f"{what} are not restarted from chat on this deployment: token forwarding is off "
            "(PULSE_AGENT_TOKEN_FORWARDING=false), so the write would run as Pulse's service account instead of "
            "as you. Use the ConfigMap or Secret page > Used by > Restart consumers, which writes as you."
        )
    if not _kc.get_current_user_token():
        raise ConsumerInputError(
            "Your access token did not reach Pulse with this request, and workloads are never restarted as "
            "Pulse's service account. Sign in again and retry."
        )


def _dry_run(items: list[dict], namespace: str) -> list[dict]:
    """Server-side dry run of every restart patch, as the caller; the ones refused."""
    body = restart_body(datetime.now(UTC).isoformat())
    errors = []
    for i in items:
        res = _patch(i["kind"], i["name"], namespace, body, dry_run=True)
        if isinstance(res, ToolError):
            errors.append({"kind": i["kind"], "name": i["name"], "error": res.message})
    return errors


def _text(value: Any, limit: int = 300) -> str:
    from ..monitor.confidence import sanitize_block

    return sanitize_block(str(value or ""))[:limit].replace("\n", " ")


def _block(lines: list[str]) -> str:
    return "--- BEGIN CLUSTER DATA ---\n" + "\n".join(lines) + "\n--- END CLUSTER DATA ---"


def _scope_line(found: dict, kind: str, namespace: str) -> str:
    kinds = ", ".join(_PLURAL[k] for k in found["kindsRead"]) or "none"
    line = f"Scanned {found['scanned']} pod template(s) in {namespace} ({kinds})."
    if kind == "Secret":
        line += " Including ServiceAccount image pull secrets and volume driver credentials; no Secret value was read."
    return line + f" Not checked: {NOT_SCANNED}."


def _failure_lines(found: dict) -> list[str]:
    if not found["failures"]:
        return []
    return [
        "Could not check (the list may be incomplete): "
        + "; ".join(f"{_PLURAL.get(k, k)}: {_text(msg, 200)}" for k, msg in found["failures"])
    ]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@beta_tool
def find_config_consumers(kind: str, name: str, namespace: str):
    """Find the workloads that use a ConfigMap or Secret, and when each sees a change.

    Scans every Deployment, StatefulSet, DaemonSet, DeploymentConfig, CronJob and Job
    pod template in the namespace for env valueFrom, envFrom, volume, projected volume,
    image pull secret (also through the pod's ServiceAccount) and volume driver
    references, as the ConfigMap/Secret page's "Used by" section does. Says per
    consumer whether its running pods need a restart. Never reads Secret values.
    Use restart_consumers to restart the ones that need it.

    Args:
        kind: ConfigMap or Secret.
        name: The ConfigMap or Secret name.
        namespace: Its namespace.
    """
    try:
        k = _validate(kind, name, namespace)
    except ConsumerInputError as e:
        return str(e)
    found = scan(k, name, namespace)
    lines = [_scope_line(found, k, namespace), *_failure_lines(found)]
    consumers = found["consumers"]
    if not consumers:
        if found["failures"]:
            lines.append("No consumer found among the kinds that could be read.")
        else:
            lines.append(f"No workload in {namespace} references {k} {name}.")
        return "\n".join(lines)
    data = []
    for c in consumers:
        note = ""
        if c["pickUp"] == "restart" and c["kind"] not in RESTARTABLE_KINDS:
            note = f" (Pulse does not restart {c['kind']}s: oc rollout latest dc/{c['name']})"
        data.append(f"- {c['kind']} {c['name']}: picks up changes {_PICK_UP_TEXT[c['pickUp']]}{note}")
        data += [f"    {_text(describe_usage(u, k))}" for u in c["uses"]]
    needs = [c for c in consumers if c["pickUp"] == "restart" and c["kind"] in RESTARTABLE_KINDS]
    lines.append(f"{len(consumers)} workload(s) use {k} {name}:")
    lines.append(_block(data))
    if needs:
        names = ", ".join(c["kind"] + " " + c["name"] for c in needs)
        lines.append(
            f"{len(needs)} keep the old values until restarted: {names}. "
            f"restart_consumers(kind='{k}', name='{name}', namespace='{namespace}') restarts them after the user confirms."
        )
    return "\n".join(lines)


def preview_restart_consumers(args: dict) -> dict[str, Any]:
    """What the confirmation shows before restart_consumers runs: every workload, how, and the dry run."""
    preview: dict[str, Any] = {"kind": "restart_consumers", "valid": False}
    try:
        k = _validate(str(args.get("kind", "")), str(args.get("name", "")), str(args.get("namespace", "")))
        require_user_writes()
    except ConsumerInputError as e:
        preview["error"] = str(e)
        return preview
    name, namespace = str(args["name"]), str(args["namespace"])
    plan = build_plan(k, name, namespace)
    runnable = [i for i in plan["items"] if not i["skip"]]
    preview.update(
        {
            "target": {"kind": k, "name": name, "namespace": namespace},
            "patch": restart_body("<now>"),
            "restart": [
                {
                    "kind": i["kind"],
                    "name": i["name"],
                    "strategy": i["strategy"],
                    "services": i["services"],
                    "uses": i["uses"],
                    "gitops": i["gitops"],
                }
                for i in runnable
            ],
            "skipped": [
                {"kind": i["kind"], "name": i["name"], "reason": i["skip"]} for i in plan["items"] if i["skip"]
            ],
            "notRestarted": [
                {"kind": c["kind"], "name": c["name"], "pickUp": _PICK_UP_TEXT[c["pickUp"]]} for c in plan["others"]
            ],
            "incomplete": [_PLURAL.get(kd, kd) for kd, _ in plan["failures"]],
            "notScanned": NOT_SCANNED,
            "orderUnknown": plan["orderUnknown"],
            "sequence": (
                "One after another in this order (no Service first, traffic-serving last), without waiting "
                "for each rollout to finish; the first refused restart stops the rest."
            ),
            "undo": "A restart has no undo: pods are replaced with the same spec. Nothing else changes.",
        }
    )
    if not runnable:
        preview["error"] = f"No consumer of {k} {name} needs a restart to pick up a change."
        return preview
    if len(runnable) > MAX_RESTARTS:
        preview["error"] = (
            f"{len(runnable)} workloads is more than restart_consumers restarts at once ({MAX_RESTARTS})."
        )
        return preview
    errors = _dry_run(runnable, namespace)
    preview["dryRun"] = {"passed": not errors, "errors": errors}
    if errors:
        preview["error"] = "The server-side dry run refused " + ", ".join(f"{e['kind']} {e['name']}" for e in errors)
        return preview
    preview["valid"] = True
    return preview


@beta_tool
def restart_consumers(kind: str, name: str, namespace: str):
    """Rolling-restart every Deployment, StatefulSet and DaemonSet that keeps a ConfigMap's or Secret's old values until restarted. REQUIRES USER CONFIRMATION.

    Use after changing a ConfigMap or Secret. Restarts the consumers find_config_consumers
    marks as "only after a restart" (env, envFrom, subPath mounts) with the
    kubectl.kubernetes.io/restartedAt template annotation, as `oc rollout restart` does.
    CronJobs, Jobs, DeploymentConfigs and consumers that pick up changes on their own are
    listed, not restarted. Every patch is dry-run first; nothing restarts if one is refused.

    Args:
        kind: ConfigMap or Secret.
        name: The ConfigMap or Secret name.
        namespace: Its namespace.
    """
    try:
        k = _validate(kind, name, namespace)
        require_user_writes()
    except ConsumerInputError as e:
        return f"Nothing restarted: {e}"
    plan = build_plan(k, name, namespace)
    runnable = [i for i in plan["items"] if not i["skip"]]
    lines: list[str] = [_scope_line(plan, k, namespace), *_failure_lines(plan)]
    skipped = [f"- {i['kind']} {i['name']}: {i['skip']}" for i in plan["items"] if i["skip"]]
    others = [f"- {c['kind']} {c['name']}: picks up changes {_PICK_UP_TEXT[c['pickUp']]}" for c in plan["others"]]
    if not runnable:
        lines.insert(0, f"Nothing restarted: no consumer of {k} {name} needs a restart to pick up a change.")
        if skipped or others:
            lines.append(_block(skipped + others))
        return "\n".join(lines)
    if len(runnable) > MAX_RESTARTS:
        return (
            f"Nothing restarted: {len(runnable)} workloads use {k} {name}, more than this tool restarts at once "
            f"({MAX_RESTARTS}). Restart them from the {k} page (Used by > Restart consumers)."
        )
    errors = _dry_run(runnable, namespace)
    if errors:
        lines.insert(0, "Nothing restarted: the server-side dry run refused these restarts.")
        lines.append(_block([f"- {e['kind']} {e['name']}: {_text(e['error'])}" for e in errors]))
        return "\n".join(lines)

    now = datetime.now(UTC).isoformat()
    body = restart_body(now)
    done: list[dict] = []
    failed: dict | None = None
    for i in runnable:
        res = _patch(i["kind"], i["name"], namespace, body, dry_run=False)
        if isinstance(res, ToolError):
            failed = {**i, "error": res.message}
            break
        done.append(i)
        logger.info("Rolling restart of %s %s/%s for %s %s", i["kind"], namespace, i["name"], k, name)
    not_attempted = runnable[len(done) + (1 if failed else 0) :]

    if failed is None:
        head = f"Restarted {len(done)} workload(s) that use {k} {name} ({RESTART_ANNOTATION}={now}):"
    elif done:
        head = f"Partly done: {len(done)} of {len(runnable)} restarts started, then one was refused and the rest were not attempted."
    else:
        head = "Nothing restarted: the first restart was refused, so the rest were not attempted."
    data = [f"- restarted: {i['kind']} {i['name']} ({_text(i['strategy'])})" for i in done]
    if failed:
        data.append(f"- FAILED: {failed['kind']} {failed['name']}: {_text(failed['error'])}")
    data += [f"- not attempted: {i['kind']} {i['name']}" for i in not_attempted]
    data += [f"- not restarted (skipped): {s[2:]}" for s in skipped]
    data += [f"- not restarted (no restart needed): {o[2:]}" for o in others]
    gitops = sorted({i["gitops"] for i in done if i["gitops"]})
    tail = [
        "Rollouts were started one after another and run on their own; check them with describe_resource or "
        "the workload pages before calling it done."
    ]
    if gitops:
        tail.append(
            f"Managed by Argo CD ({', '.join(gitops)}): the restart annotation is drift Argo CD may revert, "
            "which starts a second rollout. Make the config change itself through Git (propose_git_change)."
        )
    return "\n".join([head, *lines, _block(data), *tail])
