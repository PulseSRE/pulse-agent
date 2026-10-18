"""Pulse says what it cannot see, and what to change, in words a person can act on."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from sre_agent.monitor.scanner_health import explain_failure, scan_degraded_capabilities

SECRETS_403 = (
    'forbidden: secrets is forbidden: User "system:serviceaccount:openshiftpulse:pulse-agent" '
    'cannot list resource "secrets" in API group "" at the cluster scope'
)
OPERATORS_403 = (
    'forbidden: clusteroperators.config.openshift.io is forbidden: User "system:serviceaccount:x:y" '
    'cannot list resource "clusteroperators" in API group "config.openshift.io" at the cluster scope'
)


def test_secrets_point_at_the_opt_in_switch():
    why = explain_failure("cert_expiry", SECRETS_403)
    assert why["reason"] == "Pulse is not allowed to list secrets"
    assert "allowSecretAccess" in why["fix"]
    assert why["name"] == "Certificate Expiry" or why["name"]  # display name from the registry
    assert why["unwatched"], "say which checks are off"


def test_a_missing_role_rule_names_the_resource():
    why = explain_failure("operators", OPERATORS_403)
    assert why["reason"] == "Pulse is not allowed to list config.openshift.io/clusteroperators"
    assert "config.openshift.io/clusteroperators" in why["fix"]
    assert "pulse-operator" in why["fix"]


def test_prometheus_down_points_at_the_monitoring_stack():
    why = explain_failure("trend_memory", "Prometheus unreachable: connection refused")
    assert why["reason"] == "Pulse cannot query Prometheus"
    assert "thanos-querier" in why["fix"]


def test_unknown_errors_still_say_something_useful():
    why = explain_failure("hpa", "KeyError: 'items'")
    assert why["fix"] == "See the agent logs for this scanner"


def test_the_degraded_finding_carries_the_fix():
    with patch("sre_agent.monitor.scanner_health.consecutive_failures", return_value={"cert_expiry": (4, SECRETS_403)}):
        with patch("sre_agent.monitor.scanner_health.investigation_failure_streak", return_value=(0, "")):
            with patch("sre_agent.monitor.scanner_health.investigation_failure_rate", return_value=(0, 0, "")):
                findings = [f for f in scan_degraded_capabilities() if f["resources"][0].get("kind") == "Scanner"]
    assert len(findings) == 1
    f = findings[0]
    assert f["title"].endswith("checks are off: Pulse is not allowed to list secrets")
    assert "allowSecretAccess" in f["remedy"]
    assert "4" not in f["title"], "the streak length belongs in the summary, not the identity"
    assert "To fix:" in f["summary"]


def test_a_dashboard_opened_later_still_sees_standing_findings():
    from sre_agent.monitor.cluster_monitor import ClusterMonitor
    from sre_agent.monitor.findings import _make_finding

    class Client:
        trust_level = 1

        def __init__(self):
            self.got = []

        async def on_event(self, data):
            self.got.append(data)

    m = ClusterMonitor()
    standing = _make_finding("warning", "degraded", "Certificate Expiry checks are off", "x", [])
    noisy = _make_finding("warning", "pending", "flappy", "x", [])
    noisy["noiseScore"] = 0.9
    m._last_findings = {"a": standing, "b": noisy}
    client = Client()
    asyncio.run(m.subscribe(client))
    assert [e["id"] for e in client.got] == [standing["id"]]
