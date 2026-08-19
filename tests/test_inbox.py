"""Tests for inbox item CRUD, lifecycle, priority, and dedup."""

import time

import pytest


@pytest.fixture(autouse=True)
def _clean_inbox():
    """Clear inbox_items between tests to prevent cross-test pollution."""
    yield
    try:
        from sre_agent.db import get_database

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
    except Exception:
        pass


def _make_item(**overrides):
    """Helper to create an inbox item dict with defaults."""
    defaults = {
        "item_type": "task",
        "title": "Pod crashlooping",
        "summary": "payment-api pod restarting every 30s",
        "severity": "critical",
        "confidence": 0.9,
        "noise_score": 0.0,
        "namespace": "production",
        "resources": [{"kind": "Pod", "name": "payment-api-abc", "namespace": "production"}],
        "correlation_key": "crashloop:payment-api:production",
        "created_by": "system:monitor",
    }
    defaults.update(overrides)
    return defaults


class TestInboxCRUD:
    def test_create_item(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item

        item = _make_item()
        item_id = create_inbox_item(item)
        assert item_id is not None
        assert item_id.startswith("inb-")

        fetched = get_inbox_item(item_id)
        assert fetched is not None
        assert fetched["title"] == "Pod crashlooping"
        assert fetched["status"] == "new"
        assert fetched["item_type"] == "task"

    def test_list_items_with_filters(self):
        from sre_agent.inbox import create_inbox_item, list_inbox_items

        create_inbox_item(_make_item(title="Finding 1", item_type="task"))
        create_inbox_item(_make_item(title="Task 1", item_type="task", severity=None))
        create_inbox_item(_make_item(title="Alert 1", item_type="task"))

        all_items = list_inbox_items()
        assert len(all_items["items"]) >= 3

        findings = list_inbox_items(item_type="task")
        assert all(i["item_type"] == "task" for i in findings["items"])

        tasks = list_inbox_items(item_type="task")
        assert all(i["item_type"] == "task" for i in tasks["items"])

    def test_list_items_excludes_snoozed(self):
        from sre_agent.inbox import create_inbox_item, list_inbox_items, snooze_item

        item_id = create_inbox_item(_make_item(title="Snoozed item"))
        snooze_item(item_id, hours=24)

        items = list_inbox_items()
        ids = [i["id"] for i in items["items"]]
        assert item_id not in ids

    def test_update_status(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item())
        ok = update_item_status(item_id, "triaged")
        assert ok is True

        fetched = get_inbox_item(item_id)
        assert fetched["status"] == "triaged"

    def test_stats(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_stats

        create_inbox_item(_make_item(title="S1"))
        create_inbox_item(_make_item(title="S2"))

        stats = get_inbox_stats()
        assert stats["new"] >= 2
        assert "total" in stats


class TestNeedsAttentionFilter:
    """Regression tests: newly-bridged monitor findings (status='new') and
    items still in the async agent triage pipeline ('agent_reviewing',
    'agent_review_failed') must appear in the default '__needs_attention__'
    view. Otherwise the Inbox shows zero items right after a scan even though
    the cluster posture banner reports active critical findings, because
    bridge_finding_to_inbox() creates items in 'new' status and the LLM
    triage/investigate pipeline that advances them to 'triaged' runs
    asynchronously (and can be slow, rate-limited, or fail outright).
    """

    def test_new_critical_finding_appears_in_needs_attention(self):
        from sre_agent.inbox import bridge_finding_to_inbox, list_inbox_items

        finding = {
            "id": "f-crit-1",
            "category": "crashloop",
            "namespace": "production",
            "title": "Pod acm-mcp-server restarting",
            "summary": "acm-mcp-server pod restarting (3x)",
            "severity": "critical",
            "confidence": 0.9,
            "resources": [{"kind": "Pod", "name": "acm-mcp-server-abc123", "namespace": "production"}],
        }
        item_id = bridge_finding_to_inbox(finding)

        result = list_inbox_items(status="__needs_attention__")
        ids = [i["id"] for i in result["items"]]
        assert item_id in ids
        assert result["total"] >= 1

    def test_agent_reviewing_item_stays_in_needs_attention(self):
        from sre_agent.inbox import create_inbox_item, list_inbox_items, update_item_status

        item_id = create_inbox_item(_make_item(title="Reviewing item"))
        assert update_item_status(item_id, "agent_reviewing")

        result = list_inbox_items(status="__needs_attention__")
        ids = [i["id"] for i in result["items"]]
        assert item_id in ids

    def test_agent_review_failed_item_stays_in_needs_attention(self):
        """A failed AI triage must surface to a human, not vanish silently."""
        from sre_agent.inbox import create_inbox_item, list_inbox_items, update_item_status

        item_id = create_inbox_item(_make_item(title="Failed triage"))
        assert update_item_status(item_id, "agent_review_failed")

        result = list_inbox_items(status="__needs_attention__")
        ids = [i["id"] for i in result["items"]]
        assert item_id in ids

    def test_agent_cleared_and_resolved_excluded_from_needs_attention(self):
        from sre_agent.inbox import create_inbox_item, list_inbox_items, update_item_status

        cleared_id = create_inbox_item(_make_item(title="Cleared item"))
        update_item_status(cleared_id, "agent_cleared")

        resolved_id = create_inbox_item(_make_item(title="Resolved item"))
        update_item_status(resolved_id, "triaged")
        update_item_status(resolved_id, "claimed")
        update_item_status(resolved_id, "in_progress")
        update_item_status(resolved_id, "resolved")

        result = list_inbox_items(status="__needs_attention__")
        ids = [i["id"] for i in result["items"]]
        assert cleared_id not in ids
        assert resolved_id not in ids

    def test_stats_needs_attention_includes_new_items(self):
        from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_stats

        finding = {
            "id": "f-crit-2",
            "category": "oom",
            "namespace": "production",
            "title": "Pod OOMKilled",
            "summary": "payment-api OOMKilled",
            "severity": "critical",
            "confidence": 0.85,
            "resources": [{"kind": "Pod", "name": "payment-api-xyz", "namespace": "production"}],
        }
        bridge_finding_to_inbox(finding)

        stats = get_inbox_stats()
        assert stats.get("new", 0) >= 1
        assert stats["needs_attention"] >= stats["new"]


class TestLifecycle:
    def test_simplified_lifecycle(self):
        """All item types share the same lifecycle: new → triaged → claimed → in_progress → resolved."""
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        for item_type in ("task",):
            item_id = create_inbox_item(_make_item(item_type=item_type, title=f"{item_type} lifecycle"))
            assert update_item_status(item_id, "triaged")
            assert update_item_status(item_id, "claimed")
            assert update_item_status(item_id, "in_progress")
            assert update_item_status(item_id, "resolved")
            assert get_inbox_item(item_id)["status"] == "resolved"

    def test_invalid_transition(self):
        from sre_agent.inbox import create_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task"))
        assert update_item_status(item_id, "in_progress") is False

    def test_agent_pipeline_lifecycle(self):
        """Agent pipeline: new → agent_reviewing → triaged → claimed → in_progress → resolved."""
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task"))
        assert update_item_status(item_id, "agent_reviewing")
        assert update_item_status(item_id, "triaged")
        assert update_item_status(item_id, "claimed")
        assert update_item_status(item_id, "in_progress")
        assert update_item_status(item_id, "resolved")
        assert get_inbox_item(item_id)["status"] == "resolved"

    def test_assessment_escalation(self):
        from sre_agent.inbox import (
            create_inbox_item,
            escalate_assessment,
            get_inbox_item,
            update_item_status,
        )

        item_id = create_inbox_item(_make_item(item_type="task", title="Memory trend"))
        assert update_item_status(item_id, "triaged") is True

        finding_id = escalate_assessment(item_id)
        assert finding_id is not None

        old = get_inbox_item(item_id)
        assert old["status"] == "resolved"

        new_finding = get_inbox_item(finding_id)
        assert new_finding["item_type"] == "task"
        assert new_finding["status"] == "new"
        assert new_finding["metadata"].get("escalated_from") == item_id


class TestPriorityScore:
    def test_critical_scores_higher_than_warning(self):
        from sre_agent.inbox import compute_priority_score

        critical = compute_priority_score(
            severity="critical",
            confidence=0.9,
            noise_score=0.0,
            created_at=int(time.time()),
            due_date=None,
        )
        warning = compute_priority_score(
            severity="warning",
            confidence=0.9,
            noise_score=0.0,
            created_at=int(time.time()),
            due_date=None,
        )
        assert critical > warning

    def test_noise_reduces_score(self):
        from sre_agent.inbox import compute_priority_score

        clean = compute_priority_score(
            severity="warning",
            confidence=0.9,
            noise_score=0.0,
            created_at=int(time.time()),
            due_date=None,
        )
        noisy = compute_priority_score(
            severity="warning",
            confidence=0.9,
            noise_score=0.8,
            created_at=int(time.time()),
            due_date=None,
        )
        assert clean > noisy

    def test_due_date_bonus(self):
        from sre_agent.inbox import compute_priority_score

        now = int(time.time())
        due_soon = compute_priority_score(
            severity="info",
            confidence=0.5,
            noise_score=0.0,
            created_at=now,
            due_date=now + 3600 * 12,
        )
        no_due = compute_priority_score(
            severity="info",
            confidence=0.5,
            noise_score=0.0,
            created_at=now,
            due_date=None,
        )
        assert due_soon > no_due

    def test_age_bonus(self):
        from sre_agent.inbox import compute_priority_score

        now = int(time.time())
        old = compute_priority_score(
            severity="warning",
            confidence=0.9,
            noise_score=0.0,
            created_at=now - 3600 * 10,
            due_date=None,
        )
        fresh = compute_priority_score(
            severity="warning",
            confidence=0.9,
            noise_score=0.0,
            created_at=now,
            due_date=None,
        )
        assert old > fresh


class TestDedup:
    def test_upsert_updates_existing(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, upsert_inbox_item

        item_id = create_inbox_item(
            _make_item(
                title="Crashloop",
                correlation_key="crashloop:api:prod",
            )
        )

        upsert_id = upsert_inbox_item(
            _make_item(
                title="Crashloop",
                correlation_key="crashloop:api:prod",
                resources=[{"kind": "Pod", "name": "api-xyz", "namespace": "prod"}],
            )
        )
        assert upsert_id == item_id

        fetched = get_inbox_item(item_id)
        assert len(fetched["resources"]) == 2

    def test_upsert_creates_new_for_different_key(self):
        from sre_agent.inbox import create_inbox_item, upsert_inbox_item

        create_inbox_item(_make_item(correlation_key="crashloop:api:prod"))

        new_id = upsert_inbox_item(_make_item(correlation_key="crashloop:web:prod"))
        assert new_id is not None


class TestSnooze:
    def test_snooze_and_unsnooze(self):
        from sre_agent.inbox import (
            create_inbox_item,
            get_inbox_item,
            snooze_item,
            unsnooze_expired,
            update_item_status,
        )

        item_id = create_inbox_item(_make_item(title="Snooze test"))
        update_item_status(item_id, "triaged")

        snooze_item(item_id, hours=0)

        unsnooze_expired()

        fetched = get_inbox_item(item_id)
        assert fetched["status"] == "triaged"
        assert fetched["snoozed_until"] is None


class TestCorrelation:
    def test_grouped_response(self):
        from sre_agent.inbox import create_inbox_item, list_inbox_items

        for i in range(3):
            create_inbox_item(
                _make_item(
                    title=f"Crashloop pod-{i}",
                    correlation_key="crashloop:payment:prod",
                    resources=[{"kind": "Pod", "name": f"pod-{i}", "namespace": "prod"}],
                )
            )

        create_inbox_item(
            _make_item(
                title="Unrelated task",
                item_type="task",
                correlation_key=None,
            )
        )

        result = list_inbox_items(group_by="correlation")
        assert len(result["groups"]) >= 1
        group = next(g for g in result["groups"] if g["correlation_key"] == "crashloop:payment:prod")
        assert group["count"] == 3
        assert group["top_severity"] == "critical"


class TestCleanup:
    def test_prune_old_resolved(self):
        import sre_agent.inbox as _inbox_mod
        from sre_agent.inbox import (
            create_inbox_item,
            prune_old_items,
            update_item_status,
        )

        _inbox_mod._last_prune_time = 0

        item_id = create_inbox_item(_make_item(title="Old resolved"))
        update_item_status(item_id, "triaged")
        update_item_status(item_id, "claimed")
        update_item_status(item_id, "in_progress")
        update_item_status(item_id, "resolved")

        from sre_agent.db import get_database

        db = get_database()
        old_ts = int(time.time()) - 31 * 86400
        db.execute(
            "UPDATE inbox_items SET resolved_at = ? WHERE id = ?",
            (old_ts, item_id),
        )
        db.commit()

        pruned = prune_old_items(max_age_days=30)
        assert pruned >= 1


class TestAgentTool:
    def test_create_inbox_task_tool(self):
        from sre_agent.inbox import create_inbox_task

        result = create_inbox_task(
            title="Rotate TLS certs",
            detail="Certs expire in 5 days on ingress controller",
            urgency="this_week",
            namespace="production",
        )
        assert "created" in result.lower() or "inb-" in result.lower()

    def test_create_inbox_task_urgency_today(self):
        from sre_agent.inbox import create_inbox_task

        result = create_inbox_task(title="Urgent task", urgency="today")
        assert "inb-" in result

    def test_create_inbox_task_invalid_urgency(self):
        from sre_agent.inbox import create_inbox_task

        result = create_inbox_task(title="Bad urgency", urgency="invalid")
        assert "error" in result.lower() or "invalid" in result.lower()


class TestNoDeadEndStatuses:
    """Every status must have at least one valid forward transition."""

    def test_all_statuses_have_forward_transitions(self):
        from sre_agent.inbox import VALID_TRANSITIONS

        for item_type, transitions in VALID_TRANSITIONS.items():
            for status, next_statuses in transitions.items():
                assert len(next_statuses) > 0, f"{item_type}/{status} has no forward transitions — dead end!"

    def test_unified_lifecycle_single_type(self):
        """Single 'task' type with unified transitions."""
        from sre_agent.inbox import VALID_TRANSITIONS

        assert "task" in VALID_TRANSITIONS
        assert len(VALID_TRANSITIONS["task"]) > 0

    def test_full_lifecycle_forward(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task"))

        path = ["agent_reviewing", "triaged", "claimed", "in_progress", "resolved"]
        for step in path:
            ok = update_item_status(item_id, step)
            assert ok, f"Could not transition to {step}"
        assert get_inbox_item(item_id)["status"] == "resolved"

    def test_agent_cleared_and_restore(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, restore_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task"))
        assert update_item_status(item_id, "agent_cleared")
        assert get_inbox_item(item_id)["status"] == "agent_cleared"

        assert restore_item(item_id)
        assert get_inbox_item(item_id)["status"] == "new"

    def test_agent_review_failed_recovery(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task", title="Failed review"))
        assert update_item_status(item_id, "agent_review_failed")
        assert get_inbox_item(item_id)["status"] == "agent_review_failed"
        assert update_item_status(item_id, "new")
        assert get_inbox_item(item_id)["status"] == "new"

    def test_agent_review_failed_to_triaged(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(_make_item(item_type="task", title="Failed then manual"))
        assert update_item_status(item_id, "agent_review_failed")
        assert update_item_status(item_id, "triaged")
        assert get_inbox_item(item_id)["status"] == "triaged"

    def test_claim_sets_status_and_view(self):
        from sre_agent.inbox import claim_item, create_inbox_item, get_inbox_item, update_item_status

        item_id = create_inbox_item(
            _make_item(
                title="Claim with investigation",
                metadata={
                    "investigation_summary": "Found issue",
                    "action_plan": [{"title": "Fix it", "status": "pending"}],
                },
            )
        )
        update_item_status(item_id, "triaged")
        claim_item(item_id, "test-user")
        item = get_inbox_item(item_id)
        assert item["claimed_by"] == "test-user"
        assert item["status"] == "claimed"
        assert item["metadata"].get("view_status") in ("generating", "ready", "failed")

    def test_action_plan_in_metadata(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item

        plan = [
            {"title": "Step 1", "description": "Do thing", "tool": None, "risk": "low", "status": "pending"},
            {
                "title": "Step 2",
                "description": "Do other",
                "tool": "scale_deployment",
                "risk": "medium",
                "status": "pending",
            },
        ]
        item_id = create_inbox_item(_make_item(title="With plan", metadata={"action_plan": plan}))
        item = get_inbox_item(item_id)
        assert len(item["metadata"]["action_plan"]) == 2
        assert item["metadata"]["action_plan"][0]["status"] == "pending"


class TestResolveFindingInboxItem:
    """Tests for resolve_finding_inbox_item linking finding→inbox resolution."""

    def test_resolves_linked_inbox_item(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, resolve_finding_inbox_item

        item_id = create_inbox_item(_make_item(finding_id="f-test-123"))
        assert get_inbox_item(item_id)["status"] == "new"

        resolved = resolve_finding_inbox_item("f-test-123")
        assert resolved is True
        item = get_inbox_item(item_id)
        assert item["status"] == "resolved"
        assert item["resolved_at"] is not None

    def test_noop_when_no_linked_item(self):
        from sre_agent.inbox import resolve_finding_inbox_item

        resolved = resolve_finding_inbox_item("f-nonexistent")
        assert resolved is False

    def test_skips_already_resolved(self):
        from sre_agent.db import get_database
        from sre_agent.inbox import create_inbox_item, resolve_finding_inbox_item

        item_id = create_inbox_item(_make_item(finding_id="f-already-done"))
        db = get_database()
        now = int(time.time())
        db.execute(
            "UPDATE inbox_items SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (now, item_id),
        )
        db.commit()

        resolved = resolve_finding_inbox_item("f-already-done")
        assert resolved is False


class TestResourceExists:
    """Tests for _resource_exists pre-flight check."""

    def test_returns_true_for_unknown_kind(self):
        from sre_agent.inbox import _resource_exists

        assert _resource_exists({"kind": "CustomThing", "name": "x", "namespace": "default"}) is True

    def test_returns_true_for_empty_resource(self):
        from sre_agent.inbox import _resource_exists

        assert _resource_exists({}) is True

    def test_returns_false_on_404(self):
        from unittest.mock import patch

        from kubernetes.client.rest import ApiException

        from sre_agent.inbox import _resource_exists

        with patch("sre_agent.k8s_client.get_core_client") as mock_core:
            mock_core.return_value.read_namespaced_pod.side_effect = ApiException(status=404)
            assert _resource_exists({"kind": "Pod", "name": "gone-pod", "namespace": "default"}) is False

    def test_returns_true_on_non_404_error(self):
        from unittest.mock import patch

        from kubernetes.client.rest import ApiException

        from sre_agent.inbox import _resource_exists

        with patch("sre_agent.k8s_client.get_core_client") as mock_core:
            mock_core.return_value.read_namespaced_pod.side_effect = ApiException(status=403)
            assert _resource_exists({"kind": "Pod", "name": "forbidden-pod", "namespace": "default"}) is True

    def test_returns_true_on_success(self):
        from unittest.mock import MagicMock, patch

        from sre_agent.inbox import _resource_exists

        with patch("sre_agent.k8s_client.get_core_client") as mock_core:
            mock_core.return_value.read_namespaced_pod.return_value = MagicMock()
            assert _resource_exists({"kind": "Pod", "name": "alive-pod", "namespace": "default"}) is True


class TestMergeResources:
    """Tests for _merge_resources cap and dedup."""

    def test_deduplicates_by_kind_name_namespace(self):
        from sre_agent.inbox import _merge_resources

        existing = [{"kind": "Pod", "name": "a", "namespace": "ns"}]
        new = [{"kind": "Pod", "name": "a", "namespace": "ns"}]
        result = _merge_resources(existing, new)
        assert len(result) == 1

    def test_new_resources_come_first(self):
        from sre_agent.inbox import _merge_resources

        existing = [{"kind": "Pod", "name": "old", "namespace": "ns"}]
        new = [{"kind": "Pod", "name": "new", "namespace": "ns"}]
        result = _merge_resources(existing, new)
        assert result[0]["name"] == "new"
        assert result[1]["name"] == "old"

    def test_caps_at_max(self):
        from sre_agent.inbox import _MAX_RESOURCES, _merge_resources

        existing = [{"kind": "Pod", "name": f"old-{i}", "namespace": "ns"} for i in range(8)]
        new = [{"kind": "Pod", "name": f"new-{i}", "namespace": "ns"} for i in range(8)]
        result = _merge_resources(existing, new)
        assert len(result) == _MAX_RESOURCES
        assert result[0]["name"] == "new-0"


class TestFindingCorrKey:
    """Tests for correlation key narrowing."""

    def test_includes_primary_resource(self):
        from sre_agent.inbox import _finding_corr_key

        finding = {
            "category": "crashloop",
            "namespace": "prod",
            "resources": [{"kind": "Deployment", "name": "web", "namespace": "prod"}],
        }
        key = _finding_corr_key(finding)
        assert key == "crashloop:prod:Deployment/web"

    def test_strips_pod_hash(self):
        from sre_agent.inbox import _finding_corr_key

        finding = {
            "category": "crashloop",
            "namespace": "prod",
            "resources": [{"kind": "Pod", "name": "web-abc123-xyz", "namespace": "prod"}],
        }
        key = _finding_corr_key(finding)
        assert "abc123-xyz" not in key
        assert "crashloop:prod:Pod/" in key

    def test_no_resources_with_title(self):
        from sre_agent.inbox import _finding_corr_key

        finding = {"category": "security", "namespace": "", "title": "Security: Resource Limits"}
        key = _finding_corr_key(finding)
        assert key == "security::Security: Resource Limits"

    def test_no_resources_no_title_fallback(self):
        from sre_agent.inbox import _finding_corr_key

        finding = {"category": "alerts", "namespace": "monitoring"}
        key = _finding_corr_key(finding)
        assert key == "alerts:monitoring"

    def test_different_resources_different_keys(self):
        from sre_agent.inbox import _finding_corr_key

        f1 = {
            "category": "crashloop",
            "namespace": "prod",
            "resources": [{"kind": "Pod", "name": "api-abc-123", "namespace": "prod"}],
        }
        f2 = {
            "category": "crashloop",
            "namespace": "prod",
            "resources": [{"kind": "Pod", "name": "worker-def-456", "namespace": "prod"}],
        }
        assert _finding_corr_key(f1) != _finding_corr_key(f2)


class TestBridgeReopensResolved:
    """bridge_finding_to_inbox should reopen a recently-resolved item instead of creating a duplicate."""

    def test_reopens_recently_resolved_item(self):
        from sre_agent.inbox import bridge_finding_to_inbox, get_inbox_item
        from sre_agent.repositories.inbox_repo import get_inbox_repo

        finding = {
            "id": "f-sec-1",
            "category": "security",
            "namespace": "",
            "title": "Security: Resource Limits",
            "summary": "5 containers without limits",
            "severity": "warning",
            "confidence": 0.8,
            "resources": [],
        }
        item_id = bridge_finding_to_inbox(finding)
        assert item_id.startswith("inb-")

        # Resolve the item
        repo = get_inbox_repo()
        now = int(time.time())
        repo.update_status(item_id, "resolved", now)

        # Same finding recurs — should reopen, not create new
        finding["id"] = "f-sec-2"
        reopened_id = bridge_finding_to_inbox(finding)
        assert reopened_id == item_id

        item = get_inbox_item(reopened_id)
        assert item["status"] == "new"

    def test_creates_new_if_resolved_too_long_ago(self):
        from sre_agent.inbox import bridge_finding_to_inbox
        from sre_agent.repositories.inbox_repo import get_inbox_repo

        finding = {
            "id": "f-sec-3",
            "category": "security",
            "namespace": "",
            "title": "Security: Resource Limits",
            "summary": "old finding",
            "severity": "warning",
            "confidence": 0.8,
            "resources": [],
        }
        item_id = bridge_finding_to_inbox(finding)

        # Resolve the item and backdate it beyond the 1-hour window
        repo = get_inbox_repo()
        old_time = int(time.time()) - 7200
        repo.update_status(item_id, "resolved", old_time)
        repo.db.execute("UPDATE inbox_items SET updated_at = ? WHERE id = ?", (old_time, item_id))
        repo.db.commit()

        # Same finding recurs — should create new (old one is too stale)
        finding["id"] = "f-sec-4"
        new_id = bridge_finding_to_inbox(finding)
        assert new_id != item_id


class TestSweepStaleItems:
    """Tests for startup sweep of stale agent_reviewing items."""

    def test_sweep_resets_stale_agent_reviewing(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, sweep_stale_items
        from sre_agent.repositories.inbox_repo import get_inbox_repo

        item = _make_item(title="Stale item")
        item_id = create_inbox_item(item)
        repo = get_inbox_repo()
        stale_time = int(time.time()) - 600
        repo.update_triage_result(item_id, "agent_reviewing", {"triaged": True}, "summary", stale_time)
        repo.db.execute("UPDATE inbox_items SET updated_at = ? WHERE id = ?", (stale_time, item_id))
        repo.db.commit()

        swept = sweep_stale_items()
        assert swept == 1
        result = get_inbox_item(item_id)
        assert result["status"] == "new"
        assert result.get("metadata", {}).get("triaged") is None

    def test_sweep_ignores_recently_updated(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_item, sweep_stale_items
        from sre_agent.repositories.inbox_repo import get_inbox_repo

        item = _make_item(title="Fresh item")
        item_id = create_inbox_item(item)
        repo = get_inbox_repo()
        now = int(time.time())
        repo.update_triage_result(item_id, "agent_reviewing", {}, "summary", now)

        swept = sweep_stale_items()
        assert swept == 0
        result = get_inbox_item(item_id)
        assert result["status"] == "agent_reviewing"

    def test_sweep_handles_empty_table(self):
        from sre_agent.inbox import sweep_stale_items

        swept = sweep_stale_items()
        assert swept == 0


class TestStatsDedup:
    """Tests for unique_issues dedup in stats."""

    def test_unique_issues_less_than_total(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_stats
        from sre_agent.repositories.inbox_repo import get_inbox_repo

        repo = get_inbox_repo()
        now = int(time.time())
        for i in range(3):
            item = _make_item(title=f"Dup {i}", correlation_key="same:key")
            create_inbox_item(item)
        item = _make_item(title="Unique", correlation_key="other:key")
        create_inbox_item(item)

        # Transition all to triaged so they count in needs_attention
        for row in repo.db.fetchall("SELECT id FROM inbox_items"):
            repo.update_triage_result(row["id"], "triaged", {}, "s", now)

        stats = get_inbox_stats()
        assert stats["total"] == 4
        assert stats["unique_issues"] == 2


class TestCorrelationKeyDeterministic:
    """Verify generator correlation keys are stable."""

    def test_make_assessment_deterministic(self):
        from sre_agent.inbox_generators import _make_assessment

        item1 = _make_assessment(
            title="Test",
            summary="test",
            severity="info",
            urgency_hours=24,
            generator="cert_expiry",
            namespace="prod",
        )
        item2 = _make_assessment(
            title="Test",
            summary="test",
            severity="info",
            urgency_hours=24,
            generator="cert_expiry",
            namespace="prod",
        )
        assert item1["correlation_key"] == item2["correlation_key"]
        assert item1["correlation_key"] == "cert_expiry:prod"

    def test_cluster_scoped_key(self):
        from sre_agent.inbox_generators import _make_assessment

        item = _make_assessment(
            title="Test",
            summary="test",
            severity="info",
            urgency_hours=24,
            generator="privileged_workloads",
            namespace=None,
        )
        assert item["correlation_key"] == "privileged_workloads:cluster"


class TestInboxStalenessAndCorrelation:
    """Regressions found by inspecting a live cluster's inbox, not from tests."""

    def test_correlation_key_includes_the_namespace(self):
        """Two same-named workloads in different namespaces must not collide.

        _make_finding never sets a top-level "namespace" — scanners put it in
        resources[0] — so _finding_corr_key produced "crashloop::Pod/name" for
        everything. On the live cluster every key had an empty namespace
        segment, meaning e.g. a "controller-manager" in two operator
        namespaces would share one inbox item.
        """
        from sre_agent.inbox import _finding_corr_key

        a = _finding_corr_key(
            {
                "category": "crashloop",
                "resources": [{"kind": "Pod", "name": "controller-manager-abc12-x9f2j", "namespace": "ns-a"}],
            }
        )
        b = _finding_corr_key(
            {
                "category": "crashloop",
                "resources": [{"kind": "Pod", "name": "controller-manager-abc12-q4t7z", "namespace": "ns-b"}],
            }
        )
        assert a != b, "same workload name in different namespaces must not share a correlation key"
        assert "ns-a" in a and "ns-b" in b
        # Pod hash still stripped, so the key survives pod rotation.
        assert a.endswith("Pod/controller-manager")

    def test_correlation_key_survives_pod_rotation(self):
        """The key must be stable when the ReplicaSet spins a new pod."""
        from sre_agent.inbox import _finding_corr_key

        base = {"category": "crashloop"}
        first = _finding_corr_key(
            {**base, "resources": [{"kind": "Pod", "name": "discovery-operator-564dcd46cb-qrlql", "namespace": "mce"}]}
        )
        second = _finding_corr_key(
            {**base, "resources": [{"kind": "Pod", "name": "discovery-operator-564dcd46cb-kplj2", "namespace": "mce"}]}
        )
        assert first == second, "a new pod for the same workload must reuse the existing item"


class TestNoveltyRanking:
    """Something that just started must outrank something true for a month."""

    def test_fresh_item_outranks_a_stale_one_of_equal_severity(self):
        import time

        from sre_agent.inbox import compute_priority_score

        now = int(time.time())
        fresh = compute_priority_score("warning", 0.95, 0.0, now - 600, None)
        week_old = compute_priority_score("warning", 0.8, 0.3, now - 7 * 86400, None)
        assert fresh > week_old, (
            "age_bonus alone ranked a week-old warning above a fresh one, which is how "
            "permanent conditions drift to the top of the inbox and stay there"
        )

    def test_novelty_decays_so_ranking_settles(self):
        """After the window the bonus is gone and ordering is unchanged from before."""
        import time

        from sre_agent.inbox import NOVELTY_WINDOW_HOURS, compute_priority_score

        now = int(time.time())
        past_window = now - int((NOVELTY_WINDOW_HOURS + 1) * 3600)
        with_novelty = compute_priority_score("warning", 0.9, 0.0, past_window, None)
        baseline = 2 * 0.9 + min(((NOVELTY_WINDOW_HOURS + 1) * 0.1), 2.0)
        assert abs(with_novelty - baseline) < 0.01, "novelty must decay to zero, not linger"

    def test_severity_still_dominates(self):
        """Novelty must not let a fresh info item outrank a fresh critical."""
        import time

        from sre_agent.inbox import compute_priority_score

        now = int(time.time())
        fresh_info = compute_priority_score("info", 1.0, 0.0, now, None)
        fresh_critical = compute_priority_score("critical", 0.9, 0.0, now, None)
        assert fresh_critical > fresh_info


class TestInboxMute:
    """An operator must be able to say 'I know, stop telling me'."""

    def test_muted_condition_is_never_created(self, monkeypatch):
        from sre_agent import inbox

        class _Repo:
            def is_muted(self, key, now):
                return key == "crashloop:ns:Pod/known-flapper"

        monkeypatch.setattr(inbox, "get_inbox_repo", lambda: _Repo())
        created = inbox.upsert_inbox_item(
            {"item_type": "task", "title": "x", "correlation_key": "crashloop:ns:Pod/known-flapper"}
        )
        assert created == "", "a muted condition must not produce an item at all"

    def test_unmuted_condition_is_unaffected(self, monkeypatch):
        """The mute check must not swallow everything else."""
        from sre_agent import inbox

        seen = {}

        class _Repo:
            def is_muted(self, key, now):
                return False

            def find_active_by_correlation(self, key, item_type):
                seen["looked_up"] = True
                return None

            def find_recently_resolved(self, key, item_type, since):
                # Real path checks this when no active item matches, so the
                # double has to answer it too.
                return None

        monkeypatch.setattr(inbox, "get_inbox_repo", lambda: _Repo())
        monkeypatch.setattr(inbox, "create_inbox_item", lambda item: "inb-created")
        result = inbox.upsert_inbox_item(
            {"item_type": "task", "title": "x", "correlation_key": "crashloop:ns:Pod/real"}
        )
        assert result == "inb-created"
        assert seen.get("looked_up"), "an unmuted key must still go through normal correlation"
