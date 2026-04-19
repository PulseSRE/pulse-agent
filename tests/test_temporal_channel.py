"""Tests for temporal channel wiring into ORCA skill selector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sre_agent.config import get_settings


def test_temporal_cache_ttl_default():
    s = get_settings()
    assert s.temporal_cache_ttl == 60


# --- TemporalSignal dataclass + cached builder ---

from sre_agent.skill_selector import TemporalSignal, _build_temporal_signal


def test_temporal_signal_defaults():
    sig = TemporalSignal(recent_deploys=[], time_of_day="business_hours", active_incidents=0)
    assert sig.time_of_day == "business_hours"
    assert sig.recent_deploys == []
    assert sig.active_incidents == 0


def test_build_temporal_signal_caches():
    """Second call within TTL returns cached result without hitting DB."""
    call_count = 0

    def mock_fetchone(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"cnt": 2}

    mock_db = MagicMock()
    mock_db.fetchone = mock_fetchone

    with (
        patch("sre_agent.db.get_database", return_value=mock_db),
        patch("sre_agent.skill_selector._temporal_signal_cache", {"signal": None, "time": 0.0}),
    ):
        _build_temporal_signal(cache_ttl=60)
        _build_temporal_signal(cache_ttl=60)
        assert call_count == 2  # 2 queries per build (deploys + incidents)


def test_build_temporal_signal_db_failure():
    """When DB is unreachable, returns neutral signal."""
    with (
        patch("sre_agent.db.get_database", side_effect=Exception("DB down")),
        patch("sre_agent.skill_selector._temporal_signal_cache", {"signal": None, "time": 0.0}),
    ):
        sig = _build_temporal_signal(cache_ttl=60)
        assert sig.recent_deploys == []
        assert sig.active_incidents == 0


# --- _score_temporal() rework ---

from sre_agent.skill_loader import Skill
from sre_agent.skill_selector import SkillSelector


def _make_skills() -> dict[str, Skill]:
    """Minimal skill dicts for selector tests."""
    return {
        "sre": Skill(
            name="sre",
            version=1,
            description="SRE diagnostics",
            keywords=["pods", "crash", "deploy"],
            categories=["diagnostics", "workloads", "operations"],
            write_tools=True,
            priority=1,
            system_prompt="",
            path=Path("."),
        ),
        "security": Skill(
            name="security",
            version=1,
            description="Security scanning",
            keywords=["cve", "vulnerability", "rbac"],
            categories=["security"],
            write_tools=False,
            priority=2,
            system_prompt="",
            path=Path("."),
        ),
        "slo-management": Skill(
            name="slo-management",
            version=1,
            description="SLO management",
            keywords=["slo", "burn", "error budget"],
            categories=["monitoring"],
            write_tools=False,
            priority=3,
            system_prompt="",
            path=Path("."),
        ),
        "postmortem": Skill(
            name="postmortem",
            version=1,
            description="Post-incident review",
            keywords=["postmortem", "incident"],
            categories=["diagnostics"],
            write_tools=False,
            priority=4,
            system_prompt="",
            path=Path("."),
        ),
    }


def test_temporal_recent_deploy_boosts_sre():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[{"count": 3}], time_of_day="business_hours", active_incidents=0)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("why are pods crashing")
    assert scores.get("sre", 0) >= 0.3
    assert scores.get("postmortem", 0) >= 0.15


def test_temporal_off_hours_boosts_slo():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[], time_of_day="off_hours", active_incidents=0)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("check error budget")
    assert scores.get("slo-management", 0) >= 0.1


def test_temporal_active_incidents_boosts_sre():
    skills = _make_skills()
    selector = SkillSelector(skills, keyword_index={})
    signal = TemporalSignal(recent_deploys=[], time_of_day="business_hours", active_incidents=3)
    with patch.object(selector, "_get_temporal_signal", return_value=signal):
        scores = selector._score_temporal("what is going on")
    assert scores.get("sre", 0) >= 0.2
