"""Harness-level deny policy: rules that hold regardless of model or approver."""

import pytest

from sre_agent import config as config_mod
from sre_agent.errors import ToolError
from sre_agent.policy import check_write_policy


@pytest.fixture
def fresh_settings(monkeypatch):
    """Reset the settings singleton around each test so env vars apply."""
    config_mod._reset_settings()
    yield monkeypatch
    config_mod._reset_settings()


def test_delete_pod_denied_in_production(fresh_settings):
    denied = check_write_policy("delete_pod", {"namespace": "production", "name": "api-1"})
    assert isinstance(denied, ToolError)
    assert denied.category == "forbidden"
    assert "restart_deployment" in denied.message


def test_delete_pod_denied_in_wildcard_platform_namespaces(fresh_settings):
    assert check_write_policy("delete_pod", {"namespace": "openshift-monitoring", "name": "x"}) is not None
    assert check_write_policy("delete_pod", {"namespace": "kube-system", "name": "x"}) is not None


def test_delete_pod_allowed_outside_protected_namespaces(fresh_settings):
    assert check_write_policy("delete_pod", {"namespace": "dev-sandbox", "name": "x"}) is None


def test_restart_deployment_allowed_in_production(fresh_settings):
    """Routine remediation must not be collateral damage of the policy."""
    assert check_write_policy("restart_deployment", {"namespace": "production", "name": "api"}) is None


def test_node_ops_denied_by_default(fresh_settings):
    for tool in ("drain_node", "cordon_node"):
        denied = check_write_policy(tool, {"name": "worker-1"})
        assert isinstance(denied, ToolError)
        assert denied.category == "forbidden"
        assert "change" in denied.message.lower()


def test_node_ops_allowed_with_break_glass(fresh_settings):
    fresh_settings.setenv("PULSE_AGENT_ALLOW_NODE_OPS", "1")
    assert check_write_policy("drain_node", {"name": "worker-1"}) is None


def test_protected_namespaces_configurable(fresh_settings):
    fresh_settings.setenv("PULSE_AGENT_PROTECTED_NAMESPACES", "payments,pci-*")
    assert check_write_policy("delete_pod", {"namespace": "pci-cardholder", "name": "x"}) is not None
    assert check_write_policy("delete_pod", {"namespace": "production", "name": "x"}) is None


def test_policy_disabled_with_empty_namespace_list(fresh_settings):
    fresh_settings.setenv("PULSE_AGENT_PROTECTED_NAMESPACES", "")
    assert check_write_policy("delete_pod", {"namespace": "production", "name": "x"}) is None


def test_read_tools_never_denied(fresh_settings):
    assert check_write_policy("list_pods", {"namespace": "production"}) is None
    assert check_write_policy("describe_pod", {"namespace": "kube-system", "name": "x"}) is None
