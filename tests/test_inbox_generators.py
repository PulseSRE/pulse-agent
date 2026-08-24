"""Tests for the 15 proactive inbox task generators."""

import time
from unittest.mock import patch


class TestCertExpiryGenerator:
    @patch("sre_agent.inbox_generators._get_tls_secrets")
    def test_generates_items_for_expiring_certs(self, mock_secrets):
        from sre_agent.inbox_generators import gen_cert_expiry

        mock_secrets.return_value = [
            {"name": "api-cert", "namespace": "production", "expiry_timestamp": int(time.time()) + 24 * 3600},
            {"name": "web-cert", "namespace": "production", "expiry_timestamp": int(time.time()) + 720 * 3600},
        ]
        items = gen_cert_expiry()
        assert len(items) == 1
        assert items[0]["severity"] in ("critical", "warning")
        assert items[0]["item_type"] == "task"

    @patch("sre_agent.inbox_generators._get_tls_secrets")
    def test_no_items_when_certs_healthy(self, mock_secrets):
        from sre_agent.inbox_generators import gen_cert_expiry

        mock_secrets.return_value = [
            {"name": "healthy", "namespace": "default", "expiry_timestamp": int(time.time()) + 2160 * 3600},
        ]
        items = gen_cert_expiry()
        assert len(items) == 0


class TestTrendPredictionGenerator:
    @patch("sre_agent.inbox_generators._get_trend_findings")
    def test_converts_trend_findings(self, mock_trends):
        from sre_agent.inbox_generators import gen_trend_prediction

        mock_trends.return_value = [
            {
                "title": "Memory pressure predicted in 18h",
                "severity": "warning",
                "confidence": 0.85,
                "resources": [{"kind": "Node", "name": "worker-03", "namespace": ""}],
                "metadata": {"predicted_hours": 18},
            },
        ]
        items = gen_trend_prediction()
        assert len(items) == 1
        assert items[0]["item_type"] == "task"
        assert items[0]["metadata"]["urgency_hours"] == 18


class TestDegradedOperatorGenerator:
    @patch("sre_agent.inbox_generators._get_degraded_operators")
    def test_generates_for_degraded(self, mock_ops):
        from sre_agent.inbox_generators import gen_degraded_operator

        mock_ops.return_value = [
            {"name": "authentication", "degraded_duration_hours": 2},
        ]
        items = gen_degraded_operator()
        assert len(items) == 1
        assert items[0]["severity"] == "critical"

    @patch("sre_agent.inbox_generators._get_degraded_operators")
    def test_no_items_when_healthy(self, mock_ops):
        from sre_agent.inbox_generators import gen_degraded_operator

        mock_ops.return_value = []
        items = gen_degraded_operator()
        assert len(items) == 0


class TestUpgradeAvailableGenerator:
    @patch("sre_agent.inbox_generators._get_available_updates")
    def test_generates_for_available_update(self, mock_updates):
        from sre_agent.inbox_generators import gen_upgrade_available

        mock_updates.return_value = [
            {"version": "4.15.2", "channel": "stable-4.15"},
        ]
        items = gen_upgrade_available()
        assert len(items) == 1
        assert items[0]["severity"] == "info"
        assert items[0]["metadata"]["urgency_hours"] == 168


class TestSLOBurnGenerator:
    @patch("sre_agent.inbox_generators._get_slo_burn_rates")
    def test_generates_for_burning_slo(self, mock_slos):
        from sre_agent.inbox_generators import gen_slo_burn

        mock_slos.return_value = [
            {"name": "api-availability", "budget_remaining_hours": 12, "burn_rate": 2.5},
        ]
        items = gen_slo_burn()
        assert len(items) == 1
        assert items[0]["severity"] == "critical"


class TestCapacityProjectionGenerator:
    @patch("sre_agent.inbox_generators._get_node_capacity")
    def test_generates_for_near_capacity(self, mock_capacity):
        from sre_agent.inbox_generators import gen_capacity_projection

        mock_capacity.return_value = [
            {"node": "worker-01", "cpu_pct": 92, "hours_to_full": 6},
        ]
        items = gen_capacity_projection()
        assert len(items) == 1
        assert items[0]["severity"] == "warning"


class TestStaleFindingGenerator:
    @patch("sre_agent.inbox_generators._get_stale_findings")
    def test_generates_for_stale(self, mock_stale):
        from sre_agent.inbox_generators import gen_stale_finding

        mock_stale.return_value = [
            {"title": "Old crashloop", "hours_stale": 96, "finding_id": "f-123"},
        ]
        items = gen_stale_finding()
        assert len(items) == 1
        assert items[0]["severity"] == "warning"
        assert items[0]["metadata"]["urgency_hours"] < 0


class TestPrivilegedWorkloadsGenerator:
    @patch("sre_agent.inbox_generators._get_privileged_workloads")
    def test_generates_for_privileged(self, mock_workloads):
        from sre_agent.inbox_generators import gen_privileged_workloads

        mock_workloads.return_value = [
            {"pod": "debug-pod", "namespace": "default", "container": "main"},
        ]
        items = gen_privileged_workloads()
        assert len(items) == 1
        assert items[0]["metadata"]["urgency_hours"] == 24


class TestRBACDriftGenerator:
    @patch("sre_agent.inbox_generators._get_rbac_drift")
    def test_generates_for_drift(self, mock_drift):
        from sre_agent.inbox_generators import gen_rbac_drift

        mock_drift.return_value = [
            {"binding": "admin-binding", "user": "alice"},
        ]
        items = gen_rbac_drift()
        assert len(items) == 1
        assert items[0]["metadata"]["urgency_hours"] == 12


