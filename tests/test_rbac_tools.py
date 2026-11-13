"""who_can, effective_permissions, get_rbac_changes (JTBD J4, J5, J13, J14).

All three are read-only and run only with the caller's forwarded token: the
agent's service account has no grant for ResourceAccessReview or impersonate,
and Pulse does not add one.
"""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent import k8s_client, rbac_history, rbac_tools
from sre_agent.api import rbac_history_rest
from sre_agent.db import get_database
from sre_agent.errors import ToolError
from sre_agent.k8s_client import user_token_context


def _call(tool, **args):
    out = tool.call(args)
    return out[0] if isinstance(out, tuple) else out


@pytest.fixture
def as_user():
    with user_token_context("alice-token"):
        yield


def _rb(name, ns, role_kind, role, subjects):
    return {
        "metadata": {"name": name, "namespace": ns},
        "roleRef": {"kind": role_kind, "name": role},
        "subjects": subjects,
    }


CLUSTER_ROLES = [
    {"metadata": {"name": "cluster-admin"}, "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]},
    {
        "metadata": {"name": "admin"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["delete", "get"]}],
    },
    {"metadata": {"name": "view"}, "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]},
]
ROLES = [
    {
        "metadata": {"name": "secret-deleter", "namespace": "payments"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["delete"]}],
    }
]
CRBS = [
    {
        "metadata": {"name": "cluster-admins"},
        "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [{"kind": "Group", "name": "system:masters"}],
    }
]
RBS = [
    _rb("secret-deleters", "payments", "Role", "secret-deleter", [{"kind": "User", "name": "alice"}]),
    _rb("deployer-admin", "payments", "ClusterRole", "admin", [{"kind": "ServiceAccount", "name": "deployer"}]),
    _rb("team-admin", "payments", "ClusterRole", "admin", [{"kind": "Group", "name": "payments-admins"}]),
    _rb("viewers", "payments", "ClusterRole", "view", [{"kind": "User", "name": "carol"}]),
]
GROUPS = [{"metadata": {"name": "payments-admins"}, "users": ["bob", "dave"]}]
REVIEW = {
    "users": ["alice", "bob", "system:serviceaccount:payments:deployer", "system:admin"],
    "groups": ["payments-admins", "system:masters"],
}


class FakeApi:
    def __init__(self):
        self.raw: dict[str, object] = {
            "/apis/authorization.openshift.io/v1": {
                "resources": [{"name": "resourceaccessreviews"}, {"name": "localresourceaccessreviews"}]
            },
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings": {"items": CRBS},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/payments/rolebindings": {"items": RBS},
            "/apis/rbac.authorization.k8s.io/v1/clusterroles": {"items": CLUSTER_ROLES},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/payments/roles": {"items": ROLES},
            "/apis/user.openshift.io/v1/groups": {"items": GROUPS},
        }
        self.custom = MagicMock()
        self.custom.create_namespaced_custom_object.return_value = copy.deepcopy(REVIEW)
        self.custom.create_cluster_custom_object.return_value = copy.deepcopy(REVIEW)
        self.paths: list[str] = []

    def get_raw_json(self, path, operation="", **_):
        self.paths.append(path)
        got = self.raw.get(path, ToolError(message="not found", category="not_found", status_code=404))
        return copy.deepcopy(got)


@pytest.fixture
def api():
    fake = FakeApi()
    with (
        patch.object(k8s_client, "get_raw_json", side_effect=fake.get_raw_json),
        patch.object(k8s_client, "get_custom_client", return_value=fake.custom),
        patch("sre_agent.monitor.scanner_health.report_api_error"),
    ):
        yield fake


# ---------------------------------------------------------------------------
# who_can
# ---------------------------------------------------------------------------


def test_who_can_refuses_without_the_callers_token_and_names_the_missing_rule(api):
    text = _call(rbac_tools.who_can, verb="delete", resource="secrets", namespace="payments")
    assert text.startswith("Not checked:")
    assert "resourceaccessreviews, localresourceaccessreviews" in text and "verbs: [create]" in text
    assert "service account has no such grant" in text
    api.custom.create_namespaced_custom_object.assert_not_called()


