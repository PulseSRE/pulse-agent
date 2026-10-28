"""Tests for SLO/SLI registry."""

from __future__ import annotations

from sre_agent.slo_registry import SLODefinition, SLORegistry


class TestSLORegistry:
    def test_register_and_get(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="checkout", slo_type="availability", target=0.999)
        reg.register(slo)
        assert reg.get("checkout", "availability") is not None

    def test_unregister(self):
        reg = SLORegistry()
        reg.register(SLODefinition(service_name="api", slo_type="latency", target=0.95))
        assert reg.unregister("api", "latency") is True
        assert reg.get("api", "latency") is None

    def test_list_all(self):
        reg = SLORegistry()
        reg.register(SLODefinition(service_name="a", slo_type="availability", target=0.999))
        reg.register(SLODefinition(service_name="b", slo_type="latency", target=0.95))
        assert len(reg.list_all()) == 2


class TestBurnRate:
    def test_healthy_service(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=0.9998)
        assert status.alert_level == "ok"
        assert status.error_budget_remaining > 0.5

    def test_warning_budget(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        # 0.075% bad against a 0.1% budget: three quarters of it spent.
        status = reg.check_burn_rate(slo, current_value=0.99925, recent_value=0.9995)
        assert status.error_budget_remaining < 0.3
        assert status.alert_level == "warning"

    def test_budget_counts_all_bad_events_not_the_shortfall_below_target(self):
        """99.95% against 99.9% has spent half the budget; it used to read as full."""
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=0.9995)
        assert status.error_budget_remaining == 0.5

    def test_overspent_budget_is_critical(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        assert reg.check_burn_rate(slo, current_value=0.9982).alert_level == "critical"

    def test_fast_burn_is_critical_even_with_budget_left(self):
        """Burning 20x in the last hour pages, whatever the month looks like."""
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=0.9999, recent_value=0.98)
        assert status.burn_rate == 20.0
        assert status.alert_level == "critical"
        assert status.budget_remaining_hours is not None and status.budget_remaining_hours < 48

    def test_percentage_targets_are_normalised(self):
        """99.9 used to make the budget negative and every check 'ok'."""
        slo = SLODefinition(service_name="api", slo_type="availability", target=99.9)
        assert slo.target == 0.999
        assert SLORegistry().check_burn_rate(slo, current_value=0.99).alert_level == "critical"

    def test_critical_budget(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=0.9980)
        assert status.alert_level == "critical"

    def test_perfect_service(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=1.0)
        assert status.error_budget_remaining == 1.0
        assert status.alert_level == "ok"


class TestEvaluateAll:
    def test_evaluates_registered_slos(self):
        reg = SLORegistry()
        reg.register(SLODefinition(service_name="a", slo_type="availability", target=0.999))
        reg.register(SLODefinition(service_name="b", slo_type="latency", target=0.95))
        results = reg.evaluate_all({"a:availability": 0.998, "b:latency": 0.96})
        assert len(results) == 2


class TestSelectorContext:
    def test_empty_when_healthy(self):
        reg = SLORegistry()
        assert reg.get_context_for_selector() == ""

    def test_singleton(self):
        from sre_agent.slo_registry import get_slo_registry

        r1 = get_slo_registry()
        r2 = get_slo_registry()
        assert r1 is r2


class TestPersistence:
    """SLOs registered through the API must survive pod restarts.

    The slo_definitions table existed since migration 016 but nothing read or
    wrote it — every restart silently reset the SLO stack to defaults.
    """

    def test_register_persists_through_repo(self):
        from unittest.mock import MagicMock, patch

        repo = MagicMock()
        reg = SLORegistry()
        with patch("sre_agent.repositories.get_slo_repo", return_value=repo):
            reg.register(SLODefinition(service_name="checkout", slo_type="availability", target=0.999, description="d"))
        repo.save.assert_called_once_with("checkout", "availability", 0.999, 30, "d")

    def test_register_with_persist_false_skips_repo(self):
        from unittest.mock import MagicMock, patch

        repo = MagicMock()
        reg = SLORegistry()
        with patch("sre_agent.repositories.get_slo_repo", return_value=repo):
            reg.register(SLODefinition(service_name="x", slo_type="latency", target=0.95), persist=False)
        repo.save.assert_not_called()

    def test_unregister_deletes_persisted_row(self):
        from unittest.mock import MagicMock, patch

        repo = MagicMock()
        reg = SLORegistry()
        reg.register(SLODefinition(service_name="x", slo_type="latency", target=0.95), persist=False)
        with patch("sre_agent.repositories.get_slo_repo", return_value=repo):
            assert reg.unregister("x", "latency") is True
        repo.delete.assert_called_once_with("x", "latency")

    def test_register_survives_missing_database(self):
        # No DB in this test process: registration must still land in memory.
        reg = SLORegistry()
        reg.register(SLODefinition(service_name="a", slo_type="availability", target=0.999))
        assert reg.get("a", "availability") is not None

    def test_load_persisted_overrides_default_with_same_key(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.slo_registry import _load_persisted, _register_defaults

        repo = MagicMock()
        repo.fetch_all.return_value = [
            {
                "service_name": "openshiftpulse",
                "slo_type": "availability",
                "target": 0.995,  # operator retuned the default's 0.999
                "window_days": 14,
                "description": "retuned",
            }
        ]
        reg = SLORegistry()
        _register_defaults(reg)
        with patch("sre_agent.repositories.get_slo_repo", return_value=repo):
            _load_persisted(reg)
        slo = reg.get("openshiftpulse", "availability")
        assert slo is not None and slo.target == 0.995 and slo.window_days == 14


class TestApiserverLatencyQuery:
    def test_apiserver_latency_is_a_real_ratio_sli(self):
        """The seeded apiserver latency SLO must measure request latency, not
        the restart-rate proxy (which is not even a ratio comparable to a target)."""
        reg = SLORegistry()
        slo = SLODefinition(service_name="kube-apiserver", slo_type="latency", target=0.99)
        q = reg._build_prom_query(slo)
        assert "apiserver_request_duration_seconds_bucket" in q
        assert "restarts" not in q

    def test_other_services_have_no_latency_or_error_sli(self):
        """A restart *rate* compared with a 0.99 target read as a 0.0 SLI: permanently critical."""
        reg = SLORegistry()
        for slo_type in ("latency", "error_rate"):
            slo = SLODefinition(service_name="checkout", slo_type=slo_type, target=0.99)
            assert reg._build_prom_query(slo) == ""

    def test_apiserver_error_rate_is_a_ratio(self):
        reg = SLORegistry()
        q = reg._build_prom_query(SLODefinition(service_name="kube-apiserver", slo_type="error_rate", target=0.999))
        assert 'code=~"5.."' in q and q.startswith("1 - ")

    def test_burn_window_query_uses_the_short_window(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        assert "[1h]" in reg._build_prom_query(slo, "1h")
        assert "[30d]" in reg._build_prom_query(slo)
