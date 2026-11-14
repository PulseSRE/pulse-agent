"""Confirm-gated RBAC writes: ``grant_access`` and ``revoke_access`` (JTBD J2, J3, W1).

``grant_access`` creates one RoleBinding (namespace given) or ClusterRoleBinding
(no namespace) for one subject. ``revoke_access`` removes one subject from one
binding. They follow the silence and workload write tools:

* Both are write tools (``WRITE_TOOLS``), so the agent loop asks the person to
  confirm each call, and ``confirm_preview`` attaches a preview computed as the
  caller from the same planning code the tool runs: the exact binding YAML and
  a line diff, the role's rules, what the subject can newly do (grant), the
  other subjects left on the binding (revoke), the API server's dry run, and a
  risk level with its reasons.
* Every write is first sent with ``dryRun=All``; the tool refuses when the dry
  run fails. The API server's own escalation check applies: a person can only
  grant what they hold or may ``bind``.
* They run as the person. With token forwarding off, or without the caller's
  token, they refuse; they never fall back to Pulse's service account.
* ``revoke_access`` removes only that subject, with the request pulse-ui's
  "Remove access" dialog sends (``identity/RemoveAccessDialog.tsx``,
  ``engine/subjectAccess.ts removeSubjectsPatch``): a JSON Patch that ``test``s
  the binding's resourceVersion and the exact subject entry at its index, then
  removes it, so a concurrent edit is refused rather than removing someone
  else. The binding is deleted (with uid and resourceVersion preconditions)
  only when that subject is its only subject, and the preview says so.
* HIGH risk (cluster-admin, admin cluster-wide, ``*`` verbs or resources,
  escalate/bind/impersonate, secrets readable cluster-wide, everyone-groups,
  rules that could not be read, protected namespaces, removing your own
  access) is called out in the preview; pulse-ui never auto-approves an access
  change and accepts no keyboard shortcut for a HIGH one.
* Refused outright: an equivalent binding that already exists, system bindings
  (``system:*`` names, ``kubernetes.io/bootstrapping`` labels, autoupdate
  annotations), controller-owned bindings, anonymous/unauthenticated subjects,
  and anything touching Pulse's own service accounts or the bindings that
  grant them.
* Each write is recorded in the Pulse write ledger (``pulse_write_ledger``)
  with the resourceVersion it produced, so ``get_rbac_changes`` and Access
  review > RBAC changes credit the change to the person who approved it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from kubernetes.client.rest import ApiException

from . import k8s_client as _kc
from .decorators import beta_tool
from .errors import ToolError, classify_api_error
from .k8s_tools.validators import _validate_k8s_namespace
from .rbac_tools import _rule_text, _text, rule_allows, rules_review_as, subject_matches, subject_title, username_of

logger = logging.getLogger("pulse_agent.rbac_writes")

RBAC = "rbac.authorization.k8s.io"
RBAC_PATH = f"/apis/{RBAC}/v1"
JSON_PATCH = "application/json-patch+json"
BINDING_KINDS = ("RoleBinding", "ClusterRoleBinding")
GITOPS_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
GITOPS_INSTANCE_LABELS = ("argocd.argoproj.io/instance", "app.kubernetes.io/instance")

_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,252}$")
_SUBJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:+-]{0,252}$")
_SA_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
# Groups (and the anonymous user) that mean "everyone": never granted from chat.
REFUSED_SUBJECTS = {("User", "system:anonymous"), ("Group", "system:unauthenticated")}
# Groups that mean "everyone who can sign in" (or every service account): HIGH risk.
BROAD_GROUPS = {
    "system:authenticated",
    "system:authenticated:oauth",
    "system:serviceaccounts",
}
ESCALATING_VERBS = ("escalate", "bind", "impersonate")
_SERVER_FIELDS = ("managedFields", "resourceVersion", "uid", "creationTimestamp", "generation", "selfLink")
MAX_RULE_LINES = 40

ACCESS_WRITE_TOOL_NAMES = frozenset({"grant_access", "revoke_access"})


class AccessWriteError(ValueError):
    """Input or state that makes the write refuse before anything is sent."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def require_user_writes() -> None:
    """Access changes run as the person, never as the agent's service account."""
    from .config import get_settings

    if not get_settings().agent.token_forwarding:
        raise AccessWriteError(
            "Access is not changed from chat on this deployment: token forwarding is off "
            "(PULSE_AGENT_TOKEN_FORWARDING=false), so the write would run as Pulse's service account instead of "
            "as you. Use Identity > Access review in the console, which writes with your own permissions."
        )
    if not _kc.get_current_user_token():
        raise AccessWriteError(
            "Your access token did not reach Pulse with this request, and access is never changed as "
            "Pulse's service account. Sign in again and retry."
        )


def pulse_namespace() -> str:
    from .k8s_tools.audit import _agent_namespace

    return _agent_namespace()


