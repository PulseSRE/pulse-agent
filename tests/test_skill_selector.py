"""Tests for ORCA skill selector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.db import Database, set_database
from sre_agent.db_migrations import run_migrations
from sre_agent.skill_selector import SelectionResult, SkillSelector

from .conftest import _TEST_DB_URL


def _make_test_db() -> Database:
    db = Database(_TEST_DB_URL)
    db.execute("DROP TABLE IF EXISTS skill_selection_log CASCADE")
    db.commit()
    return db


class TestMigration:
    def test_migration_creates_table(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)

        row = db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'skill_selection_log') AS exists"
        )
        assert row["exists"] is True

    def test_skill_selection_log_columns(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)
        row = db.fetchone(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'skill_selection_log' AND column_name = 'channel_scores'"
        )
        assert row is not None

    def test_skill_selection_log_jsonb_columns(self):
        db = _make_test_db()
        db.execute("DELETE FROM schema_migrations WHERE version >= 14")
        db.commit()
        set_database(db)
        run_migrations(db)
        row = db.fetchone(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'skill_selection_log' AND column_name = 'fused_scores'"
        )
        assert row is not None
        assert row["data_type"] == "jsonb"


def _mock_skill(name, categories=None, priority=10, keywords=None):
    s = MagicMock()
    s.name = name
    s.categories = categories or []
    s.priority = priority
    s.keywords = keywords or []
    return s


class TestSkillSelectorKeywords:
    def test_keyword_match(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics", "workloads"]),
            "security": _mock_skill("security", ["security"]),
        }
        index = [
            ("crash", "sre", 5),
            ("pod", "sre", 3),
            ("rbac", "security", 4),
            ("audit", "security", 5),
        ]
        selector = SkillSelector(skills, keyword_index=index)
        result = selector.select("why is my pod crashlooping")
        assert result.skill_name == "sre"
        assert result.channel_scores["keyword"]["sre"] > 0

    def test_security_keywords(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics"]),
            "security": _mock_skill("security", ["security"]),
        }
        index = [("rbac", "security", 4), ("audit", "security", 5)]
        selector = SkillSelector(skills, keyword_index=index)
        result = selector.select("audit rbac permissions")
        assert result.skill_name == "security"


class TestSkillSelectorComponentTags:
    def test_pod_matches_diagnostics(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics", "workloads"]),
            "security": _mock_skill("security", ["security"]),
        }
        selector = SkillSelector(skills)
        scores = selector._score_component_tags("check the pod status")
        assert "sre" in scores
        assert scores["sre"] > 0

    def test_secret_matches_security(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics"]),
            "security": _mock_skill("security", ["security"]),
        }
        selector = SkillSelector(skills)
        scores = selector._score_component_tags("check secrets and networkpolicy")
        assert "security" in scores

    def test_no_resources_returns_empty(self):
        skills = {"sre": _mock_skill("sre", ["diagnostics"])}
        selector = SkillSelector(skills)
        scores = selector._score_component_tags("what is going on")
        assert scores == {}


class TestSkillSelectorFusion:
    def test_fused_scores_weighted(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics"]),
            "security": _mock_skill("security", ["security"]),
        }
        selector = SkillSelector(skills)
        channel_scores = {
            "keyword": {"sre": 1.0, "security": 0.3},
            "component": {"sre": 0.8},
            "historical": {},
            "taxonomy": {},
            "temporal": {},
        }
        fused = selector._fuse_scores(channel_scores)
        assert fused["sre"] > fused.get("security", 0)

    def test_threshold_p1_lower(self):
        skills = {"sre": _mock_skill("sre")}
        selector = SkillSelector(skills)
        assert selector._compute_threshold({"incident_priority": "P1"}) == 0.35

    def test_threshold_p3_higher(self):
        skills = {"sre": _mock_skill("sre")}
        selector = SkillSelector(skills)
        assert selector._compute_threshold({"incident_priority": "P3"}) == 0.60

    def test_threshold_default(self):
        skills = {"sre": _mock_skill("sre")}
        selector = SkillSelector(skills)
        assert selector._compute_threshold(None) == 0.45


class TestSkillSelectorSelect:
    def test_returns_selection_result(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics", "workloads"]),
        }
        index = [("pod", "sre", 3), ("crash", "sre", 5)]
        selector = SkillSelector(skills, keyword_index=index)
        result = selector.select("pod is crashing")
        assert isinstance(result, SelectionResult)
        assert result.skill_name == "sre"
        assert result.selection_ms >= 0

    def test_fallback_when_no_match(self):
        skills = {
            "sre": _mock_skill("sre", ["diagnostics"]),
        }
        selector = SkillSelector(skills, keyword_index=[])
        result = selector.select("xyzzy gibberish query")
        assert result.source == "fallback"

    @patch("sre_agent.skill_selector._historical_cache", None)
    def test_historical_channel_graceful_on_db_error(self):
        skills = {"sre": _mock_skill("sre", ["diagnostics"])}
        selector = SkillSelector(skills)
        with patch("sre_agent.db.get_database", side_effect=Exception("DB down")):
            scores = selector._score_historical("check pods")
            assert scores == {}