def test_who_can_explains_each_subject_with_its_binding_and_role(api, as_user):
    text = _call(rbac_tools.who_can, verb="delete", resource="secrets", namespace="payments")
    args = api.custom.create_namespaced_custom_object.call_args.args
    assert args[:4] == ("authorization.openshift.io", "v1", "payments", "localresourceaccessreviews")
    assert args[4]["kind"] == "LocalResourceAccessReview"
    assert {k: args[4][k] for k in ("verb", "resource", "resourceAPIGroup")} == {
        "verb": "delete",
        "resource": "secrets",
        "resourceAPIGroup": "",
    }
    assert "User alice: RoleBinding payments/secret-deleters > Role secret-deleter" in text
    assert "ServiceAccount payments/deployer: RoleBinding payments/deployer-admin > ClusterRole admin" in text
    assert "Group payments-admins (members: bob, dave): RoleBinding payments/team-admin > ClusterRole admin" in text
    # bob can only through the listed group: shown on the group's row, not twice.
    assert "User bob" not in text
    # view does not grant delete; carol is not in the review either.
    assert "carol" not in text and "viewers" not in text
    # Platform subjects are counted, not listed as people.
    assert "2 platform subject(s)" in text and "Platform subjects (2): Group system:masters, User system:admin" in text
    assert "3 named people" in text  # alice, bob, dave


def test_who_can_cluster_wide_uses_resourceaccessreviews(api, as_user):
    _call(rbac_tools.who_can, verb="delete", resource="secrets")
    args = api.custom.create_cluster_custom_object.call_args.args
    assert args[:3] == ("authorization.openshift.io", "v1", "resourceaccessreviews")
    assert args[3]["kind"] == "ResourceAccessReview"


def test_who_can_splits_resource_dot_group(api, as_user):
    _call(rbac_tools.who_can, verb="patch", resource="deployments.apps", namespace="payments")
    body = api.custom.create_namespaced_custom_object.call_args.args[4]
    assert (body["resource"], body["resourceAPIGroup"]) == ("deployments", "apps")


def test_who_can_says_when_the_api_is_not_served_and_does_not_approximate(api, as_user):
    del api.raw["/apis/authorization.openshift.io/v1"]
    text = _call(rbac_tools.who_can, verb="delete", resource="secrets", namespace="payments")
    assert "does not serve ResourceAccessReview" in text and "does not approximate" in text
    api.custom.create_namespaced_custom_object.assert_not_called()


def test_who_can_names_the_rule_when_the_caller_is_refused(api, as_user):
    api.custom.create_namespaced_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
    text = _call(rbac_tools.who_can, verb="delete", resource="secrets", namespace="payments")
    assert "you may not create authorization.openshift.io localresourceaccessreviews in payments" in text
    assert "verbs: [create]" in text


def test_who_can_reports_unreadable_bindings(api, as_user):
    api.raw["/apis/rbac.authorization.k8s.io/v1/clusterrolebindings"] = ToolError(
        message="forbidden", category="forbidden", status_code=403
    )
    text = _call(rbac_tools.who_can, verb="delete", resource="secrets", namespace="payments")
    assert "Could not read ClusterRoleBindings as you" in text
    assert "User alice: RoleBinding payments/secret-deleters" in text


def test_who_can_rejects_bad_input(api, as_user):
    assert "verb must be" in _call(rbac_tools.who_can, verb="DROP TABLE", resource="secrets")
    assert "resource must be" in _call(rbac_tools.who_can, verb="get", resource="Secrets!")
    assert api.paths == []


def test_rule_allows_matches_the_ui():
    rule = {"verbs": ["get", "delete"], "apiGroups": [""], "resources": ["secrets"]}
    assert rbac_tools.rule_allows(rule, "delete", "", "secrets")
    assert not rbac_tools.rule_allows(rule, "delete", "apps", "secrets")
    # Asking about '*' means every verb, which only a '*' rule grants.
    assert not rbac_tools.rule_allows(rule, "*", "", "secrets")
    assert rbac_tools.rule_allows({"verbs": ["*"], "apiGroups": ["*"], "resources": ["*"]}, "*", "x", "*")