def parse_subject(kind: str, name: str, namespace: str = "", *, for_grant: bool = True) -> dict:
    """A subject reference ({kind, name[, namespace]}) from tool input; refuses what must not be granted.

    Taking access away from "everyone" is allowed (``for_grant=False``); giving it is not.
    """
    k = (kind or "").strip().lower()
    n = (name or "").strip()
    if k in ("serviceaccount", "serviceaccounts", "sa") or (
        k in ("user", "users") and n.startswith("system:serviceaccount:")
    ):
        if n.startswith("system:serviceaccount:"):
            parts = n.split(":")
            if len(parts) != 4:
                raise AccessWriteError(f"{name!r} is not a service account username.")
            ns, sa = parts[2], parts[3]
        else:
            ns, _, sa = n.rpartition("/")
            ns = ns or namespace
        if not ns or _validate_k8s_namespace(ns) or not _SA_NAME_RE.match(sa or ""):
            raise AccessWriteError(
                "A service account is 'namespace/name' or system:serviceaccount:<ns>:<name> "
                "(or just its name when it lives in the binding's namespace)."
            )
        ref = {"kind": "ServiceAccount", "namespace": ns, "name": sa}
    elif k in ("user", "users"):
        if not _SUBJECT_NAME_RE.match(n):
            raise AccessWriteError(f"{name!r} is not a valid user name.")
        ref = {"kind": "User", "name": n}
    elif k in ("group", "groups"):
        if not _SUBJECT_NAME_RE.match(n):
            raise AccessWriteError(f"{name!r} is not a valid group name.")
        ref = {"kind": "Group", "name": n}
    else:
        raise AccessWriteError(f"subject_kind must be User, Group or ServiceAccount, got {kind!r}.")
    if for_grant and (ref["kind"], ref["name"]) in REFUSED_SUBJECTS:
        raise AccessWriteError(
            f"{subject_title(ref)} means anyone, signed in or not. Pulse does not change its access from chat."
        )
    if ref["kind"] == "ServiceAccount" and ref["namespace"] == pulse_namespace():
        raise AccessWriteError(
            f"{subject_title(ref)} is in Pulse's own namespace ({ref['namespace']}). Pulse never changes its own "
            "service accounts' access; pulse-operator owns that RBAC."
        )
    return ref


def binding_subject(ref: dict) -> dict:
    """The ``subjects[]`` entry for a reference, as pulse-ui's grantBody writes it."""
    if ref["kind"] == "ServiceAccount":
        return {"kind": "ServiceAccount", "name": ref["name"], "namespace": ref["namespace"]}
    return {"kind": ref["kind"], "apiGroup": RBAC, "name": ref["name"]}


def suggest_binding_name(subject: str, role: str) -> str:
    """pulse-ui's suggestBindingName: ``<subject>-<role>``, lowercased, DNS-safe."""
    s = re.sub(r"[^a-z0-9.-]+", "-", f"{subject}-{role}".lower()).strip("-")[:63].strip("-.")
    return s or "binding"


def binding_path(kind: str, name: str = "", namespace: str = "") -> str:
    if kind == "ClusterRoleBinding":
        base = f"{RBAC_PATH}/clusterrolebindings"
    else:
        base = f"{RBAC_PATH}/namespaces/{namespace}/rolebindings"
    return f"{base}/{name}" if name else base


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


# ---------------------------------------------------------------------------
# API calls (as the caller)
# ---------------------------------------------------------------------------


