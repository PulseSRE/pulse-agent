"""Go/no-go for an OpenShift minor upgrade, from the APIs it removes.

Before a minor upgrade an admin needs three answers: which APIs the target
release removes that something on this cluster still calls, who is calling
them, and whether the upgrade is held behind an admin acknowledgement. This
module computes them from OpenShift's own records:

- ``apiserver.openshift.io/v1`` ``APIRequestCount``: one object per served
  resource version (``<resource>.<version>.<group>``) with
  ``status.removedInRelease`` and the last 24h of requests by node and user;
- ``config.openshift.io/v1`` ``ClusterVersion/version``: current version,
  channel and the updates on offer;
- ConfigMap ``openshift-config-managed/admin-gates`` (gate key -> description)
  and ConfigMap ``openshift-config/admin-acks`` (gate key -> ``"true"`` once
  acknowledged).

``compute_upgrade_api_removals`` is pure: it takes what was read (or the
``ToolError`` the read produced) and returns the verdict. ``read_upgrade_api_removals``
does the reads. The agent tool, the inbox generator and the tests share both.

Honesty rules: a read that failed is "could not check", never "go". A 404 on
APIRequestCount means the API is not served here (not OpenShift), so the
answer is unknown. A verdict that is already proven -- a removed API still in
use, or an unacknowledged gate -- stays ``no_go`` even when another read
failed, because nothing that read could have returned would make it ``go``.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ToolError

# Reads of OpenShift objects this module makes.
APIREQUESTCOUNT = ("apiserver.openshift.io", "v1", "apirequestcounts")
CLUSTERVERSION = ("config.openshift.io", "v1", "clusterversions", "version")
ADMIN_GATES = ("openshift-config-managed", "admin-gates")
ADMIN_ACKS = ("openshift-config", "admin-acks")

# Callers listed per API. The rest are counted, not dropped silently.
MAX_CALLERS = 10

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$")
_GATE_TARGET_RE = re.compile(r"-in-(\d+\.\d+)$")

# OCP 4.N ships Kubernetes 1.(N+13): 4.16 -> 1.29, 4.19 -> 1.32.
_OCP4_TO_KUBE_OFFSET = 13


def parse_minor(version: Any) -> tuple[int, int] | None:
    """``"4.18.3"`` -> ``(4, 18)``; ``None`` when it is not a version."""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def kube_minor_for_ocp(ocp: tuple[int, int]) -> tuple[int, int] | None:
    """The Kubernetes minor an OpenShift minor ships, for OpenShift 4."""
    if ocp[0] != 4:
        return None
    return 1, ocp[1] + _OCP4_TO_KUBE_OFFSET


def _fmt(minor: tuple[int, int] | None) -> str | None:
    return f"{minor[0]}.{minor[1]}" if minor else None


def valid_target_version(target: str) -> bool:
    """A caller-supplied target must look like ``4.19`` or ``4.19.2``."""
    return parse_minor(target) is not None


def _read_failure(what: str, err: ToolError) -> str:
    return f"could not read {what} ({err.status_code or 'error'}: {err.message})"


def current_version(cv: dict) -> str | None:
    """The version the cluster runs: last Completed history entry, else desired."""
    status = cv.get("status") or {}
    for entry in status.get("history") or []:
        if entry.get("state") == "Completed" and entry.get("version"):
            return entry["version"]
    return (status.get("desired") or {}).get("version") or None


def _offered_minors(cv: dict) -> list[tuple[int, int]]:
    status = cv.get("status") or {}
    versions = [u.get("version") for u in status.get("availableUpdates") or []]
    versions += [(cu.get("release") or {}).get("version") for cu in status.get("conditionalUpdates") or []]
    return sorted({m for m in (parse_minor(v) for v in versions) if m})


def _upgradeable(cv: dict) -> dict[str, str]:
    for c in (cv.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Upgradeable":
            return {
                "status": str(c.get("status", "")),
                "reason": str(c.get("reason", "")),
                "message": str(c.get("message", "")),
            }
    return {"status": "", "reason": "", "message": ""}


def split_api_name(name: str) -> dict[str, str]:
    """``flowschemas.v1beta3.flowcontrol.apiserver.k8s.io`` -> resource/version/group."""
    parts = name.split(".", 2)
    if len(parts) == 2:  # core group: "pods.v1"
        return {"resource": parts[0], "version": parts[1], "group": ""}
    if len(parts) == 3:
        return {"resource": parts[0], "version": parts[1], "group": parts[2]}
    return {"resource": name, "version": "", "group": ""}


def _callers(status: dict) -> list[dict[str, Any]]:
    """Requests over the last 24h by (username, userAgent), largest first.

    Platform callers (``system:`` users, usually cluster components updated
    with the cluster) rank after everyone else but are never dropped.
    """
    hours = status.get("last24h") or []
    if not hours and status.get("currentHour"):
        hours = [status["currentHour"]]
    totals: dict[tuple[str, str], int] = {}
    for hour in hours:
        for node in (hour or {}).get("byNode") or []:
            for user in (node or {}).get("byUser") or []:
                key = (str(user.get("username", "")), str(user.get("userAgent", "")))
                totals[key] = totals.get(key, 0) + int(user.get("requestCount") or 0)
    ranked = sorted(
        ((u.startswith("system:"), -n, u, ua) for (u, ua), n in totals.items() if n > 0),
    )
    return [{"username": u, "userAgent": ua, "count": -neg, "platform": platform} for platform, neg, u, ua in ranked]


def _cm_data(cm: Any) -> dict[str, str]:
    if isinstance(cm, dict):
        return dict(cm.get("data") or {})
    return dict(getattr(cm, "data", None) or {})


def compute_upgrade_api_removals(
    cluster_version: dict | ToolError,
    api_request_counts: list[dict] | ToolError,
    admin_gates: dict | Any | ToolError,
    admin_acks: dict | Any | ToolError,
    target_version: str = "",
) -> dict[str, Any]:
    """Decide go / no_go / unknown for upgrading to ``target_version``.

    Each input is what its read returned, or the ``ToolError`` it produced.
    With no ``target_version`` the target is the lowest minor above the
    current one that ClusterVersion offers; with none on offer the answer is
    unknown. An API is in scope when it is removed after the current
    Kubernetes minor and no later than the target's; in scope with requests
    in the last 24h it blocks the upgrade, with none it is "removed but
    unused". A gate is in scope when it names a release after the current
    minor and no later than the target.
    """
    result: dict[str, Any] = {
        "status": "unknown",
        "reason": "",
        # Why the status is unknown: not_openshift | read_failed | no_current |
        # bad_target | no_newer_minor. None when the status is go or no_go.
        "cause": None,
        "current": {"ocp": None, "kubernetes": None},
        "target": {"ocp": None, "kubernetes": None, "source": None},
        "channel": None,
        "upgradeable": {"status": "", "reason": "", "message": ""},
        "inUse": [],
        "unused": [],
        "scheduledForRemoval": [],
        "gates": [],
        "checks": {"clusterVersion": "ok", "apiRequestCounts": "ok", "adminGates": "ok", "adminAcks": "ok"},
    }
    checks = result["checks"]

    # -- Where the cluster is and where it is going ------------------------
    if isinstance(cluster_version, ToolError):
        if cluster_version.status_code == 404:
            checks["clusterVersion"] = "not found"
            result["cause"] = "not_openshift"
            result["reason"] = (
                "ClusterVersion was not found, so this is not an OpenShift cluster and there is no "
                "OpenShift upgrade to check."
            )
        else:
            checks["clusterVersion"] = _read_failure("ClusterVersion", cluster_version)
            result["cause"] = "read_failed"
            result["reason"] = f"Could not check: {checks['clusterVersion']}."
        return result

    cv = cluster_version or {}
    result["channel"] = (cv.get("spec") or {}).get("channel") or None
    result["upgradeable"] = _upgradeable(cv)
    cur_version = current_version(cv)
    cur_minor = parse_minor(cur_version)
    cur_kube = kube_minor_for_ocp(cur_minor) if cur_minor else None
    result["current"] = {"ocp": cur_version, "kubernetes": _fmt(cur_kube)}

    target_minor: tuple[int, int] | None = None
    target_problem = ""
    target_cause = ""
    if not cur_minor or not cur_kube:
        target_cause = "no_current"
        target_problem = (
            f"the current OpenShift version could not be determined from ClusterVersion "
            f"({cur_version or 'no version recorded'})"
        )
    elif target_version:
        target_minor = parse_minor(target_version)
        if not target_minor or not kube_minor_for_ocp(target_minor):
            target_problem = f"target {target_version!r} is not an OpenShift 4 version"
            target_cause = "bad_target"
            target_minor = None
        elif target_minor <= cur_minor:
            target_problem = (
                f"target {_fmt(target_minor)} is not a newer minor than the current {_fmt(cur_minor)}; "
                "patch updates within a minor do not remove APIs"
            )
            target_cause = "bad_target"
            target_minor = None
        else:
            result["target"]["source"] = "requested"
    else:
        newer = [m for m in _offered_minors(cv) if m > cur_minor]
        if newer:
            target_minor = newer[0]
            result["target"]["source"] = "available_update"
        else:
            target_cause = "no_newer_minor"
            target_problem = (
                f"no update to a newer minor than {_fmt(cur_minor)} is offered on channel "
                f"{result['channel'] or '(none)'}; pass a target version (for example "
                f"{cur_minor[0]}.{cur_minor[1] + 1}) to check against it"
            )
    target_kube = kube_minor_for_ocp(target_minor) if target_minor else None
    result["target"]["ocp"] = _fmt(target_minor)
    result["target"]["kubernetes"] = _fmt(target_kube)

    # -- APIs removed in the target, and whether anything still calls them --
    if isinstance(api_request_counts, ToolError):
        if api_request_counts.status_code == 404:
            checks["apiRequestCounts"] = (
                "not available: the APIRequestCount API is not served on this cluster (not OpenShift, or too old)"
            )
        else:
            checks["apiRequestCounts"] = _read_failure("APIRequestCounts", api_request_counts)
    else:
        for item in api_request_counts or []:
            status = item.get("status") or {}
            removed = parse_minor(status.get("removedInRelease"))
            if not removed:
                continue
            if cur_kube and removed <= cur_kube:
                continue  # already gone in the running release: not this upgrade's problem
            name = (item.get("metadata") or {}).get("name", "")
            count = int(status.get("requestCount") or 0)
            entry: dict[str, Any] = {
                "name": name,
                **split_api_name(name),
                "removedInRelease": _fmt(removed),
                "requestCount": count,
            }
            if count > 0:
                callers = _callers(status)
                entry["callers"] = callers[:MAX_CALLERS]
                entry["callerCount"] = len(callers)
            result["scheduledForRemoval"].append(entry)
            if target_kube and removed <= target_kube:
                (result["inUse"] if count > 0 else result["unused"]).append(entry)
        result["scheduledForRemoval"].sort(
            key=lambda e: (parse_minor(e["removedInRelease"]), -e["requestCount"], e["name"])
        )
        result["inUse"].sort(key=lambda e: (-e["requestCount"], e["name"]))
        result["unused"].sort(key=lambda e: e["name"])

    # -- Admin gates for the target ----------------------------------------
    gates_data: dict[str, str] | None = None
    if isinstance(admin_gates, ToolError):
        if admin_gates.status_code == 404:
            gates_data = {}  # the CVO publishes no gates for this release
            checks["adminGates"] = "none published"
        else:
            checks["adminGates"] = _read_failure("ConfigMap openshift-config-managed/admin-gates", admin_gates)
    else:
        gates_data = _cm_data(admin_gates)

    acks_data: dict[str, str] | None = None
    if isinstance(admin_acks, ToolError):
        if admin_acks.status_code == 404:
            acks_data = {}  # nothing acknowledged yet
            checks["adminAcks"] = "none recorded"
        else:
            checks["adminAcks"] = _read_failure("ConfigMap openshift-config/admin-acks", admin_acks)
    else:
        acks_data = _cm_data(admin_acks)

    if gates_data is not None and target_minor and cur_minor:
        for key in sorted(gates_data):
            m = _GATE_TARGET_RE.search(key)
            gate_minor = parse_minor(m.group(1)) if m else None
            if not gate_minor or not (cur_minor < gate_minor <= target_minor):
                continue
            acked = None if acks_data is None else str(acks_data.get(key, "")).strip().lower() == "true"
            result["gates"].append(
                {"key": key, "description": gates_data[key], "release": _fmt(gate_minor), "acked": acked}
            )

    # -- Verdict -----------------------------------------------------------
    unacked = [g for g in result["gates"] if g["acked"] is False]
    failed = [v for k, v in checks.items() if k != "clusterVersion" and not _check_ok(v)]
    blockers: list[str] = []
    if result["inUse"]:
        n = len(result["inUse"])
        blockers.append(
            f"{n} API{'s' if n != 1 else ''} removed in Kubernetes {_fmt(target_kube)} "
            f"{'are' if n != 1 else 'is'} still being called (last 24h)"
        )
    if unacked:
        n = len(unacked)
        releases = ", ".join(sorted({g["release"] for g in unacked}, key=lambda r: parse_minor(r) or (0, 0)))
        blockers.append(f"{n} admin gate{'s' if n != 1 else ''} for {releases} not acknowledged")

    if target_problem:
        result["reason"] = f"Cannot decide: {target_problem}."
        result["cause"] = target_cause
        return result
    if blockers:
        result["status"] = "no_go"
        reason = f"Upgrade to {_fmt(target_minor)} is blocked: " + "; ".join(blockers) + "."
        if failed:
            reason += " Also could not check: " + "; ".join(failed) + "."
        result["reason"] = reason
        return result
    if failed:
        result["reason"] = "Could not check: " + "; ".join(failed) + ". Nothing found so far blocks the upgrade."
        result["cause"] = "read_failed"
        return result
    result["status"] = "go"
    unused = len(result["unused"])
    acked_n = len(result["gates"])
    parts = [f"nothing removed in Kubernetes {_fmt(target_kube)} was called in the last 24h"]
    if unused:
        parts.append(f"{unused} removed API{'s are' if unused != 1 else ' is'} unused")
    parts.append(f"{acked_n} admin gate{'s' if acked_n != 1 else ''} acknowledged" if acked_n else "no admin gates")
    result["reason"] = f"Upgrade to {_fmt(target_minor)} is clear of API removals: " + ", ".join(parts) + "."
    return result


def _check_ok(value: str) -> bool:
    return value in ("ok", "none published", "none recorded")


def read_upgrade_api_removals(target_version: str = "") -> dict[str, Any]:
    """Read the four sources (read-only) and compute the verdict."""
    from . import k8s_client as _kc

    custom = _kc.get_custom_client()
    core = _kc.get_core_client()
    group, version, plural, name = CLUSTERVERSION
    cv = _kc.safe(lambda: custom.get_cluster_custom_object(group, version, plural, name))
    if isinstance(cv, ToolError):
        return compute_upgrade_api_removals(cv, [], {}, {}, target_version)

    a_group, a_version, a_plural = APIREQUESTCOUNT
    arcs = _kc.safe(lambda: custom.list_cluster_custom_object(a_group, a_version, a_plural))
    arc_items = arcs if isinstance(arcs, ToolError) else list((arcs or {}).get("items") or [])
    gates = _kc.safe(lambda: core.read_namespaced_config_map(ADMIN_GATES[1], ADMIN_GATES[0]))
    acks = _kc.safe(lambda: core.read_namespaced_config_map(ADMIN_ACKS[1], ADMIN_ACKS[0]))
    return compute_upgrade_api_removals(cv, arc_items, gates, acks, target_version)


def _caller_line(c: dict[str, Any]) -> str:
    tag = " [platform]" if c["platform"] else ""
    agent = f" ({c['userAgent']})" if c["userAgent"] else ""
    return f"{c['username'] or '(unknown user)'}{agent}: {c['count']}{tag}"


def render_upgrade_api_removals(data: dict[str, Any], *, all_scheduled: bool = False) -> str:
    """Plain-text report of a ``compute_upgrade_api_removals`` result."""
    verdict = {"go": "GO", "no_go": "NO-GO", "unknown": "UNKNOWN"}[data["status"]]
    cur, tgt = data["current"], data["target"]
    lines = [f"Verdict: {verdict} — {data['reason']}"]
    lines.append(f"Current: OpenShift {cur['ocp'] or 'unknown'} (Kubernetes {cur['kubernetes'] or 'unknown'})")
    if tgt["ocp"]:
        src = " (requested)" if tgt["source"] == "requested" else " (next minor on offer)"
        lines.append(f"Target: OpenShift {tgt['ocp']} (Kubernetes {tgt['kubernetes']}){src}")
    up = data["upgradeable"]
    if up["status"] == "False":
        lines.append(f"ClusterVersion Upgradeable=False ({up['reason']}): {up['message']}")

    if data["inUse"]:
        lines.append("")
        lines.append(f"Removed in the target and still called ({len(data['inUse'])}):")
        for api in data["inUse"]:
            lines.append(
                f"- {api['name']} (removed in {api['removedInRelease']}): {api['requestCount']} requests / 24h"
            )
            for c in api.get("callers", []):
                lines.append(f"    {_caller_line(c)}")
            hidden = api.get("callerCount", 0) - len(api.get("callers", []))
            if hidden > 0:
                lines.append(f"    (+{hidden} more callers)")
    if data["unused"]:
        lines.append("")
        lines.append(f"Removed in the target but not called in the last 24h ({len(data['unused'])}):")
        for api in data["unused"]:
            lines.append(f"- {api['name']} (removed in {api['removedInRelease']})")
    if data["gates"]:
        lines.append("")
        lines.append("Admin gates for the target:")
        for g in data["gates"]:
            state = {True: "acknowledged", False: "NOT acknowledged", None: "acknowledgement unknown"}[g["acked"]]
            lines.append(f"- {g['key']}: {state} — {g['description']}")
        if any(g["acked"] is False for g in data["gates"]):
            lines.append(
                '  An admin acknowledges a gate by setting that key to "true" in ConfigMap '
                "openshift-config/admin-acks, after migrating the callers above."
            )
    if all_scheduled:
        later = [a for a in data["scheduledForRemoval"] if a not in data["inUse"] and a not in data["unused"]]
        if later:
            lines.append("")
            lines.append("Deprecated and scheduled for removal in a later release:")
            for api in later:
                lines.append(
                    f"- {api['name']} (removed in {api['removedInRelease']}): {api['requestCount']} requests / 24h"
                )
                for c in api.get("callers", [])[:3]:
                    lines.append(f"    {_caller_line(c)}")
    problems = [f"{k}: {v}" for k, v in data["checks"].items() if not _check_ok(v)]
    if problems:
        lines.append("")
        lines.append("Not checked: " + "; ".join(problems))
    return "\n".join(lines)
