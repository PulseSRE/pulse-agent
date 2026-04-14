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
        status = reg.check_burn_rate(slo, current_value=0.9995)
        assert status.alert_level == "ok"
        assert status.error_budget_remaining > 0.5

    def test_warning_budget(self):
        reg = SLORegistry()
        slo = SLODefinition(service_name="api", slo_type="availability", target=0.999)
        status = reg.check_burn_rate(slo, current_value=0.9982)
        assert status.error_budget_remaining < 0.3
        assert status.alert_level == "warning"

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
