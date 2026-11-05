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


class TestDeletedDefaultsStayDeleted:
    """Deleting a default SLO used to last only until the next restart."""

    KEY = ("openshiftpulse", "availability")

    def _restart(self):
        import sre_agent.slo_registry as mod

        mod._registry = None
        return mod.get_slo_registry()

    def _clean(self):
        from sre_agent.db import get_database

        db = get_database()
        db.execute("DELETE FROM operational_flags WHERE key LIKE %s", ("slo_deleted:%",))
        db.execute("DELETE FROM slo_definitions")
        db.commit()

    def test_a_deleted_default_is_not_registered_again_after_restart(self):
        import sre_agent.slo_registry as mod

        self._clean()
        try:
            reg = self._restart()
            assert reg.get(*self.KEY) is not None
            assert reg.unregister(*self.KEY, deleted_by="alice") is True

            reg = self._restart()
            assert reg.get(*self.KEY) is None, "the operator deleted it; a restart must not bring it back"
            # The other defaults are unaffected.
            assert reg.get("pulse-openshift-sre-agent", "availability") is not None

            # Registering it again is how it comes back, and that survives a restart too.
            reg.register(SLODefinition(service_name=self.KEY[0], slo_type=self.KEY[1], target=0.995))
            reg = self._restart()
            slo = reg.get(*self.KEY)
            assert slo is not None and slo.target == 0.995
        finally:
            self._clean()
            mod._registry = None

    def test_delete_endpoint_tombstones_the_default(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import sre_agent.slo_registry as mod
        from sre_agent.api.auth import get_owner, verify_token
        from sre_agent.api.monitor_rest import router
        from sre_agent.db import get_database

        self._clean()
        try:
            self._restart()
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[verify_token] = lambda: None
            app.dependency_overrides[get_owner] = lambda: "alice"
            resp = TestClient(app).delete(f"/slo/{self.KEY[0]}/{self.KEY[1]}")
            assert resp.status_code == 200
            assert self._restart().get(*self.KEY) is None
            row = get_database().fetchone(
                "SELECT updated_by FROM operational_flags WHERE key = %s", (f"slo_deleted:{self.KEY[0]}:{self.KEY[1]}",)
            )
            assert row["updated_by"] == "alice", "the tombstone records who deleted it"
        finally:
            self._clean()
            mod._registry = None

    def test_no_database_registers_every_default(self):
        from unittest.mock import patch

        from sre_agent.slo_registry import _load_tombstones, _register_defaults

        with patch("sre_agent.repositories.get_slo_repo", side_effect=RuntimeError("no db")):
            assert _load_tombstones() == set()
        reg = SLORegistry()
        _register_defaults(reg, skip=set())
        assert reg.get(*self.KEY) is not None


class TestPromQLInjection:
    """A service name reached PromQL unescaped and GET /slo ran it as the service account."""

    INJECTIONS = (
        'a"} or vector(1) #',
        "checkout\n",
        "check\nout",
        "a\\",
        'a"',
        "Checkout",
        "a" * 254,
        "",
    )

    def test_escaping_keeps_quotes_and_backslashes_literal(self):
        from sre_agent.slo_registry import promql_label_value

        assert promql_label_value("checkout") == "checkout"
        assert promql_label_value('a"} or vector(1) #') == 'a\\"} or vector(1) #'
        assert promql_label_value("a\\") == "a\\\\"
        assert promql_label_value('\\"') == '\\\\\\"'
        assert promql_label_value("a\nb\r") == "a\\nb\\r"

    def test_escaped_injection_parses_as_one_literal_selector(self):
        import promql_parser

        from sre_agent.slo_registry import promql_label_value

        value = 'a"} or vector(1) #\n\\'
        expr = promql_parser.parse(f'up{{deployment="{promql_label_value(value)}"}}')
        assert type(expr).__name__ == "VectorSelector"
        assert "vector(1)" not in str(expr).split("{")[0]

    def test_invalid_names_are_rejected(self):
        from sre_agent.slo_registry import slo_definition_error

        for name in self.INJECTIONS:
            assert slo_definition_error(name, "availability") is not None, repr(name)

    def test_other_fields_are_validated(self):
        from sre_agent.slo_registry import slo_definition_error

        assert slo_definition_error("api", "availability", 0.999, 30) is None
        assert slo_definition_error("kube-apiserver", "latency", 99.9, 90) is None
        assert slo_definition_error("api", "throughput") is not None
        assert slo_definition_error("api", "availability", float("nan")) is not None
        assert slo_definition_error("api", "availability", float("inf")) is not None
        assert slo_definition_error("api", "availability", "0.999") is not None
        assert slo_definition_error("api", "availability", 0.999, 91) is not None
        assert slo_definition_error("api", "availability", 0.999, "30d") is not None
        assert slo_definition_error("api", "availability", 0.999, True) is not None

    def test_register_rejects_an_invalid_new_slo(self):
        import pytest

        reg = SLORegistry()
        with pytest.raises(ValueError):
            reg.register(SLODefinition(service_name='a"} or vector(1) #', slo_type="availability", target=0.999))
        assert reg.list_all() == []

    def test_valid_query_is_unchanged(self):
        reg = SLORegistry()
        q = reg._build_prom_query(SLODefinition(service_name="checkout", slo_type="availability", target=0.999))
        assert q == (
            'avg_over_time(kube_deployment_status_replicas_available{deployment="checkout"}[30d]) / '
            'avg_over_time(kube_deployment_spec_replicas{deployment="checkout"}[30d])'
        )

    def test_invalid_stored_row_is_skipped_not_queried(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.slo_registry import _load_persisted

        bad = 'a"} or vector(1) #'
        repo = MagicMock()
        repo.fetch_all.return_value = [
            {"service_name": bad, "slo_type": "availability", "target": 0.999, "window_days": 30, "description": ""},
            {
                "service_name": "checkout",
                "slo_type": "availability",
                "target": 0.999,
                "window_days": 30,
                "description": "",
            },
        ]
        reg = SLORegistry()
        with patch("sre_agent.repositories.get_slo_repo", return_value=repo):
            _load_persisted(reg)
        # Kept, so it stays visible and deletable, but marked invalid.
        stored = reg.get(bad, "availability")
        assert stored is not None and reg.invalid_reason(stored)
        assert reg._build_prom_query(stored) == ""

        client = MagicMock()
        client.query.return_value = {"status": "success", "data": {"result": [{"value": [0, "1"]}]}}
        with patch("sre_agent.prometheus.get_prometheus_client", return_value=client):
            values = reg.query_prometheus_values()
        queries = [c.args[0] for c in client.query.call_args_list]
        assert len(queries) == 1 and 'deployment="checkout"' in queries[0]
        assert all("vector(1)" not in q for q in queries)
        assert values == {"checkout:availability": 1.0}

    def test_get_slo_reports_invalid_stored_row_as_no_data(self):
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from sre_agent.api import monitor_rest
        from sre_agent.api.auth import verify_token

        bad = 'a"} or vector(1) #'
        reg = SLORegistry()
        reg.register(SLODefinition(bad, "availability", 0.999), persist=False)
        reg.register(SLODefinition("api", "availability", 0.999), persist=False)
        with (
            patch("sre_agent.slo_registry.get_slo_registry", return_value=reg),
            patch.object(reg, "query_prometheus_values", return_value={"api:availability": 0.9995}),
        ):
            app = FastAPI()
            app.include_router(monitor_rest.router)
            app.dependency_overrides[verify_token] = lambda: None
            body = TestClient(app).get("/slo").json()
        rows = {r["service"]: r for r in body["slos"]}
        assert rows[bad]["alert_level"] == "no_data" and rows[bad]["current_value"] is None
        assert rows[bad]["invalid_reason"]
        assert rows["api"]["alert_level"] == "ok" and rows["api"]["invalid_reason"] is None


class TestValidatorsRejectTrailingNewline:
    """``^...$`` with ``re.match`` accepted ``"name\\n"``: ``$`` matches before a final newline."""

    def test_k8s_name_and_namespace(self):
        from sre_agent.k8s_tools.validators import _validate_k8s_name, _validate_k8s_namespace

        assert _validate_k8s_name("checkout\n") is not None
        assert _validate_k8s_namespace("default\n") is not None
        assert _validate_k8s_name("checkout") is None
        assert _validate_k8s_namespace("default") is None

    def test_cluster_label(self):
        import pytest

        from sre_agent.promql_recipes import inject_cluster_label

        with pytest.raises(ValueError):
            inject_cluster_label("up", "prod\n")
        assert 'cluster="prod"' in inject_cluster_label("up", "prod")

    def test_resource_recommendations_time_range(self):
        from sre_agent.k8s_tools.advanced import get_resource_recommendations

        out = get_resource_recommendations.func("default", "1h:]) or vector(1) #")
        assert out.startswith("Error: time_range")
