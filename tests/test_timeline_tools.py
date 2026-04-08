"""Tests for Time Machine incident correlation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.timeline_tools import correlate_incident


def _ts(minutes_ago=5):
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def _make_event(reason="Pulled", message="Pulled image", event_type="Normal", kind="Pod", name="web-1", minutes_ago=5):
    return SimpleNamespace(
        type=event_type,
        reason=reason,
        message=message,
        last_timestamp=_ts(minutes_ago),
        event_time=None,
        involved_object=SimpleNamespace(kind=kind, name=name, namespace="default"),
    )


def _make_deploy(name="web", ns="default", progressing_minutes_ago=10):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=ns, owner_references=[]),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(
                    type="Progressing",
                    status="True",
                    last_transition_time=_ts(progressing_minutes_ago),
                    reason="NewReplicaSetAvailable",
                    message=f"ReplicaSet {name}-abc has been updated",
                ),
            ]
        ),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(image="nginx:1.25"),
                    ]
                )
            ),
        ),
    )


@pytest.fixture
def mock_timeline():
    with (
        patch("sre_agent.timeline_tools.get_core_client") as core_p,
        patch("sre_agent.timeline_tools.get_apps_client") as apps_p,
        patch("sre_agent.timeline_tools.get_custom_client") as custom_p,
    ):
        core = MagicMock()
        apps = MagicMock()
        custom = MagicMock()
        core_p.return_value = core
        apps_p.return_value = apps
        custom_p.return_value = custom

        # Default: Alertmanager not reachable, ArgoCD not installed
        core.connect_get_namespaced_service_proxy_with_path.side_effect = Exception("not reachable")
        custom.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")

        yield {"core": core, "apps": apps, "custom": custom}


class TestCorrelateIncident:
    def test_merges_events_and_deployments(self, mock_timeline):
        mock_timeline["core"].list_namespaced_event.return_value = SimpleNamespace(
            items=[
                _make_event("BackOff", "Back-off restarting", "Warning", minutes_ago=3),
            ]
        )
        mock_timeline["apps"].list_namespaced_deployment.return_value = SimpleNamespace(
            items=[
                _make_deploy("web", progressing_minutes_ago=5),
            ]
        )
        mock_timeline["apps"].list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

        text, component = correlate_incident.call({"namespace": "default", "minutes_back": 30})
        assert "BackOff" in text
        assert "web" in text
        assert "Timeline" in text
        assert component["kind"] == "timeline"
        assert len(component["lanes"]) > 0

    def test_auto_correlation(self, mock_timeline):
        # Warning event 3 min ago, deployment change 5 min ago → correlation
        mock_timeline["core"].list_namespaced_event.return_value = SimpleNamespace(
            items=[
                _make_event("Unhealthy", "Readiness probe failed", "Warning", minutes_ago=3),
            ]
        )
        mock_timeline["apps"].list_namespaced_deployment.return_value = SimpleNamespace(
            items=[
                _make_deploy("web", progressing_minutes_ago=5),
            ]
        )
        mock_timeline["apps"].list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

        text, component = correlate_incident.call({"namespace": "default", "minutes_back": 30})
        assert "PROBABLE CAUSE" in text
        assert component["kind"] == "timeline"

    def test_empty_timeline(self, mock_timeline):
        mock_timeline["core"].list_namespaced_event.return_value = SimpleNamespace(items=[])
        mock_timeline["apps"].list_namespaced_deployment.return_value = SimpleNamespace(items=[])
        mock_timeline["apps"].list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

        text, component = correlate_incident.call({"namespace": "default", "minutes_back": 5})
        assert "No events found" in text
        assert component["kind"] == "timeline"
        assert component["lanes"] == []

    def test_clamps_minutes(self, mock_timeline):
        mock_timeline["core"].list_namespaced_event.return_value = SimpleNamespace(items=[])
        mock_timeline["apps"].list_namespaced_deployment.return_value = SimpleNamespace(items=[])
        mock_timeline["apps"].list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

        # Should not crash with extreme values
        correlate_incident.call({"namespace": "default", "minutes_back": 9999})
        correlate_incident.call({"namespace": "default", "minutes_back": -5})
