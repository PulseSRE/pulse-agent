"""OLM InstallPlan approvals: which operator updates wait for a person, and approving one.

A Subscription with ``installPlanApproval: Manual`` stops at an InstallPlan
with ``spec.approved: false`` until someone sets it to true. Finding those
meant opening each operator in turn. ``list_pending_installplans`` lists every
one across namespaces with the operator, the CSV it replaces and the CSV it
installs; ``approve_installplan`` approves one behind the confirmation gate.

There is no "deny". OLM has no reject for an InstallPlan: the honest ways to
decline an update are to leave the plan unapproved (nothing installs and the
installed version keeps running), to keep the Subscription on Manual approval,
or to change its channel. Deleting the plan is not a refusal: while the
Subscription still resolves the newer CSV, OLM usually creates a new plan for
the same update.

The approval runs as the person (their forwarded token), never as Pulse's
service account, and is held to the resourceVersion they reviewed: if the plan
changed after they listed it, the approval is refused rather than applied to
something they did not see.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from ..monitor.confidence import sanitize_block
from .validators import _validate_k8s_name, _validate_k8s_namespace

logger = logging.getLogger("pulse_agent.k8s_tools.olm")

GROUP = "operators.coreos.com"
VERSION = "v1alpha1"
MAX_PLANS = 100
MAX_STEPS_SHOWN = 40

# Argo CD marks what it manages with a tracking annotation (annotation
# tracking) or the app.kubernetes.io/instance label (label tracking, the default).
_ARGO_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
_ARGO_INSTANCE_LABEL = "app.kubernetes.io/instance"

_CSV_VERSION_RE = re.compile(r"^(?P<pkg>.+?)\.v?(?P<ver>\d+\.\d+.*)$")

NO_DENY_TEXT = (
    "OLM has no 'deny' for an InstallPlan. To decline this update, leave the plan unapproved: nothing installs "
    "and the current version keeps running. To stop future updates arriving automatically, keep the "
    "Subscription's installPlanApproval on Manual, or move it to another channel. Deleting the plan does not "
    "refuse the update: while the Subscription still resolves the newer CSV, OLM usually creates a new plan for it."
)


class OLMReadError(Exception):
    """InstallPlans could not be read (forbidden, OLM absent, API error)."""


class ApprovalInputError(Exception):
    """An approval that must not be attempted, with the reason for the person."""


def _cluster_text(text: Any, limit: int = 200) -> str:
    return sanitize_block(str(text or ""))[:limit].replace("\n", " ")


def csv_version(csv_name: str) -> str:
    """'elasticsearch-operator.v5.8.1' -> '5.8.1'; the name itself when no version is in it."""
    m = _CSV_VERSION_RE.match(csv_name or "")
    return m.group("ver") if m else (csv_name or "")


def _csv_package(csv_name: str) -> str:
    m = _CSV_VERSION_RE.match(csv_name or "")
    return m.group("pkg") if m else (csv_name or "")


def gitops_owner(obj: dict) -> str | None:
    """The Argo CD application that manages ``obj``, if it is tracked by one."""
    meta = obj.get("metadata") or {}
    tracking = (meta.get("annotations") or {}).get(_ARGO_TRACKING_ANNOTATION)
    if tracking:
        return str(tracking).split(":", 1)[0]
    instance = (meta.get("labels") or {}).get(_ARGO_INSTANCE_LABEL)
    return str(instance) if instance else None


def _created_ts(meta: dict) -> float | None:
    ts = meta.get("creationTimestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _age_text(created: float | None, now: float | None = None) -> str:
    if created is None:
        return "unknown age"
    secs = max(0, int((now or time.time()) - created))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _owning_subscription_name(plan: dict) -> str | None:
    for ref in (plan.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("kind") == "Subscription" and ref.get("name"):
            return str(ref["name"])
    return None


def _replaced_by_manifests(plan: dict) -> list[str]:
    """``spec.replaces`` of the CSVs in the plan's steps, when the manifests are inline."""
    replaced: list[str] = []
    for step in (plan.get("status") or {}).get("plan") or []:
        res = step.get("resource") or {}
        if res.get("kind") != "ClusterServiceVersion":
            continue
        try:
            manifest = json.loads(res.get("manifest") or "{}")
        except (TypeError, ValueError):
            continue
        rep = ((manifest or {}).get("spec") or {}).get("replaces") if isinstance(manifest, dict) else None
        if rep and rep not in replaced:
            replaced.append(str(rep))
    return replaced


