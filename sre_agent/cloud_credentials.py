"""Explain cloud-credential problems Pulse cannot fix inside the cluster.

When the ``cloud-credential`` ClusterOperator is Degraded the cause is almost
never in the cluster: a component's CredentialsRequest asks for cloud
permissions the cloud account does not give. The degraded-operators check
stopped at "cloud-credential Degraded"; this module reads what the Cloud
Credential Operator (CCO) itself records and says, per failing request:

- which component is affected, what it is for, and what breaks without it;
- the cluster's credentials mode, read from ``CloudCredential/cluster``
  ``spec.credentialsMode`` and the ``cloudcredential.openshift.io/mode``
  annotation on the root Secret (metadata-only read: the Secret's data never
  leaves the API server);
- the condition reason and message, verbatim apart from control characters;
- the exact cloud permissions the request lists in ``spec.providerSpec``;
- a fix checklist for that mode, and whether ClusterVersion reports Failing
  because of it.

``compute_cloud_credentials`` is pure: it takes what was read (or the
``ToolError`` the read produced). ``read_cloud_credentials`` does the reads.
The monitor scanner, the chat tool and the tests share both.

Honesty rules: a read that failed is reported as unreadable, with the RBAC rule
it needs, never as healthy. A mode that could not be read is ``unknown`` and the
checklist then gives each alternative instead of assuming one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import ToolError

CCO_NAMESPACE = "openshift-cloud-credential-operator"
CLUSTER_OPERATOR = ("config.openshift.io", "v1", "clusteroperators", "cloud-credential")
CREDENTIALS_REQUESTS = ("cloudcredential.openshift.io", "v1", "credentialsrequests")
CCO_CONFIG = ("operator.openshift.io", "v1", "cloudcredentials", "cluster")
AUTHENTICATION = ("config.openshift.io", "v1", "authentications", "cluster")
CLUSTERVERSION = ("config.openshift.io", "v1", "clusterversions", "version")

MODE_ANNOTATION = "cloudcredential.openshift.io/mode"
# On a minted AWS target Secret: the IAM policy the operator last put on the
# minted user (a policy document, not a credential).
AWS_POLICY_ANNOTATION = "cloudcredential.openshift.io/aws-policy-last-applied"

# What the Cloud Credential Operator's AWS mint check needs on the root
# credential (OpenShift docs, "Using mint mode": permissions for the root IAM
# user). Named so a person can compare it with the user's policy.
AWS_MINT_ACTIONS = (
    "iam:CreateAccessKey",
    "iam:CreateUser",
    "iam:DeleteAccessKey",
    "iam:DeleteUser",
    "iam:DeleteUserPolicy",
    "iam:GetUser",
    "iam:GetUserPolicy",
    "iam:ListAccessKeys",
    "iam:PutUserPolicy",
    "iam:TagUser",
    "iam:SimulatePrincipalPolicy",
)

# CredentialsRequest condition types that mean the request is not satisfied.
# ``Ignored`` is not one of them: it marks a request for another platform.
FAILING_CONDITIONS = (
    "InsufficientCloudCreds",
    "CredentialsProvisionFailure",
    "MissingTargetNamespace",
    "CredentialsDeprovisionFailure",
    "StaleCredentials",
)
# Reasons a person fixes with cloud permissions (mode-specific checklist).
_PERMISSION_REASONS = {"InsufficientCloudCreds", "CredentialsProvisionFailure"}

MAX_MESSAGE_CHARS = 1000
MAX_PROBLEMS = 50
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Root credential the CCO reads in Mint and Passthrough modes, by cloud.
ROOT_SECRETS: dict[str, tuple[str, str]] = {
    "aws": ("kube-system", "aws-creds"),
    "gcp": ("kube-system", "gcp-credentials"),
    "azure": ("kube-system", "azure-credentials"),
    "vsphere": ("kube-system", "vsphere-creds"),
    "openstack": ("kube-system", "openstack-credentials"),
    "ovirt": ("kube-system", "ovirt-credentials"),
}

_PROVIDER_CLOUD = {
    "AWSProviderSpec": "aws",
    "GCPProviderSpec": "gcp",
    "AzureProviderSpec": "azure",
    "VSphereProviderSpec": "vsphere",
    "OpenStackProviderSpec": "openstack",
    "OvirtProviderSpec": "ovirt",
    "IBMCloudProviderSpec": "ibmcloud",
    "IBMCloudPowerVSProviderSpec": "ibmcloud",
    "NutanixProviderSpec": "nutanix",
}

_CLOUD_WORDS: dict[str, dict[str, str]] = {
    "aws": {"name": "AWS", "principal": "IAM user", "console": "the AWS console (IAM)", "ccoctl": "aws"},
    "gcp": {
        "name": "Google Cloud",
        "principal": "service account",
        "console": "the Google Cloud console (IAM & Admin)",
        "ccoctl": "gcp",
    },
    "azure": {
        "name": "Azure",
        "principal": "service principal",
        "console": "the Azure portal (Access control (IAM))",
        "ccoctl": "azure",
    },
    "vsphere": {"name": "vSphere", "principal": "vCenter user", "console": "vCenter (Roles)", "ccoctl": ""},
}
_GENERIC_WORDS = {"name": "cloud", "principal": "account", "console": "your cloud provider's console", "ccoctl": ""}

# What a request is for, from its name (OpenShift's own CredentialsRequest
# names). First match wins, so the specific patterns come first.
_COMPONENTS: list[tuple[str, str, str, str]] = [
    (
        "ebs-csi",
        "AWS EBS CSI driver",
        "creates, attaches and resizes EBS volumes for PersistentVolumeClaims",
        "new volumes cannot be provisioned, and volumes cannot be attached to or detached from nodes, "
        "so pods that need storage stay Pending or ContainerCreating",
    ),
    (
        "efs-csi",
        "AWS EFS CSI driver",
        "creates EFS access points for PersistentVolumeClaims",
        "new EFS volumes cannot be provisioned",
    ),
    (
        "pd-csi",
        "GCP Persistent Disk CSI driver",
        "creates and attaches persistent disks for PersistentVolumeClaims",
        "new volumes cannot be provisioned, attached or detached",
    ),
    (
        "azure-disk",
        "Azure Disk CSI driver",
        "creates and attaches managed disks for PersistentVolumeClaims",
        "new volumes cannot be provisioned, attached or detached",
    ),
    (
        "azure-file",
        "Azure File CSI driver",
        "creates Azure file shares for PersistentVolumeClaims",
        "new file-share volumes cannot be provisioned",
    ),
    (
        "vsphere-csi",
        "vSphere CSI driver",
        "creates and attaches vSphere volumes for PersistentVolumeClaims",
        "new volumes cannot be provisioned, attached or detached",
    ),
    (
        "vsphere-problem-detector",
        "vSphere problem detector",
        "checks the vSphere configuration the cluster depends on",
        "vSphere configuration problems are no longer reported",
    ),
    (
        "machine-api",
        "Machine API",
        "creates and deletes cloud instances for MachineSets",
        "new nodes cannot be created (scale-up, replacing a failed node) and deleted machines can leave "
        "instances running",
    ),
    (
        "cluster-api",
        "Cluster API",
        "creates and deletes cloud instances for Cluster API machines",
        "Cluster API machines cannot be created or deleted",
    ),
    (
        "image-registry",
        "Integrated image registry",
        "stores images in the cloud object storage bucket it manages",
        "registry storage cannot be created or reached, so pushes to the internal registry can fail",
    ),
    (
        "ingress",
        "Ingress operator",
        "manages the DNS records (and load balancers) for the cluster's routers",
        "DNS records for the router's wildcard domain (*.apps) cannot be created or updated",
    ),
    (
        "cloud-network-config",
        "Cloud network config controller",
        "assigns egress IP addresses on the cloud network",
        "egress IPs cannot be assigned to nodes",
    ),
    (
        "ccm",
        "Cloud controller manager",
        "manages cloud load balancers for LoadBalancer Services and node lifecycle",
        "LoadBalancer Services cannot get a cloud load balancer",
    ),
    (
        "iam-ro",
        "Cloud Credential Operator's read-only check",
        "lets the operator read the cloud account to verify credentials",
        "the operator cannot verify that other components' credentials are sufficient",
    ),
]


def _clean(value: Any, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Control characters stripped and length capped; otherwise verbatim."""
    text = _CONTROL_RE.sub("", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_component(name: str, target_namespace: str, target_name: str = "") -> dict[str, str]:
    """What a CredentialsRequest is for, from its name; generic when unknown."""
    lower = (name or "").lower()
    for needle, component, purpose, impact in _COMPONENTS:
        if needle in lower:
            return {"component": component, "purpose": purpose, "impact": impact, "known": "true"}
    if target_namespace == "openshift-cluster-csi-drivers":
        return {
            "component": f"Storage (CSI) driver ({name})",
            "purpose": "provisions and attaches cloud volumes",
            "impact": "volumes from this driver cannot be provisioned or attached",
            "known": "true",
        }
    return {
        "component": f"CredentialsRequest {name}",
        "purpose": f"its component reads cloud credentials from Secret {target_namespace}/{target_name}",
        "impact": "the component cannot authenticate to the cloud",
        "known": "",
    }


def cloud_permissions(provider_spec: dict | None) -> tuple[str, list[str]]:
    """(cloud, one plain line per permission entry) from ``spec.providerSpec``."""
    spec = provider_spec if isinstance(provider_spec, dict) else {}
    kind = str(spec.get("kind") or "")
    cloud = _PROVIDER_CLOUD.get(kind, "")
    lines: list[str] = []
    if kind == "AWSProviderSpec":
        for entry in spec.get("statementEntries") or []:
            if not isinstance(entry, dict):
                continue
            actions = entry.get("action") or []
            if isinstance(actions, str):
                actions = [actions]
            effect = _clean(entry.get("effect") or "Allow", 20)
            resource = _clean(entry.get("resource") or "*", 300)
            line = f"{effect} {', '.join(_clean(a, 120) for a in actions)} on {resource}"
            cond = entry.get("policyCondition")
            if cond:
                line += f" (condition: {_clean(json.dumps(cond, sort_keys=True, default=str), 300)})"
            lines.append(line)
    elif kind == "GCPProviderSpec":
        for role in spec.get("predefinedRoles") or []:
            lines.append(f"role {_clean(role, 200)}")
        for perm in spec.get("permissions") or []:
            lines.append(f"permission {_clean(perm, 200)}")
    elif kind == "AzureProviderSpec":
        for rb in spec.get("roleBindings") or []:
            role = rb.get("role") if isinstance(rb, dict) else rb
            lines.append(f"role {_clean(role, 200)}")
        for perm in spec.get("permissions") or []:
            lines.append(f"permission {_clean(perm, 200)}")
        for perm in spec.get("dataPermissions") or []:
            lines.append(f"data permission {_clean(perm, 200)}")
    elif kind == "VSphereProviderSpec":
        for group in spec.get("permissions") or []:
            privileges = group.get("privileges") if isinstance(group, dict) else None
            for p in privileges or []:
                lines.append(f"privilege {_clean(p, 200)}")
    elif kind in ("IBMCloudProviderSpec", "IBMCloudPowerVSProviderSpec"):
        for policy in spec.get("policies") or []:
            if isinstance(policy, dict):
                roles = ", ".join(_clean(r, 120) for r in policy.get("roles") or [])
                attrs = ", ".join(
                    f"{_clean(a.get('name'), 60)}={_clean(a.get('value'), 120)}"
                    for a in policy.get("attributes") or []
                    if isinstance(a, dict)
                )
                lines.append(f"roles {roles}" + (f" where {attrs}" if attrs else ""))
    return cloud, lines


def _conditions(obj: dict) -> list[dict]:
    return [c for c in ((obj.get("status") or {}).get("conditions") or []) if isinstance(c, dict)]


def _operator_state(co: Any) -> dict[str, Any]:
    if isinstance(co, ToolError):
        if co.status_code == 404:
            return {"readable": True, "present": False, "degraded": None, "reason": "", "message": "", "since": ""}
        return {
            "readable": False,
            "present": None,
            "degraded": None,
            "reason": "",
            "message": "",
            "since": "",
            "error": _clean(co, 300),
        }
    for c in _conditions(co or {}):
        if c.get("type") == "Degraded":
            return {
                "readable": True,
                "present": True,
                "degraded": c.get("status") == "True",
                "reason": _clean(c.get("reason"), 200),
                "message": _clean(c.get("message")),
                "since": str(c.get("lastTransitionTime") or ""),
            }
    return {"readable": True, "present": True, "degraded": False, "reason": "", "message": "", "since": ""}


def _cluster_version_state(cv: Any) -> dict[str, Any]:
    if isinstance(cv, ToolError) or not isinstance(cv, dict):
        return {"readable": False, "failing": None, "message": "", "becauseOfThis": False, "lastUpdate": None}
    failing = next((c for c in _conditions(cv) if c.get("type") == "Failing"), None)
    message = _clean((failing or {}).get("message"))
    history = (cv.get("status") or {}).get("history") or []
    last = history[0] if history and isinstance(history[0], dict) else None
    return {
        "readable": True,
        "failing": bool(failing and failing.get("status") == "True"),
        "message": message,
        "becauseOfThis": bool(failing and failing.get("status") == "True" and "cloud-credential" in message),
        "lastUpdate": (
            {"version": _clean(last.get("version"), 60), "state": _clean(last.get("state"), 40)} if last else None
        ),
    }


def _mode(cco: Any, root_meta: Any, auth: Any, cloud: str) -> dict[str, Any]:
    """The credentials mode as configured and as the root Secret's annotation records it."""
    setting: str | None
    if isinstance(cco, ToolError) or not isinstance(cco, dict):
        setting = None
    else:
        setting = str((cco.get("spec") or {}).get("credentialsMode") or "")

    root_ns, root_name = ROOT_SECRETS.get(cloud, ("", ""))
    root: dict[str, Any] = {"namespace": root_ns, "name": root_name, "state": "n/a", "annotation": None}
    if root_name:
        if isinstance(root_meta, ToolError):
            root["state"] = "absent" if root_meta.status_code == 404 else "unreadable"
        elif isinstance(root_meta, dict):
            root["state"] = "present"
            ann = (root_meta.get("annotations") or {}).get(MODE_ANNOTATION)
            root["annotation"] = _clean(ann, 40) if ann else None
            if root_meta.get("lastEdit"):
                root["lastEdit"] = root_meta["lastEdit"]
        else:
            root["state"] = "unreadable"

    sts: bool | None
    if isinstance(auth, ToolError) or not isinstance(auth, dict):
        sts = None
    else:
        sts = bool((auth.get("spec") or {}).get("serviceAccountIssuer"))

    annotation = (root["annotation"] or "").lower()
    if setting == "Manual":
        effective = "manual_sts" if sts else "manual"
    elif annotation == "insufficient":
        # The operator's own verdict on the root credential outranks the
        # configured mode: it cannot mint or pass through, whatever is asked.
        effective = "insufficient"
    elif setting == "Mint":
        effective = "mint"
    elif setting == "Passthrough":
        effective = "passthrough"
    elif annotation in ("mint", "passthrough", "insufficient"):
        # Default mode (or a setting Pulse could not read): the operator
        # decides from what the root credential can do and records it here.
        effective = annotation
    elif annotation == "manual":
        effective = "manual"
    else:
        effective = "unknown"

    label = {
        "mint": "Mint",
        "passthrough": "Passthrough",
        "insufficient": "Root credential insufficient (operator annotation): it can neither mint nor pass through",
        "manual": "Manual",
        "manual_sts": "Manual with short-lived tokens (STS / workload identity)",
        "unknown": "unknown",
    }[effective]
    if setting == "" and effective in ("mint", "passthrough", "insufficient"):
        label = f"Default (operator chose {label})" if effective != "insufficient" else label
    if setting == "Manual" and sts is None:
        label = "Manual (whether it uses short-lived tokens could not be read)"
    return {"setting": setting, "effective": effective, "label": label, "sts": sts, "rootSecret": root}


def _platform_cloud(requests: list[dict]) -> str:
    """The cloud this cluster runs on, from the requests the operator did not ignore."""
    counts: dict[str, int] = {}
    for cr in requests:
        if any(c.get("type") == "Ignored" and c.get("status") == "True" for c in _conditions(cr)):
            continue
        kind = str(((cr.get("spec") or {}).get("providerSpec") or {}).get("kind") or "")
        cloud = _PROVIDER_CLOUD.get(kind)
        if cloud:
            counts[cloud] = counts.get(cloud, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else ""


def _policy_actions(policy_json: Any) -> list[str] | None:
    """Actions in an IAM policy document (JSON text); None when it cannot be read."""
    try:
        doc = json.loads(str(policy_json))
    except (TypeError, ValueError):
        return None
    statements = doc.get("Statement") if isinstance(doc, dict) else None
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return None
    actions: list[str] = []
    for st in statements:
        if not isinstance(st, dict):
            continue
        act = st.get("Action") or []
        actions += [act] if isinstance(act, str) else [str(a) for a in act if isinstance(a, str)]
    return actions


def _spec_actions(provider_spec: Any) -> list[str]:
    spec = provider_spec if isinstance(provider_spec, dict) else {}
    out: list[str] = []
    for entry in spec.get("statementEntries") or []:
        if isinstance(entry, dict):
            act = entry.get("action") or []
            for a in [act] if isinstance(act, str) else act:
                if isinstance(a, str) and a not in out:
                    out.append(a)
    return out


def last_edit(metadata: dict) -> dict[str, str] | None:
    """The newest managedFields entry (time and manager) of an object's metadata."""
    entries = [m for m in (metadata.get("managedFields") or []) if isinstance(m, dict) and m.get("time")]
    if not entries:
        return None
    newest = max(entries, key=lambda m: str(m.get("time")))
    return {"time": _clean(newest.get("time"), 40), "manager": _clean(newest.get("manager"), 120)}


def secret_summary(got: Any, keep: tuple[str, ...]) -> Any:
    """From a metadata-only Secret read, only the named annotations and the last edit.

    Everything else is dropped: ``kubectl.kubernetes.io/last-applied-configuration``
    and similar annotations can carry a Secret's data.
    """
    if not isinstance(got, dict):
        return got
    meta = got.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    return {
        "annotations": {k: annotations[k] for k in keep if k in annotations},
        "lastEdit": last_edit(meta),
    }


# A cloud error that says the credential itself is no good: deleted, rotated or
# expired on the cloud side (a dev-account key reaper, a manual rotation).
_STALE_RE = re.compile(
    r"InvalidClientTokenId|SignatureDoesNotMatch|security token included in the request is invalid|"
    r"access key[^.]{0,60}?(does not exist|not found|invalid|expired|deleted)|invalid[_ ]grant|"
    r"invalid (access key|credentials?)|credentials? (have |has )?expired|key (has )?expired",
    re.IGNORECASE,
)
# A cloud error that says the root credential is not allowed to do something.
_DENIED_RE = re.compile(
    r"insufficient|not authorized to perform|AccessDenied|UnauthorizedOperation|AuthorizationFailed|"
    r"PERMISSION_DENIED|does not have permission",
    re.IGNORECASE,
)


def credential_cause(problem: dict[str, Any], mode: dict[str, Any]) -> tuple[str, str]:
    """Why the request fails, from its condition reason and message.

    ``root_permissions``: the root credential lacks permissions (recreating the
    Secret cannot help). ``stale``: the credential delivered to the component is
    invalid or gone on the cloud side (in Mint mode, deleting the target Secret
    makes the operator mint a fresh one). ``unclear``: the condition does not say.
    """
    cond = problem["condition"]
    text = f"{cond['reason']} {cond['message']}"
    gen, synced = problem.get("generation"), problem.get("lastSyncGeneration")
    if (
        mode.get("effective") == "insufficient"
        and isinstance(gen, int)
        and isinstance(synced, int)
        and gen > synced
        and problem.get("targetSecret") == "present"
    ):
        return (
            "update_blocked",
            f"this CredentialsRequest changed (generation {gen}, last synced {synced}) and the root credential is "
            "marked insufficient, so the operator cannot update the credential it minted earlier",
        )
    if cond["type"] == "InsufficientCloudCreds" or mode.get("effective") == "insufficient":
        return (
            "root_permissions",
            "the condition is InsufficientCloudCreds: the operator checked the root credential and it lacks the "
            "permissions it needs",
        )
    stale = _STALE_RE.search(text)
    if stale or cond["type"] == "StaleCredentials":
        why = f'the message reports "{stale.group(0)}"' if stale else "the condition is StaleCredentials"
        return "stale", why + ": the credential delivered to the component is invalid or no longer exists"
    denied = _DENIED_RE.search(text)
    if denied:
        return "root_permissions", f'the message reports "{denied.group(0)}": a permission is missing'
    return "unclear", ""


def _permission_steps(problem: dict[str, Any], mode: dict[str, Any]) -> list[str]:
    secret = problem["secret"]
    target = f"{secret['namespace']}/{secret['name']}"
    words = _CLOUD_WORDS.get(problem["cloud"], _GENERIC_WORDS)
    root = mode["rootSecret"]
    root_ref = f"{root['namespace']}/{root['name']}" if root.get("name") else "the cluster's root cloud credential"
    perms = "the permissions this CredentialsRequest lists"
    effective = mode["effective"]
    principal, console = words["principal"], words["console"]
    if effective == "mint":
        return [
            f"Mint mode: the Cloud Credential Operator uses the root credential in {root_ref} to create a separate "
            f"{principal} for each component.",
            f"In {console}, open the {principal} whose credential is stored in {root_ref} and make sure it can "
            f"still create {principal}s, their policies and keys (the permissions OpenShift documents for mint "
            "mode).",
            f"{problem['component']} needs {perms}; in mint mode the operator puts them on the {principal} it "
            "creates, so you do not add them by hand.",
        ]
    if effective == "passthrough":
        return [
            f"Passthrough mode: every component uses the root credential in {root_ref} itself.",
            f"In {console}, add {perms} to the {principal} (or its policy) whose credential is stored in {root_ref}.",
        ]
    if effective == "insufficient":
        return [
            f"The root credential in {root_ref} can neither create per-component {principal}s (Mint) nor do what "
            f"the components need itself (Passthrough); the operator marked it insufficient.",
            f"In {console}, give the {principal} whose credential is stored in {root_ref} either the mint "
            f"permissions OpenShift documents, or {perms} (and every other component's) for passthrough.",
        ]
    if effective == "manual_sts":
        tool = words["ccoctl"]
        return [
            "Manual mode with short-lived tokens: the component assumes a cloud role through the cluster's OIDC "
            "issuer, and the Cloud Credential Operator does not create or change that role.",
            f"Update the role for {problem['name']} so it has {perms}"
            + (
                f": extract the release's CredentialsRequests (`oc adm release extract --credentials-requests "
                f"--cloud={tool} --to=<dir> <release image>`), run `ccoctl {tool} create-iam-roles` (or the "
                f"{tool} equivalent) for this request, and apply the Secret manifest ccoctl writes for {target}."
                if tool
                else "."
            ),
        ]
    if effective == "manual":
        return [
            "Manual mode: the Cloud Credential Operator does not create or change cloud credentials; an "
            f"administrator provides Secret {target} for each request.",
            f"In {console}, create or update the {principal} for {problem['name']} with {perms}, then update Secret "
            f"{target} with its credential (or regenerate it with ccoctl if the cluster was installed that way).",
        ]
    return [
        "Pulse could not read the credentials mode, so here is each case:",
        f"Mint or Passthrough: in {console}, give the {principal} whose credential is stored in {root_ref} "
        f"{perms} (Passthrough) or the permission to create per-component {principal}s (Mint).",
        f"Manual or STS: create or update the {principal} or role for {problem['name']} with {perms} and the "
        f"credential in Secret {target} (ccoctl for STS).",
    ]


def _stale_steps(problem: dict[str, Any], mode: dict[str, Any]) -> list[str]:
    secret = problem["secret"]
    target = f"{secret['namespace']}/{secret['name']}"
    words = _CLOUD_WORDS.get(problem["cloud"], _GENERIC_WORDS)
    root = mode["rootSecret"]
    root_ref = f"{root['namespace']}/{root['name']}" if root.get("name") else "the cluster's root cloud credential"
    effective = mode["effective"]
    principal = words["principal"]
    minted = f" ({principal} {problem['mintedUser']})" if problem.get("mintedUser") else ""
    delete = [
        f"Mint mode: the root credential can still mint, but the key minted for this component{minted} is no "
        "longer valid (deleted, rotated or expired on the cloud side, for example by a key reaper). Deleting the "
        "target Secret makes the Cloud Credential Operator mint a fresh key.",
        f"Run it yourself (Pulse does not delete it): `oc delete secret {secret['name']} -n {secret['namespace']}`. "
        "The operator recreates the Secret within a few minutes.",
        f"Pods that mount the Secret as a file pick up the new key on their own; pods that read it as environment "
        f"variables keep the old one until restarted (`oc rollout restart` on {problem['component']}'s Deployment "
        f"or DaemonSet in {secret['namespace']}).",
    ]
    if effective == "mint":
        return delete
    if effective == "passthrough":
        return [
            f"Passthrough mode: the component uses a copy of the root credential in {root_ref}, so the root "
            "credential itself is invalid or was rotated.",
            f"If the key in {root_ref} is no longer valid, put a valid key for the {principal} there; the operator "
            f"copies it to Secret {target}.",
            f"If {root_ref} already holds a valid key and only the copy is old, run yourself (Pulse does not delete "
            f"it): `oc delete secret {secret['name']} -n {secret['namespace']}`; the operator copies the root "
            "credential again.",
        ]
    if effective in ("manual", "manual_sts"):
        return [
            f"Manual mode: the operator does not create credentials, so the one in Secret {target} must be "
            "replaced by whoever provides it (a new key, or re-running ccoctl for STS).",
        ]
    return [
        "If the cluster uses Mint mode: " + delete[0],
        delete[1],
        delete[2],
        f"If it uses Passthrough: put a valid key in {root_ref}. If Manual: replace the credential in Secret "
        f"{target} yourself.",
    ]


def fix_steps(problem: dict[str, Any], mode: dict[str, Any]) -> list[str]:
    """The fix checklist for one failing request, matched to the cause and the credentials mode."""
    reason = problem["condition"]["type"]
    secret = problem["secret"]
    target = f"{secret['namespace']}/{secret['name']}"

    if reason == "MissingTargetNamespace":
        return [
            f"The namespace {secret['namespace']} that should receive Secret {target} does not exist.",
            "Usually the component is not installed (an optional capability that is turned off): if you do not "
            "use it, nothing needs fixing.",
            f"If you expect {problem['component']} to run, install or enable it; the Cloud Credential Operator "
            "creates the Secret once the namespace exists.",
        ]
    if reason not in _PERMISSION_REASONS and reason != "StaleCredentials":
        return [
            "Read the operator's message above: it names what failed.",
            f"Check the Cloud Credential Operator's logs in {CCO_NAMESPACE} for repeated errors about {problem['name']}.",
            "The operator retries on its own; Pulse resolves this item when the condition clears.",
        ]

    effective = mode["effective"]
    manual = effective in ("manual", "manual_sts")
    cause, why = credential_cause(problem, mode)
    steps: list[str] = []
    if reason == "CredentialsProvisionFailure":
        steps.append("Read the operator's message above: it says what the cloud API refused.")
    words = _CLOUD_WORDS.get(problem["cloud"], _GENERIC_WORDS)
    root = mode["rootSecret"]
    root_ref = f"{root['namespace']}/{root['name']}" if root.get("name") else "the cluster's root cloud credential"
    minted = problem.get("mintedUser")
    missing = problem.get("missingActions")
    if cause == "update_blocked":
        principal, console = words["principal"], words["console"]
        user = f"{principal} {minted}" if minted else f"the {principal} the operator minted for this component"
        steps.append(f"Cause: {why}.")
        if missing:
            steps.append(
                f"Missing from the policy last applied to {user}: {', '.join(missing)}. Only calls that need these "
                "fail."
            )
        elif missing is None:
            steps.append(
                "Pulse could not compare the request with the policy last applied to the minted credential, so it "
                "cannot name the missing actions."
            )
        steps.append(
            f"The existing credential in Secret {target} still works for everything else {problem['component']} does."
        )
        steps.append(
            f"Do NOT delete Secret {target}: in this state that removes the working credential, and the operator "
            "cannot mint a replacement while the root credential is insufficient."
        )
        mint_perms = f" ({', '.join(AWS_MINT_ACTIONS)})" if problem["cloud"] == "aws" else ""
        steps.append(
            f"Fix: in {console}, give the {principal} whose credential is stored in {root_ref} the mint-mode "
            f"permissions OpenShift documents again{mint_perms}. The operator then marks it mint and updates the "
            f"policy of {user} by itself."
        )
        if missing:
            steps.append(
                f"Stopgap until then: in {console}, add {', '.join(missing)} to the policy of {user}. The operator "
                "replaces that policy with the full one once it can mint again."
            )
        if root.get("lastEdit"):
            edit = root["lastEdit"]
            steps.append(
                f"For context: {root_ref} was last changed at {edit['time']} by {edit['manager'] or 'an unnamed client'}"
                " (managedFields); a replaced root key with fewer permissions is the usual cause."
            )
    elif cause == "root_permissions":
        steps.append(f"Cause: {why}.")
        steps += _permission_steps(problem, mode)
        if manual:
            consequence = "in Manual mode the operator does not recreate them."
        elif problem.get("targetSecret") == "present":
            consequence = (
                "it removes the credential the component has now, and the operator cannot create a new one while "
                "the root credential lacks permissions."
            )
        else:
            consequence = "the operator recreates them with the same root credential and fails the same way."
        steps.append(
            f"Deleting Secret {target} or the CredentialsRequest {problem['name']} does not fix this: {consequence}"
        )
    elif cause == "stale":
        steps.append(f"Cause: {why}.")
        steps += _stale_steps(problem, mode)
    else:
        steps.append(
            "Pulse cannot tell from the condition whether the root credential lacks permissions or the credential "
            "delivered to this component is stale; check both."
        )
        steps += ["If permissions are missing: " + s for s in _permission_steps(problem, mode)[:1]]
        steps += _permission_steps(problem, mode)[1:]
        if not manual:
            steps += ["If the minted key is stale: " + s for s in _stale_steps(problem, mode)[:1]]
            steps += _stale_steps(problem, mode)[1:]
            steps.append(
                "Deleting the Secret helps only in the stale case; with missing permissions the operator recreates "
                "it and fails the same way."
            )
    steps.append(
        "After the change the Cloud Credential Operator retries on its own; Pulse resolves this item when the "
        "condition clears and cloud-credential is no longer Degraded."
    )
    return steps


def _problem(cr: dict, failing: list[dict], cloud_default: str) -> dict[str, Any]:
    meta = cr.get("metadata") or {}
    spec = cr.get("spec") or {}
    secret_ref = spec.get("secretRef") or {}
    name = _clean(meta.get("name"), 253)
    target_ns = _clean(secret_ref.get("namespace"), 253)
    target_name = _clean(secret_ref.get("name"), 253)
    cloud, permissions = cloud_permissions(spec.get("providerSpec"))
    # Most specific failure first: a permission problem before a missing namespace.
    failing.sort(key=lambda c: FAILING_CONDITIONS.index(c.get("type")))
    primary = failing[0]
    desc = describe_component(name, target_ns, target_name)
    provider_status = (cr.get("status") or {}).get("providerStatus") or {}
    if not isinstance(provider_status, dict):
        provider_status = {}
    return {
        "name": name,
        "component": desc["component"],
        "purpose": desc["purpose"],
        "impact": desc["impact"],
        "secret": {"namespace": target_ns, "name": target_name},
        "cloud": cloud or cloud_default,
        "providerKind": _clean(((spec.get("providerSpec") or {}).get("kind")), 60),
        "permissions": permissions,
        "condition": {
            "type": str(primary.get("type")),
            "reason": _clean(primary.get("reason"), 200),
            "message": _clean(primary.get("message")),
            "since": str(primary.get("lastTransitionTime") or ""),
        },
        "otherConditions": [str(c.get("type")) for c in failing[1:]],
        "generation": meta.get("generation") if isinstance(meta.get("generation"), int) else None,
        "lastSyncGeneration": (
            (cr.get("status") or {}).get("lastSyncGeneration")
            if isinstance((cr.get("status") or {}).get("lastSyncGeneration"), int)
            else None
        ),
        "specActions": _spec_actions(spec.get("providerSpec")),
        # The cloud user the operator minted (Mint mode), a name only.
        "mintedUser": _clean(provider_status.get("user") or provider_status.get("serviceAccountID"), 120) or None,
    }


def _apply_target_secret(problem: dict[str, Any], got: Any) -> None:
    """Target Secret state and, for AWS, the actions missing from its last-applied policy."""
    problem.setdefault("missingActions", None)
    if got is None:
        problem["targetSecret"] = "unknown"
        return
    if isinstance(got, ToolError):
        problem["targetSecret"] = "absent" if got.status_code == 404 else "unreadable"
        return
    problem["targetSecret"] = "present"
    applied = _policy_actions((got.get("annotations") or {}).get(AWS_POLICY_ANNOTATION))
    if applied is not None and problem["specActions"]:
        have = {a.lower() for a in applied}
        problem["missingActions"] = [a for a in problem["specActions"] if a.lower() not in have]


def failing_targets(items: list[dict]) -> list[tuple[str, str]]:
    """(namespace, name) of the target Secret of each failing request, for the metadata reads."""
    out: list[tuple[str, str]] = []
    for cr in items:
        if any(c.get("type") in FAILING_CONDITIONS and c.get("status") == "True" for c in _conditions(cr)):
            ref = (cr.get("spec") or {}).get("secretRef") or {}
            ns, name = str(ref.get("namespace") or ""), str(ref.get("name") or "")
            if ns and name and (ns, name) not in out:
                out.append((ns, name))
    return out[:MAX_PROBLEMS]


def compute_cloud_credentials(
    cluster_operator: Any,
    credentials_requests: Any,
    cco_config: Any = None,
    root_secret_meta: Any = None,
    authentication: Any = None,
    cluster_version: Any = None,
    target_secrets: dict[tuple[str, str], Any] | None = None,
) -> dict[str, Any]:
    """The explanation, from what was read. Every input may be a ``ToolError``.

    ``target_secrets`` maps (namespace, name) of a failing request's target
    Secret to its metadata-only summary (``secret_summary``) or the read error.
    """
    operator = _operator_state(cluster_operator)
    cv = _cluster_version_state(cluster_version)
    unreadable: list[dict[str, str]] = []
    if not operator["readable"]:
        unreadable.append(
            {
                "what": "ClusterOperator cloud-credential",
                "rule": "get on clusteroperators.config.openshift.io",
                "error": operator.get("error", ""),
            }
        )

    base: dict[str, Any] = {
        "operator": operator,
        "clusterVersion": cv,
        "mode": None,
        "problems": [],
        "unreadable": unreadable,
    }

    if isinstance(credentials_requests, ToolError):
        if credentials_requests.status_code == 404 and operator["present"] is not True:
            return {**base, "status": "not_applicable", "reason": "This cluster has no Cloud Credential Operator."}
        unreadable.append(
            {
                "what": f"CredentialsRequests in {CCO_NAMESPACE}",
                "rule": f"get/list on credentialsrequests.cloudcredential.openshift.io in namespace {CCO_NAMESPACE}",
                "error": _clean(credentials_requests, 300),
            }
        )
        why = (
            "cloud-credential is Degraded, but Pulse cannot read the CredentialsRequests to say which component "
            "is affected"
            if operator["degraded"]
            else "Pulse cannot read the CredentialsRequests, so it cannot tell whether any component lacks "
            "cloud credentials"
        )
        return {**base, "status": "unknown", "reason": f"{why} (needs {unreadable[-1]['rule']})."}

    raw = (credentials_requests.get("items") or []) if isinstance(credentials_requests, dict) else []
    items = [i for i in raw if isinstance(i, dict)]
    cloud = _platform_cloud(items)
    mode = _mode(cco_config, root_secret_meta, authentication, cloud)
    base["mode"] = mode
    base["cloud"] = cloud
    if mode["setting"] is None:
        unreadable.append(
            {
                "what": "CloudCredential cluster (credentials mode)",
                "rule": "get on cloudcredentials.operator.openshift.io",
                "error": _clean(cco_config, 300) if isinstance(cco_config, ToolError) else "",
            }
        )

    problems: list[dict[str, Any]] = []
    for cr in sorted(items, key=lambda i: str((i.get("metadata") or {}).get("name") or "")):
        failing = [c for c in _conditions(cr) if c.get("type") in FAILING_CONDITIONS and c.get("status") == "True"]
        if failing:
            problem = _problem(cr, failing, cloud)
            _apply_target_secret(
                problem, (target_secrets or {}).get((problem["secret"]["namespace"], problem["secret"]["name"]))
            )
            problem["steps"] = fix_steps(problem, mode)
            problems.append(problem)
    base["problems"] = problems[:MAX_PROBLEMS]

    if problems:
        names = ", ".join(p["name"] for p in problems[:5]) + (
            f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        )
        return {**base, "status": "failing", "reason": f"{len(problems)} CredentialsRequest(s) failing: {names}."}
    if operator["degraded"]:
        return {
            **base,
            "status": "degraded_unexplained",
            "reason": (
                "cloud-credential is Degraded but no CredentialsRequest reports a failing condition; the cause is "
                f"in the operator itself. Check the Cloud Credential Operator pods and logs in {CCO_NAMESPACE}."
            ),
        }
    if operator["degraded"] is None and not operator["readable"]:
        return {
            **base,
            "status": "healthy_requests",
            "reason": "No CredentialsRequest reports a failing condition (the operator's own status was unreadable).",
        }
    return {**base, "status": "healthy", "reason": "Every CredentialsRequest is satisfied."}


def read_cloud_credentials() -> dict[str, Any]:
    """Read the sources (read-only; the root Secret metadata-only) and compute the explanation.

    Only the CredentialsRequest list is required: the mode, the root Secret's
    annotation, the OIDC issuer and ClusterVersion are optional context, read
    under ``optional_access`` so that a missing grant for one of them reports
    the mode as unknown instead of failing the monitor scanner.
    """
    from . import k8s_client as _kc
    from .monitor.scanner_health import optional_access

    custom = _kc.get_custom_client()
    g, v, p, n = CLUSTER_OPERATOR
    with optional_access():
        co = _kc.safe(lambda: custom.get_cluster_custom_object(g, v, p, n))
    rg, rv, rp = CREDENTIALS_REQUESTS
    crs = _kc.safe(lambda: custom.list_namespaced_custom_object(rg, rv, CCO_NAMESPACE, rp))
    if isinstance(crs, ToolError):
        return compute_cloud_credentials(co, crs)

    items = [i for i in (crs or {}).get("items") or [] if isinstance(i, dict)]
    cloud = _platform_cloud(items)
    with optional_access():
        cg, cv_, cp, cn = CCO_CONFIG
        cco = _kc.safe(lambda: custom.get_cluster_custom_object(cg, cv_, cp, cn))
        ag, av, ap, an = AUTHENTICATION
        auth = _kc.safe(lambda: custom.get_cluster_custom_object(ag, av, ap, an))
        vg, vv, vp, vn = CLUSTERVERSION
        cv = _kc.safe(lambda: custom.get_cluster_custom_object(vg, vv, vp, vn))
        from .monitor.change_correlation import _METADATA_ONE

        root_meta: Any = None
        if cloud in ROOT_SECRETS:
            root_ns, root_name = ROOT_SECRETS[cloud]
            got = _kc.get_raw_json(
                f"/api/v1/namespaces/{root_ns}/secrets/{root_name}",
                f"get Secret {root_ns}/{root_name} (metadata only)",
                accept=_METADATA_ONE,
            )
            root_meta = secret_summary(got, (MODE_ANNOTATION,))
        targets: dict[tuple[str, str], Any] = {}
        for ns, name in failing_targets(items):
            targets[(ns, name)] = secret_summary(
                _kc.get_raw_json(
                    f"/api/v1/namespaces/{ns}/secrets/{name}",
                    f"get Secret {ns}/{name} (metadata only)",
                    accept=_METADATA_ONE,
                ),
                (AWS_POLICY_ANNOTATION,),
            )
    return compute_cloud_credentials(co, crs, cco, root_meta, auth, cv, targets)


# -- Rendering --

PRIVACY_NOTE = (
    "Pulse never reads credential values: it reads the CredentialsRequests and only the mode annotation of the "
    "root Secret."
)


def _mode_line(mode: dict[str, Any] | None) -> str:
    if not mode:
        return "Credentials mode: unknown (not read)."
    setting = mode["setting"]
    configured = (
        "could not be read"
        if setting is None
        else (f"spec.credentialsMode={setting}" if setting else "spec.credentialsMode unset (default)")
    )
    root = mode["rootSecret"]
    if root.get("name"):
        ref = f"{root['namespace']}/{root['name']}"
        if root["state"] == "present":
            ann = root["annotation"]
            root_text = (
                f"root Secret {ref} annotated {MODE_ANNOTATION}={ann}"
                if ann
                else f"root Secret {ref} has no mode annotation"
            )
        elif root["state"] == "absent":
            root_text = f"root Secret {ref} is not present"
        else:
            root_text = f"root Secret {ref} could not be read"
    else:
        root_text = "no root Secret for this cloud"
    return f"Credentials mode: {mode['label']} ({configured}; {root_text})."


def _cluster_version_line(cv: dict[str, Any]) -> str | None:
    if not cv.get("failing"):
        return None
    last = cv.get("lastUpdate")
    done = (
        f" The last update ({last['version']}) is {last['state']}; Failing clears once cloud-credential is no longer "
        "Degraded."
        if last and cv.get("becauseOfThis")
        else ""
    )
    if cv.get("becauseOfThis"):
        return f'ClusterVersion reports Failing because of this: "{cv["message"]}".{done}'
    return f'ClusterVersion also reports Failing: "{cv["message"]}".'


def render_problem(problem: dict[str, Any], data: dict[str, Any]) -> str:
    """Plain-text explanation of one failing request (finding summary, tool output)."""
    secret = problem["secret"]
    cond = problem["condition"]
    if cond["type"] == "MissingTargetNamespace":
        first = (
            f"{problem['component']} ({problem['purpose']}): the operator cannot deliver its credential because "
            f"namespace {secret['namespace']} does not exist."
        )
    else:
        first = f"{problem['component']} ({problem['purpose']}) has no working cloud credential: {problem['impact']}."
    lines = [
        first,
        f"CredentialsRequest {CCO_NAMESPACE}/{problem['name']} → Secret {secret['namespace']}/{secret['name']}.",
        f"Condition {cond['type']}" + (f" ({cond['reason']})" if cond["reason"] else "") + f': "{cond["message"]}"',
        _mode_line(data.get("mode")),
    ]
    cv_line = _cluster_version_line(data.get("clusterVersion") or {})
    if cv_line:
        lines.append(cv_line)
    if problem["permissions"]:
        cloud = _CLOUD_WORDS.get(problem["cloud"], _GENERIC_WORDS)["name"]
        lines.append(f"{cloud} permissions it needs ({problem['providerKind']}):")
        lines += [f"  - {p}" for p in problem["permissions"]]
    elif cond["type"] in _PERMISSION_REASONS:
        lines.append(
            f"The request lists no cloud permissions Pulse can read ({problem['providerKind'] or 'no providerSpec'})."
        )
    lines.append("Fix:")
    lines += [f"  {i}. {s}" for i, s in enumerate(problem["steps"], 1)]
    lines.append(PRIVACY_NOTE)
    return "\n".join(lines)


def problem_title(problem: dict[str, Any]) -> str:
    cond = problem["condition"]["type"]
    if cond == "MissingTargetNamespace":
        return f"{problem['component']}: credentials not delivered, namespace {problem['secret']['namespace']} missing"
    return f"{problem['component']} cannot get its cloud credentials ({cond})"


def render_cloud_credentials(data: dict[str, Any]) -> str:
    """Plain-text report of a ``compute_cloud_credentials`` result."""
    status = data["status"]
    headline = {
        "failing": "FAILING",
        "healthy": "HEALTHY",
        "healthy_requests": "NO FAILING REQUESTS",
        "degraded_unexplained": "DEGRADED",
        "unknown": "UNKNOWN",
        "not_applicable": "NOT APPLICABLE",
    }[status]
    lines = [f"Cloud credentials: {headline} — {data['reason']}"]
    op = data["operator"]
    if op.get("degraded"):
        lines.append(f'ClusterOperator cloud-credential is Degraded ({op["reason"]}): "{op["message"]}"')
    elif op.get("degraded") is False:
        lines.append("ClusterOperator cloud-credential is not Degraded.")
    if status != "failing":
        if data.get("mode"):
            lines.append(_mode_line(data["mode"]))
        cv_line = _cluster_version_line(data.get("clusterVersion") or {})
        if cv_line:
            lines.append(cv_line)
    for i, problem in enumerate(data["problems"], 1):
        lines.append("")
        lines.append(f"[{i}] {problem_title(problem)}")
        lines.append(render_problem(problem, data))
    for u in data["unreadable"]:
        lines.append("")
        lines.append(f"Could not read {u['what']}: Pulse needs {u['rule']}. {u['error']}".rstrip())
    return "\n".join(lines)