def _call(
    method: str, path: str, *, body: Any = None, content_type: str = "application/json", dry_run: bool = False
) -> dict | ToolError:
    api = _kc.get_rbac_client().api_client
    query = [("dryRun", "All")] if dry_run else []
    if method in ("POST", "PATCH"):
        query.append(("fieldManager", "pulse-agent"))
    try:
        resp = api.call_api(
            path,
            method,
            body=body,
            header_params={"Content-Type": content_type, "Accept": "application/json"},
            query_params=query,
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
    except ApiException as e:
        return classify_api_error(e, f"{method} {path}")
    raw = resp[0] if isinstance(resp, tuple) else resp
    data = raw.data if hasattr(raw, "data") else raw.read()
    try:
        out = json.loads(data) if data else {}
    except ValueError:
        return ToolError(message="The API server returned an unreadable response.", category="server")
    return out if isinstance(out, dict) else {}


def _get(path: str, what: str) -> dict | ToolError | None:
    """The object at ``path``; None when it does not exist."""
    got = _kc.get_raw_json(path, f"get {what}")
    if isinstance(got, ToolError):
        return None if got.status_code == 404 else got
    return got if isinstance(got, dict) else None


def _items(path: str) -> list[dict] | ToolError:
    got = _kc.get_raw_json(path, "list")
    if isinstance(got, ToolError):
        return got
    return list((got or {}).get("items") or [])


# ---------------------------------------------------------------------------
# Roles, risk and what the subject can newly do
# ---------------------------------------------------------------------------


def read_role(role_kind: str, role_name: str, namespace: str) -> tuple[dict | None, str]:
    """(role, note): the role as the caller reads it. Raises when it does not exist."""
    path = (
        f"{RBAC_PATH}/clusterroles/{role_name}"
        if role_kind == "ClusterRole"
        else (f"{RBAC_PATH}/namespaces/{namespace}/roles/{role_name}")
    )
    where = f" in {namespace}" if role_kind == "Role" else ""
    got = _get(path, f"{role_kind} {role_name}")
    if got is None:
        raise AccessWriteError(f"{role_kind} {role_name}{where} does not exist.")
    if isinstance(got, ToolError):
        return None, f"The {role_kind}'s rules could not be read as you ({_text(got.message, 200)})."
    return got, ""


def is_admin_equivalent(rules: list[dict]) -> bool:
    return any(
        "*" in (r.get("verbs") or []) and "*" in (r.get("resources") or []) and "*" in (r.get("apiGroups") or [])
        for r in rules
    )


def risk_reasons(
    *,
    cluster_scope: bool,
    role_name: str,
    rules: list[dict] | None,
    subject: dict,
    namespace: str,
) -> list[str]:
    """Why a grant is HIGH risk; empty when it is not."""
    reasons: list[str] = []
    if role_name == "cluster-admin":
        reasons.append(
            "cluster-admin hands over full control" + (" of the cluster" if cluster_scope else " of the namespace")
        )
    elif role_name == "admin" and cluster_scope:
        reasons.append("admin cluster-wide: full control of every namespace, including its RBAC")
    if rules is None:
        reasons.append("the role's rules could not be read, so what it grants is unknown")
    else:
        if any("*" in (r.get("verbs") or []) for r in rules):
            reasons.append("the role grants every verb ('*') on some resources")
        if any("*" in (r.get("resources") or []) for r in rules):
            reasons.append("the role grants access to every resource ('*') in some API groups")
        for verb in ESCALATING_VERBS:
            if any(verb in (r.get("verbs") or []) for r in rules):
                reasons.append(f"the role grants '{verb}', which lets the subject raise its own access")
        if cluster_scope and any(rule_allows(r, v, "", "secrets") for r in rules for v in ("get", "list", "watch")):
            reasons.append("the role can read Secrets, and this grant is cluster-wide: every namespace's Secrets")
    if subject["kind"] == "Group" and subject["name"] in BROAD_GROUPS:
        reasons.append(f"Group {subject['name']} is everyone who can sign in (or every service account)")
    if namespace and _protected(namespace):
        reasons.append(f"{namespace} is a protected namespace (PULSE_AGENT_PROTECTED_NAMESPACES)")
    return list(dict.fromkeys(reasons))


def _protected(namespace: str) -> bool:
    from .policy import _is_protected

    try:
        return _is_protected(namespace)
    except Exception:
        return False


def _expand(rule: dict) -> list[tuple[str, str, str]]:
    return [
        (v, g, r)
        for v in rule.get("verbs") or []
        for g in rule.get("apiGroups") or [""]
        for r in rule.get("resources") or []
    ]


def new_permissions(subject: dict, namespace: str, rules: list[dict]) -> dict:
    """Which of the role's rules the subject does not have yet in ``namespace``.

    One SelfSubjectRulesReview impersonating the subject alone (as
    ``effective_permissions`` does for its first call); access through groups
    is not counted, which the note says. Needs the caller to be allowed to
    impersonate; otherwise ``state: unavailable``.
    """
    resource_rules = [r for r in rules if r.get("resources")]
    if subject["kind"] == "Group":
        return {
            "state": "unavailable",
            "rules": [_rule_text(r) for r in resource_rules[:MAX_RULE_LINES]],
            "alreadyHad": [],
            "note": "Computed per member, as on the group's page: every member gets these rules here.",
        }
    if not namespace:
        return {
            "state": "not-applicable",
            "rules": [_rule_text(r) for r in resource_rules[:MAX_RULE_LINES]],
            "alreadyHad": [],
            "note": "Cluster-wide: these rules apply in every namespace (and to cluster-scoped resources).",
        }
    try:
        current = rules_review_as(namespace, username_of(subject), None).get("resourceRules") or []
    except Exception as e:  # RbacToolError, or anything the review raises
        return {
            "state": "unavailable",
            "rules": [_rule_text(r) for r in resource_rules[:MAX_RULE_LINES]],
            "alreadyHad": [],
            "note": f"Their current access could not be computed (impersonation as you: {_text(e, 160)}), "
            "so every rule of the role is listed.",
        }
    new: list[str] = []
    had: list[str] = []
    for r in resource_rules:
        covered = all(any(rule_allows(c, v, g, res) for c in current) for v, g, res in _expand(r))
        (had if covered else new).append(_rule_text(r))
    return {
        "state": "computed",
        "rules": new[:MAX_RULE_LINES],
        "alreadyHad": had[:MAX_RULE_LINES],
        "note": f"Compared with what {subject_title(subject)} can do in {namespace} now (as a user alone; access "
        "through group membership is not counted, so some rules listed as new may already reach them that way).",
    }


# ---------------------------------------------------------------------------
# Bindings: YAML, diffs, ownership
# ---------------------------------------------------------------------------


def clean_binding(obj: dict) -> dict:
    """The binding as a person reads it: no server-owned metadata or status."""
    meta = {k: v for k, v in (obj.get("metadata") or {}).items() if k not in _SERVER_FIELDS}
    ann = {
        k: v
        for k, v in (meta.get("annotations") or {}).items()
        if k != "kubectl.kubernetes.io/last-applied-configuration"
    }
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    out: dict = {"apiVersion": obj.get("apiVersion") or f"{RBAC}/v1", "kind": obj.get("kind", ""), "metadata": meta}
    if "roleRef" in obj:
        out["roleRef"] = obj["roleRef"]
    out["subjects"] = obj.get("subjects") or []
    return out


def to_yaml(obj: dict | None) -> str:
    if obj is None:
        return ""
    import yaml

    return str(yaml.safe_dump(obj, sort_keys=False, default_flow_style=False))


def line_diff(before: str, after: str) -> list[dict]:
    """A full line diff, ``op`` one of '+', '-', ' '."""
    out: list[dict] = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("? "):
            continue
        out.append({"op": line[0] if line[0] in "+-" else " ", "text": line[2:]})
    return out


def gitops_owner(obj: dict) -> dict | None:
    meta = obj.get("metadata") or {}
    ann = meta.get("annotations") or {}
    labels = meta.get("labels") or {}
    if ann.get(GITOPS_TRACKING_ANNOTATION):
        return {
            "tool": "Argo CD",
            "app": str(ann[GITOPS_TRACKING_ANNOTATION]).split(":", 1)[0],
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
            f"Managed by Helm release{app} ({owner['detail']}): the next helm upgrade restores it. "
            "Change the chart's values as well."
        )
    return (
        f"Managed by Argo CD application{app} ({owner['detail']}): the next sync can revert this. "
        "Prefer a pull request to the application's source (propose_git_change); a direct edit is drift."
    )


def _argo_autosync(namespace: str) -> str | None:
    if not namespace:
        return None
    from .gitops_tools import check_argo_auto_sync

    try:
        return check_argo_auto_sync(namespace)
    except Exception:
        logger.debug("Argo auto-sync check failed for %s", namespace, exc_info=True)
        return None


def system_binding_reason(obj: dict) -> str:
    """Why a binding is the platform's (refused), or ''."""
    meta = obj.get("metadata") or {}
    name = str(meta.get("name") or "")
    labels = meta.get("labels") or {}
    ann = meta.get("annotations") or {}
    if name.startswith("system:"):
        return f"'{name}' is a system binding (system:* names belong to the platform)"
    if labels.get("kubernetes.io/bootstrapping"):
        return f"'{name}' is a bootstrap binding (label kubernetes.io/bootstrapping={labels['kubernetes.io/bootstrapping']})"
    if str(ann.get("rbac.authorization.kubernetes.io/autoupdate", "")).lower() == "true":
        return f"'{name}' is reconciled by the API server (annotation rbac.authorization.kubernetes.io/autoupdate=true); it would be restored"
    owners = meta.get("ownerReferences") or []
    if owners:
        o = owners[0]
        return f"'{name}' is owned by {o.get('kind', '?')} {o.get('name', '?')}; its controller would restore it"
    return ""


def pulse_sa_subjects(obj: dict) -> list[dict]:
    """Subjects of a binding that are Pulse's own service accounts."""
    own = pulse_namespace()
    b_ns = (obj.get("metadata") or {}).get("namespace")
    out = []
    for s in obj.get("subjects") or []:
        is_sa = s.get("kind") == "ServiceAccount" and (s.get("namespace") or b_ns) == own
        as_user = s.get("kind") == "User" and str(s.get("name", "")).startswith(f"system:serviceaccount:{own}:")
        if is_sa or as_user:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Plans (shared by the preview and the tool)
# ---------------------------------------------------------------------------


@dataclass
class AccessPlan:
    tool: str
    action: str  # create | remove-subject | delete
    subject: dict = field(default_factory=dict)
    binding_kind: str = ""
    binding_name: str = ""
    namespace: str = ""
    role: dict = field(default_factory=dict)  # {kind, name}
    rules: list[dict] | None = None
    rules_note: str = ""
    before: dict | None = None
    after: dict | None = None
    method: str = ""
    path: str = ""
    content_type: str = "application/json"
    body: Any = None
    risk_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gitops: dict | None = None
    newly: dict | None = None
    other_subjects: list[dict] = field(default_factory=list)
    dry_run_ok: bool = False
    dry_run_message: str = ""
    error: str = ""
    impact: str = ""
    undo: str = ""
    resource_version: str = ""

    @property
    def valid(self) -> bool:
        return not self.error and self.dry_run_ok

    @property
    def risk(self) -> str:
        return "HIGH" if self.risk_reasons else "MEDIUM"

    def to_preview(self) -> dict[str, Any]:
        before_yaml, after_yaml = to_yaml(self.before), to_yaml(self.after)
        preview: dict[str, Any] = {
            "kind": "access_change",
            "tool": self.tool,
            "action": self.action,
            "valid": self.valid,
            "risk": self.risk,
            "riskReasons": self.risk_reasons,
            # Never approved without a person, at any trust level.
            "requiresHuman": True,
            "subject": dict(self.subject),
            "role": {
                **self.role,
                "rules": [_rule_text(r) for r in (self.rules or [])[:MAX_RULE_LINES]],
                "rulesKnown": self.rules is not None,
                "rulesNote": self.rules_note,
            },
            "binding": {
                "kind": self.binding_kind,
                "name": self.binding_name,
                "namespace": self.namespace or None,
            },
            "scope": "namespace" if self.namespace else "cluster",
            "yaml": after_yaml if self.action == "create" else before_yaml,
            "yamlAfter": after_yaml,
            "diff": line_diff(before_yaml, after_yaml),
            "newPermissions": self.newly,
            "otherSubjects": self.other_subjects,
            "deletesBinding": self.action == "delete",
            "dryRun": {
                "state": "passed" if self.dry_run_ok else ("failed" if self.method else "unavailable"),
                "message": self.dry_run_message,
            },
            "impact": self.impact,
            "undo": self.undo,
            "gitops": self.gitops,
            "warnings": self.warnings,
            "protectedNamespace": _protected(self.namespace) if self.namespace else False,
        }
        if self.method:
            preview["request"] = {
                "method": self.method,
                "path": self.path,
                "type": self.content_type,
                "body": self.body,
            }
        if self.error:
            preview["error"] = self.error
        return preview

    def dry_run(self) -> dict | None:
        got = _call(self.method, self.path, body=self.body, content_type=self.content_type, dry_run=True)
        if isinstance(got, ToolError):
            self.dry_run_ok = False
            self.dry_run_message = f"Dry run refused by the API server: {_text(got.message, 400)}"
            return None
        self.dry_run_ok = True
        self.dry_run_message = "Passed: the API server accepts this change as you (dryRun=All)"
        return got


def _roleref_text(ref: dict) -> str:
    return f"{ref.get('kind', '')} {ref.get('name', '')}"


def plan_grant(args: dict) -> AccessPlan:
    plan = AccessPlan(tool="grant_access", action="create")
    try:
        require_user_writes()
        namespace = str(args.get("namespace") or "").strip()
        if namespace and (err := _validate_k8s_namespace(namespace)):
            raise AccessWriteError(err.removeprefix("Error: "))
        role_kind = str(args.get("role_kind") or "ClusterRole").strip()
        role_kind = {"clusterrole": "ClusterRole", "role": "Role"}.get(role_kind.lower(), role_kind)
        if role_kind not in ("ClusterRole", "Role"):
            raise AccessWriteError("role_kind must be ClusterRole or Role.")
        if role_kind == "Role" and not namespace:
            raise AccessWriteError("A Role exists in one namespace: give the namespace, or use a ClusterRole.")
        role_name = str(args.get("role_name") or "").strip()
        if not _ROLE_NAME_RE.match(role_name):
            raise AccessWriteError(f"{role_name!r} is not a valid role name.")
        subject = parse_subject(str(args.get("subject_kind") or ""), str(args.get("subject_name") or ""), namespace)
    except AccessWriteError as e:
        plan.error = str(e)
        return plan

    cluster_scope = not namespace
    plan.subject = subject
    plan.namespace = namespace
    plan.binding_kind = "ClusterRoleBinding" if cluster_scope else "RoleBinding"
    plan.role = {"kind": role_kind, "name": role_name}
    try:
        role, plan.rules_note = read_role(role_kind, role_name, namespace)
    except AccessWriteError as e:
        plan.error = str(e)
        return plan
    plan.rules = (role.get("rules") or []) if role is not None else None
    if role is not None and role.get("aggregationRule") and not plan.rules:
        # Its rules arrive later from the roles it aggregates: what it will grant is unknown now.
        plan.rules = None
        plan.rules_note = "An aggregated ClusterRole whose rules are not filled in yet; what it grants is unknown."

    # An equivalent binding already there: same role, same scope, this subject.
    def equivalent(b: dict, kind: str) -> bool:
        ref = b.get("roleRef") or {}
        b_ns = (b.get("metadata") or {}).get("namespace")
        same_role = ref.get("name") == role_name and (ref.get("kind") == role_kind or kind == "ClusterRoleBinding")
        return same_role and any(subject_matches(s, subject, b_ns) for s in b.get("subjects") or [])

    scans = [("ClusterRoleBinding", binding_path("ClusterRoleBinding"))]
    if namespace:
        scans.insert(0, ("RoleBinding", binding_path("RoleBinding", namespace=namespace)))
    for kind, path in scans:
        items = _items(path)
        if isinstance(items, ToolError):
            plan.warnings.append(
                f"{kind}s could not be listed as you ({_text(items.message, 160)}), so an existing equivalent "
                "grant could not be ruled out."
            )
            continue
        for b in items:
            if not equivalent(b, kind) or (kind == "ClusterRoleBinding" and role_kind == "Role"):
                continue
            meta = b.get("metadata") or {}
            where = f" in {meta.get('namespace')}" if kind == "RoleBinding" else (" cluster-wide" if namespace else "")
            plan.error = (
                f"{subject_title(subject)} already has {role_kind} {role_name}{where} through {kind} "
                f"{meta.get('name', '')}. Nothing to grant; no duplicate binding is created."
            )
            return plan

    # The binding name: pulse-ui's suggestion, made unique when another binding has it.
    name = suggest_binding_name(subject["name"], role_name)
    taken = _get(binding_path(plan.binding_kind, name, namespace), f"{plan.binding_kind} {name}")
    if taken is not None:
        digest = hashlib.sha256(
            f"{subject['kind']}/{subject.get('namespace', '')}/{subject['name']}/{role_kind}/{role_name}/{namespace}".encode()
        ).hexdigest()[:5]
        name = f"{name[:57].rstrip('-.')}-{digest}"
    plan.binding_name = name

    plan.after = {
        "apiVersion": f"{RBAC}/v1",
        "kind": plan.binding_kind,
        "metadata": {"name": name, **({"namespace": namespace} if namespace else {})},
        "roleRef": {"apiGroup": RBAC, "kind": "ClusterRole" if cluster_scope else role_kind, "name": role_name},
        "subjects": [binding_subject(subject)],
    }
    plan.method = "POST"
    plan.path = binding_path(plan.binding_kind, namespace=namespace)
    plan.body = plan.after
    plan.risk_reasons = risk_reasons(
        cluster_scope=cluster_scope, role_name=role_name, rules=plan.rules, subject=subject, namespace=namespace
    )
    if plan.rules is not None and is_admin_equivalent(plan.rules) and role_name != "cluster-admin":
        plan.risk_reasons.insert(
            0, f"{role_kind} {role_name} is equivalent to cluster-admin (every verb on every resource)"
        )
    where = f"in {namespace}" if namespace else "in every namespace and on cluster-scoped resources"
    plan.impact = f"{subject_title(subject)} gets {role_kind} {role_name} {where}, from the moment the binding exists."
    plan.undo = (
        f"revoke_access(subject_kind='{subject['kind']}', subject_name='{_subject_input(subject)}', "
        f"binding_kind='{plan.binding_kind}', binding_name='{name}'"
        + (f", namespace='{namespace}'" if namespace else "")
        + "), or delete the binding in Access review"
    )
    if autosync := _argo_autosync(namespace):
        plan.warnings.append(
            _text(autosync, 400)
            + " A binding created here is not in Git: prefer adding it to the application's source."
        )
    plan.newly = new_permissions(subject, namespace, plan.rules or []) if plan.rules is not None else None
    plan.dry_run()
    return plan


def _subject_input(subject: dict) -> str:
    return f"{subject['namespace']}/{subject['name']}" if subject["kind"] == "ServiceAccount" else subject["name"]


def plan_revoke(args: dict) -> AccessPlan:
    plan = AccessPlan(tool="revoke_access", action="remove-subject")
    try:
        require_user_writes()
        namespace = str(args.get("namespace") or "").strip()
        binding_kind = str(args.get("binding_kind") or "").strip()
        binding_kind = {"rolebinding": "RoleBinding", "clusterrolebinding": "ClusterRoleBinding"}.get(
            binding_kind.lower(), binding_kind
        )
        if binding_kind not in BINDING_KINDS:
            raise AccessWriteError("binding_kind must be RoleBinding or ClusterRoleBinding.")
        if binding_kind == "RoleBinding" and not namespace:
            raise AccessWriteError("A RoleBinding needs its namespace.")
        if binding_kind == "ClusterRoleBinding":
            namespace = ""
        if namespace and (err := _validate_k8s_namespace(namespace)):
            raise AccessWriteError(err.removeprefix("Error: "))
        binding_name = str(args.get("binding_name") or "").strip()
        if not _ROLE_NAME_RE.match(binding_name):
            raise AccessWriteError(f"{binding_name!r} is not a valid binding name.")
        subject = parse_subject(
            str(args.get("subject_kind") or ""), str(args.get("subject_name") or ""), namespace, for_grant=False
        )
    except AccessWriteError as e:
        plan.error = str(e)
        return plan

    plan.subject, plan.namespace = subject, namespace
    plan.binding_kind, plan.binding_name = binding_kind, binding_name
    where = f"{binding_kind} {namespace + '/' if namespace else ''}{binding_name}"
    path = binding_path(binding_kind, binding_name, namespace)
    got = _get(path, where)
    if got is None:
        plan.error = f"{where} does not exist."
        return plan
    if isinstance(got, ToolError):
        plan.error = f"{where} could not be read as you: {_text(got.message, 300)}"
        return plan
    obj = got
    if reason := system_binding_reason(obj):
        plan.error = f"Not changed: {reason}. Pulse does not edit platform-managed bindings from chat."
        return plan
    if pulse_sa_subjects(obj):
        plan.error = (
            f"{where} grants Pulse's own service account. Pulse never edits its own RBAC; pulse-operator owns it."
        )
        return plan

    subjects = list(obj.get("subjects") or [])
    indexes = [i for i, s in enumerate(subjects) if subject_matches(s, subject, namespace or None)]
    if not indexes:
        names = ", ".join(
            subject_title(
                {"kind": s.get("kind", ""), "name": s.get("name", ""), "namespace": s.get("namespace") or namespace}
            )
            for s in subjects[:10]
        )
        plan.error = f"{subject_title(subject)} is not a subject of {where} (subjects: {_text(names, 400) or 'none'})."
        return plan

    role_ref = obj.get("roleRef") or {}
    plan.role = {"kind": str(role_ref.get("kind", "")), "name": str(role_ref.get("name", ""))}
    try:
        role, plan.rules_note = read_role(plan.role["kind"], plan.role["name"], namespace)
        plan.rules = (role or {}).get("rules") or [] if role is not None else None
    except AccessWriteError as e:
        plan.rules, plan.rules_note = [], f"{e} The binding points at a role that is gone; it grants nothing."
    plan.before = clean_binding(obj)
    meta = obj.get("metadata") or {}
    rv = str(meta.get("resourceVersion") or "")
    plan.resource_version = rv
    others = [s for i, s in enumerate(subjects) if i not in indexes]
    plan.other_subjects = [
        {
            "kind": s.get("kind", ""),
            "name": s.get("name", ""),
            **({"namespace": s["namespace"]} if s.get("namespace") else {}),
        }
        for s in others
    ]
    plan.gitops = gitops_owner(obj)
    if plan.gitops and plan.gitops["tool"] == "Argo CD" and (autosync := _argo_autosync(namespace)):
        plan.error = (
            f"{where} is tracked by Argo CD and its namespace syncs automatically, so this removal would be "
            f"reverted. {_text(autosync, 300)} Remove the subject in Git instead (propose_git_change)."
        )
        return plan

    if not others:
        plan.action = "delete"
        plan.after = None
        plan.method = "DELETE"
        plan.path = path
        plan.body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": meta.get("uid"), "resourceVersion": rv},
        }
        plan.impact = (
            f"{subject_title(subject)} is the only subject, so the whole {binding_kind} is deleted "
            f"(only if unchanged since read: uid and resourceVersion preconditions). "
            f"They lose {_roleref_text(role_ref)} {'in ' + namespace if namespace else 'cluster-wide'} through it."
        )
    else:
        ops: list[dict] = [{"op": "test", "path": "/metadata/resourceVersion", "value": rv}]
        for i in sorted(set(indexes), reverse=True):
            ops += [
                {"op": "test", "path": f"/subjects/{i}", "value": subjects[i]},
                {"op": "remove", "path": f"/subjects/{i}"},
            ]
        plan.method = "PATCH"
        plan.path = path
        plan.content_type = JSON_PATCH
        plan.body = ops
        after = clean_binding(obj)
        after["subjects"] = others
        plan.after = after
        plan.impact = (
            f"{subject_title(subject)} is removed from {where}; {len(others)} other subject(s) keep "
            f"{_roleref_text(role_ref)}. The patch tests the resourceVersion and the exact subject entry first, so a "
            "concurrent edit is refused instead of removing someone else."
        )
    plan.warnings.append(
        f"Other bindings, and groups {subject['name']} belongs to, are unchanged: check with effective_permissions "
        "or who_can that no other path still grants this access."
    )
    if plan.gitops:
        plan.warnings.append(gitops_note(plan.gitops))
    if plan.rules is not None and (is_admin_equivalent(plan.rules) or plan.role["name"] == "cluster-admin"):
        plan.risk_reasons.append(f"{_roleref_text(role_ref)} is admin-equivalent: check another admin keeps access")
    from .request_context import get_current_user

    me = get_current_user()
    if me and username_of(subject) == me:
        plan.risk_reasons.append("you are removing your own access; you may not be able to undo it yourself")
    if namespace and _protected(namespace):
        plan.risk_reasons.append(f"{namespace} is a protected namespace (PULSE_AGENT_PROTECTED_NAMESPACES)")
    subj = binding_subject(subject) if subject["kind"] != "ServiceAccount" else subjects[indexes[0]]
    plan.undo = (
        f"grant_access with the same subject and {_roleref_text(role_ref)}"
        + (f" in {namespace}" if namespace else " cluster-wide")
        + (" (recreates the binding under a new name)" if plan.action == "delete" else "")
        + f"; the removed entry was {json.dumps(subj, sort_keys=True)}"
    )
    plan.dry_run()
    return plan


