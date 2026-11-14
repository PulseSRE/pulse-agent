"""cloud-credential Degraded is explained, not just reported.

The live cluster this comes from: OpenShift 5.0.1 on AWS, cloud-credential
Degraded and ClusterVersion Failing=True after a completed update, because the
AWS EBS CSI driver's CredentialsRequest had InsufficientCloudCreds. Pulse said
"cloud-credential Degraded" and stopped; the person had to find the cause in the
Cloud Credential Operator. These fixtures are shaped like the real objects.
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.cloud_credentials import (
    MODE_ANNOTATION,
    compute_cloud_credentials,
    read_cloud_credentials,
    render_cloud_credentials,
)
from sre_agent.errors import ToolError
from sre_agent.monitor import scanner_health
from sre_agent.monitor.scanner_health import explain_failure, get_failure, scanning

CCO_NS = "openshift-cloud-credential-operator"

EBS_ACTIONS = [
    "ec2:AttachVolume",
    "ec2:CreateSnapshot",
    "ec2:CreateTags",
    "ec2:CreateVolume",
    "ec2:DeleteSnapshot",
    "ec2:DeleteTags",
    "ec2:DeleteVolume",
    "ec2:DescribeInstances",
    "ec2:DescribeSnapshots",
    "ec2:DescribeTags",
    "ec2:DescribeVolumes",
    "ec2:DescribeVolumesModifications",
    "ec2:DetachVolume",
    "ec2:ModifyVolume",
    "ec2:DescribeAvailabilityZones",
    "ec2:EnableFastSnapshotRestores",
]

EBS_CR = {
    "apiVersion": "cloudcredential.openshift.io/v1",
    "kind": "CredentialsRequest",
    "metadata": {
        "name": "aws-ebs-csi-driver-operator",
        "namespace": CCO_NS,
        "annotations": {"include.release.openshift.io/self-managed-high-availability": "true"},
    },
    "spec": {
        "providerSpec": {
            "apiVersion": "cloudcredential.openshift.io/v1",
            "kind": "AWSProviderSpec",
            "statementEntries": [
                {"action": EBS_ACTIONS, "effect": "Allow", "resource": "*"},
                {
                    "action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    "effect": "Allow",
                    "resource": "*",
                },
                {
                    "action": ["kms:RevokeGrant", "kms:CreateGrant", "kms:ListGrants"],
                    "effect": "Allow",
                    "resource": "*",
                    "policyCondition": {"Bool": {"kms:GrantIsForAWSResource": True}},
                },
            ],
        },
        "secretRef": {"name": "ebs-cloud-credentials", "namespace": "openshift-cluster-csi-drivers"},
        "serviceAccountNames": ["aws-ebs-csi-driver-operator", "aws-ebs-csi-driver-controller-sa"],
    },
    "status": {
        "conditions": [
            {
                "type": "InsufficientCloudCreds",
                "status": "True",
                "reason": "CloudCredsInsufficient",
                "message": "cloud credentials insufficient to satisfy credentials request",
                "lastProbeTime": "2026-11-13T08:00:00Z",
                "lastTransitionTime": "2026-11-12T08:00:00Z",
            }
        ],
        "lastSyncGeneration": 0,
        "provisioned": False,
    },
}

MACHINE_API_CR = {
    "metadata": {"name": "openshift-machine-api-aws", "namespace": CCO_NS},
    "spec": {
        "providerSpec": {
            "kind": "AWSProviderSpec",
            "statementEntries": [{"action": ["ec2:RunInstances"], "effect": "Allow", "resource": "*"}],
        },
        "secretRef": {"name": "aws-cloud-credentials", "namespace": "openshift-machine-api"},
    },
    "status": {"conditions": [], "provisioned": True, "lastSyncGeneration": 1},
}

# A request for another platform: the operator marks it Ignored.
GCP_IGNORED_CR = {
    "metadata": {"name": "openshift-gcp-pd-csi-driver-operator", "namespace": CCO_NS},
    "spec": {
        "providerSpec": {"kind": "GCPProviderSpec", "predefinedRoles": ["roles/compute.storageAdmin"]},
        "secretRef": {"name": "gcp-pd-cloud-credentials", "namespace": "openshift-cluster-csi-drivers"},
    },
    "status": {
        "conditions": [{"type": "Ignored", "status": "True", "reason": "InfrastructureMismatch", "message": "x"}]
    },
}

CO_DEGRADED = {
    "metadata": {"name": "cloud-credential"},
    "status": {
        "conditions": [
            {"type": "Available", "status": "True"},
            {
                "type": "Degraded",
                "status": "True",
                "reason": "CredentialsFailing",
                "message": "1 of 7 credentials requests are failing to sync.",
                "lastTransitionTime": "2026-11-12T08:01:00Z",
            },
        ]
    },
}
CO_HEALTHY = {
    "metadata": {"name": "cloud-credential"},
    "status": {"conditions": [{"type": "Degraded", "status": "False"}]},
}

CV_FAILING = {
    "status": {
        "conditions": [
            {
                "type": "Failing",
                "status": "True",
                "reason": "ClusterOperatorDegraded",
                "message": "Cluster operator cloud-credential is degraded",
            }
        ],
        "history": [{"version": "5.0.1", "state": "Completed"}],
    }
}
CV_OK = {"status": {"conditions": [{"type": "Failing", "status": "False"}], "history": []}}


def _cco(mode: str) -> dict:
    return {"metadata": {"name": "cluster"}, "spec": {"credentialsMode": mode}}


def _root(annotation: str | None) -> dict:
    return {"annotations": {MODE_ANNOTATION: annotation} if annotation else {}}


AUTH_NO_STS = {"spec": {"serviceAccountIssuer": ""}}
AUTH_STS = {"spec": {"serviceAccountIssuer": "https://example-oidc.s3.us-east-1.amazonaws.com"}}

FORBIDDEN = ToolError(
    message='credentialsrequests.cloudcredential.openshift.io is forbidden: User "system:serviceaccount:'
    'openshiftpulse:pulse-agent" cannot list resource "credentialsrequests" in API group '
    f'"cloudcredential.openshift.io" in the namespace "{CCO_NS}"',
    category="forbidden",
    status_code=403,
)
NOT_FOUND = ToolError(message="not found", category="not_found", status_code=404)


def _crs(*items: dict) -> dict:
    return {"items": [copy.deepcopy(i) for i in items]}


def _mint() -> dict:
    return compute_cloud_credentials(
        CO_DEGRADED,
        _crs(EBS_CR, MACHINE_API_CR, GCP_IGNORED_CR),
        _cco("Mint"),
        _root("mint"),
        AUTH_NO_STS,
        CV_FAILING,
    )


# -- compute: AWS EBS CSI, Mint mode, InsufficientCloudCreds --


class TestMintInsufficientCloudCreds:
    def test_one_problem_for_the_failing_request_only(self):
        data = _mint()
        assert data["status"] == "failing"
        assert [p["name"] for p in data["problems"]] == ["aws-ebs-csi-driver-operator"]

    def test_names_the_component_and_what_breaks(self):
        p = _mint()["problems"][0]
        assert p["component"] == "AWS EBS CSI driver"
        assert "volumes" in p["impact"] and "attached" in p["impact"]
        assert p["secret"] == {"namespace": "openshift-cluster-csi-drivers", "name": "ebs-cloud-credentials"}

    def test_condition_is_verbatim(self):
        cond = _mint()["problems"][0]["condition"]
        assert cond["type"] == "InsufficientCloudCreds"
        assert cond["reason"] == "CloudCredsInsufficient"
        assert cond["message"] == "cloud credentials insufficient to satisfy credentials request"

    def test_lists_every_aws_action_with_resource_and_condition(self):
        p = _mint()["problems"][0]
        joined = "\n".join(p["permissions"])
        for action in [*EBS_ACTIONS, "kms:CreateGrant", "kms:Decrypt"]:
            assert action in joined
        assert "on *" in p["permissions"][0]
        assert "kms:GrantIsForAWSResource" in p["permissions"][2]

    def test_mode_is_read_not_assumed(self):
        mode = _mint()["mode"]
        assert mode["setting"] == "Mint" and mode["effective"] == "mint"
        assert mode["rootSecret"] == {
            "namespace": "kube-system",
            "name": "aws-creds",
            "state": "present",
            "annotation": "mint",
        }

    def test_checklist_matches_mint_and_warns_against_deleting(self):
        steps = _mint()["problems"][0]["steps"]
        text = "\n".join(steps)
        assert "Mint mode" in text and "kube-system/aws-creds" in text
        assert "AWS console (IAM)" in text
        assert "Cause: the condition is InsufficientCloudCreds" in text
        assert (
            "Deleting Secret openshift-cluster-csi-drivers/ebs-cloud-credentials or the CredentialsRequest "
            "aws-ebs-csi-driver-operator does not fix this" in text
        )
        assert "fails the same way" in text
        # Recreating cannot help here, so no delete command is offered.
        assert "oc delete" not in text

    def test_cluster_version_failing_is_attributed(self):
        data = _mint()
        assert data["clusterVersion"]["becauseOfThis"] is True
        text = render_cloud_credentials(data)
        assert "ClusterVersion reports Failing because of this" in text
        assert "5.0.1" in text and "Completed" in text

    def test_render_says_credentials_are_never_read(self):
        text = render_cloud_credentials(_mint())
        assert "Pulse never reads credential values" in text
        assert "AWS EBS CSI driver cannot get its cloud credentials (InsufficientCloudCreds)" in text
        assert "ec2:AttachVolume" in text


class TestModes:
    def test_default_mode_uses_the_root_secret_annotation(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), _cco(""), _root("mint"), AUTH_NO_STS, CV_OK)
        assert data["mode"]["effective"] == "mint"
        assert data["mode"]["label"] == "Default (operator chose Mint)"

    def test_insufficient_annotation_offers_mint_or_passthrough(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), _cco(""), _root("insufficient"), AUTH_NO_STS, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert data["mode"]["effective"] == "insufficient"
        assert "neither" in text and "Passthrough" in text and "Mint" in text

    def test_passthrough_adds_permissions_to_the_root_credential(self):
        data = compute_cloud_credentials(
            CO_DEGRADED, _crs(EBS_CR), _cco("Passthrough"), _root("passthrough"), AUTH_NO_STS, CV_OK
        )
        text = "\n".join(data["problems"][0]["steps"])
        assert "Passthrough mode" in text
        assert "add the permissions this CredentialsRequest lists to the IAM user (or its policy)" in text
        assert "kube-system/aws-creds" in text

    def test_manual_mode(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), _cco("Manual"), NOT_FOUND, AUTH_NO_STS, CV_OK)
        assert data["mode"]["effective"] == "manual"
        assert data["mode"]["rootSecret"]["state"] == "absent"
        text = "\n".join(data["problems"][0]["steps"])
        assert "Manual mode" in text
        assert "update Secret openshift-cluster-csi-drivers/ebs-cloud-credentials" in text
        assert "in Manual mode the operator does not recreate them" in text

    def test_manual_sts_points_to_ccoctl(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), _cco("Manual"), NOT_FOUND, AUTH_STS, CV_OK)
        assert data["mode"]["effective"] == "manual_sts"
        text = "\n".join(data["problems"][0]["steps"])
        assert "ccoctl aws create-iam-roles" in text
        assert "--credentials-requests --cloud=aws" in text

    def test_manual_with_unreadable_issuer_says_so(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), _cco("Manual"), NOT_FOUND, FORBIDDEN, CV_OK)
        assert data["mode"]["effective"] == "manual"
        assert "could not be read" in data["mode"]["label"]

    def test_unreadable_mode_is_unknown_and_gives_each_case(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(EBS_CR), FORBIDDEN, FORBIDDEN, FORBIDDEN, CV_OK)
        assert data["mode"]["effective"] == "unknown"
        text = "\n".join(data["problems"][0]["steps"])
        assert "could not read the credentials mode" in text
        assert "Mint or Passthrough" in text and "Manual or STS" in text
        assert any("cloudcredentials.operator.openshift.io" in u["rule"] for u in data["unreadable"])
        assert "could not be read" in render_cloud_credentials(data)


def _failing(cond_type: str, message: str, *, minted_user: str | None = None) -> dict:
    cr = copy.deepcopy(EBS_CR)
    cr["status"]["conditions"] = [
        {"type": cond_type, "status": "True", "reason": "CredentialsProvisionFailure", "message": message}
    ]
    if minted_user:
        cr["status"]["providerStatus"] = {"kind": "AWSProviderStatus", "user": minted_user, "policy": "p"}
    return cr


class TestStaleVersusMissingPermissions:
    """Deleting the target Secret helps when the minted key is stale, never when the root lacks permissions."""

    def test_invalid_minted_key_in_mint_mode_offers_the_delete_command(self):
        cr = _failing(
            "CredentialsProvisionFailure",
            "failed to sync: InvalidClientTokenId: The security token included in the request is invalid.",
            minted_user="mycluster-abc12-aws-ebs-csi-driver-operator-xyz",
        )
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco(""), _root("mint"), AUTH_NO_STS, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert 'Cause: the message reports "InvalidClientTokenId"' in text
        assert "`oc delete secret ebs-cloud-credentials -n openshift-cluster-csi-drivers`" in text
        assert "Pulse does not delete it" in text
        assert "mycluster-abc12-aws-ebs-csi-driver-operator-xyz" in text
        assert "rollout restart" in text
        assert "does not fix this" not in text

    def test_stale_credentials_condition_is_the_stale_case(self):
        cr = _failing("StaleCredentials", "credentials are stale")
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco("Mint"), _root("mint"), AUTH_NO_STS, CV_OK)
        assert "oc delete secret ebs-cloud-credentials" in "\n".join(data["problems"][0]["steps"])

    def test_stale_key_in_passthrough_points_at_the_root_credential(self):
        cr = _failing("CredentialsProvisionFailure", "InvalidClientTokenId: token invalid")
        data = compute_cloud_credentials(
            CO_DEGRADED, _crs(cr), _cco("Passthrough"), _root("passthrough"), AUTH_NO_STS, CV_OK
        )
        text = "\n".join(data["problems"][0]["steps"])
        assert "valid key" in text and "kube-system/aws-creds" in text
        # A healthy root credential: deleting the copy makes the operator copy it again.
        assert "`oc delete secret ebs-cloud-credentials -n openshift-cluster-csi-drivers`" in text

    def test_access_denied_is_a_permission_problem(self):
        cr = _failing(
            "CredentialsProvisionFailure",
            "AccessDenied: User arn:aws:iam::1:user/root is not authorized to perform: iam:CreateUser",
        )
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco("Mint"), _root("mint"), AUTH_NO_STS, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert "a permission is missing" in text
        assert "does not fix this" in text and "oc delete" not in text

    def test_unclear_message_lists_both_cases(self):
        cr = _failing("CredentialsProvisionFailure", "error syncing credentials: timeout")
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco("Mint"), _root("mint"), AUTH_NO_STS, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert "cannot tell" in text
        assert "If permissions are missing" in text and "If the minted key is stale" in text
        assert "oc delete secret ebs-cloud-credentials -n openshift-cluster-csi-drivers" in text
        assert "helps only in the stale case" in text

    def test_stale_with_unknown_mode_gives_each_mode(self):
        cr = _failing("CredentialsProvisionFailure", "InvalidClientTokenId")
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), FORBIDDEN, FORBIDDEN, FORBIDDEN, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert "If the cluster uses Mint mode" in text and "If it uses Passthrough" in text


# The live cluster: the 5.0 update added four EC2 actions to the EBS request
# (generation 2, last synced 1); the root key had been replaced in a browser on
# 2026-10-30, and the operator annotated it `insufficient` on 2026-11-09.
NEW_IN_5_0 = ["ec2:DescribeInstanceTypes", "ec2:DescribeVolumeStatus", "ec2:CopyVolumes", "ec2:LockSnapshot"]
MINTED_USER = "mycluster-x7k2p-aws-ebs-csi-driver-operator-4bq9z"


def _live_ebs_cr() -> dict:
    cr = copy.deepcopy(EBS_CR)
    cr["metadata"]["generation"] = 2
    cr["spec"]["providerSpec"]["statementEntries"][0]["action"] = EBS_ACTIONS + NEW_IN_5_0
    cr["status"]["lastSyncGeneration"] = 1
    cr["status"]["providerStatus"] = {
        "apiVersion": "cloudcredential.openshift.io/v1",
        "kind": "AWSProviderStatus",
        "user": MINTED_USER,
        "policy": f"{MINTED_USER}-policy",
    }
    return cr


def _last_applied_policy() -> str:
    import json

    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": EBS_ACTIONS, "Resource": "*"},
                {
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["kms:RevokeGrant", "kms:CreateGrant", "kms:ListGrants"],
                    "Resource": "*",
                },
                {"Effect": "Allow", "Action": ["iam:GetUser"], "Resource": f"arn:aws:iam::1:user/{MINTED_USER}"},
            ],
        }
    )


TARGET = ("openshift-cluster-csi-drivers", "ebs-cloud-credentials")


def _live(target_meta=None, root=None) -> dict:
    from sre_agent.cloud_credentials import AWS_POLICY_ANNOTATION

    if target_meta is None:
        target_meta = {"annotations": {AWS_POLICY_ANNOTATION: _last_applied_policy()}, "lastEdit": None}
    if root is None:
        root = {
            "annotations": {MODE_ANNOTATION: "insufficient"},
            "lastEdit": {"time": "2026-11-09T04:12:00Z", "manager": "cloud-credential-operator"},
        }
    return compute_cloud_credentials(
        CO_DEGRADED,
        _crs(_live_ebs_cr(), MACHINE_API_CR),
        _cco(""),
        root,
        AUTH_NO_STS,
        CV_FAILING,
        {TARGET: target_meta},
    )


class TestUpdateBlockedByInsufficientRoot:
    """The live case: the update changed the request, and the root credential can no longer mint."""

    def test_it_is_recognised(self):
        from sre_agent.cloud_credentials import credential_cause

        data = _live()
        p = data["problems"][0]
        assert data["mode"]["effective"] == "insufficient"
        assert (p["generation"], p["lastSyncGeneration"], p["targetSecret"]) == (2, 1, "present")
        assert credential_cause(p, data["mode"])[0] == "update_blocked"

    def test_names_exactly_the_missing_actions(self):
        assert _live()["problems"][0]["missingActions"] == NEW_IN_5_0

    def test_checklist(self):
        text = "\n".join(_live()["problems"][0]["steps"])
        assert "generation 2, last synced 1" in text
        assert "ec2:DescribeInstanceTypes, ec2:DescribeVolumeStatus, ec2:CopyVolumes, ec2:LockSnapshot" in text
        assert MINTED_USER in text
        assert "still works for everything else" in text
        assert "Do NOT delete Secret openshift-cluster-csi-drivers/ebs-cloud-credentials" in text
        assert "removes the working credential" in text and "cannot mint a replacement" in text
        assert "oc delete" not in text
        # The fix: mint permissions back on the root; stopgap: the four actions on the minted user.
        assert "mint-mode permissions" in text and "iam:CreateUser" in text and "iam:PutUserPolicy" in text
        assert "updates the policy of IAM user " + MINTED_USER in text
        assert "Stopgap" in text
        assert "2026-11-09T04:12:00Z" in text

    def test_root_annotation_outranks_an_explicit_mint_setting(self):
        data = compute_cloud_credentials(
            CO_DEGRADED,
            _crs(_live_ebs_cr()),
            _cco("Mint"),
            _root("insufficient"),
            AUTH_NO_STS,
            CV_OK,
            {TARGET: {"annotations": {}, "lastEdit": None}},
        )
        assert data["mode"]["effective"] == "insufficient"
        assert "oc delete" not in "\n".join(data["problems"][0]["steps"])

    def test_unreadable_policy_says_it_cannot_name_the_actions(self):
        data = _live(target_meta={"annotations": {}, "lastEdit": None})
        text = "\n".join(data["problems"][0]["steps"])
        assert data["problems"][0]["missingActions"] is None
        assert "cannot name the missing actions" in text
        assert "Do NOT delete Secret" in text

    def test_target_secret_gone_is_not_update_blocked(self):
        data = _live(target_meta=NOT_FOUND)
        p = data["problems"][0]
        assert p["targetSecret"] == "absent"
        text = "\n".join(p["steps"])
        assert "recreates them with the same root credential" in text

    def test_synced_request_is_not_update_blocked(self):
        cr = _live_ebs_cr()
        cr["status"]["lastSyncGeneration"] = 2
        data = compute_cloud_credentials(
            CO_DEGRADED,
            _crs(cr),
            _cco(""),
            _root("insufficient"),
            AUTH_NO_STS,
            CV_OK,
            {TARGET: {"annotations": {}, "lastEdit": None}},
        )
        text = "\n".join(data["problems"][0]["steps"])
        assert "Cause: the condition is InsufficientCloudCreds" in text
        assert "removes the credential the component has now" in text


class TestOtherStates:
    def test_unreadable_requests_are_unknown_and_name_the_rule(self):
        data = compute_cloud_credentials(CO_DEGRADED, FORBIDDEN)
        assert data["status"] == "unknown"
        assert data["problems"] == []
        assert "credentialsrequests.cloudcredential.openshift.io" in data["reason"]
        assert "Degraded" in data["reason"]
        assert "UNKNOWN" in render_cloud_credentials(data)

    def test_healthy(self):
        data = compute_cloud_credentials(
            CO_HEALTHY, _crs(MACHINE_API_CR, GCP_IGNORED_CR), _cco(""), _root("mint"), AUTH_NO_STS, CV_OK
        )
        assert data["status"] == "healthy" and data["problems"] == []
        assert "HEALTHY" in render_cloud_credentials(data)

    def test_no_cloud_credential_operator(self):
        assert compute_cloud_credentials(NOT_FOUND, NOT_FOUND)["status"] == "not_applicable"

    def test_degraded_without_a_failing_request_points_at_the_operator(self):
        data = compute_cloud_credentials(CO_DEGRADED, _crs(MACHINE_API_CR), _cco(""), _root("mint"), AUTH_NO_STS, CV_OK)
        assert data["status"] == "degraded_unexplained"
        assert CCO_NS in data["reason"]

    def test_missing_target_namespace_is_not_a_permission_problem(self):
        cr = copy.deepcopy(EBS_CR)
        cr["status"]["conditions"] = [
            {
                "type": "MissingTargetNamespace",
                "status": "True",
                "reason": "NamespaceMissing",
                "message": "target namespace openshift-cluster-csi-drivers not found",
            }
        ]
        data = compute_cloud_credentials(CO_HEALTHY, _crs(cr), _cco(""), _root("mint"), AUTH_NO_STS, CV_OK)
        text = "\n".join(data["problems"][0]["steps"])
        assert "does not exist" in text and "nothing needs fixing" in text
        assert "IAM" not in text

    def test_control_characters_are_stripped_from_the_message(self):
        cr = copy.deepcopy(EBS_CR)
        cr["status"]["conditions"][0]["message"] = "insufficient\x1b[31m creds\x00"
        data = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco("Mint"), _root("mint"), AUTH_NO_STS, CV_OK)
        assert data["problems"][0]["condition"]["message"] == "insufficient[31m creds"

    def test_unknown_component_falls_back_to_its_name_and_secret(self):
        cr = copy.deepcopy(EBS_CR)
        cr["metadata"]["name"] = "acme-backup-exporter"
        cr["spec"]["secretRef"] = {"name": "acme-creds", "namespace": "acme"}
        p = compute_cloud_credentials(CO_DEGRADED, _crs(cr), _cco("Mint"), _root("mint"), AUTH_NO_STS, CV_OK)[
            "problems"
        ][0]
        assert p["component"] == "CredentialsRequest acme-backup-exporter"
        assert "acme/acme-creds" in p["purpose"]

    def test_gcp_and_azure_permissions(self):
        from sre_agent.cloud_credentials import cloud_permissions

        cloud, lines = cloud_permissions(
            {"kind": "GCPProviderSpec", "predefinedRoles": ["roles/compute.storageAdmin"], "permissions": ["a.b.c"]}
        )
        assert cloud == "gcp" and lines == ["role roles/compute.storageAdmin", "permission a.b.c"]
        cloud, lines = cloud_permissions(
            {"kind": "AzureProviderSpec", "roleBindings": [{"role": "Contributor"}], "permissions": ["Microsoft.X/y"]}
        )
        assert cloud == "azure" and lines == ["role Contributor", "permission Microsoft.X/y"]


# -- reads: Secret metadata only; scanner failure on a missing grant --


def _api_exc(status: int, message: str = "nope") -> ApiException:
    e = ApiException(status=status, reason=message)
    e.body = f'{{"message": "{message}"}}'
    return e


def _custom(crs_result=None, crs_error=None, cco=None):
    custom = MagicMock()

    def get_cluster(group, version, plural, name):
        if plural == "clusteroperators":
            return copy.deepcopy(CO_DEGRADED)
        if plural == "cloudcredentials":
            if cco is None:
                raise _api_exc(403, "forbidden")
            return cco
        if plural == "authentications":
            return AUTH_NO_STS
        if plural == "clusterversions":
            return CV_FAILING
        raise AssertionError(plural)

    def list_ns(group, version, namespace, plural):
        assert (group, namespace, plural) == ("cloudcredential.openshift.io", CCO_NS, "credentialsrequests")
        if crs_error is not None:
            raise crs_error
        return crs_result

    custom.get_cluster_custom_object.side_effect = get_cluster
    custom.list_namespaced_custom_object.side_effect = list_ns
    return custom


@pytest.fixture(autouse=True)
def _reset_failures():
    scanner_health.reset()
    yield
    scanner_health.reset()


class TestReads:
    def test_root_secret_is_read_metadata_only_and_only_the_mode_annotation_kept(self):
        raw_calls = []

        def raw(path, operation="", *, query=None, accept=""):
            raw_calls.append((path, accept))
            return {
                "kind": "PartialObjectMetadata",
                "metadata": {
                    "name": "aws-creds",
                    "annotations": {
                        MODE_ANNOTATION: "mint",
                        "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"aws_secret_access_key":"x"}}',
                    },
                },
            }

        core = MagicMock()
        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=_custom(_crs(EBS_CR), cco=_cco("Mint"))),
            patch("sre_agent.k8s_client.get_core_client", return_value=core),
            patch("sre_agent.k8s_client.get_raw_json", side_effect=raw),
        ):
            data = read_cloud_credentials()
        metadata_only = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
        assert raw_calls == [
            ("/api/v1/namespaces/kube-system/secrets/aws-creds", metadata_only),
            ("/api/v1/namespaces/openshift-cluster-csi-drivers/secrets/ebs-cloud-credentials", metadata_only),
        ]
        # The typed Secret read (which returns data) is never used.
        core.read_namespaced_secret.assert_not_called()
        assert data["mode"]["rootSecret"]["annotation"] == "mint"
        assert "aws_secret_access_key" not in repr(data)
        assert "last-applied" not in render_cloud_credentials(data)

    def test_forbidden_requests_fail_the_scanner_with_the_missing_rule(self):
        from sre_agent.monitor.scanners import scan_cloud_credentials

        msg = (
            f'credentialsrequests.cloudcredential.openshift.io is forbidden: User \\"system:serviceaccount:x:y\\" '
            f'cannot list resource \\"credentialsrequests\\" in API group \\"cloudcredential.openshift.io\\" in the '
            f'namespace \\"{CCO_NS}\\"'
        )
        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=_custom(crs_error=_api_exc(403, msg))),
            scanning("cloud_credentials"),
        ):
            findings = scan_cloud_credentials()
        assert findings == []
        failure = get_failure("cloud_credentials")
        assert failure and failure.startswith("forbidden")
        explained = explain_failure("cloud_credentials", failure)
        assert "cloudcredential.openshift.io/credentialsrequests" in explained["reason"]
        assert "does not grant" in explained["fix"]
        assert "explain cloud credentials" in explained["fix"]

    def test_unreadable_mode_does_not_fail_the_scanner(self):
        from sre_agent.monitor.scanners import scan_cloud_credentials

        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=_custom(_crs(EBS_CR), cco=None)),
            patch("sre_agent.k8s_client.get_raw_json", return_value=FORBIDDEN),
            scanning("cloud_credentials"),
        ):
            findings = scan_cloud_credentials()
        assert get_failure("cloud_credentials") is None
        assert len(findings) == 1
        assert findings[0]["credentialsMode"] == "unknown"

    def test_scanner_finding_shape(self):
        from sre_agent.monitor.scanners import scan_cloud_credentials

        with (
            patch(
                "sre_agent.k8s_client.get_custom_client",
                return_value=_custom(_crs(EBS_CR, MACHINE_API_CR), cco=_cco("Mint")),
            ),
            patch(
                "sre_agent.k8s_client.get_raw_json",
                return_value={"metadata": {"annotations": {MODE_ANNOTATION: "mint"}}},
            ),
            scanning("cloud_credentials"),
        ):
            findings = scan_cloud_credentials()
        assert len(findings) == 1
        f = findings[0]
        assert f["category"] == "cloud_credentials"
        assert f["severity"] == "critical"  # cloud-credential is Degraded
        assert f["title"] == "AWS EBS CSI driver cannot get its cloud credentials (InsufficientCloudCreds)"
        assert f["resources"][0] == {
            "kind": "CredentialsRequest",
            "name": "aws-ebs-csi-driver-operator",
            "namespace": CCO_NS,
        }
        assert f["checklist"] and "does not fix this" in "\n".join(f["checklist"])
        assert "cloud credentials insufficient to satisfy credentials request" in f["summary"]
        assert "ClusterVersion reports Failing because of this" in f["summary"]
        assert f["startedAt"] > 0

    def test_healthy_cluster_has_no_findings_and_no_failure(self):
        from sre_agent.monitor.scanners import scan_cloud_credentials

        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=_custom(_crs(MACHINE_API_CR), cco=_cco(""))),
            patch("sre_agent.k8s_client.get_raw_json", return_value={"metadata": {"annotations": {}}}),
            scanning("cloud_credentials"),
        ):
            assert scan_cloud_credentials() == []
        assert get_failure("cloud_credentials") is None

    def test_scanner_is_registered(self):
        from sre_agent.monitor.scanners import get_all_scanner_instances

        names = [s.meta.name for s in get_all_scanner_instances()]
        assert names.count("cloud_credentials") == 1


# -- degraded-operator pointers, chat tool --


class TestPointersAndTool:
    def test_degraded_operator_finding_points_to_the_explanation(self):
        from sre_agent.monitor.scanners import _operator_summary

        assert "explain cloud credentials" in _operator_summary("cloud-credential", {"message": "x"})
        assert _operator_summary("dns", {"message": "x"}) == "Operator degraded: x"

    def test_degraded_operator_item_points_to_the_explanation(self):
        from sre_agent.inbox_generators import gen_degraded_operator

        with patch(
            "sre_agent.inbox_generators._get_degraded_operators",
            return_value=[{"name": "cloud-credential", "degraded_duration_hours": 30.0, "message": ""}],
        ):
            items = gen_degraded_operator()
        assert "Cloud Credentials items" in items[0]["summary"]

    def test_runbook_names_the_tool(self):
        from sre_agent.runbooks import RUNBOOK_CHUNKS

        assert "explain_cloud_credentials" in RUNBOOK_CHUNKS["operator_degraded"]

    def test_tool_is_read_only_and_renders(self):
        from sre_agent.k8s_tools import WRITE_TOOLS, explain_cloud_credentials

        assert "explain_cloud_credentials" not in WRITE_TOOLS
        with patch("sre_agent.cloud_credentials.read_cloud_credentials", return_value=_mint()):
            text = explain_cloud_credentials.call({})
        assert "AWS EBS CSI driver" in text and "kube-system/aws-creds" in text


_MCP = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]
QUERIES = [
    "why is the cloud-credential operator degraded?",
    "clusterversion says Failing after the 5.0.1 update, cloud-credential degraded",
    "the ebs csi driver credentialsrequest has InsufficientCloudCreds",
    "explain cloud credentials",
]


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


def test_tool_is_in_the_sre_skills_categories():
    from sre_agent.skill_loader import get_skill, load_skills
    from sre_agent.tool_categories import MODE_CATEGORIES, TOOL_CATEGORIES

    load_skills()
    skill = get_skill("sre")
    assert skill is not None
    names: set[str] = set()
    for cat in skill.categories:
        names.update(TOOL_CATEGORIES.get(cat, {}).get("tools", []))
    mode_names: set[str] = set()
    for cat in MODE_CATEGORIES["sre"] or []:
        mode_names.update(TOOL_CATEGORIES[cat]["tools"])
    assert "explain_cloud_credentials" in names and "explain_cloud_credentials" in mode_names


@pytest.mark.parametrize("query", QUERIES)
def test_query_routes_to_sre(query):
    from sre_agent.skill_loader import load_skills
    from sre_agent.skill_router import classify_query

    load_skills()
    assert classify_query(query).name == "sre"


@pytest.mark.parametrize("query", QUERIES)
def test_sre_mode_keeps_the_tool_under_the_budget_cap(query):
    from sre_agent.agent import TOOL_MAP
    from sre_agent.skill_loader import MAX_TOOL_BUDGET, select_tools

    all_tools = list(TOOL_MAP.values()) + [_FakeTool(t["name"]) for t in _MCP]
    tool_map = {t.name: t for t in all_tools}
    with patch("sre_agent.mcp_client.list_mcp_tools", return_value=_MCP):
        _, _, offered = select_tools(query, all_tools, tool_map, "sre")
    assert len(offered) == MAX_TOOL_BUDGET
    assert "explain_cloud_credentials" in offered


@pytest.mark.parametrize("query", QUERIES)
def test_sre_skill_config_offers_the_tool(query):
    from sre_agent.skill_loader import build_config_from_skill, get_skill, load_skills
    from sre_agent.tool_predictor import PredictionResult

    load_skills()
    skill = get_skill("sre")
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult(tools=[], confidence="low")),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=[]),
    ):
        config = build_config_from_skill(skill, query=query)
    assert "explain_cloud_credentials" in config["tool_map"]