def test_service_account_appears_as_user_in_a_binding():
    ref = {"kind": "ServiceAccount", "namespace": "payments", "name": "deployer"}
    assert rbac_tools.subject_matches({"kind": "User", "name": "system:serviceaccount:payments:deployer"}, ref)
    assert rbac_tools.subject_matches({"kind": "ServiceAccount", "name": "deployer"}, ref, "payments")
    assert not rbac_tools.subject_matches({"kind": "ServiceAccount", "name": "deployer"}, ref, "other")


# ---------------------------------------------------------------------------
# effective_permissions
# ---------------------------------------------------------------------------


class FakeReviews:
    """Answers SelfSubjectRulesReview per impersonated (user, group)."""

    def __init__(self, by_identity: dict[tuple[str, str | None], dict]):
        self.by_identity = by_identity
        self.refuse: set[tuple[str, str | None]] = set()
        self.calls: list[dict] = []

    def call_api(self, path, method, header_params=None, body=None, **_):
        self.calls.append({"path": path, "method": method, "headers": dict(header_params or {}), "body": body})
        who = (header_params["Impersonate-User"], header_params.get("Impersonate-Group"))
        if who in self.refuse:
            raise ApiException(status=403, reason="Forbidden")
        status = self.by_identity.get(who, {"resourceRules": [], "nonResourceRules": []})
        return (SimpleNamespace(data=json.dumps({"status": status}).encode()), 201, {})


@pytest.fixture
def reviews(api):
    api.raw["/apis/user.openshift.io/v1/users/bob"] = {"metadata": {"name": "bob"}, "identities": ["htpasswd:bob"]}
    fake = FakeReviews(
        {
            ("bob", None): {"resourceRules": [{"verbs": ["get"], "apiGroups": [""], "resources": ["pods"]}]},
            ("bob", "payments-admins"): {
                "resourceRules": [
                    {"verbs": ["get"], "apiGroups": [""], "resources": ["pods"]},
                    {"verbs": ["delete", "get"], "apiGroups": [""], "resources": ["secrets"]},
                ],
                "incomplete": True,
                "evaluationError": "webhook authorizer unavailable",
            },
        }
    )
    core = MagicMock()
    core.api_client.call_api.side_effect = fake.call_api
    with patch.object(k8s_client, "get_core_client", return_value=core):
        yield fake


def test_effective_permissions_impersonates_alone_then_per_group_and_unions(reviews, as_user):
    text = _call(rbac_tools.effective_permissions, subject_kind="User", subject_name="bob", namespace="payments")
    identities = [(c["headers"]["Impersonate-User"], c["headers"].get("Impersonate-Group")) for c in reviews.calls]
    assert identities == [("bob", None), ("bob", "payments-admins"), ("bob", "system:authenticated:oauth")]
    assert all(c["path"] == "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews" for c in reviews.calls)
    assert reviews.calls[0]["body"]["spec"] == {"namespace": "payments"}
    assert "2 resource rule(s)" in text  # the duplicated pods rule counted once
    assert "delete, get on secrets [core]" in text
    assert "Incomplete" in text and "webhook authorizer unavailable" in text


def test_effective_permissions_service_account_needs_one_review(reviews, as_user):
    _call(
        rbac_tools.effective_permissions,
        subject_kind="ServiceAccount",
        subject_name="deployer",
        namespace="payments",
    )
    assert [c["headers"]["Impersonate-User"] for c in reviews.calls] == ["system:serviceaccount:payments:deployer"]
    assert "Impersonate-Group" not in reviews.calls[0]["headers"]


def test_effective_permissions_refused_impersonation_is_said_plainly(reviews, as_user):
    reviews.refuse = {("bob", None)}
    text = _call(rbac_tools.effective_permissions, subject_kind="User", subject_name="bob", namespace="payments")
    assert text.startswith("Not checked: impersonating bob was refused")
    assert "never impersonates with its own service account" in text