@dataclass
class PendingPlan:
    namespace: str
    name: str
    resource_version: str
    installs: list[str]
    replaces: list[str]
    operator: str
    subscription: str | None
    channel: str | None
    approval: str
    phase: str
    created: float | None
    # True: the Subscription points at this plan. False: a newer plan replaced
    # it. None: the Subscription could not be read.
    current: bool | None
    gitops_app: str | None = None
    subscription_gitops_app: str | None = None
    steps: list[dict] = field(default_factory=list)

    @property
    def from_text(self) -> str:
        return ", ".join(self.replaces) if self.replaces else "nothing installed yet / unknown"

    def summary(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "resourceVersion": self.resource_version,
            "operator": self.operator,
            "subscription": self.subscription,
            "channel": self.channel,
            "installs": self.installs,
            "replaces": self.replaces,
            "phase": self.phase,
            "current": self.current,
        }


def _list(plural: str, namespace: str) -> dict | ToolError:
    api = _kc.get_custom_client()
    if namespace.upper() == "ALL":
        return _kc.safe(lambda: api.list_cluster_custom_object(GROUP, VERSION, plural))
    return _kc.safe(lambda: api.list_namespaced_custom_object(GROUP, VERSION, namespace, plural))


def _plan_from_objects(plan: dict, subs: dict[tuple[str, str], dict] | None) -> PendingPlan:
    meta = plan.get("metadata") or {}
    spec = plan.get("spec") or {}
    status = plan.get("status") or {}
    ns = str(meta.get("namespace", ""))
    name = str(meta.get("name", ""))
    sub_name = _owning_subscription_name(plan)
    sub = subs.get((ns, sub_name)) if (subs is not None and sub_name) else None
    installs = [str(c) for c in spec.get("clusterServiceVersionNames") or []]
    replaces: list[str] = []
    current: bool | None = None
    channel: str | None = None
    operator = ""
    if sub is not None:
        sub_spec = sub.get("spec") or {}
        sub_status = sub.get("status") or {}
        installed = sub_status.get("installedCSV")
        if installed and installed not in installs:
            replaces.append(str(installed))
        channel = sub_spec.get("channel")
        operator = str(sub_spec.get("name") or "")
        ref = (sub_status.get("installPlanRef") or {}).get("name")
        current = (ref == name) if ref else None
    for rep in _replaced_by_manifests(plan):
        if rep not in replaces and rep not in installs:
            replaces.append(rep)
    if not operator:
        operator = _csv_package(installs[0]) if installs else (sub_name or name)
    steps = [
        {"kind": str((s.get("resource") or {}).get("kind", "")), "name": str((s.get("resource") or {}).get("name", ""))}
        for s in status.get("plan") or []
    ]
    return PendingPlan(
        namespace=ns,
        name=name,
        resource_version=str(meta.get("resourceVersion", "")),
        installs=installs,
        replaces=replaces,
        operator=operator,
        subscription=sub_name,
        channel=str(channel) if channel else None,
        approval=str(spec.get("approval", "")),
        phase=str(status.get("phase", "")),
        created=_created_ts(meta),
        current=current,
        gitops_app=gitops_owner(plan),
        subscription_gitops_app=gitops_owner(sub) if sub is not None else None,
        steps=steps,
    )


def is_pending(plan: dict) -> bool:
    """Waiting for a person: not approved, and not already failed or finished."""
    if (plan.get("spec") or {}).get("approved") is not False:
        return False
    return (plan.get("status") or {}).get("phase") not in ("Failed", "Complete")


