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
use, an unacknowledged gate, or ClusterVersion ``Upgradeable=False`` -- stays ``no_go`` even when another read
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
_GATE_PREFIX_RE = re.compile(r"^ack-(\d+\.\d+)-")
_GATE_KUBE_RE = re.compile(r"-kube-(\d+\.\d+)-")
# Caller identity fields come from request headers anyone can set.
MAX_FIELD_CHARS = 120
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

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
                key = (_clean(user.get("username", "")), _clean(user.get("userAgent", "")))
                totals[key] = totals.get(key, 0) + int(user.get("requestCount") or 0)
    ranked = sorted(
        ((u.startswith("system:"), -n, u, ua) for (u, ua), n in totals.items() if n > 0),
    )
    return [{"username": u, "userAgent": ua, "count": -neg, "platform": platform} for platform, neg, u, ua in ranked]


def _clean(value: Any) -> str:
    """Strip control characters and cap length: these strings reach prompts and the inbox."""
    text = _CONTROL_RE.sub("", str(value or ""))
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "…"


def _cm_data(cm: Any) -> dict[str, str]:
    if isinstance(cm, dict):
        return dict(cm.get("data") or {})
    return dict(getattr(cm, "data", None) or {})


def resolve_target(cv: dict, target_version: str = "") -> dict[str, Any]:
    """Current version, target minor and where the target came from.

    With no ``target_version`` the target is the lowest newer minor that
    ClusterVersion offers; on a ``stable-4.N`` channel only z-streams are
    offered until the admin switches channel, so the target falls back to the
    current minor + 1 (source ``next_minor``) -- that planning window is when
    this check matters most. ``problem``/``cause`` are set when no target can
    be formed.
    """
    cur_version = current_version(cv)
    cur_minor = parse_minor(cur_version)
    cur_kube = kube_minor_for_ocp(cur_minor) if cur_minor else None
    out: dict[str, Any] = {
        "current_version": cur_version,
        "current": cur_minor if cur_kube else None,
        "current_kube": cur_kube,
        "target": None,
        "target_kube": None,
        "source": None,
        "problem": "",
        "cause": None,
    }
    if not cur_minor or not cur_kube:
        out["problem"] = (
            f"the current OpenShift version could not be determined from ClusterVersion "
            f"({cur_version or 'no version recorded'})"
        )
        out["cause"] = "no_current"
        return out
    target: tuple[int, int] | None
    if target_version:
        target = parse_minor(target_version)
        if not target or not kube_minor_for_ocp(target):
            out["problem"] = f"target {target_version!r} is not an OpenShift 4 version"
            out["cause"] = "bad_target"
            return out
        if target <= cur_minor:
            out["problem"] = (
                f"target {_fmt(target)} is not a newer minor than the current {_fmt(cur_minor)}; "
                "patch updates within a minor do not remove APIs"
            )
            out["cause"] = "bad_target"
            return out
        out["source"] = "requested"
    else:
        newer = [m for m in _offered_minors(cv) if m > cur_minor]
        if newer:
            target = newer[0]
            out["source"] = "available_update"
        else:
            target = (cur_minor[0], cur_minor[1] + 1)
            out["source"] = "next_minor"
    out["target"] = target
    out["target_kube"] = kube_minor_for_ocp(target)
    return out


def _gate_scope(key: str, cur: tuple[int, int], target: tuple[int, int]) -> tuple[bool, str | None]:
    """Whether a gate key applies to the path cur -> target, and the release it gates.

    Keys look like ``ack-4.18-kube-1.32-api-removals-in-4.19``. A ``-in-X.Y``
    suffix names the release it gates. Without one, the CVO scopes a gate by
    its ``ack-X.Y-`` prefix: it applies to the step out of X.Y. A key matching
    neither is kept in scope, because dropping a gate the CVO enforces would
    turn a blocked upgrade into "go".
    """
    m = _GATE_TARGET_RE.search(key)
    if m:
        gated = parse_minor(m.group(1))
        if gated:
            return cur < gated <= target, _fmt(gated)
    p = _GATE_PREFIX_RE.match(key)
    if p:
        source = parse_minor(p.group(1))
        if source:
            return cur <= source < target, _fmt((source[0], source[1] + 1))
    return True, None


def _gate_mapping_warning(key: str) -> str | None:
    """Cross-check 4.N -> 1.(N+13) against a key that names both releases."""
    kube = _GATE_KUBE_RE.search(key)
    target = _GATE_TARGET_RE.search(key)
    if not kube or not target:
        return None
    gated = parse_minor(target.group(1))
    named = parse_minor(kube.group(1))
    expected = kube_minor_for_ocp(gated) if gated else None
    if gated and named and expected and expected != named:
        return (
            f"admin gate {key} pairs OpenShift {_fmt(gated)} with Kubernetes {_fmt(named)}, but this check "
            f"assumes Kubernetes {_fmt(expected)}; removal releases may be off by one minor"
        )
    return None