def test_effective_permissions_refused_group_is_left_out_and_said(reviews, as_user):
    reviews.refuse = {("bob", "payments-admins")}
    text = _call(rbac_tools.effective_permissions, subject_kind="User", subject_name="bob", namespace="payments")
    assert "payments-admins was refused" in text
    assert "secrets" not in text.split("--- BEGIN CLUSTER DATA ---")[1]


def test_effective_permissions_never_runs_as_the_service_account(reviews):
    text = _call(rbac_tools.effective_permissions, subject_kind="User", subject_name="bob", namespace="payments")
    assert text.startswith("Not checked:") and "impersonate" in text
    assert reviews.calls == []
    with pytest.raises(rbac_tools.RbacToolError):
        rbac_tools.rules_review_as("payments", "bob", None)


def test_effective_permissions_for_a_group_points_to_members(reviews, as_user):
    text = _call(rbac_tools.effective_permissions, subject_kind="Group", subject_name="x", namespace="payments")
    assert "computed per user" in text and reviews.calls == []


# ---------------------------------------------------------------------------
# get_rbac_changes (real history in the disposable test database)
# ---------------------------------------------------------------------------


def _role(name, ns, verbs, rv):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": ns, "uid": f"uid-{name}", "resourceVersion": rv},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": list(verbs)}],
    }


def _binding(name, subjects, rv):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": rv},
        "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": subjects,
    }


class _Lister:
    def __init__(self):
        self.items: dict[str, list[dict]] = {k: [] for k in rbac_history.WATCHED_KINDS}

    def __call__(self, kind):
        return copy.deepcopy(self.items[kind])


@pytest.fixture
def history():
    db = get_database()
    db.execute("TRUNCATE rbac_object_versions, pulse_write_ledger RESTART IDENTITY")
    db.commit()
    rbac_history.reset_for_tests()
    now = int(time.time() * 1000)
    c = _Lister()
    c.items["Role"] = [_role("secret-manager", "payments", ("get",), "1"), _role("other", "shop", ("get",), "1")]
    c.items["ClusterRoleBinding"] = [_binding("break-glass", [{"kind": "User", "name": "ops"}], "1")]
    rbac_history.observe(now - 600_000, lister=c)
    c.items["Role"] = [_role("secret-manager", "payments", ("get", "delete"), "2"), _role("other", "shop", ("*",), "2")]
    c.items["ClusterRoleBinding"] = [
        _binding("break-glass", [{"kind": "User", "name": "ops"}, {"kind": "User", "name": "mallory"}], "2")
    ]
    rbac_history.observe(now, lister=c)
    yield db
    db.execute("TRUNCATE rbac_object_versions, pulse_write_ledger RESTART IDENTITY")
    db.commit()
    rbac_history.reset_for_tests()


@pytest.mark.real_rbac_history
def test_rbac_changes_say_what_was_added(history, as_user):
    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        text = _call(rbac_tools.get_rbac_changes, since="24h")
    assert "3 shown" in text
    assert "updated ClusterRoleBinding break-glass" in text and "subjects added: User mallory" in text
    assert "updated Role payments/secret-manager" in text and "rules added: get, delete on secrets [core]" in text
    assert "rules removed: get on secrets [core]" in text


@pytest.mark.real_rbac_history
def test_rbac_changes_by_namespace_keep_cluster_scoped(history, as_user):
    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        text = _call(rbac_tools.get_rbac_changes, since="24h", namespace="payments")
    assert "payments/secret-manager" in text and "break-glass" in text
    assert "shop/other" not in text


@pytest.mark.real_rbac_history
def test_rbac_changes_hide_what_the_caller_may_not_read(history, as_user):
    def can_get(group, plural, ns):
        return plural == "roles"

    with patch.object(rbac_history_rest, "_can_get", side_effect=can_get):
        text = _call(rbac_tools.get_rbac_changes, since="7d")
    assert "break-glass" not in text and "mallory" not in text
    assert "1 hidden because you may not read those objects" in text


