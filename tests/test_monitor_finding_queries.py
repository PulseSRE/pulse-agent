"""Real PostgreSQL contracts for authoritative monitor inbox findings."""

import json
import time

import pytest

from sre_agent.db import get_database
from sre_agent.inbox import bridge_finding_to_inbox
from sre_agent.monitor.findings import _make_finding
from sre_agent.repositories.monitor_repo import MonitorRepository
from sre_agent.repositories.skill_analytics_repo import SkillAnalyticsRepository
from tests.conftest import truncate_tables


@pytest.fixture
def db():
    database = get_database()
    truncate_tables(database, "inbox_items", "skill_selection_log")
    return database


def add_finding(category, namespace, name):
    finding = _make_finding(
        "critical", category, name, "Observed failure", [{"kind": "Deployment", "namespace": namespace, "name": name}]
    )
    return bridge_finding_to_inbox(finding)


def test_authoritative_category_resources_and_resolution(db):
    repo = MonitorRepository(db)
    crash = add_finding("crashloop", "team-a", "same")
    add_finding("scheduling", "team-b", "same")
    audit = add_finding("audit_deployment", "team-a", "rollout")
    rows = repo.fetch_active_findings()
    assert {(r["category"], r["resources"]) for r in rows} == {
        ("crashloop", "Deployment:team-a:same"),
        ("scheduling", "Deployment:team-b:same"),
        ("audit_deployment", "Deployment:team-a:rollout"),
    }
    assert repo.fetch_unresolved_finding_status() == rows
    analytics = SkillAnalyticsRepository(db)
    assert analytics.fetch_active_incident_count()["cnt"] == 2
    assert analytics.fetch_recent_deploy_count()["cnt"] == 1
    db.execute("UPDATE inbox_items SET status = 'resolved' WHERE id = ?", (crash,))
    db.execute("UPDATE inbox_items SET status = 'archived' WHERE id = ?", (audit,))
    db.commit()
    assert analytics.fetch_active_incident_count()["cnt"] == 1
    assert len(repo.fetch_active_findings()) == 1
    assert not repo.fetch_deployment_risk_findings()
    # Deployment history includes resolved/archived findings, while risk only
    # covers active findings. Inbox timestamps are seconds, unlike legacy memory.
    assert repo.fetch_recent_deployments(15)[0]["resources"] == "Deployment:team-a:rollout"
    db.execute("UPDATE inbox_items SET created_at = ? WHERE id = ?", (int(time.time()) - 3600, audit))
    db.commit()
    assert not repo.fetch_recent_deployments(15)
    assert analytics.fetch_recent_deploy_count()["cnt"] == 0


@pytest.mark.asyncio
async def test_async_projection_uses_same_real_schema(db):
    from sre_agent.async_db import reset_async_database

    add_finding("crashloop", "team", "web")
    try:
        rows = await MonitorRepository(db).async_fetch_active_findings()
        assert rows[0]["resources"] == "Deployment:team:web"
        assert rows[0]["category"] == "crashloop"
    finally:
        await reset_async_database()


def test_phase_trace_is_persisted_through_actual_runtime(db):
    from sre_agent.plan_runtime import PlanRuntime
    from sre_agent.skill_plan import SkillOutput

    PlanRuntime()._store_phase_trace(
        "query-fix",
        "diagnose",
        SkillOutput(
            skill_id="sre",
            phase_id="diagnose",
            status="complete",
            findings={"cause": "OOM"},
            evidence_summary="Observed container termination",
            confidence=0.85,
        ),
    )
    row = db.fetchone(
        "SELECT channel_scores, fused_scores, channel_weights FROM skill_selection_log WHERE session_id = ?",
        ("plan:query-fix",),
    )
    assert row is not None
    assert row["channel_scores"] == {}
    assert row["fused_scores"] == {}
    assert row["channel_weights"]["evidence"] == "Observed container termination"


def test_malformed_resource_payload_is_not_silently_empty(db):
    item = add_finding("crashloop", "team", "web")
    db.execute("UPDATE inbox_items SET resources = ? WHERE id = ?", (json.dumps({"bad": "shape"}), item))
    db.commit()
    with pytest.raises(ValueError):
        MonitorRepository(db).fetch_active_findings()
