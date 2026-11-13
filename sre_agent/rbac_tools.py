"""Read-only RBAC tools: who can, what can a subject do, what changed (JTBD J4, J5, J13, J14).

- ``who_can`` asks OpenShift's ResourceAccessReview (``oc adm policy who-can``)
  and explains each subject with the binding → role chain behind it, the way
  pulse-ui's Who can panel does (``views/identity/WhoCanPanel.tsx``,
  ``engine/subjectAccess.ts``). Where the API is not served it says so; it does
  not approximate the answer from bindings alone.
- ``effective_permissions`` runs a SelfSubjectRulesReview while impersonating
  the subject, as the UI's subject page does (``engine/rulesReview.ts``): once
  as the user alone and once per group, rules combined (RBAC only adds).
- ``get_rbac_changes`` reads the observed RBAC/SCC/webhook history and the
  Pulse write ledger behind ``GET /rbac/changes``, filtered to what the caller
  may read, exactly as that endpoint filters it.

All three run only with the caller's forwarded token. The agent's service
account has no grant for ``resourceaccessreviews``/
``localresourceaccessreviews`` or ``impersonate``, and Pulse does not add one:
these answers are the caller's own, under the caller's own RBAC.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from . import k8s_client as _kc
from .decorators import beta_tool
from .errors import ToolError
from .k8s_tools.validators import _validate_k8s_namespace

logger = logging.getLogger("pulse_agent.rbac_tools")

AUTHZ = "authorization.openshift.io"
RBAC_PATH = "/apis/rbac.authorization.k8s.io/v1"
_VERB_RE = re.compile(r"^(\*|[a-z]+)$")
_RESOURCE_RE = re.compile(r"^(\*|[a-z0-9][a-z0-9.-]*(/[a-z]+)?)$")
_GROUP_RE = re.compile(r"^(\*|[a-z0-9][a-z0-9.-]*)?$")
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:+-]{0,252}$")
MAX_ROWS = 40
MAX_RULES = 60

# The grants a caller (not the service account) needs, named exactly when refused.
WHO_CAN_RULE = (
    "apiGroups: [authorization.openshift.io], resources: [resourceaccessreviews, localresourceaccessreviews], "
    "verbs: [create]"
)


class RbacToolError(ValueError):
    pass


# RBAC names are full of "system:" (system:masters, system:serviceaccount:ns:sa,
# system:authenticated). The prompt sanitizer redacts "system:" as a role marker,
# which would turn every platform subject into "[REDACTED]masters". A name glued
# to the colon is not a role marker, so it is kept; "system: <text>" still is.
_RBAC_NAME_PREFIX = re.compile(r"\b(system|assistant):(?=[A-Za-z0-9])", re.IGNORECASE)
_KEEP = "\x00{}\x00"


def _text(value: Any, limit: int = 300) -> str:
    from .monitor.confidence import sanitize_block

    raw = str(value or "").replace("\x00", "")
    kept: list[str] = []

    def hold(m: re.Match) -> str:
        kept.append(m.group(0))
        return _KEEP.format(len(kept) - 1)

    out = sanitize_block(_RBAC_NAME_PREFIX.sub(hold, raw))
    out = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out)
    return out[:limit].replace("\n", " ")


def _block(lines: list[str]) -> str:
    return "--- BEGIN CLUSTER DATA ---\n" + "\n".join(lines) + "\n--- END CLUSTER DATA ---"


def _require_user_token(what: str, rule: str = "") -> str:
    """The caller's token; refuses (with the rule the service account lacks) when it is not here."""
    from .config import get_settings

    missing = f" The agent's service account has no such grant ({rule}), and Pulse does not add one." if rule else ""
    if not get_settings().agent.token_forwarding:
        raise RbacToolError(
            f"{what} needs your own token, and token forwarding is off on this deployment "
            f"(PULSE_AGENT_TOKEN_FORWARDING=false).{missing} Use Identity > Access review in the console."
        )
    token = _kc.get_current_user_token()
    if not token:
        raise RbacToolError(
            f"{what} needs your own token, and it did not reach Pulse with this request.{missing} Sign in again."
        )
    return token


