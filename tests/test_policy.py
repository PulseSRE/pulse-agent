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


class TestAutoFixPathEnforcement:
    """The unsupervised path must not be weaker than the supervised one.

    The monitor's auto-fix dispatches through fix_planner.execute_fix and calls
    the K8s API directly — it never reaches agent._execute_tool, where the deny
    policy is applied for chat. Without enforcement here, asking the agent to
    delete a production pod is refused while the agent deleting one on its own
    initiative is not.
    """

    def test_restart_controller_blocked_in_protected_namespace(self, fresh_settings):
        from sre_agent.monitor.fix_planner import FixPlan, execute_fix

        plan = FixPlan(
            strategy="restart_controller",
            cause_category="crashloop",
            confidence=0.9,
            description="restart",
            params={"resources": [{"name": "payments-1", "namespace": "production", "kind": "Pod"}]},
        )
        tool, _before, after = execute_fix(plan)
        assert tool == "blocked"
        assert "Blocked by policy" in after

    def test_restart_controller_allowed_outside_protected_namespaces(self, fresh_settings, monkeypatch):
        """Guard against the block being unconditional — dev namespaces still auto-fix."""
        from sre_agent.monitor import fix_planner

        called = {}

        def fake_executor(plan):
            called["yes"] = True
            return ("delete_pod", "before", "after")

        monkeypatch.setitem(fix_planner._EXECUTORS, "restart_controller", fake_executor)
        plan = fix_planner.FixPlan(
            strategy="restart_controller",
            cause_category="crashloop",
            confidence=0.9,
            description="restart",
            params={"resources": [{"name": "web-1", "namespace": "dev-sandbox", "kind": "Pod"}]},
        )
        tool, _before, _after = fix_planner.execute_fix(plan)
        assert called.get("yes") is True
        assert tool == "delete_pod"