class TestNetworkPolicyGapsGenerator:
    @patch("sre_agent.inbox_generators._get_network_policy_gaps")
    def test_generates_for_gaps(self, mock_gaps):
        from sre_agent.inbox_generators import gen_network_policy_gaps

        mock_gaps.return_value = [{"namespace": "staging"}]
        items = gen_network_policy_gaps()
        assert len(items) == 1
        assert items[0]["severity"] == "info"


class TestRouteCertExpiryGenerator:
    @patch("sre_agent.inbox_generators._get_route_cert_expiry")
    def test_generates_for_expiring_routes(self, mock_routes):
        from sre_agent.inbox_generators import gen_route_cert_expiry

        mock_routes.return_value = [
            {"name": "api-route", "namespace": "production", "hours_until_expiry": 48},
        ]
        items = gen_route_cert_expiry()
        assert len(items) == 1
        assert items[0]["metadata"]["urgency_hours"] == 48


class TestServiceEndpointGapsGenerator:
    @patch("sre_agent.inbox_generators._get_service_endpoint_gaps")
    def test_generates_for_gaps(self, mock_gaps):
        from sre_agent.inbox_generators import gen_service_endpoint_gaps

        mock_gaps.return_value = [{"service": "payment-svc", "namespace": "production"}]
        items = gen_service_endpoint_gaps()
        assert len(items) == 1
        assert items[0]["metadata"]["urgency_hours"] == 1


class TestGeneratorRegistration:
    def test_all_16_registered(self):
        from sre_agent.inbox_generators import TASK_GENERATORS

        assert len(TASK_GENERATORS) == 16
        names = [n for n, _ in TASK_GENERATORS]
        assert "skill_gaps" in names
        assert "skill_low_performers" in names
        assert "skill_curation" in names

    def test_run_all_generators_returns_list(self):
        from sre_agent.inbox_generators import run_all_generators

        with (
            patch("sre_agent.inbox_generators._get_tls_secrets", return_value=[]),
            patch("sre_agent.inbox_generators._get_trend_findings", return_value=[]),
            patch("sre_agent.inbox_generators._get_degraded_operators", return_value=[]),
            patch("sre_agent.inbox_generators._get_available_updates", return_value=[]),
            patch("sre_agent.inbox_generators._get_slo_burn_rates", return_value=[]),
            patch("sre_agent.inbox_generators._get_node_capacity", return_value=[]),
            patch("sre_agent.inbox_generators._get_stale_findings", return_value=[]),
            patch("sre_agent.inbox_generators._get_privileged_workloads", return_value=[]),
            patch("sre_agent.inbox_generators._get_rbac_drift", return_value=[]),
            patch("sre_agent.inbox_generators._get_network_policy_gaps", return_value=[]),
            patch("sre_agent.inbox_generators._get_route_cert_expiry", return_value=[]),
            patch("sre_agent.inbox_generators._get_service_endpoint_gaps", return_value=[]),
        ):
            result = run_all_generators()
            assert isinstance(result, list)
            assert len(result) == 0


class TestSkillGapGenerator:
    """Detected skill gaps must become visible proposals, not log lines."""

    @patch("sre_agent.selector_learning.identify_skill_gaps")
    def test_generates_proposal_per_gap(self, mock_gaps):
        from sre_agent.inbox_generators import gen_skill_gaps

        mock_gaps.return_value = [
            {"pattern": "etcd defrag latency", "occurrences": 7},
            {"pattern": "pvc resize stuck", "occurrences": 4},
        ]
        items = gen_skill_gaps()
        assert len(items) == 2
        assert items[0]["correlation_key"] == "skill-gap:etcd-defrag-latency"
        assert "7x" in items[0]["title"]
        assert items[0]["item_type"] == "task"
        assert items[0]["metadata"]["generator"] == "skill_gaps"

    @patch("sre_agent.selector_learning.identify_skill_gaps")
    def test_caps_at_three_proposals(self, mock_gaps):
        from sre_agent.inbox_generators import gen_skill_gaps

        mock_gaps.return_value = [{"pattern": f"pattern {i}", "occurrences": 3} for i in range(10)]
        assert len(gen_skill_gaps()) == 3

    @patch("sre_agent.selector_learning.identify_skill_gaps", return_value=[])
    def test_no_gaps_no_items(self, _):
        from sre_agent.inbox_generators import gen_skill_gaps

        assert gen_skill_gaps() == []


class TestSkillLowPerformerGenerator:
    """A flagged skill becomes a quarantine proposal carrying its evidence."""

    @patch("sre_agent.selector_learning.prune_low_performers")
    def test_generates_quarantine_proposal(self, mock_prune):
        from sre_agent.inbox_generators import gen_skill_low_performers

        mock_prune.return_value = [
            {"skill": "flaky_skill", "override_rate": 0.45, "overrides": 9, "total": 20},
        ]
        items = gen_skill_low_performers()
        assert len(items) == 1
        assert items[0]["correlation_key"] == "skill-quarantine:flaky_skill"
        assert "45%" in items[0]["title"]
        assert "quarantine" in items[0]["summary"]
        assert items[0]["metadata"]["generator"] == "skill_low_performers"

    @patch("sre_agent.selector_learning.prune_low_performers", return_value=[])
    def test_healthy_skills_no_items(self, _):
        from sre_agent.inbox_generators import gen_skill_low_performers

        assert gen_skill_low_performers() == []
