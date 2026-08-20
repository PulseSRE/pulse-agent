"""Tests for the self-check that makes Pulse admit when it is broken.

The bug these guard against is not a crash — it is silence. A scanner that
errors returns an empty list, and so does a scanner that ran perfectly against
a healthy cluster. Every assertion here is about telling those two apart.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.monitor import scanner_health as sh

MODULE = "sre_agent.monitor.scanner_health"


@pytest.fixture(autouse=True)
def _clean():
    sh.reset()
    yield
    sh.reset()


def _runs(*per_run):
    """Fake scan_runs rows, newest first. Each arg is a list of (name, status)."""
    return [
        {"scanner_results": json.dumps([{"name": n, "status": s, "error": "boom"} for n, s in run])} for run in per_run
    ]


def _db(rows):
    db = MagicMock()
    db.fetchall.return_value = rows
    return patch("sre_agent.db.get_database", return_value=db)


# ── reporting a swallowed failure ─────────────────────────────────────────


def test_a_scanner_can_report_a_failure_it_swallowed():
    with sh.scanning("crashloop"):
        sh.report_failure(RuntimeError("api timeout"))
    assert sh.get_failure("crashloop") == "api timeout"


def test_reporting_outside_a_scan_is_ignored_not_an_error():
    """Scanners get called directly by tests and by the CLI."""
    sh.report_failure(RuntimeError("nowhere"))
    assert sh.take_failures() == {}


def test_the_name_comes_from_the_dispatcher_not_the_scanner():
    """Scanners are plain functions and do not know their registry name."""
    with sh.scanning("oom"):
        sh.report_failure("bad")
    assert sh.get_failure("oom") == "bad"
    assert sh.get_failure("crashloop") is None


def test_nested_scans_restore_the_outer_name():
    with sh.scanning("outer"):
        with sh.scanning("inner"):
            sh.report_failure("inner failed")
        sh.report_failure("outer failed")
    failures = sh.take_failures()
    assert failures == {"inner": "inner failed", "outer": "outer failed"}


def test_take_failures_drains():
    with sh.scanning("hpa"):
        sh.report_failure("x")
    assert sh.take_failures() == {"hpa": "x"}
    assert sh.take_failures() == {}


# ── reading the streak history nothing ever read ──────────────────────────


def test_a_scanner_failing_every_recent_run_is_reported():
    with _db(_runs([("alerts", "error")], [("alerts", "error")], [("alerts", "error")])):
        assert sh.consecutive_failures()["alerts"][0] == 3


def test_a_streak_shorter_than_the_threshold_is_not_reported():
    """One bad run is a rolling apiserver, not a broken scanner."""
    with _db(_runs([("alerts", "error")], [("alerts", "clean")])):
        assert sh.consecutive_failures() == {}


def test_a_success_ends_the_streak():
    """Failed yesterday, works now — silence is correct."""
    with _db(_runs([("alerts", "clean")], [("alerts", "error")], [("alerts", "error")], [("alerts", "error")])):
        assert "alerts" not in sh.consecutive_failures()


def test_streaks_are_tracked_per_scanner():
    runs = _runs(
        [("alerts", "error"), ("oom", "clean")],
        [("alerts", "error"), ("oom", "error")],
        [("alerts", "error"), ("oom", "error")],
    )
    with _db(runs):
        result = sh.consecutive_failures()
    assert result["alerts"][0] == 3
    assert "oom" not in result


def test_the_last_error_is_carried_so_the_finding_can_name_it():
    with _db(_runs([("alerts", "error")], [("alerts", "error")], [("alerts", "error")])):
        assert sh.consecutive_failures()["alerts"][1] == "boom"


def test_unparseable_history_is_skipped_not_crashed():
    with _db([{"scanner_results": "not json"}, {"scanner_results": None}]):
        assert sh.consecutive_failures() == {}


# ── the AI backend ────────────────────────────────────────────────────────


def _investigations(*statuses):
    return [{"status": s, "error": "Connection error."} for s in statuses]


def test_consecutive_investigation_failures_are_counted():
    with _db(_investigations("failed", "failed", "failed")):
        assert sh.investigation_failure_streak() == (3, "Connection error.")


def test_one_success_ends_the_investigation_streak():
    with _db(_investigations("failed", "failed", "completed", "failed")):
        streak, _ = sh.investigation_failure_streak()
        assert streak == 2


# ── the findings it produces ──────────────────────────────────────────────


def test_a_broken_scanner_becomes_a_finding_that_says_unwatched():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={"alerts": (7, "timeout")}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(0, 0, "")),
    ):
        finding = sh.scan_degraded_capabilities()[0]
    assert "alerts" in finding["title"]
    assert "unwatched" in finding["summary"]
    assert finding["category"] == "degraded"


def test_a_long_ai_outage_is_critical_not_a_warning():
    """1,111 in a row happened on a real cluster and produced no signal at all."""
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(1111, "Connection error.")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(0, 0, "")),
    ):
        finding = sh.scan_degraded_capabilities()[0]
    assert finding["severity"] == "critical"
    assert "1111" in finding["title"]


def test_a_short_ai_blip_stays_a_warning():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(6, "timeout")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(0, 0, "")),
    ):
        assert sh.scan_degraded_capabilities()[0]["severity"] == "warning"


def test_below_the_investigation_threshold_says_nothing():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(2, "timeout")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(0, 0, "")),
    ):
        assert sh.scan_degraded_capabilities() == []


def test_a_healthy_pulse_reports_nothing():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(0, 0, "")),
    ):
        assert sh.scan_degraded_capabilities() == []


def test_the_self_check_never_raises_even_if_its_own_queries_fail():
    """The one scanner that must not go quiet is the one reporting quietness."""
    with (
        patch(f"{MODULE}.consecutive_failures", side_effect=RuntimeError("db down")),
        patch(f"{MODULE}.investigation_failure_streak", side_effect=RuntimeError("db down")),
    ):
        assert sh.scan_degraded_capabilities() == []


def test_it_is_registered_in_both_dispatch_paths():
    from sre_agent.monitor.scanners import _get_all_scanners, get_all_scanner_instances

    assert "degraded" in {n for n, _ in _get_all_scanners()}
    assert "degraded" in {s.meta.name for s in get_all_scanner_instances()}


# ── end to end: a real scanner's swallowed error must surface ─────────────


def test_a_real_scanner_that_swallows_an_error_now_reports_it():
    """scan_pending_pods catches its own exception and returns []. Before this
    change the dispatcher recorded that as 'clean' — a healthy scan of a
    healthy cluster looks exactly the same."""
    from sre_agent.monitor.scanners import scan_pending_pods

    with patch("sre_agent.monitor.scanners.get_core_client", side_effect=RuntimeError("apiserver down")):
        with sh.scanning("pending"):
            findings = scan_pending_pods()

    assert findings == []
    assert sh.get_failure("pending") == "apiserver down"


def test_a_real_scanner_that_succeeds_reports_no_failure():
    from types import SimpleNamespace

    from sre_agent.monitor.scanners import scan_pending_pods

    core = MagicMock()
    core.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])
    with patch("sre_agent.monitor.scanners.get_core_client", return_value=core):
        with sh.scanning("pending"):
            findings = scan_pending_pods()

    assert findings == []
    assert sh.get_failure("pending") is None


def test_partial_results_survive_a_reported_failure():
    """Losing 49 real findings because the 50th item was malformed would be a
    worse trade than reporting the run degraded and keeping them."""
    with sh.scanning("crashloop"):
        sh.report_failure("item 50 was malformed")
    assert sh.get_failure("crashloop") == "item 50 was malformed"


# ── flapping failure, which a consecutive streak cannot see ───────────────
# Found on a live cluster after the streak check shipped: 65 of the last 70
# investigations had failed — 93% — and the check said nothing, because the
# most recent one happened to succeed. A quota-limited backend does not fail
# in a clean run; it flaps.


def _mixed(*statuses):
    return [{"status": s, "error": "Connection error."} for s in statuses]


def test_a_mostly_failing_backend_is_reported_even_with_a_zero_streak():
    with _db(_mixed(*(["completed"] + ["failed"] * 39))):
        failed, total, _ = sh.investigation_failure_rate()
    assert failed == 39
    assert total == 40
    # the streak from newest is zero — the rate is the only evidence
    with _db(_mixed(*(["completed"] + ["failed"] * 39))):
        assert sh.investigation_failure_streak()[0] == 0


def test_the_rate_finding_names_the_proportion():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(65, 70, "Connection error.")),
    ):
        finding = sh.scan_degraded_capabilities()[0]
    assert "65 of the last 70" in finding["title"]
    assert "93%" in finding["summary"]


def test_an_almost_total_failure_rate_is_critical():
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(39, 40, "err")),
    ):
        assert sh.scan_degraded_capabilities()[0]["severity"] == "critical"


def test_a_healthy_backend_with_occasional_failures_stays_quiet():
    """Two failures in forty is a backend working, not a backend broken."""
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(2, 40, "err")),
    ):
        assert sh.scan_degraded_capabilities() == []


def test_a_small_sample_is_not_enough_to_call_it_degraded():
    """Three failures out of three is a blip, not evidence of a broken backend."""
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(0, "")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(3, 3, "err")),
    ):
        assert sh.scan_degraded_capabilities() == []


def test_one_fault_produces_one_finding_not_two():
    """A flat outage trips both checks; the operator should see it once."""
    with (
        patch(f"{MODULE}.consecutive_failures", return_value={}),
        patch(f"{MODULE}.investigation_failure_streak", return_value=(80, "err")),
        patch(f"{MODULE}.investigation_failure_rate", return_value=(40, 40, "err")),
    ):
        findings = sh.scan_degraded_capabilities()
    assert len(findings) == 1
    assert "in a row" in findings[0]["title"]