@pytest.mark.real_rbac_history
def test_rbac_changes_credit_pulse_writes_from_the_ledger(history, as_user):
    from sre_agent.repositories import get_rbac_history_repo

    get_rbac_history_repo().record_write(
        {
            "at": int(time.time() * 1000) - 700_000,
            "actor": "erin",
            "actor_verified": True,
            "source": "ui",
            "verb": "patch",
            "kind": "Role",
            "namespace": "payments",
            "name": "secret-manager",
            "resource_version": "2",
            "reason": "on-call needs delete",
        }
    )
    with patch.object(rbac_history_rest, "_can_get", return_value=True):
        text = _call(rbac_tools.get_rbac_changes, since="24h")
    assert "Role payments/secret-manager by erin via Pulse" in text


@pytest.mark.real_rbac_history
def test_rbac_changes_need_the_callers_token(history):
    with patch.object(rbac_history_rest, "_can_get", return_value=True) as can:
        text = _call(rbac_tools.get_rbac_changes, since="24h")
    assert text.startswith("Not read:") and "token" in text
    can.assert_not_called()


def test_parse_since():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    assert rbac_tools.parse_since("24h", now) == now - timedelta(hours=24)
    assert rbac_tools.parse_since("", now) == now - timedelta(days=7)
    assert rbac_tools.parse_since("90d", now) == now - timedelta(days=30)  # retention
    assert rbac_tools.parse_since("2026-11-13T10:00:00Z", now) == datetime(2026, 11, 13, 10, tzinfo=UTC)
    with pytest.raises(rbac_tools.RbacToolError):
        rbac_tools.parse_since("yesterday", now)


def test_rbac_changes_with_unavailable_storage(as_user):
    repo = MagicMock()
    repo.fetch_changes.side_effect = RuntimeError("db down")
    with patch("sre_agent.repositories.get_rbac_history_repo", return_value=repo):
        text = _call(rbac_tools.get_rbac_changes, since="24h")
    assert "storage is unavailable" in text


# ---------------------------------------------------------------------------
# Registration and reach
# ---------------------------------------------------------------------------

WHO_CAN_QUERY = "who can delete secrets in payments?"


def test_registered_read_only_in_security():
    from sre_agent.tool_categories import TOOL_CATEGORIES, get_tool_category
    from sre_agent.tool_registry import TOOL_REGISTRY, WRITE_TOOL_NAMES

    for name in ("who_can", "effective_permissions", "get_rbac_changes"):
        assert name in TOOL_REGISTRY and name not in WRITE_TOOL_NAMES
        assert name in TOOL_CATEGORIES["security"]["tools"]
        assert get_tool_category(name) == "security"


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


def test_sre_mode_keeps_who_can_under_the_budget_cap():
    from sre_agent.agent import TOOL_MAP
    from sre_agent.skill_loader import MAX_TOOL_BUDGET, select_tools

    mcp = [{"name": f"mcp_extra_tool_{i}"} for i in range(40)]
    all_tools = list(TOOL_MAP.values()) + [_FakeTool(t["name"]) for t in mcp]
    tool_map = {t.name: t for t in all_tools}
    with patch("sre_agent.mcp_client.list_mcp_tools", return_value=mcp):
        _, _, offered = select_tools(WHO_CAN_QUERY, all_tools, tool_map, "sre")
    assert len(offered) == MAX_TOOL_BUDGET
    assert "who_can" in offered


@pytest.mark.parametrize("skill_name", ["sre", "security"])
def test_skill_config_offers_who_can(skill_name):
    from sre_agent.skill_loader import build_config_from_skill, get_skill, load_skills
    from sre_agent.tool_predictor import PredictionResult

    load_skills()
    skill = get_skill(skill_name)
    assert skill is not None
    with (
        patch("sre_agent.tool_predictor.predict_tools", return_value=PredictionResult(tools=[], confidence="low")),
        patch("sre_agent.tool_predictor.llm_pick_tools", return_value=[]),
    ):
        config = build_config_from_skill(skill, query=WHO_CAN_QUERY)
    assert {"who_can", "effective_permissions", "get_rbac_changes"} <= set(config["tool_map"])
