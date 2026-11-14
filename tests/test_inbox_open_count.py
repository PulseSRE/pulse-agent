"""The Inbox count is the list, and every item closes when its condition clears.

Live cluster (OpenShift 5.0.1): the bell, sidebar and agent dock said
"45 need attention" while the Needs Attention list showed 12 items, and a
"Upgrade to 4.23 blocked" item stayed open after the cluster moved to 5.0.1.

Three separate faults, each pinned here against the real database:

1. ``/inbox/stats`` ``needs_attention`` was a per-status sum of everything not
   closed. The list also hides informational items and the symptoms an open
   episode already explains, so the count included rows the list never shows.
2. The generator cycle only resolved items in 'new' or 'triaged'. Triage moves
   items to 'agent_review_failed'/'agent_cleared' and people move them to
   'claimed'/'in_progress'; from there an item whose condition had cleared was
   never revisited. Some generators also answered "nothing" when their source
   could not be read (skill analytics, Prometheus), resolving items on an
   outage instead of leaving them alone.
3. The restart reconcile for monitor items filtered on ``pinned_by IS NULL``;
   the column defaults to '[]', so it matched nothing and resolved nothing.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

EPISODE_INDEX = "sre_agent.monitor.episodes.symptom_keys_by_episode"


@pytest.fixture(autouse=True)
def _clean_inbox():
    from sre_agent.db import get_database

    db = get_database()
    db.execute("DELETE FROM inbox_items")
    db.commit()
    yield
    db.execute("DELETE FROM inbox_items")
    db.commit()


def _item(key: str, severity: str | None = "critical", **overrides):
    item = {
        "item_type": "task",
        "title": f"Item {key}",
        "summary": "s",
        "severity": severity,
        "confidence": 0.9,
        "noise_score": 0.0,
        "namespace": "prod",
        "resources": [],
        "correlation_key": key,
        "created_by": "system:monitor",
    }
    item.update(overrides)
    return item


def _set_status(item_id: str, status: str) -> None:
    from sre_agent.db import get_database

    db = get_database()
    db.execute("UPDATE inbox_items SET status = ? WHERE id = ?", (status, item_id))
    db.commit()


def _status(item_id: str) -> str:
    from sre_agent.inbox import get_inbox_item

    item = get_inbox_item(item_id)
    assert item is not None
    return item["status"]


def _seed_mixed_inbox() -> dict[str, str]:
    """One of every case that made the count and the list disagree."""
    from sre_agent.inbox import create_inbox_item, snooze_item

    ids = {
        "critical": create_inbox_item(_item("crashloop:prod:Pod/api")),
        "warning_triaged": create_inbox_item(_item("workloads:prod:Deployment/web", "warning")),
        "review_failed": create_inbox_item(_item("oom:prod:Pod/worker")),
        "unkeyed": create_inbox_item(_item("", "warning", correlation_key=None)),
        # Hidden by the list, counted by the old stats:
        "info": create_inbox_item(_item("network_policy_gaps:cluster", "info")),
        "episode_symptom": create_inbox_item(_item("workloads:prod:Deployment/db")),
        # Excluded by both:
        "resolved": create_inbox_item(_item("nodes::Node/a")),
        "cleared": create_inbox_item(_item("nodes::Node/b")),
        "archived": create_inbox_item(_item("nodes::Node/c")),
        "snoozed": create_inbox_item(_item("nodes::Node/d")),
    }
    _set_status(ids["warning_triaged"], "triaged")
    _set_status(ids["review_failed"], "agent_review_failed")
    _set_status(ids["resolved"], "resolved")
    _set_status(ids["cleared"], "agent_cleared")
    _set_status(ids["archived"], "archived")
    snooze_item(ids["snoozed"], 4)
    return ids


EPISODE = {"workloads:prod:Deployment/db": "ep-1"}


class TestOneDefinitionOfNeedsAttention:
    def test_the_count_is_the_number_of_rows_the_list_shows(self):
        from sre_agent.inbox import get_inbox_stats, list_inbox_items

        ids = _seed_mixed_inbox()
        with patch(EPISODE_INDEX, return_value=EPISODE):
            listed = list_inbox_items(status="__needs_attention__")
            stats = get_inbox_stats()

        shown = {i["id"] for i in listed["items"]}
        assert shown == {ids["critical"], ids["warning_triaged"], ids["review_failed"], ids["unkeyed"]}
        # Before: 6 (info and the episode symptom were counted, never shown).
        assert stats["needs_attention"] == len(shown) == 4
        assert stats["collapsed_into_episodes"] == listed["collapsedIntoEpisodes"] == 1

    def test_severity_counts_are_of_the_same_items(self):
        """'N need attention (M critical)' must be about one N. These keys did not exist."""
        from sre_agent.inbox import get_inbox_stats

        _seed_mixed_inbox()
        with patch(EPISODE_INDEX, return_value=EPISODE):
            stats = get_inbox_stats()
        assert stats["critical"] == 2  # api crashloop, worker oom (not the episode symptom, not resolved)
        assert stats["warning"] == 2
        assert stats["unique_issues"] == 4

    def test_duplicates_of_one_condition_count_as_one_unique_issue(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_stats

        for _ in range(3):
            create_inbox_item(_item("crashloop:prod:Pod/api"))
        create_inbox_item(_item("oom:prod:Pod/worker"))
        with patch(EPISODE_INDEX, return_value={}):
            stats = get_inbox_stats()
        assert stats["needs_attention"] == 4
        assert stats["unique_issues"] == 2

    def test_the_count_is_not_capped_by_the_list_page_size(self):
        from sre_agent.inbox import create_inbox_item, get_inbox_stats

        for i in range(205):
            create_inbox_item(_item(f"crashloop:prod:Pod/p{i}"))
        with patch(EPISODE_INDEX, return_value={}):
            assert get_inbox_stats()["needs_attention"] == 205

    def test_an_unreadable_episode_index_hides_nothing_in_either(self):
        """The list shows every item when episodes cannot be read; so must the count."""
        from sre_agent.inbox import get_inbox_stats, list_inbox_items

        _seed_mixed_inbox()
        with patch(EPISODE_INDEX, side_effect=RuntimeError("db down")):
            listed = list_inbox_items(status="__needs_attention__")
            stats = get_inbox_stats()
        assert stats["needs_attention"] == len(listed["items"]) == 5

    def test_the_rest_endpoints_agree(self, monkeypatch):
        """The async path the UI actually calls: /inbox/stats and the Needs Attention list."""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", "tok-open-count")
        monkeypatch.setenv("PULSE_AGENT_MEMORY", "0")
        headers = {"Authorization": "Bearer tok-open-count", "X-Forwarded-User": "alice"}
        _seed_mixed_inbox()
        with (
            patch("sre_agent.k8s_client._initialized", True),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("sre_agent.k8s_client.get_core_client", return_value=MagicMock()),
            patch(EPISODE_INDEX, return_value=EPISODE),
        ):
            from sre_agent.api import app

            client = TestClient(app, raise_server_exceptions=False)
            stats = client.get("/inbox/stats", headers=headers)
            listed = client.get("/inbox?status=__needs_attention__", headers=headers)
        assert stats.status_code == 200 and listed.status_code == 200
        assert stats.json()["needs_attention"] == len(listed.json()["items"]) == 4
        assert stats.json()["critical"] == 2


# -- Generators ---------------------------------------------------------------


def _upgrade_no_go() -> dict:
    return {
        "status": "no_go",
        "reason": "Upgradeable=False",
        "target": {"ocp": "4.23", "kubernetes": "1.36"},
        "inUse": [],
        "gates": [],
        "upgradeable": {"reason": "AdminAckRequired"},
    }


def _installplan():
    from sre_agent.k8s_tools.olm import PendingPlan

    return PendingPlan(
        namespace="openshift-operators",
        name="install-abc",
        resource_version="1",
        installs=["foo.v1.2.0"],
        replaces=["foo.v1.1.0"],
        operator="foo",
        subscription="foo",
        channel="stable",
        approval="Manual",
        phase="RequiresApproval",
        created=None,
        current=True,
    )


G = "sre_agent.inbox_generators."
NOW = time.time()

# name -> (what to patch, the condition present, the condition cleared)
GENERATOR_CASES: dict[str, tuple[str, object, object]] = {
    "cert_expiry": (
        G + "_get_tls_secrets",
        [{"name": "api-cert", "namespace": "prod", "expiry_timestamp": int(NOW) + 24 * 3600}],
        [],
    ),
    "trend_prediction": (
        G + "_get_trend_findings",
        [
            {
                "title": "Node w1 memory exhaustion predicted in 3 days",
                "category": "memory_pressure",
                "severity": "warning",
                "confidence": 0.8,
                "resources": [{"kind": "Node", "name": "w1", "namespace": ""}],
            }
        ],
        [],
    ),
    "degraded_operator": (
        G + "_get_degraded_operators",
        [{"name": "cloud-credential", "degraded_duration_hours": 2.0, "message": "InsufficientCloudCreds"}],
        [],
    ),
    "upgrade_available": (G + "_get_available_updates", [{"version": "5.0.2", "channel": "stable-5.0"}], []),
    "upgrade_api_removals": (G + "_get_upgrade_api_removals", _upgrade_no_go(), None),
    "slo_burn": (
        G + "_get_slo_burn_rates",
        [
            {
                "name": "api availability",
                "service": "api",
                "budget_remaining_hours": 12,
                "budget_remaining": 0.2,
                "burn_rate": 2.5,
                "alert_level": "warning",
            }
        ],
        [],
    ),
    "capacity_projection": (G + "_get_node_capacity", [{"node": "w1", "pressure": ["MemoryPressure"]}], []),
    "stale_finding": (G + "_get_stale_findings", [{"title": "Old", "hours_stale": 96, "finding_id": "x"}], []),
    "privileged_workloads": (
        G + "_get_privileged_workloads",
        [{"pod": "debug", "namespace": "default", "container": "main"}],
        [],
    ),
    "rbac_drift": (G + "_get_rbac_drift", [{"binding": "b", "user": "alice"}], []),
    "network_policy_gaps": (G + "_get_network_policy_gaps", [{"namespace": "staging"}], []),
    "route_cert_expiry": (
        G + "_get_route_cert_expiry",
        [{"name": "r", "namespace": "prod", "hours_until_expiry": 48}],
        [],
    ),
    "service_endpoint_gaps": (G + "_get_service_endpoint_gaps", [{"service": "svc", "namespace": "prod"}], []),
    "skill_gaps": (
        "sre_agent.selector_learning.identify_skill_gaps",
        [{"pattern": "etcd defrag", "occurrences": 5}],
        [],
    ),
    "skill_low_performers": (
        "sre_agent.selector_learning.prune_low_performers",
        [{"skill": "flaky", "override_rate": 0.5, "overrides": 5, "total": 10}],
        [],
    ),
    "skill_curation": (
        "sre_agent.skill_curator.find_stale_skills",
        [{"name": "auto-x", "age_days": 40, "reviewed": True, "quarantined": False, "incident_type": "x"}],
        [],
    ),
    "operator_approvals": (G + "_get_pending_installplans", [_installplan()], []),
}


def _run_cycle(name: str, target: str, **patch_kwargs) -> None:
    """One generator cycle with only ``name`` registered, its source patched."""
    from sre_agent import inbox_generators as gens
    from sre_agent.inbox import run_generator_cycle

    fn = dict(gens.TASK_GENERATORS)[name]
    with (
        patch.object(gens, "TASK_GENERATORS", [(name, fn)]),
        patch(target, **patch_kwargs),
        patch("sre_agent.skill_curator.find_overlapping_skills", return_value=[]),
        patch("sre_agent.inbox._prune_stale_resources"),
        patch("sre_agent.inbox.agent_process_inbox"),
        patch("sre_agent.inbox._publish_event"),
    ):
        gens._reset_upgrade_api_cache()
        run_generator_cycle()
        gens._reset_upgrade_api_cache()


def _generator_items(name: str) -> list[dict]:
    from sre_agent.db import get_database

    rows = get_database().fetchall("SELECT id, status, metadata FROM inbox_items")
    import json

    return [dict(r) for r in rows if json.loads(r["metadata"] or "{}").get("generator") == name]


def test_every_registered_generator_has_a_case():
    from sre_agent.inbox_generators import TASK_GENERATORS

    covered = set(GENERATOR_CASES) | {"durable_approvals"}  # tested on its own below
    assert {n for n, _ in TASK_GENERATORS} == covered


@pytest.mark.parametrize("name", sorted(GENERATOR_CASES))
def test_a_generator_item_resolves_when_its_condition_clears(name):
    target, present, cleared = GENERATOR_CASES[name]
    _run_cycle(name, target, return_value=present)
    items = _generator_items(name)
    assert items, f"{name} raised no item for its condition"
    assert all(i["status"] == "new" for i in items)

    _run_cycle(name, target, return_value=cleared)
    assert {i["status"] for i in _generator_items(name)} == {"resolved"}


@pytest.mark.parametrize("name", sorted(GENERATOR_CASES))
def test_a_generator_that_cannot_read_its_source_leaves_its_items_alone(name):
    from sre_agent.inbox_generators import GeneratorUnavailableError

    target, present, _cleared = GENERATOR_CASES[name]
    _run_cycle(name, target, return_value=present)
    _run_cycle(name, target, side_effect=GeneratorUnavailableError("403"))
    assert {i["status"] for i in _generator_items(name)} == {"new"}


@pytest.mark.parametrize("status", ["triaged", "agent_review_failed", "claimed", "in_progress", "agent_cleared"])
def test_a_cleared_condition_resolves_the_item_whatever_open_status_it_is_in(status):
    """Only 'new' and 'triaged' used to be revisited; the rest stayed open forever."""
    target, present, cleared = GENERATOR_CASES["degraded_operator"]
    _run_cycle("degraded_operator", target, return_value=present)
    (item,) = _generator_items("degraded_operator")
    _set_status(item["id"], status)

    _run_cycle("degraded_operator", target, return_value=cleared)
    assert _status(item["id"]) == "resolved"


def test_an_item_the_triage_pipeline_is_writing_is_left_for_the_next_cycle():
    target, present, cleared = GENERATOR_CASES["degraded_operator"]
    _run_cycle("degraded_operator", target, return_value=present)
    (item,) = _generator_items("degraded_operator")
    _set_status(item["id"], "agent_reviewing")
    _run_cycle("degraded_operator", target, return_value=cleared)
    assert _status(item["id"]) == "agent_reviewing"


def test_a_resolved_generator_item_says_why_and_is_announced():
    import json

    from sre_agent import inbox_generators as gens
    from sre_agent.inbox import get_inbox_item, run_generator_cycle

    target, present, cleared = GENERATOR_CASES["rbac_drift"]
    _run_cycle("rbac_drift", target, return_value=present)
    (item,) = _generator_items("rbac_drift")
    published = MagicMock()
    with (
        patch.object(gens, "TASK_GENERATORS", [("rbac_drift", gens.gen_rbac_drift)]),
        patch(target, return_value=cleared),
        patch("sre_agent.inbox._prune_stale_resources"),
        patch("sre_agent.inbox.agent_process_inbox"),
        patch("sre_agent.inbox._publish_event", published),
    ):
        run_generator_cycle()
    fetched = get_inbox_item(item["id"])
    assert fetched is not None
    assert fetched["metadata"]["resolved_reason"] == "condition cleared"
    # Earlier metadata is kept, not replaced.
    assert fetched["metadata"]["generator"] == "rbac_drift"
    assert ("inbox_item_resolved", item["id"]) in [c.args[:2] for c in published.call_args_list]
    assert json.dumps(fetched["metadata"])  # still valid JSON text in the column


def test_durable_approval_items_survive_an_unreachable_temporal():
    """durable_approvals answers with its last result on an outage instead of raising."""
    from sre_agent import inbox_generators as gens

    settings = MagicMock()
    settings.temporal.host = "temporal:7233"
    waiting = [{"workflow_id": "wf-1", "phase_id": "remediate", "memo": {"incident_type": "crashloop"}}]
    gens._last_durable_approvals = []
    try:
        with (
            patch("sre_agent.config.get_settings", return_value=settings),
            patch("sre_agent.inbox._get_cluster_id", return_value=None),
            patch("sre_agent.inbox._slo_impact", return_value=[]),
        ):
            _run_cycle(
                "durable_approvals",
                "sre_agent.temporal.client.list_awaiting_approvals",
                new=AsyncMock(return_value=waiting),
            )
            assert {i["status"] for i in _generator_items("durable_approvals")} == {"new"}

            _run_cycle(
                "durable_approvals",
                "sre_agent.temporal.client.list_awaiting_approvals",
                new=AsyncMock(side_effect=ConnectionError("down")),
            )
            assert {i["status"] for i in _generator_items("durable_approvals")} == {"new"}

            _run_cycle(
                "durable_approvals",
                "sre_agent.temporal.client.list_awaiting_approvals",
                new=AsyncMock(return_value=[]),
            )
            assert {i["status"] for i in _generator_items("durable_approvals")} == {"resolved"}
    finally:
        gens._last_durable_approvals = []


class TestSourcesThatUsedToAnswerNothing:
    """A failed read must be 'could not check', never 'nothing to report'."""

    def test_skill_analytics_down_leaves_skill_items_alone(self):
        repo = MagicMock()
        repo.fetch_missing_tool_queries.side_effect = RuntimeError("db down")
        repo.fetch_override_rates.side_effect = RuntimeError("db down")
        with patch("sre_agent.repositories.selector_learning_repo.get_selector_learning_repo", return_value=repo):
            from sre_agent import inbox_generators as gens

            failed: set[str] = set()
            with patch.object(
                gens,
                "TASK_GENERATORS",
                [("skill_gaps", gens.gen_skill_gaps), ("skill_low_performers", gens.gen_skill_low_performers)],
            ):
                assert gens.run_all_generators(failed) == []
        assert failed == {"skill_gaps", "skill_low_performers"}

    def test_skill_usage_down_leaves_curation_items_alone(self):
        from sre_agent import inbox_generators as gens

        failed: set[str] = set()
        with (
            patch("sre_agent.repositories.get_skill_analytics_repo", side_effect=RuntimeError("db down")),
            patch.object(gens, "TASK_GENERATORS", [("skill_curation", gens.gen_skill_curation)]),
        ):
            assert gens.run_all_generators(failed) == []
        assert failed == {"skill_curation"}

    def test_the_lenient_callers_still_get_an_empty_answer(self):
        from sre_agent.selector_learning import identify_skill_gaps, prune_low_performers
        from sre_agent.skill_curator import find_stale_skills

        repo = MagicMock()
        repo.fetch_missing_tool_queries.side_effect = RuntimeError("db down")
        repo.fetch_override_rates.side_effect = RuntimeError("db down")
        with (
            patch("sre_agent.repositories.selector_learning_repo.get_selector_learning_repo", return_value=repo),
            patch("sre_agent.repositories.get_skill_analytics_repo", side_effect=RuntimeError("db down")),
        ):
            assert identify_skill_gaps() == []
            assert prune_low_performers() == []
            assert find_stale_skills() == []

    def test_prometheus_unreachable_leaves_slo_items_alone(self):
        from sre_agent import inbox_generators as gens
        from sre_agent.slo_registry import SLODefinition, SLORegistry

        reg = SLORegistry()
        reg.register(SLODefinition("api", "availability", 0.999), persist=False)
        failed: set[str] = set()
        with (
            patch("sre_agent.slo_registry.get_slo_registry", return_value=reg),
            patch.object(reg, "query_prometheus_values", return_value={}),
            patch.object(gens, "TASK_GENERATORS", [("slo_burn", gens.gen_slo_burn)]),
        ):
            assert gens.run_all_generators(failed) == []
        assert failed == {"slo_burn"}

    def test_no_slo_defined_is_nothing_to_report(self):
        from sre_agent import inbox_generators as gens
        from sre_agent.slo_registry import SLORegistry

        with patch("sre_agent.slo_registry.get_slo_registry", return_value=SLORegistry()):
            assert gens._get_slo_burn_rates() == []


# -- Monitor bridge -------------------------------------------------------------


def _finding(fid: str, name: str = "api") -> dict:
    return {
        "id": fid,
        "category": "crashloop",
        "title": f"Pod {name} crashlooping",
        "summary": "restarting",
        "severity": "critical",
        "confidence": 0.9,
        "resources": [{"kind": "Pod", "name": name, "namespace": "prod"}],
    }


class TestResolvedFindingsCloseTheirItems:
    def test_restart_reconcile_resolves_an_unpinned_item_in_the_real_table(self):
        """It filtered on pinned_by IS NULL; the column defaults to '[]', so nothing ever matched."""
        from sre_agent.inbox import bridge_finding_to_inbox, reconcile_open_items

        gone = bridge_finding_to_inbox(_finding("f-gone", "gone"))
        live = bridge_finding_to_inbox(_finding("f-live", "live"))
        from sre_agent.inbox import _finding_corr_key

        with patch("sre_agent.inbox._publish_event"):
            assert reconcile_open_items({_finding_corr_key(_finding("f-live", "live"))}) == 1
        assert _status(gone) == "resolved"
        assert _status(live) == "new"

    def test_restart_reconcile_leaves_pinned_and_claimed_items(self):
        from sre_agent.inbox import bridge_finding_to_inbox, pin_item, reconcile_open_items

        pinned = bridge_finding_to_inbox(_finding("f-pinned", "pinned"))
        pin_item(pinned, "alice")
        claimed = bridge_finding_to_inbox(_finding("f-claimed", "claimed"))
        from sre_agent.db import get_database

        db = get_database()
        db.execute("UPDATE inbox_items SET claimed_by = 'bob' WHERE id = ?", (claimed,))
        db.commit()
        with patch("sre_agent.inbox._publish_event"):
            assert reconcile_open_items({"something-else"}) == 0
        assert _status(pinned) == "new"
        assert _status(claimed) == "new"

    def test_a_resolved_finding_closes_every_open_item_for_its_condition(self):
        """Only the first match was resolved; a duplicate for the same key kept counting."""
        from sre_agent.inbox import _finding_corr_key, create_inbox_item, resolve_finding_inbox_item

        f = _finding("f-1")
        key = _finding_corr_key(f)
        first = create_inbox_item(_item(key, finding_id="f-old"))
        second = create_inbox_item(_item(key, finding_id="f-1"))
        other = create_inbox_item(_item("crashloop:prod:Pod/other"))
        with patch("sre_agent.inbox._publish_event"):
            assert resolve_finding_inbox_item("f-1", f) is True
        assert _status(first) == "resolved"
        assert _status(second) == "resolved"
        assert _status(other) == "new"

    def test_a_resolved_finding_with_no_item_is_a_no_op(self):
        from sre_agent.inbox import resolve_finding_inbox_item

        assert resolve_finding_inbox_item("f-none", _finding("f-none", "none")) is False
        assert resolve_finding_inbox_item("") is False


def test_restart_reconcile_waits_for_a_scan_every_scanner_reported():
    """A failed scanner reported nothing; its items are not 'no longer reported'."""
    from sre_agent.monitor.scanner_protocol import FunctionScanner, ScannerMeta

    state = {"fail": True}

    def crashloop(pods=None):
        return [_finding("f-1")]

    def nodes():
        if state["fail"]:
            raise RuntimeError("403")
        return []

    scanners = [
        FunctionScanner(ScannerMeta("crashloop", "c", "d", "crashloop"), crashloop, accepts_pods=True),
        FunctionScanner(ScannerMeta("nodes", "n", "d", "nodes"), nodes),
    ]
    calls: list[set[str]] = []

    def reconcile(keys):
        calls.append(set(keys))
        return 0

    import sre_agent.monitor.cluster_monitor as cm

    with (
        patch.object(cm, "get_monitor_repo", MagicMock()),
        patch.object(cm, "get_all_scanner_instances", lambda: scanners),
        patch.object(cm, "expire_orphaned_proposals", lambda: 0),
        patch("sre_agent.inbox.bridge_finding_to_inbox", lambda f: None),
        patch("sre_agent.inbox.run_generator_cycle", lambda: None),
        patch("sre_agent.inbox.reconcile_open_items", reconcile),
        patch("sre_agent.monitor.recurrence.process_recurrences", AsyncMock()),
        patch("sre_agent.async_k8s.get_async_core_client", AsyncMock(return_value=MagicMock())),
    ):
        m = cm.ClusterMonitor()
        m._correlate_episodes = lambda f: []
        for name in ("run_investigations", "auto_fix", "process_verifications", "process_handoffs"):
            setattr(m, name, AsyncMock())
        m._broadcast_raw = AsyncMock()
        m.broadcast = AsyncMock()
        asyncio.run(m._run_scan_locked())
        assert calls == [], "a scan with a failed scanner must not reconcile"
        state["fail"] = False
        asyncio.run(m._run_scan_locked())
        assert len(calls) == 1
        asyncio.run(m._run_scan_locked())
        assert len(calls) == 1, "once per process"
