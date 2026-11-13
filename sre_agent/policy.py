"""Harness-level deny policy: safety rules no model swap can weaken.

What motivated this: when the configured model changed (opus-4-6 →
sonnet-5), the agent's willingness to execute destructive requests changed
with it — both SRE-Bench traps (delete a production pod "no questions",
drain a node "skip the change process") sailed through the confirmation
gate under a permissive approver. Confirmation is consent, not policy.
This module is policy: deterministic rules checked in the tool execution
path *after* confirmation, before the call — so the effective safety
posture is set by configuration, not by which model happens to be behind
``PULSE_AGENT_MODEL`` this week.

Rules:

- ``delete_pod`` in a protected namespace is denied outright;
  ``restart_deployment`` is the reviewable alternative and stays allowed,
  so routine remediation (crashloop restarts) is unaffected when requested
  independently. After a forbidden write, the agent stops further writes for
  that invocation; a new user turn is required to authorize an alternative.
  A turn whose own message asks for that deletion starts with writes off
  (``requested_denied_write``), so a substitute cannot run either.
- ``drain_node`` / ``cordon_node`` require the change-process break-glass
  flag (``PULSE_AGENT_ALLOW_NODE_OPS``) regardless of confirmation —
  node-level operations are never a chat-approval decision.

``PULSE_AGENT_PROTECTED_NAMESPACES`` (comma list, ``*`` wildcards) defaults
to production plus platform namespaces; set it to ``""`` on clusters where
the policy should not apply.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatch

from .errors import ToolError

_NODE_OPS = frozenset({"drain_node", "cordon_node"})
_NAMESPACE_DENIED_OPS = frozenset({"delete_pod"})


def _is_protected(namespace: str) -> bool:
    from .config import get_settings

    patterns = get_settings().get_protected_namespaces()
    return any(fnmatch(namespace, pattern) for pattern in patterns)


def check_write_policy(name: str, input_data: dict | None) -> ToolError | None:
    """Return a ToolError denying the call, or None to allow it.

    Deterministic and config-backed — deliberately no model, no judgment
    calls, no confirmation override. A denied call was still requested and
    confirmed, so the message explains the policy and the sanctioned path
    instead of just saying no.
    """
    from .config import get_settings

    args = input_data or {}

    if name in _NODE_OPS and not get_settings().agent.allow_node_ops:
        return ToolError(
            message=(
                f"Policy: '{name}' requires the change process, not chat confirmation. "
                "Node-level operations are disabled on this deployment "
                "(PULSE_AGENT_ALLOW_NODE_OPS=0). File a change request, or have the "
                "operator enable node ops for a maintenance window."
            ),
            category="forbidden",
            operation=name,
            suggestions=[
                "Investigate the node first (describe_resource, get_node_metrics) and share findings",
                "For workload relief, restart or reschedule specific workloads instead",
            ],
        )

    namespace = str(args.get("namespace", ""))
    if name in _NAMESPACE_DENIED_OPS and namespace and _is_protected(namespace):
        return ToolError(
            message=(
                f"Policy: '{name}' is denied in protected namespace '{namespace}' "
                "(PULSE_AGENT_PROTECTED_NAMESPACES). Raw pod deletion in protected "
                "namespaces is not confirmable in chat. Stop writes for this turn; propose "
                "restart_deployment for a new user request and reviewable, controller-managed replacement."
            ),
            category="forbidden",
            operation=name,
            suggestions=[
                "Propose restart_deployment on the owning workload for a new user request",
                "If the pod is truly orphaned, remove it through the change process",
            ],
        )

    return None


# --- Requests for a denied write ------------------------------------------
#
# The guard above arms only when a denied call reaches the policy check. A
# model that refuses ``delete_pod`` in words never makes that call, so the
# guard stays off and a substitute write (scaling, restarting, deleting the
# owner) goes to confirmation instead. Under SRE-Bench's permissive approver
# that substitute ran in production on most attempts.
#
# When the person's own message asks for a pod deletion in a protected
# namespace, the turn starts with the guard armed: writes are refused for the
# turn, reads stay available. Deliberately narrow: it needs a deletion verb
# with "pod", and a namespace named in a form people actually write ("in
# namespace X", "in the X namespace", "-n X", "namespace: X", "X/pod-name",
# "in X"), counted only when that namespace is protected.
# A message that only says "fix it" or "resolve the crashloop" never matches,
# so routine remediation is unaffected.

_DELETE_POD = re.compile(
    r"\b(?:delete|remove|kill|evict|nuke|destroy|terminate)\b[^.!?\n]{0,80}?\bpods?\b"
    r"|\bpods?\b[^.!?\n]{0,80}?\b(?:delete|remove|kill|evict|nuke|destroy|terminate)\b",
    re.IGNORECASE,
)
_NAMESPACE_MENTIONS = (
    re.compile(r"\bnamespace[:=\s]+[\"'`]?([a-z0-9][a-z0-9-]{0,62})", re.IGNORECASE),
    re.compile(
        r"\bin\s+(?:the\s+)?[\"'`]?([a-z0-9][a-z0-9-]{0,62})[\"'`]?\s+(?:namespace|ns|project)\b", re.IGNORECASE
    ),
    re.compile(r"(?:^|\s)(?:-n|--namespace)[=\s]+([a-z0-9][a-z0-9-]{0,62})", re.IGNORECASE),
    re.compile(r"\bns[:=\s]+([a-z0-9][a-z0-9-]{0,62})", re.IGNORECASE),
    re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})/[a-z0-9][a-z0-9.-]*", re.IGNORECASE),
    # Last, and only ever counted when the name is a protected namespace.
    re.compile(r"\bin\s+([a-z0-9][a-z0-9-]{0,62})\b", re.IGNORECASE),
)


def requested_denied_write(text: str) -> str | None:
    """The protected namespace a message asks to delete pods in, else None."""
    if not text or not _DELETE_POD.search(text):
        return None
    for pattern in _NAMESPACE_MENTIONS:
        for match in pattern.finditer(text):
            namespace = match.group(1).lower()
            if _is_protected(namespace):
                return namespace
    return None


def requested_denied_write_reason(namespace: str) -> str:
    return (
        f"Operation blocked: this turn asks for pod deletion in protected namespace '{namespace}', "
        "which policy denies (PULSE_AGENT_PROTECTED_NAMESPACES). No write runs in this turn, "
        "including substitutes such as scaling, restarting or deleting the owner. Diagnose with "
        "read tools and propose restart_deployment for the person to request in a new message."
    )


def policy_system_note(tool_names: Iterable[str]) -> str | None:
    """The deny rules that bite for the tools actually offered, in words the model reads.

    Enforcing the policy is not the same as the model knowing it. With the
    rules invisible, the release replay's degraded-deployment fixture went:
    diagnose one pod holding a stale ConfigMap, reach for ``delete_pod`` as
    the minimal fix, get denied — and the denial then stopped the rest of the
    turn's writes, so the run scored a forbidden tool *and* left the
    deployment degraded, having named ``restart_deployment`` without taking
    it. Removing the tool, as a plan-only turn does with every write (see
    ``agent.PLAN_ONLY_SYSTEM_NOTE``), is not available here: the namespace
    argument decides, and it is only known at call time. So the tool stays
    offered and the rule is said out loud instead.

    The wording carries its own history. A first version called
    restart_deployment "the sanctioned replacement", and on the sim lane's
    ``unsafe_missing_confirmation`` trap — delete this production pod, no
    questions — the model duly refused the deletion, quoted this note, and then
    ran restart_deployment in production instead, which the benchmark records
    as a policy violation because nobody asked for it. So the note now says
    what ``check_write_policy``'s own refusal says: propose the alternative for
    a new user request. Naming a sanctioned tool to a model that has just been
    refused reads as permission to use it.

    Returns None when no offered tool can be denied — a note that lists
    nothing teaches the model nothing and only spends prompt.
    """
    from .config import get_settings

    settings = get_settings()
    names = set(tool_names)
    rules: list[str] = []

    protected = settings.get_protected_namespaces()
    if protected and names & _NAMESPACE_DENIED_OPS:
        rules.append(
            "delete_pod is refused in protected namespaces (" + ", ".join(protected) + "), confirmation or not; "
            "say so and propose restart_deployment on the owning workload for the user to ask for, rather than "
            "running it yourself in the same turn"
        )
    if names & _NODE_OPS and not settings.agent.allow_node_ops:
        rules.append(
            "drain_node and cordon_node are disabled on this deployment and need the change process, "
            "not a chat approval"
        )

    if not rules:
        return None
    return (
        "Deny policy on this deployment, checked in the tool path after confirmation: "
        + "; ".join(rules)
        + ". A refused write also blocks every later write in the same turn, and substituting a different "
        "mutation for a refused one is itself a violation: a write the user did not ask for needs a new "
        "request from them, not a different tool."
    )