# ---------------------------------------------------------------------------
# Execution and the write ledger
# ---------------------------------------------------------------------------


def record_ledger(plan: AccessPlan, verb: str, resource_version: str, outcome: str = "ok", detail: str = "") -> None:
    """Credit the write to the signed-in person in the Pulse write ledger. Best effort."""
    try:
        from .repositories import get_rbac_history_repo
        from .request_context import get_current_user

        get_rbac_history_repo().record_write(
            {
                "at": int(time.time() * 1000),
                "actor": str(get_current_user() or "pulse-agent (no signed-in user)"),
                # The agent knows the user from the session it serves, not from a token review.
                "actor_verified": False,
                "source": "agent",
                "verb": verb,
                "kind": plan.binding_kind,
                "namespace": plan.namespace,
                "name": plan.binding_name,
                "resource_version": resource_version,
                "outcome": outcome,
                "before": plan.before,
                "after": plan.after if verb != "delete" else None,
                "detail": {
                    "tool": plan.tool,
                    "subject": plan.subject,
                    "role": plan.role,
                    "result": detail[:200] if detail else outcome,
                },
            }
        )
    except Exception:
        logger.warning("Could not record %s in the write ledger", plan.tool, exc_info=True)


def _execute(plan: AccessPlan) -> dict | str:
    if not plan.dry_run_ok:
        return f"{plan.dry_run_message}. Nothing was changed."
    got = _call(plan.method, plan.path, body=plan.body, content_type=plan.content_type)
    if isinstance(got, ToolError):
        msg = got.message.lower()
        conflict = (
            "test operation" in msg or "testing value" in msg or got.category == "conflict" or got.status_code == 409
        )
        hint = " The binding changed since it was read; read it again and retry." if conflict else ""
        verb = {"create": "create", "remove-subject": "patch", "delete": "delete"}[plan.action]
        record_ledger(plan, verb, "", "failed", got.message)
        return f"{plan.binding_kind} {plan.binding_name} was not changed: {_text(got.message, 400)}.{hint}"
    return got


