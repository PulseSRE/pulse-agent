"""OLM InstallPlan approvals: the pending list, the confirm-gated approve, its preview, and the Inbox generator."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent import inbox_generators as gens
from sre_agent.k8s_client import user_token_context
from sre_agent.k8s_tools import olm
from sre_agent.request_context import set_current_user


def _plan(
    name,
    ns="openshift-operators",
    *,
    approved=False,
    phase="RequiresApproval",
    csvs=("es-operator.v5.8.2",),
    sub="es-operator",
    rv="1001",
    labels=None,
    annotations=None,
):
    meta = {
        "name": name,
        "namespace": ns,
        "resourceVersion": rv,
        "creationTimestamp": "2026-11-10T08:00:00Z",
        "ownerReferences": [{"kind": "Subscription", "name": sub}] if sub else [],
    }
    if labels:
        meta["labels"] = labels
    if annotations:
        meta["annotations"] = annotations
    return {
        "metadata": meta,
        "spec": {"approved": approved, "approval": "Manual", "clusterServiceVersionNames": list(csvs)},
        "status": {
            "phase": phase,
            "plan": [
                {
                    "resource": {
                        "kind": "ClusterServiceVersion",
                        "name": csvs[0],
                        "manifest": '{"spec": {"replaces": "es-operator.v5.8.1"}}',
                    }
                },
                {"resource": {"kind": "CustomResourceDefinition", "name": "elasticsearches.logging.openshift.io"}},
                {"resource": {"kind": "ClusterRole", "name": "es-operator"}},
            ],
        },
    }


def _sub(
    name="es-operator",
    ns="openshift-operators",
    *,
    installed="es-operator.v5.8.1",
    plan_ref="install-new",
    channel="stable-5.8",
):
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {"name": "elasticsearch-operator", "channel": channel, "installPlanApproval": "Manual"},
        "status": {"installedCSV": installed, "currentCSV": "es-operator.v5.8.2", "installPlanRef": {"name": plan_ref}},
    }


def _api(plans, subs, *, plan_error=None, sub_error=None, patch_side_effect=None):
    api = MagicMock()

    def list_cluster(group, version, plural):
        if plural == "installplans":
            if plan_error:
                raise plan_error
            return {"items": plans}
        if sub_error:
            raise sub_error
        return {"items": subs}

    def get_ns(group, version, ns, plural, name):
        pool = plans if plural == "installplans" else subs
        for o in pool:
            if o["metadata"]["name"] == name and o["metadata"]["namespace"] == ns:
                return copy.deepcopy(o)
        raise ApiException(status=404, reason="Not Found")

    api.list_cluster_custom_object.side_effect = list_cluster
    api.get_namespaced_custom_object.side_effect = get_ns
    if patch_side_effect is not None:
        api.patch_namespaced_custom_object.side_effect = patch_side_effect
    else:
        api.patch_namespaced_custom_object.return_value = {}
    return api


def _call(tool, **args):
    out = tool.call(args)
    return out[0] if isinstance(out, tuple) else out


@pytest.fixture
def as_alice():
    set_current_user("alice@acme.com")
    with user_token_context("alice-token"):
        yield "alice@acme.com"
    set_current_user(None)


@pytest.fixture
def alice_without_token():
    set_current_user("alice@acme.com")
    yield
    set_current_user(None)


# ---- reading ---------------------------------------------------------------


def test_pending_list_has_operator_from_to_channel_and_current_flag():
    plans = [
        _plan("install-new"),
        _plan("install-old", rv="900"),
        _plan("install-done", approved=True, phase="Complete"),
        _plan("install-failed", phase="Failed"),
    ]
    with patch("sre_agent.k8s_client.get_custom_client", return_value=_api(plans, [_sub()])):
        pending, subs_error = olm.read_pending_installplans()
    assert subs_error is None
    by_name = {p.name: p for p in pending}
    assert set(by_name) == {"install-new", "install-old"}
    new = by_name["install-new"]
    assert new.operator == "elasticsearch-operator"
    assert new.channel == "stable-5.8"
    assert new.installs == ["es-operator.v5.8.2"] and new.replaces == ["es-operator.v5.8.1"]
    assert new.current is True
    assert by_name["install-old"].current is False


def test_unreadable_subscriptions_leave_from_unknown_but_still_list_plans():
    api = _api([_plan("install-new")], [], sub_error=ApiException(status=403, reason="Forbidden"))
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        pending, subs_error = olm.read_pending_installplans()
    assert subs_error and "Subscriptions could not be read" in subs_error
    assert pending[0].current is None
    # Still known from the CSV manifest in the plan's steps.
    assert pending[0].replaces == ["es-operator.v5.8.1"]


def test_forbidden_installplans_is_an_error_not_an_empty_list():
    api = _api([], [], plan_error=ApiException(status=403, reason="Forbidden"))
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        with pytest.raises(olm.OLMReadError):
            olm.read_pending_installplans()
        text = _call(olm.list_pending_installplans)
    assert "could not be listed" in text
    assert "No InstallPlan is waiting" not in text


def test_list_tool_shows_rv_and_marks_superseded_and_explains_no_deny():
    with patch(
        "sre_agent.k8s_client.get_custom_client",
        return_value=_api([_plan("install-new"), _plan("install-old", rv="900")], [_sub()]),
    ):
        text = _call(olm.list_pending_installplans)
    assert "openshift-operators/install-new rv=1001" in text
    assert "from=es-operator.v5.8.1 to=es-operator.v5.8.2" in text
    assert "SUPERSEDED" in text
    assert "no 'deny'" in text
    assert "--- BEGIN CLUSTER DATA ---" in text


def test_olm_not_installed_is_nothing_pending_not_unavailable():
    api = _api([], [], plan_error=ApiException(status=404, reason="Not Found"))
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        assert olm.read_pending_installplans() == ([], None)
        assert gens.gen_operator_approvals() == []


def test_list_tool_says_nothing_pending():
    with patch("sre_agent.k8s_client.get_custom_client", return_value=_api([], [])):
        assert "No InstallPlan is waiting" in _call(olm.list_pending_installplans)


def test_list_tool_validates_namespace():
    api = _api([], [])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(olm.list_pending_installplans, namespace="Bad_NS")
    assert "not a valid Kubernetes namespace" in text
    api.list_cluster_custom_object.assert_not_called()
    api.list_namespaced_custom_object.assert_not_called()


# ---- approving -------------------------------------------------------------


def test_approve_dry_runs_then_patches_with_the_reviewed_resource_version(as_alice):
    api = _api([_plan("install-new")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    calls = api.patch_namespaced_custom_object.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs == {"dry_run": "All"}
    assert calls[1].kwargs == {}
    body = calls[1].args[5]
    assert body == {"metadata": {"resourceVersion": "1001"}, "spec": {"approved": True}}
    assert "approved by alice@acme.com" in text
    assert "es-operator.v5.8.2" in text and "es-operator.v5.8.1" in text


def test_approve_refuses_when_the_plan_changed_since_review(as_alice):
    api = _api([_plan("install-new", rv="1005")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    api.patch_namespaced_custom_object.assert_not_called()
    assert "changed since it was reviewed" in text


def test_approve_requires_a_resource_version(as_alice):
    api = _api([_plan("install-new")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="")
    api.patch_namespaced_custom_object.assert_not_called()
    assert "resource_version is required" in text


def test_approve_refuses_a_superseded_plan(as_alice):
    api = _api([_plan("install-old", rv="900")], [_sub(plan_ref="install-new")])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-old", resource_version="900"
        )
    api.patch_namespaced_custom_object.assert_not_called()
    assert "superseded" in text


def test_approve_of_an_already_approved_plan_changes_nothing(as_alice):
    api = _api([_plan("install-new", approved=True, phase="Installing")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    api.patch_namespaced_custom_object.assert_not_called()
    assert "already approved" in text


def test_failed_dry_run_writes_nothing(as_alice):
    def patch_fn(*args, **kwargs):
        raise ApiException(status=403, reason="Forbidden")

    api = _api([_plan("install-new")], [_sub()], patch_side_effect=patch_fn)
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    assert api.patch_namespaced_custom_object.call_count == 1
    assert api.patch_namespaced_custom_object.call_args.kwargs == {"dry_run": "All"}
    assert "dry run refused" in text and "Nothing was changed" in text


def test_conflict_on_the_real_write_is_reported_as_a_precondition_failure(as_alice):
    def patch_fn(*args, **kwargs):
        if kwargs.get("dry_run"):
            return {}
        raise ApiException(status=409, reason="Conflict")

    api = _api([_plan("install-new")], [_sub()], patch_side_effect=patch_fn)
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    assert "not approved" in text and "precondition" in text


def test_approve_never_runs_without_the_users_token(alice_without_token):
    api = _api([_plan("install-new")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    api.patch_namespaced_custom_object.assert_not_called()
    api.get_namespaced_custom_object.assert_not_called()
    assert "never approved as Pulse's service account" in text


def test_approve_refuses_when_token_forwarding_is_off(as_alice, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "false")
    from sre_agent.config import _reset_settings

    _reset_settings()
    try:
        api = _api([_plan("install-new")], [_sub()])
        with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
            text = _call(
                olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
            )
        api.patch_namespaced_custom_object.assert_not_called()
        assert "token forwarding is off" in text
    finally:
        monkeypatch.delenv("PULSE_AGENT_TOKEN_FORWARDING")
        _reset_settings()


def test_gitops_managed_subscription_is_named_in_the_result(as_alice):
    sub = _sub()
    sub["metadata"]["annotations"] = {
        "argocd.argoproj.io/tracking-id": "operators:operators.coreos.com/Subscription:openshift-operators/es-operator"
    }
    api = _api([_plan("install-new")], [sub])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        text = _call(
            olm.approve_installplan, namespace="openshift-operators", name="install-new", resource_version="1001"
        )
    assert "Argo CD application 'operators'" in text and "propose_git_change" in text


# ---- confirm preview -------------------------------------------------------


def test_preview_shows_installs_replaces_steps_and_dry_run(as_alice):
    from sre_agent.confirm_preview import build_preview, has_preview

    assert has_preview("approve_installplan")
    api = _api([_plan("install-new")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        p = build_preview(
            "approve_installplan",
            {"namespace": "openshift-operators", "name": "install-new", "resource_version": "1001"},
            "alice-token",
        )
    assert p is not None
    assert p["kind"] == "installplan_approve" and p["valid"] is True
    assert p["installs"] == ["es-operator.v5.8.2"] and p["replaces"] == ["es-operator.v5.8.1"]
    assert p["resourceVersion"] == "1001" and p["currentResourceVersion"] == "1001"
    assert p["crdCount"] == 1 and p["stepCount"] == 3
    assert p["dryRun"] == "passed"
    assert p["approvedBy"] == "alice@acme.com"
    assert "no 'deny'" in p["noDeny"]
    # The preview's dry run never writes.
    assert all(c.kwargs == {"dry_run": "All"} for c in api.patch_namespaced_custom_object.call_args_list)


def test_preview_carries_the_refusal_when_the_plan_changed(as_alice):
    api = _api([_plan("install-new", rv="1005")], [_sub()])
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        p = olm.preview_approve({"namespace": "openshift-operators", "name": "install-new", "resource_version": "1001"})
    assert p["valid"] is False and "changed since it was reviewed" in p["error"]
    api.patch_namespaced_custom_object.assert_not_called()


def test_preview_is_invalid_when_the_dry_run_fails(as_alice):
    def patch_fn(*args, **kwargs):
        raise ApiException(status=403, reason="Forbidden")

    api = _api([_plan("install-new")], [_sub()], patch_side_effect=patch_fn)
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        p = olm.preview_approve({"namespace": "openshift-operators", "name": "install-new", "resource_version": "1001"})
    assert p["valid"] is False and p["dryRun"] == "failed"


def test_preview_names_a_gitops_tracked_plan(as_alice):
    plan = _plan("install-new", labels={"app.kubernetes.io/instance": "cluster-config"})
    with patch("sre_agent.k8s_client.get_custom_client", return_value=_api([plan], [_sub()])):
        p = olm.preview_approve({"namespace": "openshift-operators", "name": "install-new", "resource_version": "1001"})
    assert "cluster-config" in p["gitops"] and "propose_git_change" in p["gitops"]


# ---- registry --------------------------------------------------------------


def test_approve_is_a_write_tool_and_list_is_not():
    from sre_agent.k8s_tools import WRITE_TOOLS
    from sre_agent.tool_registry import WRITE_TOOL_NAMES

    assert "approve_installplan" in WRITE_TOOLS and "approve_installplan" in WRITE_TOOL_NAMES
    assert "list_pending_installplans" not in WRITE_TOOL_NAMES


# ---- Inbox generator -------------------------------------------------------


def test_generator_makes_one_item_per_current_plan_with_a_link_to_the_list():
    plans = [_plan("install-new"), _plan("install-old", rv="900")]
    with patch("sre_agent.k8s_client.get_custom_client", return_value=_api(plans, [_sub()])):
        items = gens.gen_operator_approvals()
    assert len(items) == 1
    item = items[0]
    assert item["correlation_key"] == "operator_approval:openshift-operators/install-new"
    assert item["metadata"]["generator"] == "operator_approvals"
    assert item["metadata"]["ui_link"] == gens.OPERATOR_APPROVALS_UI_LINK
    assert "5.8.1 → 5.8.2" in item["title"]
    assert {"kind": "InstallPlan", "name": "install-new", "namespace": "openshift-operators"} in item["resources"]


def test_generator_is_unavailable_when_installplans_are_forbidden():
    api = _api([], [], plan_error=ApiException(status=403, reason="Forbidden"))
    with patch("sre_agent.k8s_client.get_custom_client", return_value=api):
        with pytest.raises(gens.GeneratorUnavailableError):
            gens.gen_operator_approvals()
        failed: set[str] = set()
        with patch.object(gens, "TASK_GENERATORS", [("operator_approvals", gens.gen_operator_approvals)]):
            assert gens.run_all_generators(failed) == []
    assert failed == {"operator_approvals"}


def test_generator_items_resolve_when_the_plan_is_approved():
    """Approved plans drop out of the pending list, so their key is no longer generated."""
    with patch(
        "sre_agent.k8s_client.get_custom_client",
        return_value=_api([_plan("install-new", approved=True, phase="Installing")], [_sub()]),
    ):
        assert gens.gen_operator_approvals() == []


def test_ui_link_is_a_route_the_ui_serves():
    from sre_agent.ui_routes import is_ui_route

    assert is_ui_route(gens.OPERATOR_APPROVALS_UI_LINK)