def read_pending_installplans(namespace: str = "ALL") -> tuple[list[PendingPlan], str | None]:
    """Unapproved InstallPlans, oldest first, and why Subscriptions could not be read (or None).

    Raises OLMReadError when the InstallPlans themselves cannot be listed:
    "could not look" is not "nothing is pending".
    """
    plans = _list("installplans", namespace)
    if isinstance(plans, ToolError) and plans.category == "not_found":
        # The InstallPlan API is not served: OLM is not installed, so nothing can wait for approval.
        return [], None
    if isinstance(plans, ToolError) or not isinstance(plans, dict):
        raise OLMReadError(f"InstallPlans could not be listed: {plans}")
    subs_result = _list("subscriptions", namespace)
    subs: dict[tuple[str, str], dict] | None
    subs_error: str | None = None
    if isinstance(subs_result, ToolError) or not isinstance(subs_result, dict):
        subs, subs_error = (
            None,
            f"Subscriptions could not be read ({subs_result}); from-versions and channels are unknown.",
        )
    else:
        subs = {
            (str((s.get("metadata") or {}).get("namespace", "")), str((s.get("metadata") or {}).get("name", ""))): s
            for s in subs_result.get("items") or []
        }
    pending = [_plan_from_objects(p, subs) for p in plans.get("items") or [] if is_pending(p)]
    pending.sort(key=lambda p: (p.created or 0, p.namespace, p.name))
    return pending, subs_error


def _cluster_block(lines: list[str]) -> str:
    return "--- BEGIN CLUSTER DATA ---\n" + "\n".join(lines) + "\n--- END CLUSTER DATA ---"


@beta_tool
def list_pending_installplans(namespace: str = "ALL"):
    """List OLM InstallPlans waiting for approval (spec.approved=false): the operator, the CSV it replaces and the CSV it installs, channel, age and resourceVersion. Use for "which operator updates are waiting for approval?".

    Approve one with approve_installplan(namespace, name, resource_version). OLM has no 'deny': leaving a plan
    unapproved is how an update is declined.

    Args:
        namespace: Namespace to check, or 'ALL' (default) for every namespace.
    """
    if namespace.upper() != "ALL":
        if err := _validate_k8s_namespace(namespace):
            return err
    try:
        pending, subs_error = read_pending_installplans(namespace)
    except OLMReadError as e:
        return f"{e}. OLM may not be installed, or you may not be allowed to list InstallPlans here."
    where = "any namespace" if namespace.upper() == "ALL" else f"namespace '{namespace}'"
    if not pending:
        text = f"No InstallPlan is waiting for approval in {where}."
        return text + (f"\nNote: {subs_error}" if subs_error else "")
    now = time.time()
    lines = []
    for p in pending[:MAX_PLANS]:
        state = {True: "current", False: "SUPERSEDED by a newer plan (do not approve)", None: "unknown if current"}[
            p.current
        ]
        lines.append(
            f"- {_cluster_text(p.namespace, 63)}/{_cluster_text(p.name, 253)} rv={_cluster_text(p.resource_version, 32)} "
            f"operator={_cluster_text(p.operator)} channel={_cluster_text(p.channel or '?', 100)} "
            f"from={_cluster_text(p.from_text, 400)} to={_cluster_text(', '.join(p.installs) or '?', 400)} "
            f"age={_age_text(p.created, now)} phase={_cluster_text(p.phase or '?', 40)} [{state}]"
            + (
                f" gitops={_cluster_text(p.subscription_gitops_app or p.gitops_app, 100)}"
                if (p.subscription_gitops_app or p.gitops_app)
                else ""
            )
        )
    more = f"\n(+{len(pending) - MAX_PLANS} more not shown)" if len(pending) > MAX_PLANS else ""
    out = f"{len(pending)} InstallPlan(s) waiting for approval in {where}:\n" + _cluster_block(lines) + more
    if subs_error:
        out += f"\nNote: {subs_error}"
    out += (
        "\nTo approve one, call approve_installplan with its namespace, name and rv as resource_version; "
        "the user reviews what it installs and confirms. " + NO_DENY_TEXT
    )
    return out


def require_user_writes() -> None:
    """Approvals run as the person, never as Pulse's service account."""
    from ..config import get_settings

    if not get_settings().agent.token_forwarding:
        raise ApprovalInputError(
            "InstallPlans are not approved from chat on this deployment: token forwarding is off "
            "(PULSE_AGENT_TOKEN_FORWARDING=false), so the approval would run as Pulse's service account instead "
            "of as you. Approve it in Operators > Needs approval, which uses your own permissions."
        )
    if not _kc.get_current_user_token():
        raise ApprovalInputError(
            "Your access token did not reach Pulse with this request, and InstallPlans are never approved as "
            "Pulse's service account. Sign in again and retry, or approve it in Operators > Needs approval."
        )


