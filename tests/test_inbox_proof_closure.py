"""The queue proves its ranking, and chronic work has a way out of it.

Proof: the priority score was computed from severity, causal layer,
confidence and noise — then only the final float survived, so the operator
saw an ordering they could not interrogate. The factors now ride along in
item metadata, refreshed whenever the score is.

Closure: episodes counted recurrence for causes, but an ordinary item that
came back looked new every time, and there was no path from "this keeps
happening" to durable work. Items now carry their 30-day visit ordinal, and
an investigated item can be folded into the skill lifecycle as a draft
runbook — through the same unreviewed-until-approved gate as every other
agent-authored skill.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from sre_agent import inbox as inbox_mod
from sre_agent.inbox import (
    _slo_impact,
    compute_priority_score,
    create_inbox_item,
    explain_priority,
    get_inbox_item,
    update_item_status,
)


@pytest.fixture(autouse=True)
def _clean_inbox():
    yield
    try:
        from sre_agent.db import get_database

        db = get_database()
        db.execute("DELETE FROM inbox_items")
        db.commit()
    except Exception:
        pass


def _make_item(**overrides):
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
        "metadata": {"category": "crashloop"},
    }
    defaults.update(overrides)
    return defaults


class TestExplainPriority:
    def test_factors_reproduce_the_score(self):
        now = int(time.time())
        score = compute_priority_score("critical", 0.9, 0.1, now, None, category="crashloop")
        total, factors = explain_priority("critical", 0.9, 0.1, now, None, category="crashloop")
        # Two wall-clock reads a few microseconds apart shift the age/novelty
        # bonuses in the far decimals; identity is equality within a millipoint.
        assert abs(total - score) < 0.001
        assert factors["total"] == round(total, 3)
        reconstructed = factors["base"] + factors["age_bonus"] + factors["novelty_bonus"] + factors["due_bonus"]
        assert abs(reconstructed - total) < 0.01

    def test_factors_name_the_layer_not_a_number(self):
        _, factors = explain_priority("critical", 0.9, 0.0, int(time.time()), None, category="control_plane")
        assert isinstance(factors["layer"], str)
        assert factors["layer_weight"] > 1.0, "control plane must outrank workload"


class TestFactorsOnItems:
    def test_created_item_carries_its_priority_math(self):
        item_id = create_inbox_item(_make_item())
        item = get_inbox_item(item_id)
        factors = item["metadata"]["priority_factors"]
        assert factors["severity"] == "critical"
        assert factors["total"] > 0

    def test_recurrence_counts_the_visit_ordinal(self):
        first = create_inbox_item(_make_item())
        assert get_inbox_item(first)["metadata"]["recurrence_30d"] == 1
        update_item_status(first, "resolved")
        second = create_inbox_item(_make_item())
        assert get_inbox_item(second)["metadata"]["recurrence_30d"] == 2

    def test_no_correlation_key_no_recurrence_claim(self):
        item_id = create_inbox_item(_make_item(correlation_key=None))
        assert "recurrence_30d" not in get_inbox_item(item_id)["metadata"]


class TestSloImpact:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        inbox_mod._slo_defs_cache = (0.0, [])
        yield
        inbox_mod._slo_defs_cache = (0.0, [])

    def _with_defs(self, defs):
        return patch("sre_agent.slo_registry.list_slo_definitions", return_value=defs)

    def test_matches_on_namespace(self):
        with self._with_defs([{"service_name": "production", "slo_type": "availability", "target": 99.9}]):
            hits = _slo_impact("production", [])
        assert hits == [{"service": "production", "slo_type": "availability", "target": 99.9}]

    def test_matches_on_resource_name_prefix(self):
        with self._with_defs([{"service_name": "payment-api", "slo_type": "latency", "target": 99.0}]):
            hits = _slo_impact("prod", [{"kind": "Pod", "name": "payment-api-7f9d"}])
        assert len(hits) == 1

    def test_conservative_no_substring_fishing(self):
        """'api' must not match 'payment-api-7f9d' — a wrong SLO chip teaches
        the operator to ignore the right ones."""
        with self._with_defs([{"service_name": "api", "slo_type": "latency", "target": 99.0}]):
            hits = _slo_impact("prod", [{"kind": "Pod", "name": "payment-api-7f9d"}])
        assert hits == []

    def test_created_item_carries_slo_impact(self):
        with self._with_defs([{"service_name": "payment-api", "slo_type": "availability", "target": 99.9}]):
            item_id = create_inbox_item(_make_item())
        assert get_inbox_item(item_id)["metadata"]["slo_impact"][0]["service"] == "payment-api"


class TestRunbookFromItem:
    """POST /inbox/{id}/runbook folds an investigated item into the skill lifecycle."""

    def test_uninvestigated_item_is_refused(self):
        from fastapi import HTTPException

        item_id = create_inbox_item(_make_item())
        import asyncio

        from sre_agent.api.inbox_rest import create_runbook_from_item

        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_runbook_from_item(item_id, actor="admin"))
        assert exc.value.status_code == 409

    def test_investigated_item_becomes_a_draft_skill(self):
        import asyncio

        item = _make_item()
        item["metadata"].update(
            {
                "investigation_summary": "Pod exceeds its memory limit under peak load.",
                "suspected_cause": "Limit set below observed peak usage",
                "investigation_confidence": 0.82,
                "evidence": [{"observation": "OOMKilled 6x in 1h"}],
                "action_plan": [
                    {"title": "Inspect limits", "tool": "describe_pod"},
                    {"title": "Raise limit", "tool": "patch_deployment"},
                    {"title": "Watch it", "description": "no tool"},
                ],
            }
        )
        item_id = create_inbox_item(item)

        captured = {}

        def fake_learn(candidate):
            captured["candidate"] = candidate
            return "/skills/crashloop-payments/skill.md"

        from sre_agent.api.inbox_rest import create_runbook_from_item

        with patch("sre_agent.skill_lifecycle.learn_from_verified", side_effect=fake_learn):
            result = asyncio.run(create_runbook_from_item(item_id, actor="admin"))

        assert result["reviewed"] is False
        assert result["skill"] == "crashloop-payments"
        cand = captured["candidate"]
        assert cand.category == "crashloop"
        assert cand.root_cause == "Limit set below observed peak usage"
        assert cand.tools_called == ["describe_pod", "patch_deployment"]

    def test_missing_item_404s(self):
        import asyncio

        from fastapi import HTTPException

        from sre_agent.api.inbox_rest import create_runbook_from_item

        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_runbook_from_item("nope", actor="admin"))
        assert exc.value.status_code == 404