# ---------------------------------------------------------------------------
# Subjects and rules (ports of engine/subjectAccess.ts)
# ---------------------------------------------------------------------------


def _is_platform_namespace(ns: str | None) -> bool:
    if not ns:
        return False
    return ns == "openshift" or ns.startswith(("openshift-", "kube-"))


def ref_from_username(username: str) -> dict:
    m = re.match(r"^system:serviceaccount:([^:]+):([^:]+)$", username)
    if m:
        return {"kind": "ServiceAccount", "namespace": m.group(1), "name": m.group(2)}
    return {"kind": "User", "name": username}


def username_of(ref: dict) -> str:
    if ref["kind"] == "ServiceAccount":
        return f"system:serviceaccount:{ref['namespace']}:{ref['name']}"
    return ref["name"]


def subject_title(ref: dict) -> str:
    if ref["kind"] == "ServiceAccount":
        return f"ServiceAccount {ref['namespace']}/{ref['name']}"
    return f"{ref['kind']} {ref['name']}"


def subject_matches(s: dict, ref: dict, binding_namespace: str | None = None) -> bool:
    """A binding subject means this subject (a ServiceAccount may also appear as a User)."""
    if ref["kind"] == "ServiceAccount":
        if s.get("kind") == "ServiceAccount":
            return s.get("name") == ref["name"] and (s.get("namespace") or binding_namespace) == ref["namespace"]
        return s.get("kind") == "User" and s.get("name") == username_of(ref)
    return s.get("kind") == ref["kind"] and s.get("name") == ref["name"]


def rule_allows(rule: dict, verb: str, group: str, resource: str) -> bool:
    """A rule allows verb on group/resource (resourceNames, which narrow it, are ignored)."""
    verbs = rule.get("verbs") or []
    groups = rule.get("apiGroups") or []
    resources = rule.get("resources") or []
    verb_ok = "*" in verbs or (verb != "*" and verb in verbs)
    group_ok = "*" in groups or group in groups
    resource_ok = "*" in resources or (resource != "*" and resource in resources)
    return verb_ok and group_ok and resource_ok


def _is_system(ref: dict) -> bool:
    if ref["kind"] == "ServiceAccount":
        return _is_platform_namespace(ref.get("namespace"))
    return ref["name"].startswith("system:")


def explain(review: dict, verb: str, resource: str, group: str, namespace: str, rbac: dict) -> list[dict]:
    """For each subject the review returned, the bindings the caller can see that grant it (WhoCanPanel.explain)."""

    def grants_via(b: dict) -> dict | None:
        ref = b.get("roleRef") or {}
        b_ns = (b.get("metadata") or {}).get("namespace")
        if ref.get("kind") == "Role":
            role = next(
                (
                    r
                    for r in rbac["roles"]
                    if (r.get("metadata") or {}).get("name") == ref.get("name")
                    and (r.get("metadata") or {}).get("namespace") == b_ns
                ),
                None,
            )
        else:
            role = next(
                (r for r in rbac["clusterRoles"] if (r.get("metadata") or {}).get("name") == ref.get("name")), None
            )
        rules = (role or {}).get("rules") or []
        if any(rule_allows(r, verb, group, resource) for r in rules):
            return {"kind": ref.get("kind", ""), "name": ref.get("name", "")}
        return None

    bindings = [(b, "ClusterRoleBinding") for b in rbac["crbs"]] + [
        (b, "RoleBinding")
        for b in rbac["rbs"]
        if not namespace or (b.get("metadata") or {}).get("namespace") == namespace
    ]

    def grants_for(ref: dict, via_group: str | None = None) -> list[dict]:
        out = []
        for b, kind in bindings:
            meta = b.get("metadata") or {}
            if not any(subject_matches(s, ref, meta.get("namespace")) for s in b.get("subjects") or []):
                continue
            role = grants_via(b)
            if role:
                out.append(
                    {
                        "binding": {
                            "kind": kind,
                            "name": meta.get("name", ""),
                            "namespace": meta.get("namespace") if kind == "RoleBinding" else None,
                        },
                        "role": role,
                        "viaGroup": via_group,
                    }
                )
        return out

    rows: list[dict] = []
    group_objs = {(g.get("metadata") or {}).get("name"): g for g in rbac["groups"]}
    for g in review.get("groups") or []:
        ref = {"kind": "Group", "name": g}
        members = (group_objs.get(g) or {}).get("users")
        rows.append({"subject": ref, "members": members, "grants": grants_for(ref), "system": _is_system(ref)})
    group_names = set(review.get("groups") or [])
    for u in review.get("users") or []:
        ref = ref_from_username(u)
        grants = grants_for(ref)
        if not grants:
            for gname, g in group_objs.items():
                if u in (g.get("users") or []) and gname in group_names:
                    grants += grants_for({"kind": "Group", "name": gname}, gname)
        # A user who can only because a listed group of theirs can is shown on the group's row.
        if grants and all(x["viaGroup"] for x in grants):
            continue
        rows.append({"subject": ref, "grants": grants, "system": _is_system(ref)})
    return rows