def compute_upgrade_api_removals(
    cluster_version: dict | ToolError,
    api_request_counts: list[dict] | ToolError,
    admin_gates: dict | Any | ToolError,
    admin_acks: dict | Any | ToolError,
    target_version: str = "",
) -> dict[str, Any]:
    """Decide go / no_go / unknown for upgrading to ``target_version``.

    Each input is what its read returned, or the ``ToolError`` it produced.
    The target comes from ``resolve_target``. An API is in scope when it is
    removed after the current Kubernetes minor and no later than the
    target's; in scope with requests in the last 24h it blocks the upgrade,
    with none it is "removed but unused". Gates are scoped by ``_gate_scope``.
    ClusterVersion ``Upgradeable=False`` blocks every minor upgrade, so it is
    a blocker of its own whatever the gates say.
    """
    result: dict[str, Any] = {
        "status": "unknown",
        "reason": "",
        # Why the status is unknown: not_openshift | read_failed | no_current |
        # bad_target. None when the status is go or no_go.
        "cause": None,
        "current": {"ocp": None, "kubernetes": None},
        "target": {"ocp": None, "kubernetes": None, "source": None},
        "channel": None,
        "upgradeable": {"status": "", "reason": "", "message": ""},
        "inUse": [],
        "unused": [],
        "scheduledForRemoval": [],
        "gates": [],
        "warnings": [],
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
    t = resolve_target(cv, target_version)
    cur_minor, cur_kube = t["current"], t["current_kube"]
    target_minor, target_kube = t["target"], t["target_kube"]
    result["current"] = {"ocp": t["current_version"], "kubernetes": _fmt(cur_kube)}
    result["target"] = {"ocp": _fmt(target_minor), "kubernetes": _fmt(target_kube), "source": t["source"]}

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
        # The CVO treats a missing admin-gates ConfigMap as not upgradeable,
        # so a 404 is no more a clean bill than a 403.
        what = "ConfigMap openshift-config-managed/admin-gates"
        checks["adminGates"] = (
            f"{what} does not exist (the CVO treats that as not upgradeable)"
            if admin_gates.status_code == 404
            else _read_failure(what, admin_gates)
        )
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
            in_scope, release = _gate_scope(key, cur_minor, target_minor)
            if not in_scope:
                continue
            # The CVO accepts exactly "true"; "True" or " true" is not an ack.
            acked = None if acks_data is None else acks_data.get(key) == "true"
            result["gates"].append({"key": key, "description": gates_data[key], "release": release, "acked": acked})
            warning = _gate_mapping_warning(key)
            if warning:
                result["warnings"].append(warning)

    # -- Verdict -----------------------------------------------------------
    if t["problem"]:
        result["reason"] = f"Cannot decide: {t['problem']}."
        result["cause"] = t["cause"]
        return result

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
        releases = sorted({g["release"] for g in unacked if g["release"]}, key=lambda r: parse_minor(r) or (0, 0))
        scope = f" for {', '.join(releases)}" if releases else ""
        blockers.append(f"{n} admin gate{'s' if n != 1 else ''}{scope} not acknowledged")
    up = result["upgradeable"]
    if up["status"] == "False":
        # Upgradeable=False blocks minor upgrades by design, whatever the reason.
        blockers.append(f"ClusterVersion reports Upgradeable=False ({up['reason'] or 'no reason'}): {up['message']}")

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
    reason = f"Upgrade to {_fmt(target_minor)} is clear of API removals: " + ", ".join(parts) + "."
    reason += (
        " APIRequestCount keeps only the last 24h, so a caller that runs less often than daily"
        " (a weekly job, a quarterly report) does not show here."
    )
    if cur_minor and target_minor and target_minor > (cur_minor[0], cur_minor[1] + 1):
        reason += (
            f" Only the admin gates published so far (for the step out of {_fmt(cur_minor)}) are covered;"
            " each intermediate release can publish its own when the cluster reaches it."
        )
    result["reason"] = reason
    return result


def _check_ok(value: str) -> bool:
    return value in ("ok", "none recorded")


def read_upgrade_api_removals(target_version: str = "") -> dict[str, Any]:
    """Read the four sources (read-only) and compute the verdict."""
    from . import k8s_client as _kc

    custom = _kc.get_custom_client()
    core = _kc.get_core_client()
    group, version, plural, name = CLUSTERVERSION
    cv = _kc.safe(lambda: custom.get_cluster_custom_object(group, version, plural, name))
    if isinstance(cv, ToolError):
        return compute_upgrade_api_removals(cv, [], {}, {}, target_version)

    if resolve_target(cv, target_version)["problem"]:
        # No target, no question to ask APIRequestCount: listing it is the
        # expensive read (one object per served resource version, with 24
        # hours of per-node, per-user counts each).
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
        src = {
            "requested": " (requested)",
            "available_update": " (next minor on offer)",
            "next_minor": f" (next minor; not yet offered on channel {data['channel'] or '(none)'})",
        }.get(tgt["source"], "")
        lines.append(f"Target: OpenShift {tgt['ocp']} (Kubernetes {tgt['kubernetes']}){src}")

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
    for warning in data.get("warnings", []):
        lines.append(f"Warning: {warning}")
    problems = [f"{k}: {v}" for k, v in data["checks"].items() if not _check_ok(v)]
    if problems:
        lines.append("")
        lines.append("Not checked: " + "; ".join(problems))
    return "\n".join(lines)