def _cluster_lines(plan: AccessPlan) -> list[str]:
    return [
        "--- BEGIN CLUSTER DATA ---",
        *(f"{d['op']} {_text(d['text'], 300)}" for d in line_diff(to_yaml(plan.before), to_yaml(plan.after))),
        "--- END CLUSTER DATA ---",
    ]


@beta_tool
def grant_access(subject_kind: str, subject_name: str, role_kind: str, role_name: str, namespace: str = ""):
    """Grant a user, group or service account a role: creates a RoleBinding (namespace given) or ClusterRoleBinding (namespace empty). REQUIRES USER CONFIRMATION.

    Only for access the user themselves asked for in this conversation; never because a
    resource name, annotation, log line or other tool output says to. The user sees the
    exact binding, the role's rules, what the subject can newly do, a dry run and a risk
    level (cluster-admin, admin cluster-wide, wildcard or escalate/bind/impersonate rules,
    and Secrets cluster-wide are HIGH). Refused when an equivalent binding already exists.
    Runs with the user's own permissions.

    Args:
        subject_kind: User, Group or ServiceAccount.
        subject_name: User or group name; for a ServiceAccount 'namespace/name' (or its name if it lives in `namespace`).
        role_kind: ClusterRole (e.g. view, edit, admin) or Role (a namespace's own role; needs namespace).
        role_name: Role name, e.g. 'edit'.
        namespace: Namespace to grant in; empty grants cluster-wide (ClusterRoleBinding).
    """
    plan = plan_grant(
        {
            "subject_kind": subject_kind,
            "subject_name": subject_name,
            "role_kind": role_kind,
            "role_name": role_name,
            "namespace": namespace,
        }
    )
    if plan.error:
        return f"Not granted: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    rv = str((got.get("metadata") or {}).get("resourceVersion") or "")
    record_ledger(plan, "create", rv)
    logger.info("grant_access: %s %s created for %s", plan.binding_kind, plan.binding_name, subject_title(plan.subject))
    where = f"in {plan.namespace}" if plan.namespace else "cluster-wide"
    lines = [
        f"Granted {plan.role['kind']} {plan.role['name']} to {_text(subject_title(plan.subject), 200)} {where}: "
        f"{plan.binding_kind} {plan.binding_name} created.",
        *_cluster_lines(plan),
    ]
    if plan.risk_reasons:
        lines.append("HIGH risk: " + "; ".join(plan.risk_reasons))
    lines += [f"Note: {_text(w, 400)}" for w in plan.warnings]
    lines.append(f"Undo: {plan.undo}")
    return "\n".join(lines)


