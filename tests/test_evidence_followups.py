"""Follow-ups from the functionality review: confidence only from evidence that was read,
and structured evidence that survives being saved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sre_agent.evidence import Evidence, derive_confidence, ground_evidence, source_matches_called

# ---------------------------------------------------------------------------
# A cited source the run never called does not count as sourced
# ---------------------------------------------------------------------------


def test_source_matching_accepts_how_models_name_tools():
    called = ["get_pod_logs", "get_prometheus_query", "get_events", "describe_pod"]
    for source in ("get_pod_logs", "pod logs", "Prometheus query", "events", "describe_pod output"):
        assert source_matches_called(source, called), source
    for source in ("", "metrics", "kubectl", "get_node_metrics"):
        assert not source_matches_called(source, called), source


def test_an_unread_source_is_unsourced_and_kept_visible():
    evidence = [Evidence(observation="error rate 40%", kind="metric", source="prometheus", stance="supports")]
    grounded = ground_evidence(evidence, ["get_pod_logs"])
    assert grounded[0].source == ""
    assert grounded[0].unverified_source == "prometheus"
    assert grounded[0].observation == "error rate 40%"


def test_fabricated_sources_no_longer_raise_confidence():
    """The defect: any non-empty source weighed an item as fully sourced."""
    claimed = [
        Evidence(observation=f"signal {i}", source=s, stance="supports", confidence=0.9)
        for i, s in enumerate(("prometheus", "tracing", "loki"))
    ]
    as_claimed = derive_confidence(claimed, 0.95)
    as_read = derive_confidence(ground_evidence(claimed, ["get_pod_logs"]), 0.95)
    assert as_read < as_claimed
    # The same items backed by tools that were called keep their weight.
    real = [
        Evidence(observation=f"signal {i}", source=s, stance="supports", confidence=0.9)
        for i, s in enumerate(("get_pod_logs", "get_events", "describe_pod"))
    ]
    assert derive_confidence(ground_evidence(real, ["get_pod_logs", "get_events", "describe_pod"]), 0.95) == as_claimed


def test_without_a_call_record_nothing_is_changed():
    evidence = [Evidence(observation="x", source="prometheus")]
    assert ground_evidence(evidence, []) == evidence


def test_plan_phase_confidence_counts_only_sources_it_read():
    from sre_agent.plan_runtime import PlanRuntime

    body = {
        "findings": {"root_cause": "x"},
        "evidence": [
            {"observation": "a", "source": "get_prometheus_query", "stance": "supports", "confidence": 0.9},
            {"observation": "b", "source": "get_events", "stance": "supports", "confidence": 0.9},
        ],
        "confidence": 0.95,
    }
    response = "```json\n" + json.dumps(body) + "\n```"
    phase = SimpleNamespace(id="diagnose", skill_name="sre")
    runtime = PlanRuntime()
    read_both = runtime._parse_skill_output(response, phase, ["get_prometheus_query", "get_events"])
    read_one = runtime._parse_skill_output(response, phase, ["get_events"])
    assert read_one.confidence < read_both.confidence
    unread = next(e for e in read_one.evidence if e["observation"] == "a")
    assert unread["source"] == "" and unread["unverified_source"] == "get_prometheus_query"


# ---------------------------------------------------------------------------
# Saved investigations keep their structured evidence
# ---------------------------------------------------------------------------


@pytest.fixture
def _db():
    import sre_agent.monitor as _mon
    from sre_agent.db import Database, reset_database, set_database
    from tests.conftest import _TEST_DB_URL, truncate_core_tables

    db = Database(_TEST_DB_URL)
    set_database(db)
    truncate_core_tables(db)
    _mon._ensure_tables()
    yield db
    reset_database()


DETAIL = [
    {"observation": "Logs: DB_HOST not set", "kind": "log", "source": "get_pod_logs", "stance": "supports"},
    {
        "observation": "Restarts began before the rollout",
        "kind": "event",
        "source": "get_events",
        "stance": "contradicts",
    },
]


@pytest.mark.parametrize("key", ["evidenceDetail", "evidence_detail"])
def test_saved_investigation_keeps_evidence_stance(_db, key):
    """A card reloaded from history showed contradicting evidence as support."""
    from sre_agent.monitor.actions import save_investigation

    report = {
        "id": f"i-detail-{key}",
        "findingId": "f-1",
        "status": "completed",
        "summary": "s",
        "confidence": 0.5,
        "evidence": [d["observation"] for d in DETAIL],
        key: DETAIL,
    }
    save_investigation(report, {"category": "crashloop", "severity": "critical", "resources": []})
    row = _db.fetchone("SELECT evidence_detail FROM investigations WHERE id = ?", (report["id"],))
    assert json.loads(row["evidence_detail"]) == DETAIL


def test_inbox_investigation_endpoint_returns_structured_evidence(_db):
    import asyncio
    from unittest.mock import patch

    from sre_agent.api.inbox_rest import rest_get_investigation
    from sre_agent.monitor.actions import save_investigation

    save_investigation(
        {"id": "i-api", "findingId": "f-2", "status": "completed", "summary": "s", "evidenceDetail": DETAIL},
        {"category": "crashloop", "severity": "critical", "resources": []},
    )
    item = {"id": "inb-1", "metadata": {"investigation_id": "i-api"}}
    with patch("sre_agent.api.inbox_rest.get_inbox_item", return_value=item):
        result = asyncio.run(rest_get_investigation("inb-1"))
    assert result["evidence_detail"] == DETAIL


def test_migration_041_adds_the_column_to_an_existing_table(_db):
    from sre_agent.db_migrations import MIGRATIONS, _migrate_041_investigation_evidence_detail

    assert (41, "investigation_evidence_detail", _migrate_041_investigation_evidence_detail) in MIGRATIONS
    _db.execute("ALTER TABLE investigations DROP COLUMN IF EXISTS evidence_detail")
    _db.commit()
    _migrate_041_investigation_evidence_detail(_db)
    _migrate_041_investigation_evidence_detail(_db)  # idempotent
    _db.commit()
    cols = _db.fetchall("SELECT column_name FROM information_schema.columns WHERE table_name = 'investigations'")
    assert "evidence_detail" in {c["column_name"] for c in cols}
