"""Agent tools for Alertmanager silences.

``create_silence`` and ``expire_silence`` are write tools: they are in the
registry's write set, so the agent loop asks the person to confirm each call
(the confirmation carries a preview from :mod:`sre_agent.silences` with the
exact matchers, end time, comment and creator) and refuses it when the
caller's own token did not arrive. Alertmanager's proxy then applies the
caller's RBAC. ``createdBy`` is always the signed-in person, never a value the
model chose.

``propose_silence`` and ``list_silences`` only read. ``propose_silence`` turns
a finding (or an alert target) into the narrowest silence that hides its
alerts, and says what it would and would not hide, so the model proposes a
silence a person can check instead of inventing matchers.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

from . import alertmanager
from .decorators import beta_tool
from .k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace
from .monitor.confidence import sanitize_block
from .silences import (
    MAX_DURATION_MINUTES,
    Matcher,
    SilenceInputError,
    build_request,
    check_against_firing,
    describe_duration,
    labels_match,
    matchers_from_api,
    require_user_writes,
    split_by_matchers,
    validate_matchers,
    validate_silence_id,
)

logger = logging.getLogger("pulse_agent.silence_tools")

DEFAULT_PROPOSAL_MINUTES = 120

# Finding resource kind -> the alert label that names it (inverse of the
# firing-alert scanner's _ALERT_RESOURCE_LABELS).
_KIND_LABEL = {
    "Pod": "pod",
    "Deployment": "deployment",
    "StatefulSet": "statefulset",
    "DaemonSet": "daemonset",
    "Job": "job_name",
    "PersistentVolumeClaim": "persistentvolumeclaim",
    "HorizontalPodAutoscaler": "horizontalpodautoscaler",
    "Node": "node",
}


def _matchers_text(matchers: list[Matcher]) -> str:
    return ", ".join(str(m) for m in matchers)


def _cluster_text(text: Any, limit: int = 500) -> str:
    """Text that came from the cluster (silence comments and creators, alert and Inbox names), made safe to show the model."""
    return sanitize_block(str(text or ""))[:limit].replace("\n", " ")


def _cluster_block(lines: list[str]) -> str:
    return "--- BEGIN CLUSTER DATA ---\n" + "\n".join(lines) + "\n--- END CLUSTER DATA ---"


@beta_tool
def create_silence(matchers: str, duration_minutes: int, comment: str):
    """Create an Alertmanager silence. Requires the user's confirmation.

    Use propose_silence first to get the narrowest matchers for a finding or
    alert. The silence is created as the signed-in user (createdBy), with the
    user's own Alertmanager permissions.

    Args:
        matchers: Comma-separated matchers, e.g. 'node="worker-5", alertname=~"KubeNodeUnreachable|KubeletDown"'.
            Operators: = != =~ !~. At least one positive matcher must name something specific; empty or
            match-everything sets are refused.
        duration_minutes: How long the silence lasts, 5 minutes to 7 days (10080).
        comment: Why the alerts are silenced. Required; shown on the silence and in the audit log.
    """
    from .request_context import get_current_user

    try:
        require_user_writes()
        args = {"matchers": matchers, "duration_minutes": duration_minutes, "comment": comment}
        build_request(args, get_current_user())  # refuse bad input before any read
        try:
            firing = alertmanager.list_alerts(active=True, silenced=False, inhibited=False)
        except alertmanager.AlertmanagerError as e:
            return f"Silence not created: Pulse cannot check how many alerts it would hide. {e.describe()}"
        # Times are taken now, at write time, not when the request was confirmed.
        req = build_request(args, get_current_user())
        check_against_firing(req.matchers, firing)
    except SilenceInputError as e:
        return f"Silence not created: {e}"
    try:
        silence_id = alertmanager.create_silence(req.to_api())
    except alertmanager.AlertmanagerError as e:
        return f"Silence not created. {e.describe()}"
    logger.info(
        "Silence %s created by %s for %s (%s): %s",
        silence_id,
        req.created_by,
        _matchers_text(req.matchers),
        describe_duration(req.duration_minutes),
        req.comment,
    )
    return (
        f"Silence {silence_id} created by {req.created_by} for {describe_duration(req.duration_minutes)} "
        f"(until {req.to_api()['endsAt']}).\n"
        f"Matchers: {_matchers_text(req.matchers)}\n"
        f"Comment: {req.comment}\n"
        f"Undo: expire_silence(silence_id='{silence_id}'), or Alerts > Silences > Expire."
    )


@beta_tool
def expire_silence(silence_id: str):
    """Expire (end now) an Alertmanager silence by id. Requires the user's confirmation.

    The alerts it muted start notifying again. Use list_silences to find the id.

    Args:
        silence_id: The silence's UUID.
    """
    try:
        require_user_writes()
        sid = validate_silence_id(silence_id)
    except SilenceInputError as e:
        return f"Silence not expired: {e}"
    try:
        silence = alertmanager.get_silence(sid)
    except alertmanager.AlertmanagerError as e:
        return f"Silence not expired. {e.describe()}"
    state = (silence.get("status") or {}).get("state", "")
    described = _matchers_text(matchers_from_api(silence.get("matchers") or []))
    if state == "expired":
        return f"Silence {sid} had already expired; nothing changed.\n" + _cluster_block(
            [f"Matchers: {_cluster_text(described)}"]
        )
    try:
        alertmanager.expire_silence(sid)
    except alertmanager.AlertmanagerError as e:
        return f"Silence not expired. {e.describe()}"
    logger.info("Silence %s (%s) expired", sid, described)
    return f"Silence {sid} expired. Its alerts notify again.\n" + _cluster_block(
        [
            f"Matchers: {_cluster_text(described)}",
            f"Created by: {_cluster_text(silence.get('createdBy', 'unknown'), 200)}",
            f"Comment: {_cluster_text(silence.get('comment', ''))}",
        ]
    )


@beta_tool
def list_silences(state: str = "active"):
    """List Alertmanager silences with their ids, matchers, creator, comment and end time.

    Args:
        state: 'active', 'pending', 'expired' or 'all'.
    """
    if state not in ("active", "pending", "expired", "all"):
        return "state must be one of: active, pending, expired, all."
    try:
        silences = alertmanager.list_silences()
    except alertmanager.AlertmanagerError as e:
        return f"Silences could not be read. {e.describe()}"
    shown = [s for s in silences if state == "all" or (s.get("status") or {}).get("state") == state]
    if not shown:
        return f"Alertmanager reports no {state} silences." if state != "all" else "Alertmanager reports no silences."
    lines = []
    for s in sorted(shown, key=lambda x: x.get("endsAt", "")):
        lines.append(
            f"- {_cluster_text(s.get('id'), 64)} [{_cluster_text((s.get('status') or {}).get('state', '?'), 20)}] "
            f"{_cluster_text(_matchers_text(matchers_from_api(s.get('matchers') or [])))} "
            f"until {_cluster_text(s.get('endsAt', '?'), 40)} by {_cluster_text(s.get('createdBy', '?'), 200)}: "
            f"{_cluster_text(s.get('comment', ''))}"
        )
    return f"{len(shown)} {state if state != 'all' else ''} silence(s):\n" + _cluster_block(lines)


def _target_from_item(item: dict) -> tuple[dict[str, str], str, dict]:
    """Alert labels that identify the item's resource, its alert name (if it is an alert), and context."""
    resources = item.get("resources") or []
    labels: dict[str, str] = {}
    if resources:
        r = resources[0]
        kind, name, ns = r.get("kind", ""), r.get("name", ""), r.get("namespace", "") or ""
        label = _KIND_LABEL.get(kind)
        if label == "node":
            labels["node"] = name
        elif label and name:
            labels[label] = name
            if ns:
                labels["namespace"] = ns
        elif ns:
            labels["namespace"] = ns
    elif item.get("namespace"):
        labels["namespace"] = item["namespace"]
    corr = item.get("correlation_key") or ""
    alertname = item.get("title", "") if corr.startswith("alerts:") else ""
    return labels, alertname, item