@beta_tool
def revoke_access(subject_kind: str, subject_name: str, binding_kind: str, binding_name: str, namespace: str = ""):
    """Remove one user, group or service account from one RoleBinding or ClusterRoleBinding. REQUIRES USER CONFIRMATION.

    Removes only that subject (the other subjects keep their access); the binding is
    deleted only when that subject is its only subject, and the preview says so.
    Use who_can or effective_permissions first to find the bindings that grant the
    access. System bindings (system:*, bootstrap, auto-reconciled, controller-owned)
    are refused. Runs with the user's own permissions.

    Args:
        subject_kind: User, Group or ServiceAccount.
        subject_name: User or group name; for a ServiceAccount 'namespace/name'.
        binding_kind: RoleBinding or ClusterRoleBinding.
        binding_name: The binding's name.
        namespace: The RoleBinding's namespace (empty for a ClusterRoleBinding).
    """
    plan = plan_revoke(
        {
            "subject_kind": subject_kind,
            "subject_name": subject_name,
            "binding_kind": binding_kind,
            "binding_name": binding_name,
            "namespace": namespace,
        }
    )
    if plan.error:
        return f"Not revoked: {plan.error}"
    got = _execute(plan)
    if isinstance(got, str):
        return got
    if plan.action == "delete":
        # The ledger keys a deletion on the version that was deleted.
        record_ledger(plan, "delete", plan.resource_version)
        done = f"{plan.binding_kind} {plan.binding_name} deleted ({_text(subject_title(plan.subject), 200)} was its only subject)."
    else:
        rv = str((got.get("metadata") or {}).get("resourceVersion") or "")
        record_ledger(plan, "patch", rv)
        done = (
            f"Removed {_text(subject_title(plan.subject), 200)} from {plan.binding_kind} {plan.binding_name}; "
            f"{len(plan.other_subjects)} other subject(s) unchanged."
        )
    logger.info("revoke_access: %s", done)
    lines = [done, *_cluster_lines(plan)]
    lines += [f"Note: {_text(w, 400)}" for w in plan.warnings]
    lines.append(f"Undo: {plan.undo}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confirm previews
# ---------------------------------------------------------------------------


def preview_grant(args: dict) -> dict[str, Any]:
    return plan_grant(args).to_preview()


def preview_revoke(args: dict) -> dict[str, Any]:
    return plan_revoke(args).to_preview()


PREVIEW_BUILDERS = {"grant_access": preview_grant, "revoke_access": preview_revoke}

RBAC_WRITE_TOOLS: list[Any] = [grant_access, revoke_access]

from .tool_registry import register_tool

for _tool in RBAC_WRITE_TOOLS:
    register_tool(_tool, is_write=True, category="operations")
