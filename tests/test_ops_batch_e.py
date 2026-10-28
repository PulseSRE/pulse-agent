"""Batch E: ops workflow correctness.

Each test fails on the code before this change:

- E1 a generator whose API call failed returned [], and the cycle
  auto-resolved its open items;
- E2/E3 the SLO burn generator raised on a missing key, and the budget was a
  constant; burn math measured the shortfall below target, percentage
  targets disabled checks, and restart rates stood in for ratios;
- E4 trend items read an attribute nothing set;
- E5 route certs, operator durations and node capacity were constants;
- E6 urgency never reached priority (no due date);
- E7 an unmet diagnosis contract still unblocked remediation;
- E8 the timeline tool crashed on a 403;
- E9 the quota forecast mixed cores and millicores;
- E10 change risk never knew the previous image.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.errors import ToolError


def _forbidden():
    return ToolError(message="forbidden: cannot list", category="forbidden", status_code=403)


# ── E1: a failed read is not "condition cleared" ──────────────────────────


class TestGeneratorFailures:
    def test_tool_error_raises_instead_of_returning_empty(self):
        from sre_agent import inbox_generators as g

        core = MagicMock()
        core.list_node.side_effect = None
        with (
            patch("sre_agent.k8s_client.get_core_client", return_value=core),
            patch("sre_agent.k8s_client.safe", return_value=_forbidden()),
        ):
            with pytest.raises(g.GeneratorUnavailableError):
                g._get_node_capacity()

    def test_failed_generators_are_reported(self):
        from sre_agent import inbox_generators as g

        failed: set[str] = set()

        def boom():
            raise g.GeneratorUnavailableError("403")

        with patch.object(g, "TASK_GENERATORS", [("rbac_drift", boom), ("ok", lambda: [])]):
            assert g.run_all_generators(failed) == []
        assert failed == {"rbac_drift"}

    def test_cycle_keeps_items_of_a_generator_that_could_not_run(self):
        from sre_agent import inbox_generators as g
        from sre_agent.db import get_database
        from sre_agent.inbox import get_inbox_item, run_generator_cycle

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
        item = g._make_assessment(
            title="1 cluster-admin bindings to review",
            summary="x",
            severity="warning",
            urgency_hours=12,
            generator="rbac_drift",
        )
        state = {"fail": False}

        def rbac():
            if state["fail"]:
                raise g.GeneratorUnavailableError("forbidden")
            return [dict(item)]

        quiet = {
            "sre_agent.inbox.sweep_stale_items": lambda: 0,
            "sre_agent.inbox.expire_untouched_items": lambda: 0,
            "sre_agent.inbox.agent_process_inbox": lambda: None,
        }
        try:
            with (
                patch.object(g, "TASK_GENERATORS", [("rbac_drift", rbac)]),
                patch.multiple("sre_agent.inbox", **{k.rsplit(".", 1)[1]: v for k, v in quiet.items()}),
            ):
                run_generator_cycle()
                row = db.fetchone("SELECT id FROM inbox_items WHERE correlation_key = ?", (item["correlation_key"],))
                assert row
                state["fail"] = True
                run_generator_cycle()
                assert get_inbox_item(row["id"])["status"] not in ("resolved", "archived")
        finally:
            db.execute("DELETE FROM inbox_items")
            db.commit()


# ── E2/E3: SLOs ────────────────────────────────────────────────────────────


class TestSloBurnGenerator:
    def test_burning_slo_becomes_an_item_with_measured_numbers(self):
        from sre_agent import inbox_generators as g
        from sre_agent.slo_registry import SLODefinition, SLORegistry

        reg = SLORegistry()
        reg.register(SLODefinition("api", "availability", 0.999), persist=False)

        def values(window=None):
            # window: 99.95% good; last hour: 98% good (20x burn).
            return {"api:availability": 0.98 if window == "1h" else 0.9995}

        with (
            patch("sre_agent.slo_registry.get_slo_registry", return_value=reg),
            patch.object(reg, "query_prometheus_values", side_effect=values),
        ):
            burns = g._get_slo_burn_rates()
            items = g.gen_slo_burn()
        assert burns[0]["name"] == "api availability"
        assert burns[0]["burn_rate"] == 20.0
        assert burns[0]["alert_level"] == "critical"
        assert items and items[0]["severity"] == "critical"
        assert "20.0x" in items[0]["summary"]

    def test_healthy_slo_is_not_an_item(self):
        from sre_agent import inbox_generators as g
        from sre_agent.slo_registry import SLODefinition, SLORegistry

        reg = SLORegistry()
        reg.register(SLODefinition("api", "availability", 0.999), persist=False)
        with (
            patch("sre_agent.slo_registry.get_slo_registry", return_value=reg),
            patch.object(reg, "query_prometheus_values", return_value={"api:availability": 0.99999}),
        ):
            assert g._get_slo_burn_rates() == []


# ── E4: trend items ────────────────────────────────────────────────────────


class TestTrendItems:
    def test_trend_findings_come_from_the_monitor(self):
        from sre_agent import inbox_generators as g

        trend = {
            "id": "f1",
            "title": "Volume data-0 exhaustion predicted in 7 days",
            "category": "disk_pressure",
            "findingType": "trend",
            "severity": "warning",
            "resources": [],
        }
        current = {"id": "f2", "title": "Pod x crashing", "findingType": "current"}
        monitor = SimpleNamespace(
            _last_findings={"a": trend, "b": current}, _last_scanner_results=[], _noise_threshold=0.7
        )
        with patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=monitor):
            items = g.gen_trend_prediction()
        assert len(items) == 1
        assert items[0]["metadata"]["urgency_hours"] == 168

    def test_no_completed_scan_is_unavailable_not_empty(self):
        from sre_agent import inbox_generators as g

        with patch("sre_agent.monitor.cluster_monitor.get_cluster_monitor_sync", return_value=None):
            with pytest.raises(g.GeneratorUnavailableError):
                g._get_trend_findings()


# ── E5: measured values ────────────────────────────────────────────────────


def _pem(days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class TestMeasuredValues:
    def test_route_cert_expiry_is_read_from_the_certificate(self):
        from sre_agent import inbox_generators as g

        routes = {
            "items": [
                {"metadata": {"name": "soon", "namespace": "a"}, "spec": {"tls": {"certificate": _pem(3)}}},
                {"metadata": {"name": "later", "namespace": "a"}, "spec": {"tls": {"certificate": _pem(400)}}},
            ]
        }
        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.safe", return_value=routes),
        ):
            found = g._get_route_cert_expiry()
        assert [r["name"] for r in found] == ["soon"]
        assert 60 < found[0]["hours_until_expiry"] < 73

    def test_operator_degraded_duration_is_measured(self):
        from sre_agent import inbox_generators as g

        since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=5)).isoformat()
        ops = {
            "items": [
                {
                    "metadata": {"name": "dns"},
                    "status": {"conditions": [{"type": "Degraded", "status": "True", "lastTransitionTime": since}]},
                }
            ]
        }
        with (
            patch("sre_agent.k8s_client.get_custom_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.safe", return_value=ops),
        ):
            found = g._get_degraded_operators()
        assert 4.9 < found[0]["degraded_duration_hours"] < 5.1

    def test_node_pressure_reports_conditions_not_invented_numbers(self):
        from sre_agent import inbox_generators as g

        cond = SimpleNamespace(type="DiskPressure", status="True")
        node = SimpleNamespace(metadata=SimpleNamespace(name="w1"), status=SimpleNamespace(conditions=[cond]))
        with (
            patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
            patch("sre_agent.k8s_client.safe", return_value=SimpleNamespace(items=[node])),
        ):
            items = g.gen_capacity_projection()
        assert "DiskPressure" in items[0]["summary"]
        assert "95%" not in items[0]["summary"]


# ── E6: urgency ranks ──────────────────────────────────────────────────────


class TestUrgency:
    def test_items_carry_a_due_date_from_urgency(self):
        from sre_agent.inbox_generators import _make_assessment

        item = _make_assessment("t", "s", "warning", 6, "cert_expiry")
        assert abs(item["due_date"] - (time.time() + 6 * 3600)) < 5

    def test_an_urgent_item_outranks_a_relaxed_one(self):
        from sre_agent.inbox import compute_priority_score
        from sre_agent.inbox_generators import _make_assessment

        now = int(time.time())
        urgent = _make_assessment("a", "s", "warning", 2, "x")
        relaxed = _make_assessment("b", "s", "warning", 336, "x")

        def score(i):
            return compute_priority_score(i["severity"], i["confidence"], 0, now, i["due_date"])

        assert score(urgent) > score(relaxed)


# ── E7: unmet contract blocks writing phases ──────────────────────────────


class TestUnmetContract:
    def _plan(self):
        from sre_agent.skill_plan import SkillPhase, SkillPlan

        return SkillPlan(
            id="p",
            name="p",
            phases=[
                SkillPhase(id="diagnose", skill_name="sre", produces=["root_cause"]),
                SkillPhase(id="remediate", skill_name="sre", depends_on=["diagnose"]),
                SkillPhase(id="report", skill_name="readonly", depends_on=["diagnose"]),
            ],
        )

    def _runtime(self, ran):
        from sre_agent.plan_runtime import PlanRuntime
        from sre_agent.skill_plan import SkillOutput

        runtime = PlanRuntime()

        async def phase(p, incident, prior):
            ran.append(p.id)
            if p.id == "diagnose":
                return SkillOutput(
                    skill_id="sre", phase_id=p.id, status="partial", contract_missing=["root_cause"], confidence=0.3
                )
            return SkillOutput(skill_id=p.skill_name, phase_id=p.id, status="complete", confidence=0.8)

        runtime._execute_phase = phase
        return runtime

    def test_remediation_does_not_run_on_a_diagnosis_without_a_cause(self):
        ran: list[str] = []
        skills = {"sre": SimpleNamespace(write_tools=True, risk_level="low")}
        skills["readonly"] = SimpleNamespace(write_tools=False, risk_level="low")
        with patch("sre_agent.skill_loader.get_skill", side_effect=lambda n: skills.get(n)):
            result = asyncio.run(self._runtime(ran).execute(self._plan(), {}))
        assert "remediate" not in ran
        assert result.phase_outputs["remediate"].status == "needs_escalation"
        assert "root_cause" in result.phase_outputs["remediate"].evidence_summary
        assert "report" in ran, "read-only dependents still run"


# ── E8: timeline ───────────────────────────────────────────────────────────


class TestTimeline:
    def test_a_forbidden_source_is_reported_not_a_crash(self):
        from sre_agent.timeline_tools import correlate_incident

        with (
            patch("sre_agent.timeline_tools.get_core_client", return_value=MagicMock()),
            patch("sre_agent.timeline_tools.get_apps_client", return_value=MagicMock()),
            patch("sre_agent.timeline_tools.get_custom_client", side_effect=RuntimeError("no argo")),
            patch("sre_agent.timeline_tools.safe_list", return_value=_forbidden()),
        ):
            text, _component = correlate_incident.call({"namespace": "shop"})
        assert "Could not read" in text and "events" in text and "incomplete" in text

    def test_invalid_namespace_is_rejected(self):
        from sre_agent.timeline_tools import correlate_incident

        text, _ = correlate_incident.call({"namespace": "Bad Namespace!"})
        assert text.startswith("Error")


# ── E9: quota units ────────────────────────────────────────────────────────


class TestQuotaForecast:
    def test_cpu_exhaustion_uses_one_unit(self):
        from sre_agent.predict_tools import forecast_quota_exhaustion

        rq = SimpleNamespace(status=SimpleNamespace(hard={"requests.cpu": "10"}, used={"requests.cpu": "8"}))
        core = MagicMock()
        core.list_namespaced_resource_quota.return_value = SimpleNamespace(items=[rq])
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
        # Requests grow by 1 core per hour; 2 cores remain: 2 hours, not 2000.
        with (
            patch("sre_agent.predict_tools.get_core_client", return_value=core),
            patch("sre_agent.predict_tools._query_prometheus_trend", return_value=1.0),
        ):
            out = forecast_quota_exhaustion.call({"namespace": "shop"})
        assert "EXHAUSTION IN ~2h" in str(out)

    def test_namespace_is_validated_before_promql(self):
        from sre_agent.predict_tools import forecast_quota_exhaustion

        assert str(forecast_quota_exhaustion.call({"namespace": 'x"}) or vector(1'})).startswith("Error")


# ── E10: change risk knows the previous image ──────────────────────────────


class TestChangeRisk:
    def test_previous_revision_image_is_found(self):
        from sre_agent.audit_scanner import _previous_revision_image

        def rs(rev, image, owner="api"):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    owner_references=[SimpleNamespace(kind="Deployment", name=owner)],
                    annotations={"deployment.kubernetes.io/revision": rev},
                ),
                spec=SimpleNamespace(
                    template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]))
                ),
            )

        apps = MagicMock()
        apps.list_namespaced_replica_set.return_value = SimpleNamespace(
            items=[rs("3", "api:v3"), rs("2", "api:v2"), rs("2", "other:v9", owner="web")]
        )
        dep = SimpleNamespace(metadata=SimpleNamespace(namespace="shop", name="api"))
        assert _previous_revision_image(apps, dep, "3") == "api:v2"
        assert _previous_revision_image(apps, dep, "") == ""
