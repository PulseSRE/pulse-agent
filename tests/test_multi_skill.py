"""Tests for parallel multi-skill execution — routing, intent splitting, context bus."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sre_agent.config import get_settings
from sre_agent.orchestrator import split_compound_intent
from sre_agent.skill_loader import classify_query_multi
from sre_agent.skill_selector import SelectionResult

from .conftest import _mock_skill

# --- Config defaults ---


def test_multi_skill_config_defaults():
    s = get_settings()
    assert s.multi_skill is True
    assert s.multi_skill_threshold == 0.15
    assert s.multi_skill_max == 2


# --- split_compound_intent ---


def test_split_simple_conjunction():
    parts = split_compound_intent("check pod crashes and scan for CVEs")
    assert len(parts) == 2
    assert "check pod crashes" in parts[0]
    assert "scan for CVEs" in parts[1]


def test_split_no_conjunction():
    parts = split_compound_intent("why are pods crashing in production")
    assert len(parts) == 1
    assert parts[0] == "why are pods crashing in production"


def test_split_also_conjunction():
    parts = split_compound_intent("check memory usage, also run a security audit")
    assert len(parts) == 2


def test_split_preserves_single_and():
    """'and' inside a phrase should not split."""
    parts = split_compound_intent("check pods and services in namespace foo")
    assert len(parts) == 1


def test_split_triple_conjunction():
    parts = split_compound_intent("check pods and scan CVEs and review SLOs")
    assert len(parts) == 3


# --- SelectionResult.secondary_skill ---


def test_selection_result_secondary_skill_default():
    r = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.7},
        channel_scores={},
        threshold_used=0.3,
    )
    assert r.secondary_skill is None


def test_selection_result_secondary_skill_set():
    r = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.7},
        channel_scores={},
        threshold_used=0.3,
        secondary_skill="security",
    )
    assert r.secondary_skill == "security"


# --- classify_query_multi ---


def test_classify_query_multi_single_skill(set_orca_result):
    """When score gap is large, returns (primary, None)."""
    set_orca_result(
        SelectionResult(
            skill_name="sre",
            fused_scores={"sre": 0.9, "security": 0.3},
            channel_scores={},
            threshold_used=0.3,
            secondary_skill=None,
        )
    )
    with patch("sre_agent.skill_router.classify_query", return_value=_mock_skill("sre")):
        primary, secondary = classify_query_multi("check pod logs")
    assert primary.name == "sre"
    assert secondary is None


def test_classify_query_multi_two_skills_via_splitting(set_orca_result):
    """Compound query activates secondary via intent splitting fallback."""
    orca_no_secondary = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.3},
        channel_scores={},
        threshold_used=0.3,
        source="pre_route",
        secondary_skill=None,
    )
    set_orca_result(orca_no_secondary)

    mock_selector = MagicMock()
    mock_selector.select.return_value = orca_no_secondary

    with (
        patch("sre_agent.skill_router.classify_query") as mock_cq,
        patch("sre_agent.skill_loader._get_selector", return_value=mock_selector),
    ):
        mock_cq.side_effect = [_mock_skill("sre"), _mock_skill("sre"), _mock_skill("security")]
        primary, secondary = classify_query_multi("check crashes and scan for CVEs")
    assert primary.name == "sre"
    assert secondary is not None
    assert secondary.name == "security"


def test_classify_query_multi_two_skills_via_orca(set_orca_result):
    """Single-intent query with close ORCA scores returns secondary."""
    set_orca_result(
        SelectionResult(
            skill_name="sre",
            fused_scores={"sre": 0.8, "security": 0.7},
            channel_scores={},
            threshold_used=0.3,
            secondary_skill="security",
        )
    )
    with (
        patch("sre_agent.skill_router.classify_query", return_value=_mock_skill("sre")),
        patch("sre_agent.skill_loader.get_skill", side_effect=lambda n: _mock_skill(n)),
    ):
        primary, secondary = classify_query_multi("why are pods crashing")
    assert primary.name == "sre"
    assert secondary is not None
    assert secondary.name == "security"


def test_classify_query_multi_disabled():
    """When multi_skill is False, always returns (primary, None)."""
    with (
        patch("sre_agent.skill_router.classify_query", return_value=_mock_skill("sre")),
        patch("sre_agent.config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.routing = MagicMock(multi_skill=False, temporal_cache_ttl=60)
        _primary, secondary = classify_query_multi("check crashes and scan CVEs")
    assert secondary is None


# --- Context bus buffered publish ---


def test_context_bus_buffered_mode():
    import uuid

    from sre_agent.context_bus import ContextBus, ContextEntry

    bus = ContextBus(max_entries=100, ttl_seconds=3600)
    task_id = "parallel-123"
    tag = uuid.uuid4().hex[:8]

    bus.start_buffering(task_id)

    entry = ContextEntry(
        source="sre_agent",
        category="diagnosis",
        summary=f"buffered-{tag}",
        details={},
        parallel_task_id=task_id,
    )
    bus.publish(entry)

    results = bus.get_context_for(category="diagnosis", limit=20)
    buffered = [r for r in results if r.summary == f"buffered-{tag}"]
    assert len(buffered) == 0

    bus.flush_buffer(task_id)

    results = bus.get_context_for(category="diagnosis", limit=20)
    flushed = [r for r in results if r.summary == f"buffered-{tag}"]
    assert len(flushed) == 1


def test_context_bus_unbuffered_publishes_immediately():
    import uuid

    from sre_agent.context_bus import ContextBus, ContextEntry

    bus = ContextBus(max_entries=100, ttl_seconds=3600)
    tag = uuid.uuid4().hex[:8]
    entry = ContextEntry(
        source="sre_agent",
        category="diagnosis",
        summary=f"immediate-{tag}",
        details={},
    )
    bus.publish(entry)
    results = bus.get_context_for(category="diagnosis", limit=20)
    immediate = [r for r in results if r.summary == f"immediate-{tag}"]
    assert len(immediate) == 1


# --- Done event structure ---


def test_done_event_includes_multi_skill_metadata():
    from sre_agent.synthesis import Conflict

    conflicts = [
        Conflict(
            topic="scaling",
            skill_a="sre",
            position_a="scale up",
            skill_b="capacity_planner",
            position_b="node at limit",
        )
    ]
    done_event = {
        "type": "done",
        "full_response": "merged response",
        "skill_name": "sre",
        "multi_skill": {
            "skills": ["sre", "capacity_planner"],
            "conflicts": [
                {
                    "topic": c.topic,
                    "skill_a": c.skill_a,
                    "position_a": c.position_a,
                    "skill_b": c.skill_b,
                    "position_b": c.position_b,
                }
                for c in conflicts
            ],
        },
    }
    assert done_event["multi_skill"]["skills"] == ["sre", "capacity_planner"]
    assert len(done_event["multi_skill"]["conflicts"]) == 1
    assert done_event["multi_skill"]["conflicts"][0]["topic"] == "scaling"


# --- contextvars isolation ---


def test_last_selection_result_contextvar_isolation(set_orca_result):
    """ContextVar should not leak across contexts."""
    from sre_agent.skill_selector import get_last_selection_result

    set_orca_result(None)
    assert get_last_selection_result() is None

    result = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.9},
        channel_scores={},
        threshold_used=0.3,
    )
    set_orca_result(result)
    assert get_last_selection_result() is result


# --- run_parallel_skills ---


def _parallel_skill_patches(mock_client, create_called=True):
    """Common patches for run_parallel_skills tests."""
    return (
        patch("sre_agent.agent.create_async_client", return_value=mock_client),
        patch("sre_agent.agent.run_agent_streaming", new_callable=AsyncMock, return_value="test output"),
        patch(
            "sre_agent.skill_loader.build_config_from_skill",
            return_value={
                "system_prompt": "",
                "tool_defs": [],
                "tool_map": {},
                "write_tools": set(),
            },
        ),
    )


def test_run_parallel_skills_creates_client_when_none():
    """run_parallel_skills must create an API client when None is passed."""
    import asyncio

    from sre_agent.plan_runtime import run_parallel_skills

    mock_client = MagicMock()
    p1, p2, p3 = _parallel_skill_patches(mock_client)

    with p1 as mock_create, p2, p3:
        result = asyncio.run(
            run_parallel_skills(
                primary=_mock_skill("sre"),
                secondary=_mock_skill("security"),
                query="test",
                messages=[],
                client=None,
            )
        )
        mock_create.assert_called_once()
        assert result.primary_output == "test output"
        assert result.secondary_output == "test output"


def test_run_parallel_skills_uses_provided_client():
    """run_parallel_skills should not create a client when one is provided."""
    import asyncio

    from sre_agent.plan_runtime import run_parallel_skills

    mock_client = MagicMock()
    p1, p2, p3 = _parallel_skill_patches(mock_client)

    with p1 as mock_create, p2, p3:
        result = asyncio.run(
            run_parallel_skills(
                primary=_mock_skill("sre"),
                secondary=_mock_skill("security"),
                query="test",
                messages=[],
                client=mock_client,
            )
        )
        mock_create.assert_not_called()
        assert result.primary_output == "test output"


# --- SkillExecutor ---


def test_skill_output_dataclass():
    from sre_agent.api.agent_ws import SkillOutput

    out = SkillOutput(
        text="hello", tools_called=["list_pods"], components=[{"kind": "table"}], token_usage={"input_tokens": 100}
    )
    assert out.text == "hello"
    assert out.tools_called == ["list_pods"]
    assert out.components == [{"kind": "table"}]
    assert out.token_usage["input_tokens"] == 100


def test_skill_output_defaults():
    from sre_agent.api.agent_ws import SkillOutput

    out = SkillOutput()
    assert out.text == ""
    assert out.tools_called == []
    assert out.components == []
    assert out.token_usage == {}


def test_skill_executor_importable():
    from sre_agent.api.agent_ws import SkillExecutor, SkillOutput

    assert SkillExecutor is not None
    assert SkillOutput is not None


def test_parallel_result_has_token_fields():
    from sre_agent.synthesis import ParallelSkillResult

    r = ParallelSkillResult(
        primary_output="out1",
        secondary_output="out2",
        primary_skill="sre",
        secondary_skill="security",
        primary_confidence=0.8,
        secondary_confidence=0.7,
        duration_ms=1000,
        primary_tokens={"input_tokens": 500},
        secondary_tokens={"input_tokens": 300},
        primary_components=[{"kind": "table"}],
        secondary_components=[],
    )
    assert r.primary_tokens["input_tokens"] == 500
    assert r.secondary_tokens["input_tokens"] == 300
    assert len(r.primary_components) == 1
    assert len(r.secondary_components) == 0


# --- Empty output guard ---


def test_empty_output_detection():
    """Verify empty string detection for synthesis guard."""
    from sre_agent.synthesis import ParallelSkillResult

    r = ParallelSkillResult(
        primary_output="findings here",
        secondary_output="",
        primary_skill="sre",
        secondary_skill="security",
        primary_confidence=0.8,
        secondary_confidence=0.0,
        duration_ms=1000,
    )
    assert r.primary_output.strip()
    assert not r.secondary_output.strip()


# --- Eval scenarios ---


def test_compound_query_routes_to_two_skills(set_orca_result):
    """Compound SRE+Security query should activate multi-skill."""
    parts = split_compound_intent("check why pods are crashing and scan for vulnerabilities")
    assert len(parts) == 2

    orca_no_secondary = SelectionResult(
        skill_name="sre",
        fused_scores={"sre": 0.8, "security": 0.3},
        channel_scores={},
        threshold_used=0.3,
        source="pre_route",
        secondary_skill=None,
    )
    set_orca_result(orca_no_secondary)
    mock_selector = MagicMock()
    mock_selector.select.return_value = orca_no_secondary

    with (
        patch("sre_agent.skill_router.classify_query") as mock_cq,
        patch("sre_agent.skill_loader._get_selector", return_value=mock_selector),
    ):
        mock_cq.side_effect = [_mock_skill("sre"), _mock_skill("sre"), _mock_skill("security")]
        primary, secondary = classify_query_multi("check why pods are crashing and scan for vulnerabilities")
    assert primary.name == "sre"
    assert secondary is not None
    assert secondary.name == "security"


def test_single_domain_query_stays_single_skill(set_orca_result):
    """Pure SRE query should NOT activate multi-skill."""
    parts = split_compound_intent("why are pods crashing in the production namespace")
    assert len(parts) == 1

    set_orca_result(
        SelectionResult(
            skill_name="sre",
            fused_scores={"sre": 0.9, "security": 0.2},
            channel_scores={},
            threshold_used=0.3,
            secondary_skill=None,
        )
    )
    with patch("sre_agent.skill_router.classify_query", return_value=_mock_skill("sre")):
        primary, secondary = classify_query_multi("why are pods crashing in the production namespace")
    assert primary.name == "sre"
    assert secondary is None