def _lookup_item(finding_id: str) -> dict | None:
    from .inbox import _deserialize_row, get_inbox_item
    from .repositories.inbox_repo import get_inbox_repo

    item = get_inbox_item(finding_id)
    if item is not None:
        return item
    row = get_inbox_repo().find_active_by_finding_id(finding_id)
    return _deserialize_row(row) if row else None


def _proposal_minutes(due_date: Any, now: float) -> tuple[int, str]:
    try:
        due = float(due_date) if due_date else 0.0
    except (TypeError, ValueError):
        due = 0.0
    if due > now:
        minutes = math.ceil((due - now) / 60 + 5)
        minutes = int(math.ceil(minutes / 15) * 15)
        minutes = max(30, min(MAX_DURATION_MINUTES, minutes))
        return minutes, "covers the Inbox item's due time"
    return DEFAULT_PROPOSAL_MINUTES, "a default 2 hours; shorten or lengthen to the work"


@beta_tool
def propose_silence(
    finding_id: str = "",
    alertname: str = "",
    namespace: str = "",
    node: str = "",
    pod: str = "",
    reason: str = "",
):
    """Propose (do not create) the narrowest Alertmanager silence for a finding or a firing alert.

    Reads the alerts firing now and returns matchers, a duration, a draft comment,
    what the silence would hide and what it would not. Then call create_silence
    with those arguments; the user confirms it. Give finding_id (an Inbox item id
    or monitor finding id) or a target (node, or namespace with an optional pod),
    optionally narrowed by alertname.

    Args:
        finding_id: Inbox item id or monitor finding id the silence is for.
        alertname: Only this alert (default: every alert firing on the target).
        namespace: Target namespace.
        node: Target node.
        pod: Target pod (needs namespace).
        reason: Why it should be silenced (planned work, known issue); used in the comment.
    """
    labels: dict[str, str] = {}
    item: dict = {}
    if finding_id:
        try:
            found = _lookup_item(finding_id.strip())
        except Exception:
            logger.debug("Inbox lookup failed for %s", finding_id, exc_info=True)
            return (
                "The Inbox could not be read, so the finding's target is unknown. Name the node or namespace instead."
            )
        if found is None:
            return f"No open Inbox item or finding has id {finding_id!r}."
        labels, item_alert, item = _target_from_item(found)
        alertname = alertname or item_alert
    if node:
        if err := _validate_k8s_name(node, "node"):
            return err
        labels = {"node": node}
    if namespace:
        if err := _validate_k8s_namespace(namespace):
            return err
        labels["namespace"] = namespace
    if pod:
        if err := _validate_k8s_name(pod, "pod"):
            return err
        if not labels.get("namespace"):
            return "A pod target needs its namespace."
        labels["pod"] = pod
    if alertname and not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", alertname):
        return f"{alertname!r} is not a valid alert name."
    if not labels and not alertname:
        return "Name what to silence: finding_id, or a node / namespace (and optionally alertname)."

    try:
        firing = alertmanager.list_alerts(active=True, silenced=False, inhibited=False)
    except alertmanager.AlertmanagerError as e:
        return f"No silence proposed: the firing alerts could not be read. {e.describe()}"

    target = [Matcher(k, v) for k, v in sorted(labels.items())]
    on_target = [a for a in firing if labels_match(a.get("labels") or {}, target)]
    names = sorted({(a.get("labels") or {}).get("alertname", "") for a in on_target} - {""})
    if alertname:
        chosen = [alertname]
    else:
        chosen = [n for n in names if n != "Watchdog"]
    if not chosen or not on_target:
        where = _matchers_text(target) or f'alertname="{alertname}"'
        return f"No unsilenced alert is firing for {where}; there is nothing to silence right now."
    if len(chosen) == 1:
        name_matcher = Matcher("alertname", chosen[0])
    else:
        name_matcher = Matcher("alertname", "|".join(re.escape(n) for n in chosen), is_regex=True)
    matchers = [*target, name_matcher]
    try:
        validate_matchers(matchers)
    except SilenceInputError as e:
        return f"No silence proposed: {e}"

    try:
        check_against_firing(matchers, firing)
    except SilenceInputError as e:
        return f"No silence proposed: {e}"
    hides = split_by_matchers(firing, matchers)
    not_hidden = [a for a in split_by_matchers(firing, target) if a not in hides]
    minutes, why_duration = _proposal_minutes(item.get("due_date"), time.time())
    comment_parts = [reason.strip()] if reason.strip() else []
    if item.get("id"):
        # The item's id, not its title: a title is cluster text and would be
        # copied verbatim into the silence and the model's next call.
        comment_parts.append(f"(Inbox item {_cluster_text(item['id'], 64)})")
    comment = " ".join(comment_parts) or "<say why these alerts are expected, e.g. planned maintenance>"

    data = [
        f"Would hide {len(hides)} firing alert(s) now, and any new alert with these labels until it ends:",
        *[
            f"  - [{_cluster_text(a['severity'] or '?', 20)}] {_cluster_text(a['alertname'], 120)} since {_cluster_text(a['startsAt'][:19], 20)}"
            for a in hides
        ],
    ]
    if not_hidden:
        data.append("Would NOT hide (same target, other alerts):")
        data += [
            f"  - [{_cluster_text(a['severity'] or '?', 20)}] {_cluster_text(a['alertname'], 120)}" for a in not_hidden
        ]
    if item.get("title"):
        data.append(f"Inbox item: {_cluster_text(item['title'], 200)}")
    if item.get("claimed_by"):
        data.append(f"Inbox owner: {_cluster_text(item['claimed_by'], 200)}")
    lines = [
        "Proposed silence (not created):",
        f"  matchers: {_matchers_text(matchers)}",
        f"  duration_minutes: {minutes} ({describe_duration(minutes)}; {why_duration})",
        f"  comment: {comment}",
        _cluster_block(data),
    ]
    lines.append(
        "To create it, call create_silence with exactly these matchers, duration_minutes and a real comment; "
        "the user reviews and confirms it."
    )
    return "\n".join(lines)


SILENCE_TOOLS: list[Any] = [create_silence, expire_silence, list_silences, propose_silence]
SILENCE_WRITE_TOOLS = {"create_silence", "expire_silence"}

from .tool_registry import register_tool

for _tool in SILENCE_TOOLS:
    register_tool(_tool, is_write=(_tool.name in SILENCE_WRITE_TOOLS), category="monitoring")