def _grant_text(g: dict) -> str:
    b = g["binding"]
    where = f"{b['namespace']}/" if b.get("namespace") else ""
    via = f"Group {g['viaGroup']} > " if g.get("viaGroup") else ""
    return f"{via}{b['kind']} {where}{b['name']} > {g['role']['kind']} {g['role']['name']}"


def _split_resource(resource: str, group: str) -> tuple[str, str]:
    """'deployments.apps' -> (deployments, apps) when no group is given; 'secrets' -> core."""
    r = resource.strip()
    if not group and "." in r:
        r, group = r.split(".", 1)
    return r, group


def _list(path: str, query: dict[str, str] | None = None) -> list[dict] | ToolError:
    got = _kc.get_raw_json(path, "list", query=query)
    if isinstance(got, ToolError):
        return got
    return list((got or {}).get("items") or [])


# ---------------------------------------------------------------------------
# who_can
# ---------------------------------------------------------------------------


@beta_tool
def who_can(verb: str, resource: str, namespace: str = "", group: str = ""):
    """Who can <verb> <resource> (in a namespace or cluster-wide): every user, group and service account, with the binding and role that grant it.

    Uses OpenShift's ResourceAccessReview (what `oc adm policy who-can` uses), run as
    the signed-in user, then explains each subject from the RoleBindings,
    ClusterRoleBindings, Roles and ClusterRoles the user can read. Platform subjects
    (system:*, openshift-*/kube-* service accounts) are counted, not listed.

    Args:
        verb: get, list, watch, create, update, patch, delete, deletecollection, impersonate, ... or '*'.
        resource: Plural resource, e.g. 'secrets', 'deployments', 'pods/exec', or 'deployments.apps'.
        namespace: Namespace to ask about; empty for cluster-wide.
        group: API group ('' for core, 'apps', 'rbac.authorization.k8s.io'); may also be given as 'resource.group'.
    """
    verb = (verb or "").strip().lower()
    res, grp = _split_resource((resource or "").lower(), (group or "").strip().lower())
    if not _VERB_RE.match(verb):
        return f"verb must be a Kubernetes verb such as get, list, delete or '*', got {verb!r}."
    if not _RESOURCE_RE.match(res):
        return f"resource must be a plural resource such as secrets or deployments, got {resource!r}."
    if not _GROUP_RE.match(grp):
        return f"group must be an API group such as apps, got {group!r}."
    if namespace and (err := _validate_k8s_namespace(namespace)):
        return err
    try:
        _require_user_token("Who can", WHO_CAN_RULE)
    except RbacToolError as e:
        return f"Not checked: {e}"

    discovery = _kc.get_raw_json(f"/apis/{AUTHZ}/v1", "discovery")
    if isinstance(discovery, ToolError):
        if discovery.status_code == 404:
            return (
                "Not checked: this cluster does not serve ResourceAccessReview (authorization.openshift.io), "
                "which only OpenShift has. Pulse does not approximate who-can from bindings alone."
            )
        return f"Not checked: API discovery failed. {discovery.message}"
    served = {r.get("name") for r in (discovery or {}).get("resources") or []}
    kind = "localresourceaccessreviews" if namespace else "resourceaccessreviews"
    if kind not in served:
        return f"Not checked: this cluster does not serve {AUTHZ}/{kind}."

    body = {
        "apiVersion": f"{AUTHZ}/v1",
        "kind": "LocalResourceAccessReview" if namespace else "ResourceAccessReview",
        "namespace": namespace,
        "verb": verb,
        # A subresource is part of the resource, as `oc adm policy who-can create pods/exec` sends it.
        "resource": res,
        "resourceAPIGroup": grp,
        "resourceName": "",
    }
    custom = _kc.get_custom_client()
    if namespace:
        review = _kc.safe(lambda: custom.create_namespaced_custom_object(AUTHZ, "v1", namespace, kind, body))
    else:
        review = _kc.safe(lambda: custom.create_cluster_custom_object(AUTHZ, "v1", kind, body))
    if isinstance(review, ToolError):
        if review.status_code == 403:
            return (
                f"Not checked: you may not create {AUTHZ} {kind}{' in ' + namespace if namespace else ''} "
                f"({WHO_CAN_RULE}). The agent's service account has no such grant either, and Pulse does not add one."
            )
        return f"Not checked: the access review failed. {review.message}"

    ns_q = f"/namespaces/{namespace}" if namespace else ""
    reads = {
        "ClusterRoleBindings": _list(f"{RBAC_PATH}/clusterrolebindings"),
        "RoleBindings": _list(f"{RBAC_PATH}{ns_q}/rolebindings"),
        "ClusterRoles": _list(f"{RBAC_PATH}/clusterroles"),
        "Roles": _list(f"{RBAC_PATH}{ns_q}/roles"),
        "Groups": _list("/apis/user.openshift.io/v1/groups"),
    }
    unreadable = [k for k, v in reads.items() if isinstance(v, ToolError)]

    def val(k: str) -> list[dict]:
        v = reads[k]
        return [] if isinstance(v, ToolError) else v

    rbac = {
        "crbs": val("ClusterRoleBindings"),
        "rbs": val("RoleBindings"),
        "clusterRoles": val("ClusterRoles"),
        "roles": val("Roles"),
        "groups": val("Groups"),
    }
    rows = explain(review if isinstance(review, dict) else {}, verb, res, grp, namespace, rbac)
    visible = [r for r in rows if not r["system"]]
    system = [r for r in rows if r["system"]]
    people: set[str] = set()
    for r in visible:
        if r["subject"]["kind"] == "User":
            people.add(r["subject"]["name"])
        people.update(r.get("members") or [])

    what = f"{verb} {res}{'.' + grp if grp else ''}"
    where = f"in {namespace}" if namespace else "cluster-wide"
    lines = [
        f"Who can {what} {where} (ResourceAccessReview as you): {len(visible)} subject(s), "
        f"{len(people)} named people, plus {len(system)} platform subject(s) not listed."
    ]
    if isinstance(review, dict) and review.get("evaluationError"):
        lines.append(
            f"The API server could not evaluate every rule, so the list may be short: {_text(review['evaluationError'])}"
        )
    if unreadable:
        lines.append(
            f"Could not read {', '.join(unreadable)} as you: subjects are listed, but some grant chains are missing."
        )
    data = []
    for r in visible[:MAX_ROWS]:
        members = f" (members: {', '.join(r['members'][:10])})" if r.get("members") else ""
        through = "; ".join(_grant_text(g) for g in r["grants"]) or "binding not visible to you"
        data.append(f"- {_text(subject_title(r['subject']), 200)}{_text(members, 300)}: {_text(through, 600)}")
    if len(visible) > MAX_ROWS:
        data.append(f"... and {len(visible) - MAX_ROWS} more")
    if system:
        names = ", ".join(subject_title(r["subject"]) for r in system[:8])
        data.append(f"Platform subjects ({len(system)}): {_text(names, 600)}{', ...' if len(system) > 8 else ''}")
    if data:
        lines.append(_block(data))
    else:
        lines.append("Nobody but the platform's own subjects can.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# effective_permissions
# ---------------------------------------------------------------------------


def _parse_subject(kind: str, name: str, namespace: str) -> dict:
    k = (kind or "").strip().lower()
    n = (name or "").strip()
    if k in ("user", "users"):
        if not _SUBJECT_RE.match(n):
            raise RbacToolError(f"{name!r} is not a valid user name.")
        return ref_from_username(n)
    if k in ("serviceaccount", "serviceaccounts", "sa"):
        if n.startswith("system:serviceaccount:"):
            ref = ref_from_username(n)
            if ref["kind"] != "ServiceAccount":
                raise RbacToolError(f"{name!r} is not a service account username.")
            return ref
        ns, _, sa = n.rpartition("/")
        ns = ns or namespace
        if _validate_k8s_namespace(ns) or not ns or not re.match(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", sa):
            raise RbacToolError("A service account is 'namespace/name' or system:serviceaccount:<ns>:<name>.")
        return {"kind": "ServiceAccount", "namespace": ns, "name": sa}
    if k in ("group", "groups"):
        raise RbacToolError(
            "Effective permissions are computed per user, as on the group's page. Ask about a member, or use "
            "who_can for one verb and resource."
        )
    raise RbacToolError(f"subject_kind must be User or ServiceAccount, got {kind!r}.")


def _memberships(ref: dict) -> tuple[list[str], list[str]]:
    """Groups to impersonate for a user (Group objects that list them, plus OAuth's virtual group) and notes."""
    if ref["kind"] != "User":
        return [], []
    notes: list[str] = []
    groups: list[str] = []
    got = _list("/apis/user.openshift.io/v1/groups")
    if isinstance(got, ToolError):
        notes.append(
            f"Groups could not be read as you ({_text(got.message, 200)}): access through groups is not included."
        )
    else:
        groups = sorted((g.get("metadata") or {}).get("name", "") for g in got if ref["name"] in (g.get("users") or []))
    # system:authenticated:oauth applies only to OAuth sign-ins: assumed when the user has an Identity.
    user = _kc.get_raw_json(f"/apis/user.openshift.io/v1/users/{ref['name']}", "get")
    if isinstance(user, ToolError):
        groups.append("system:authenticated:oauth")
        notes.append(
            f"May overstate: includes system:authenticated:oauth, which applies only if {ref['name']} signs in "
            "through OAuth (their User object could not be read)."
        )
    elif (user or {}).get("identities"):
        groups.append("system:authenticated:oauth")
    return groups, notes


def rules_review_as(namespace: str, user: str, group: str | None) -> dict:
    """SelfSubjectRulesReview impersonating ``user`` (and ``group``), with the caller's token."""
    from kubernetes.client.rest import ApiException

    if not _kc.get_current_user_token():
        # Never impersonate with the service account's credentials.
        raise RbacToolError("no forwarded user token")
    api_client = _kc.get_core_client().api_client
    headers = {"Impersonate-User": user, "Content-Type": "application/json", "Accept": "application/json"}
    if group:
        headers["Impersonate-Group"] = group
    body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview", "spec": {"namespace": namespace}}
    try:
        resp = api_client.call_api(
            "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
            "POST",
            header_params=headers,
            body=body,
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
    except ApiException as e:
        raise RbacToolError(f"{e.status} {e.reason}") from None
    raw = resp[0] if isinstance(resp, tuple) else resp
    data = raw.data if hasattr(raw, "data") else raw.read()
    return json.loads(data).get("status") or {}


def _union(lists: list[list[dict]]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for lst in lists:
        for r in lst:
            k = json.dumps(r, sort_keys=True)
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out


def _rule_text(r: dict) -> str:
    groups = ",".join(g or "core" for g in r.get("apiGroups") or []) or "core"
    names = f" (only {', '.join(r['resourceNames'])})" if r.get("resourceNames") else ""
    return f"{', '.join(r.get('verbs') or [])} on {', '.join(r.get('resources') or []) or '-'} [{groups}]{names}"


@beta_tool
def effective_permissions(subject_kind: str, subject_name: str, namespace: str):
    """What a user or service account can actually do in a namespace (its effective RBAC rules).

    Runs a read-only SelfSubjectRulesReview while impersonating the subject with the
    signed-in user's token, as the console's subject page does: once as the user alone
    and once per group they belong to, rules combined. Needs the signed-in user to be
    allowed to impersonate that subject.

    Args:
        subject_kind: User or ServiceAccount.
        subject_name: User name, or for a ServiceAccount 'namespace/name' (or just its name if it lives in `namespace`).
        namespace: Namespace whose permissions to compute.
    """
    if not namespace or (err := _validate_k8s_namespace(namespace)):
        return err or "namespace is required: rules are computed per namespace."
    try:
        ref = _parse_subject(subject_kind, subject_name, namespace)
        _require_user_token(
            "Effective permissions",
            "apiGroups: [''], resources: [users, groups, serviceaccounts], verbs: [impersonate]",
        )
    except RbacToolError as e:
        return f"Not checked: {e}"
    user = username_of(ref)
    groups, notes = _memberships(ref)
    ran_as: list[tuple[str, str | None]] = [(user, None)]
    ran_as += [(user, g) for g in dict.fromkeys(groups) if g != "system:authenticated"]

    results: list[dict] = []
    skipped_groups: list[str] = []
    for u, g in ran_as:
        try:
            results.append(rules_review_as(namespace, u, g))
        except RbacToolError as e:
            if g is None:
                return (
                    f"Not checked: impersonating {user} was refused ({_text(e, 200)}). This needs permission to "
                    f"impersonate {'serviceaccounts' if ref['kind'] == 'ServiceAccount' else 'users'}, which you do not have; "
                    "the agent never impersonates with its own service account."
                )
            skipped_groups.append(g)
    if skipped_groups:
        notes.append(
            f"Impersonating group(s) {', '.join(skipped_groups)} was refused, so access {ref['name']} gets "
            "through them is not included."
        )
    rules = _union([r.get("resourceRules") or [] for r in results])
    non_resource = _union([r.get("nonResourceRules") or [] for r in results])
    incomplete = any(r.get("incomplete") for r in results)
    errors = sorted({r["evaluationError"] for r in results if r.get("evaluationError")})

    lines = [
        f"Effective permissions of {subject_title(ref)} in {namespace}: {len(rules)} resource rule(s), "
        f"{len(non_resource)} non-resource rule(s). Computed by impersonating {user}"
        + (f" (alone and with each of {len(ran_as) - 1} group(s))" if len(ran_as) > 1 else "")
        + ": a read-only SelfSubjectRulesReview."
    ]
    if incomplete:
        lines.append(
            "Incomplete: the API server could not evaluate every rule (e.g. a webhook authorizer), so this may be short."
            + (f" {_text('; '.join(errors), 300)}" if errors else "")
        )
    lines += notes
    if any(
        "*" in (r.get("verbs") or []) and "*" in (r.get("resources") or []) and "*" in (r.get("apiGroups") or [])
        for r in rules
    ):
        lines.append("Includes all verbs on all resources (cluster-admin level) in this namespace.")
    data = [f"- {_text(_rule_text(r), 400)}" for r in rules[:MAX_RULES]]
    if len(rules) > MAX_RULES:
        data.append(f"... and {len(rules) - MAX_RULES} more rules")
    if data:
        lines.append(_block(data))
    lines.append(
        "For where the access comes from (binding > role chains), use who_can for a specific verb and resource."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_rbac_changes
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")


def parse_since(since: str, now: datetime | None = None) -> datetime:
    """'24h', '7d', '30m', '2w' or an ISO 8601 time; clamped to the 30-day retention. Empty: 7 days."""
    now = now or datetime.now(UTC)
    s = (since or "").strip().lower()
    if not s:
        ts = now - timedelta(days=7)
    elif m := _DURATION_RE.match(s):
        n, unit = int(m.group(1)), m.group(2)
        ts = (
            now
            - {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[
                unit
            ]
        )
    else:
        try:
            ts = datetime.fromisoformat((since or "").strip().replace("Z", "+00:00"))
        except ValueError:
            raise RbacToolError(
                f"since must be a duration such as 24h or 7d, or an ISO 8601 time; got {since!r}."
            ) from None
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return max(ts, now - timedelta(days=30))


def _qualified(namespace: str | None, name: str) -> str:
    return f"{namespace}/{name}" if namespace else name


def _subject_key(s: dict) -> str:
    ns = f"{s.get('namespace')}/" if s.get("namespace") else ""
    return f"{s.get('kind', '')} {ns}{s.get('name', '')}"


def summarize_change(kind: str, before: dict | None, after: dict | None) -> str:
    """What changed between two versions, in a line: subjects and roleRef for bindings, rules for roles."""
    if before is None and after is None:
        return ""
    if kind in ("RoleBinding", "ClusterRoleBinding"):
        b_sub = {_subject_key(s) for s in (before or {}).get("subjects") or []}
        a_sub = {_subject_key(s) for s in (after or {}).get("subjects") or []}
        parts = []
        a_ref = (after or {}).get("roleRef") or {}
        b_ref = (before or {}).get("roleRef") or {}
        if after is not None and (before is None or a_ref != b_ref):
            parts.append(f"role {a_ref.get('kind', '')} {a_ref.get('name', '')}")
        elif before is not None and after is None:
            parts.append(f"role was {b_ref.get('kind', '')} {b_ref.get('name', '')}")
        if a_sub - b_sub:
            parts.append(f"subjects added: {', '.join(sorted(a_sub - b_sub))}")
        if b_sub - a_sub:
            parts.append(f"subjects removed: {', '.join(sorted(b_sub - a_sub))}")
        return "; ".join(parts) or "no subject or role change (labels, annotations or other metadata)"
    if kind in ("Role", "ClusterRole"):
        b_rules = {_rule_text(r) for r in (before or {}).get("rules") or []}
        a_rules = {_rule_text(r) for r in (after or {}).get("rules") or []}
        parts = []
        if a_rules - b_rules:
            parts.append(f"rules added: {' | '.join(sorted(a_rules - b_rules))}")
        if b_rules - a_rules:
            parts.append(f"rules removed: {' | '.join(sorted(b_rules - a_rules))}")
        return "; ".join(parts) or "no rule change (labels, annotations, aggregation or other metadata)"
    keys = sorted(
        k
        for k in set((before or {}).keys()) | set((after or {}).keys())
        if k not in ("metadata", "apiVersion", "kind") and (before or {}).get(k) != (after or {}).get(k)
    )
    return f"fields changed: {', '.join(keys)}" if keys else "metadata only"


@beta_tool
def get_rbac_changes(since: str = "24h", namespace: str = ""):
    """What changed in RBAC: Role, ClusterRole, RoleBinding, ClusterRoleBinding, SCC and admission webhook changes Pulse observed, with who made them through Pulse.

    Reads the history behind Identity > Access review > RBAC changes and the Pulse write
    ledger, showing only objects the signed-in user may read. Each change says what was
    added or removed (subjects, role, rules). Platform objects (system:*, openshift-*,
    kube-*) are counted, not listed. Kept 30 days.

    Args:
        since: How far back: '24h', '7d', '30m', or an ISO 8601 time (default 24h, at most 30 days).
        namespace: Only this namespace's Roles and RoleBindings (cluster-scoped changes, which apply everywhere, are always included).
    """
    from .api.rbac_history_rest import _actor_for, _visible, is_system_object
    from .rbac_history import WATCHED_KINDS, status

    if namespace and (err := _validate_k8s_namespace(namespace)):
        return err
    try:
        start = parse_since(since)
        token = _require_user_token("RBAC history")
    except RbacToolError as e:
        return f"Not read: {e}"
    since_ms = int(start.timestamp() * 1000)

    from .repositories import get_rbac_history_repo

    repo = get_rbac_history_repo()
    try:
        raw = repo.fetch_changes(since_ms, "", None, "", 2000)
        ledger = repo.fetch_ledger(since_ms - 86_400_000, list(WATCHED_KINDS), 5000)
        first = repo.first_observed_at()
    except Exception:
        logger.warning("RBAC history read failed", exc_info=True)
        return "Not read: RBAC history storage is unavailable; the database could not be reached."
    if namespace:
        raw = [r for r in raw if (r.get("namespace") or "") in ("", namespace)]
        ledger = [e for e in ledger if (e.get("namespace") or "") in ("", namespace)]
    rows, hidden = _visible(raw, token)
    ledger_rows, _ = _visible(ledger, token)
    kept = [r for r in rows if not is_system_object(r["kind"], r.get("namespace") or "", r["name"])]
    system_hidden = len(rows) - len(kept)
    st = status()

    where = f" in {namespace} (and cluster-scoped)" if namespace else ""
    lines = [
        f"RBAC, SCC and webhook changes since {start.isoformat(timespec='minutes')}{where}: {len(kept)} shown"
        + (f", {system_hidden} platform change(s) not listed" if system_hidden else "")
        + (f", {hidden} hidden because you may not read those objects" if hidden else "")
        + "."
    ]
    if first is None or first > since_ms:
        began = datetime.fromtimestamp(first / 1000, UTC).isoformat(timespec="minutes") if first else "not yet"
        lines.append(f"History starts {began}: nothing before that was observed.")
    if st.get("unwatched"):
        lines.append(
            "Not watched (changes to these are missing): "
            + "; ".join(f"{k}: {_text(v, 120)}" for k, v in sorted(st["unwatched"].items()))
        )
    if st.get("last_error"):
        lines.append(f"Last history scan failed: {_text(st['last_error'], 200)}")
    data = []
    for r in kept[:MAX_ROWS]:
        at = datetime.fromtimestamp(r["observed_at"] / 1000, UTC).isoformat(timespec="minutes")
        obj = f"{r['kind']} {_qualified(r.get('namespace'), r['name'])}"
        actor = _actor_for(r, ledger_rows)
        by = (
            f" by {actor['name']} via Pulse{'' if actor['verified'] else ' (unverified)'}"
            if actor
            else (f" (field manager {r.get('manager')})" if r.get("manager") else "")
        )
        what = summarize_change(
            r["kind"], r.get("previous_content"), r.get("content") if r["change"] != "deleted" else None
        )
        data.append(f"- {at} {r['change']} {_text(obj, 200)}{_text(by, 200)}: {_text(what, 600)}")
    if len(kept) > MAX_ROWS:
        data.append(f"... and {len(kept) - MAX_ROWS} more (narrow with namespace or since)")
    writes = [w for w in ledger_rows if w["at"] >= since_ms]
    if writes:
        data.append(f"Writes made through Pulse ({len(writes)}):")
        for w in writes[:20]:
            at = datetime.fromtimestamp(w["at"] / 1000, UTC).isoformat(timespec="minutes")
            obj = f"{w['kind']} {_qualified(w.get('namespace'), w['name'])}"
            reason = f" ({w['reason']})" if w.get("reason") else ""
            data.append(f"- {at} {w['actor']} {w['verb']} {obj} [{w.get('outcome') or 'ok'}, {w['source']}]{reason}")
    if data:
        lines.append(_block([_text(d, 1200) for d in data]))
    else:
        lines.append("No change observed in that window.")
    lines.append("The console's Access review > RBAC changes shows the full diff and can restore a previous version.")
    return "\n".join(lines)


RBAC_TOOLS: list[Any] = [who_can, effective_permissions, get_rbac_changes]

from .tool_registry import register_tool

for _tool in RBAC_TOOLS:
    register_tool(_tool, is_write=False, category="security")
