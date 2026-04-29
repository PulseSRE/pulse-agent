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

    def test_no_resources_fallback(self):
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