def _validate_args(args: dict) -> tuple[str, str, str]:
    namespace = str(args.get("namespace") or "").strip()
    name = str(args.get("name") or "").strip()
    rv = str(args.get("resource_version") or "").strip()
    if err := _validate_k8s_namespace(namespace):
        raise ApprovalInputError(err)
    if err := _validate_k8s_name(name):
        raise ApprovalInputError(err)
    if not rv or not rv.isdigit():
        raise ApprovalInputError(
            "resource_version is required: pass the rv that list_pending_installplans showed for this plan, so the "
            "approval applies only to the plan that was reviewed."
        )
    return namespace, name, rv


def _read_plan(namespace: str, name: str) -> tuple[dict, dict | None]:
    """The InstallPlan and its owning Subscription (None if it has none or cannot be read)."""
    api = _kc.get_custom_client()
    plan = _kc.safe(lambda: api.get_namespaced_custom_object(GROUP, VERSION, namespace, "installplans", name))
    if isinstance(plan, ToolError) or not isinstance(plan, dict):
        raise ApprovalInputError(f"InstallPlan {namespace}/{name} could not be read: {plan}")
    sub_name = _owning_subscription_name(plan)
    sub = None
    if sub_name:
        got = _kc.safe(lambda: api.get_namespaced_custom_object(GROUP, VERSION, namespace, "subscriptions", sub_name))
        sub = got if isinstance(got, dict) else None
    return plan, sub


def _info_for(plan: dict, sub: dict | None) -> PendingPlan:
    meta = plan.get("metadata") or {}
    sub_name = _owning_subscription_name(plan)
    if sub is None or not sub_name:
        return _plan_from_objects(plan, None)
    return _plan_from_objects(plan, {(str(meta.get("namespace", "")), sub_name): sub})


def _check_approvable(plan: dict, info: PendingPlan, rv: str) -> None:
    current_rv = str((plan.get("metadata") or {}).get("resourceVersion", ""))
    if (plan.get("spec") or {}).get("approved") is True:
        raise ApprovalInputError(f"InstallPlan {info.namespace}/{info.name} is already approved; nothing to do.")
    if info.phase in ("Failed", "Complete"):
        raise ApprovalInputError(
            f"InstallPlan {info.namespace}/{info.name} is in phase {info.phase}; approving it would not install anything."
        )
    if current_rv != rv:
        raise ApprovalInputError(
            f"InstallPlan {info.namespace}/{info.name} changed since it was reviewed (resourceVersion {rv} -> "
            f"{current_rv}). Nothing was approved. List the pending plans again and review the current one."
        )
    if info.current is False:
        raise ApprovalInputError(
            f"InstallPlan {info.namespace}/{info.name} is superseded: its Subscription now points at a newer plan. "
            "Approve the current plan instead (list_pending_installplans marks it 'current')."
        )


def _patch(namespace: str, name: str, rv: str, *, dry_run: bool) -> Any:
    # resourceVersion in a merge patch is a precondition: the API server
    # refuses with 409 if the object changed since that version.
    body = {"metadata": {"resourceVersion": rv}, "spec": {"approved": True}}
    api = _kc.get_custom_client()
    kwargs: dict[str, Any] = {"dry_run": "All"} if dry_run else {}
    return _kc.safe(
        lambda: api.patch_namespaced_custom_object(GROUP, VERSION, namespace, "installplans", name, body, **kwargs)
    )


def _gitops_note(info: PendingPlan) -> str | None:
    if info.gitops_app:
        return (
            f"This InstallPlan is tracked by Argo CD application '{info.gitops_app}'. Approving it here changes a "
            "GitOps-managed object; prefer a pull request to the application's source (propose_git_change)."
        )
    if info.subscription_gitops_app:
        return (
            f"Subscription '{info.subscription}' is managed by Argo CD application '{info.subscription_gitops_app}'. "
            "Approving the plan is an OLM action and does not conflict with it, but approval mode, channel and "
            "startingCSV live in Git: change those with a pull request (propose_git_change), not here."
        )
    return None


