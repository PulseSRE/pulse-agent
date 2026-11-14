"""Tests for monitor -> inbox integration."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_inbox():
    """Clear inbox_items between tests."""
    yield
    try:
        from sre_agent.db import get_database

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
    except Exception:
        pass


class TestFindingToInbox:
    def test_finding_creates_inbox_item(self):
        from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item

        finding = {
            "id": "f-test-001",
            "title": "Pod crashlooping",
            "severity": "critical",
            "category": "availability",
            "confidence": 0.95,
            "noiseScore": 0.0,
            "resources": [{"kind": "Pod", "name": "api-abc", "namespace": "prod"}],
            "namespace": "prod",
        }
        item_id = bridge_finding_to_inbox(finding)
        assert item_id is not None

        item = get_inbox_item(item_id)
        assert item["item_type"] == "task"
        assert item["finding_id"] == "f-test-001"
        assert item["severity"] == "critical"

    def test_finding_checklist_is_kept_as_steps(self):
        """Cloud-credential findings carry their fix as steps; the item keeps them separate."""
        from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item

        finding = {
            "id": "f-cco-001",
            "title": "AWS EBS CSI driver cannot get its cloud credentials (InsufficientCloudCreds)",
            "summary": "explanation",
            "severity": "critical",
            "category": "cloud_credentials",
            "confidence": 0.95,
            "noiseScore": 0.0,
            "resources": [
                {
                    "kind": "CredentialsRequest",
                    "name": "aws-ebs-csi-driver-operator",
                    "namespace": "openshift-cloud-credential-operator",
                }
            ],
            "checklist": ["Add the permissions", "Do not delete the Secret"],
        }
        item = get_inbox_item(bridge_finding_to_inbox(finding))
        assert item["metadata"]["checklist"] == ["Add the permissions", "Do not delete the Secret"]
        assert item["metadata"]["finding_category"] == "cloud_credentials"

    def test_duplicate_finding_updates_existing(self):
        from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item

        finding = {
            "id": "f-dup-001",
            "title": "OOM killed",
            "severity": "warning",
            "category": "availability",
            "confidence": 0.8,
            "noiseScore": 0.1,
            "resources": [{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
            "namespace": "prod",
        }
        id1 = bridge_finding_to_inbox(finding)

        finding["resources"] = [{"kind": "Pod", "name": "web-2", "namespace": "prod"}]
        id2 = bridge_finding_to_inbox(finding)

        assert id1 == id2
        item = get_inbox_item(id1)
        assert len(item["resources"]) == 2

    def test_finding_resolution_resolves_inbox_item(self):
        from sre_agent.inbox import (
            bridge_finding_to_inbox,
            get_inbox_item,
            resolve_finding_inbox_item,
            update_item_status,
        )

        finding = {
            "id": "f-resolve-001",
            "title": "Pending pod",
            "severity": "warning",
            "category": "scheduling",
            "confidence": 0.9,
            "noiseScore": 0.0,
            "resources": [],
            "namespace": "default",
        }
        item_id = bridge_finding_to_inbox(finding)
        update_item_status(item_id, "triaged")
        update_item_status(item_id, "claimed")
        update_item_status(item_id, "in_progress")

        resolve_finding_inbox_item("f-resolve-001")

        item = get_inbox_item(item_id)
        assert item["status"] == "resolved"


class TestGeneratorScanIntegration:
    @patch("sre_agent.inbox_generators.run_all_generators")
    def test_generators_upsert_items(self, mock_generators):
        from sre_agent.inbox import list_inbox_items, run_generator_cycle

        mock_generators.return_value = [
            {
                "item_type": "task",
                "title": "Cert expiring in 48h",
                "summary": "Renew cert",
                "severity": "warning",
                "confidence": 0.9,
                "noise_score": 0,
                "namespace": "prod",
                "resources": [],
                "correlation_key": "cert_expiry:api-cert:prod",
                "created_by": "system:monitor",
                "metadata": {"generator": "cert_expiry", "urgency_hours": 48},
            },
        ]
        run_generator_cycle()

        items = list_inbox_items(item_type="task")
        assert len(items["items"]) >= 1

    @patch("sre_agent.inbox_generators.run_all_generators")
    def test_generator_auto_resolve_cleared(self, mock_generators):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, run_generator_cycle

        item_id = create_inbox_item(
            {
                "item_type": "task",
                "title": "Was expiring",
                "created_by": "system:monitor",
                "correlation_key": "cert_expiry:old-cert:prod",
                "metadata": {"generator": "cert_expiry"},
            }
        )

        mock_generators.return_value = []
        run_generator_cycle()

        item = get_inbox_item(item_id)
        assert item["status"] == "resolved"
