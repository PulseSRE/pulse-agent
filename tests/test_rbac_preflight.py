"""RBAC preflight: a proposal the agent provably cannot execute is a dead-end
Approve button.

The reference cluster hit exactly this: read-only ClusterRole (write ops are
opt-in via spec.agent.allowWriteOperations), the planner proposed a pod
delete, an operator approved it, and execution died on a 403 the agent could
have predicted. The gate fires only on an affirmative denial from the API
server — an unverifiable check is not a denial.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.monitor import rbac_preflight
from sre_agent.monitor.rbac_preflight import can_execute, clear_cache


def _auth_client(allowed: bool) -> MagicMock:
    auth = MagicMock()
    resp = MagicMock()
    resp.status.allowed = allowed
    auth.create_self_subject_access_review.return_value = resp
    return auth


class TestCanExecute:
    def setup_method(self):
        clear_cache()

    def test_strategy_without_writes_needs_no_check(self):
        with patch("sre_agent.k8s_client.get_authorization_client") as gac:
            assert can_execute("require_human_review", "prod") == (True, "")
        gac.assert_not_called()

    def test_allowed_write_passes(self):
        with patch("sre_agent.k8s_client.get_authorization_client", return_value=_auth_client(True)):
            assert can_execute("restart_controller", "prod") == (True, "")

    def test_denial_returns_the_remediation_not_a_403(self):
        with patch("sre_agent.k8s_client.get_authorization_client", return_value=_auth_client(False)):
            allowed, reason = can_execute("restart_controller", "multicluster-engine")
        assert not allowed
        assert "multicluster-engine" in reason
        assert "allowWriteOperations" in reason

    def test_unverifiable_check_is_not_a_denial(self):
        """If the SSAR call itself fails, the fix proceeds and execution
        reports the real outcome — a failed check must never manufacture a
        permission denial."""
        with patch(
            "sre_agent.k8s_client.get_authorization_client",
            side_effect=RuntimeError("no cluster"),
        ):
            assert can_execute("restart_controller", "prod") == (True, "")

    def test_result_is_cached_per_strategy_and_namespace(self):
        auth = _auth_client(False)
        with patch("sre_agent.k8s_client.get_authorization_client", return_value=auth):
            can_execute("restart_controller", "prod")
            can_execute("restart_controller", "prod")
        assert auth.create_self_subject_access_review.call_count == 1

    def test_unverifiable_result_is_not_cached(self):
        """A transient check failure must not pin 'allowed' into the cache."""
        with patch(
            "sre_agent.k8s_client.get_authorization_client",
            side_effect=RuntimeError("blip"),
        ):
            can_execute("restart_controller", "prod")
        assert ("restart_controller", "prod") not in rbac_preflight._cache

    def test_every_write_strategy_is_declared(self):
        """Executors that write to the cluster must have a preflight entry —
        a write strategy absent from the table silently skips the gate."""
        from sre_agent.monitor.fix_planner import _EXECUTORS
        from sre_agent.monitor.rbac_preflight import _STRATEGY_WRITES

        write_free = {"create_configmap", "patch_probe", "suggest_quota_increase", "require_human_review"}
        for strategy in _EXECUTORS:
            if strategy in write_free:
                continue
            assert strategy in _STRATEGY_WRITES, f"write strategy '{strategy}' has no RBAC preflight entry"