def preview_approve(args: dict) -> dict[str, Any]:
    """What the chat shows before approve_installplan runs: what it installs and replaces, and the server's dry run."""
    from ..request_context import get_current_user

    preview: dict[str, Any] = {"kind": "installplan_approve", "valid": False, "approvedBy": get_current_user()}
    try:
        require_user_writes()
        namespace, name, rv = _validate_args(args)
        preview.update(namespace=namespace, name=name, resourceVersion=rv)
        plan, sub = _read_plan(namespace, name)
        info = _info_for(plan, sub)
        preview.update(
            {
                "currentResourceVersion": info.resource_version,
                "operator": info.operator,
                "subscription": info.subscription,
                "channel": info.channel,
                "phase": info.phase,
                "installs": info.installs,
                "replaces": info.replaces,
                "current": info.current,
                "steps": info.steps[:MAX_STEPS_SHOWN],
                "stepCount": len(info.steps),
                "crdCount": sum(1 for s in info.steps if s["kind"] == "CustomResourceDefinition"),
                "gitops": _gitops_note(info),
                "undo": (
                    "OLM cannot un-approve a plan. Once it installs, going back means reinstalling the operator at "
                    "the older version."
                ),
                "noDeny": NO_DENY_TEXT,
            }
        )
        _check_approvable(plan, info, rv)
    except ApprovalInputError as e:
        preview["error"] = str(e)
        return preview
    dry = _patch(namespace, name, rv, dry_run=True)
    if isinstance(dry, ToolError):
        preview["error"] = f"The API server's dry run refused the approval: {dry}"
        preview["dryRun"] = "failed"
        return preview
    preview["dryRun"] = "passed"
    preview["valid"] = True
    return preview


@beta_tool
def approve_installplan(namespace: str, name: str, resource_version: str):
    """Approve a pending OLM InstallPlan so the operator update installs. REQUIRES USER CONFIRMATION.

    Call list_pending_installplans first and pass the plan's rv as resource_version: the approval is refused if the
    plan changed since. Runs as the signed-in user. Not reversible: OLM cannot un-approve, and going back means
    reinstalling the older operator version. There is no 'deny' tool because OLM has none: to decline an update,
    leave the plan unapproved (nothing installs), keep the Subscription on Manual approval, or change its channel.

    Args:
        namespace: Namespace of the InstallPlan.
        name: Name of the InstallPlan (e.g. 'install-abcde').
        resource_version: The plan's resourceVersion as listed by list_pending_installplans.
    """
    from ..request_context import get_current_user

    args = {"namespace": namespace, "name": name, "resource_version": resource_version}
    try:
        require_user_writes()
        namespace, name, rv = _validate_args(args)
        plan, sub = _read_plan(namespace, name)
        info = _info_for(plan, sub)
        _check_approvable(plan, info, rv)
    except ApprovalInputError as e:
        return f"InstallPlan not approved: {e}"

    dry = _patch(namespace, name, rv, dry_run=True)
    if isinstance(dry, ToolError):
        return f"InstallPlan not approved: the API server's dry run refused it: {dry}. Nothing was changed."
    result = _patch(namespace, name, rv, dry_run=False)
    if isinstance(result, ToolError):
        if result.category == "conflict":
            return (
                f"InstallPlan not approved: {namespace}/{name} changed between the check and the write "
                "(resourceVersion precondition failed). List the pending plans again."
            )
        return f"InstallPlan not approved: {result}"
    logger.info(
        "InstallPlan %s/%s approved by %s: installs %s, replaces %s",
        namespace,
        name,
        get_current_user(),
        info.installs,
        info.replaces,
    )
    lines = [
        f"InstallPlan {namespace}/{name} approved by {get_current_user() or 'you'}. OLM now installs it.",
        _cluster_block(
            [
                f"Operator: {_cluster_text(info.operator)}",
                f"Installs: {_cluster_text(', '.join(info.installs) or '?', 400)}",
                f"Replaces: {_cluster_text(info.from_text, 400)}",
            ]
        ),
        "Follow progress with list_operator_subscriptions or Operators > Installed. OLM cannot un-approve; going "
        "back means reinstalling the older version.",
    ]
    if note := _gitops_note(info):
        lines.append(note)
    return "\n".join(lines)
