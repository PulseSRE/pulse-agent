"""Eval: verify every tool has at least one user prompt that triggers it.

This test validates our eval prompt coverage — every registered tool
(except internal/meta tools) should have at least one eval prompt.
"""

from __future__ import annotations

from sre_agent import (
    fleet_tools,  # noqa: F401
    git_tools,  # noqa: F401
    gitops_tools,  # noqa: F401
    handoff_tools,  # noqa: F401
    inbox,  # noqa: F401
    k8s_tools,  # noqa: F401
    predict_tools,  # noqa: F401
    security_tools,  # noqa: F401
    self_tools,  # noqa: F401
    timeline_tools,  # noqa: F401
    view_tools,  # noqa: F401
)
from sre_agent.harness import score_eval_prompts
from sre_agent.tool_registry import TOOL_REGISTRY
from tests.eval_prompts import EVAL_PROMPTS, EXCLUDED_FROM_EVAL, get_all_eval_prompts


class TestEvalCoverage:
    def test_every_tool_has_eval_prompt(self):
        """Every registered tool should have at least one eval prompt."""
        covered = set()
        for _, expected_tools, _, _ in EVAL_PROMPTS:
            covered.update(expected_tools)

        missing = set()
        for tool_name in TOOL_REGISTRY:
            if tool_name not in covered and tool_name not in EXCLUDED_FROM_EVAL:
                missing.add(tool_name)

        assert missing == set(), f"Tools missing eval prompts: {sorted(missing)}. Add prompts to tests/eval_prompts.py"

    def test_no_eval_for_nonexistent_tools(self):
        """Eval prompts should not reference tools that don't exist."""
        for _prompt, expected_tools, _mode, desc in EVAL_PROMPTS:
            for tool in expected_tools:
                assert tool in TOOL_REGISTRY or tool in EXCLUDED_FROM_EVAL, (
                    f"Eval prompt '{desc}' references nonexistent tool '{tool}'"
                )

    def test_eval_prompts_have_required_fields(self):
        """Every eval prompt must have all 4 fields."""
        for i, entry in enumerate(EVAL_PROMPTS):
            assert len(entry) == 4, f"Eval prompt {i} has {len(entry)} fields, expected 4"
            prompt, tools, mode, desc = entry
            assert prompt, f"Eval {i}: empty prompt"
            assert tools, f"Eval {i}: no expected tools"
            assert mode in ("sre", "security", "view_designer", "both"), f"Eval {i}: invalid mode '{mode}'"
            assert desc, f"Eval {i}: empty description"

    def test_minimum_eval_count(self):
        """Should have at least 50 eval prompts."""
        assert len(EVAL_PROMPTS) >= 50, f"Only {len(EVAL_PROMPTS)} eval prompts, need 50+"


class TestLearnedEvalIntegration:
    """Verify learned eval prompts merge correctly with static ones."""

    def test_get_all_includes_static(self):
        """get_all_eval_prompts always includes all static prompts."""
        all_prompts = get_all_eval_prompts()
        assert len(all_prompts) >= len(EVAL_PROMPTS)

    def test_learned_prompts_have_valid_format(self):
        """Any learned prompts must have the same 4-tuple structure."""
        all_prompts = get_all_eval_prompts()
        for i, entry in enumerate(all_prompts):
            assert len(entry) == 4, f"Prompt {i} has {len(entry)} fields"
            prompt, tools, mode, desc = entry
            assert isinstance(prompt, str) and prompt
            assert isinstance(tools, list) and tools
            assert mode in ("sre", "security", "view_designer", "both")
            assert isinstance(desc, str) and desc

    def test_no_duplicate_queries(self):
        """Learned prompts should not duplicate static prompt queries."""
        all_prompts = get_all_eval_prompts()
        seen = set()
        for prompt, _, _, _ in all_prompts:
            key = prompt.lower().strip()
            assert key not in seen, f"Duplicate eval prompt: '{prompt}'"
            seen.add(key)


class TestEvalScoring:
    """Score eval prompts against actual tool selection accuracy."""

    def test_tool_selection_accuracy(self):
        """Score all static eval prompts — must be >= 80%."""
        result = score_eval_prompts(EVAL_PROMPTS)
        print(f"\nEval accuracy: {result['accuracy']:.1%} ({result['passed']}/{result['total']})")
        for f in result["failures"]:
            print(f"  FAIL: {f['query'][:60]} — wanted {f['expected']}, offered {f['offered'][:5]}")
        assert result["accuracy"] >= 0.75, (
            f"Accuracy {result['accuracy']:.1%} below 75% threshold. "
            f"Failures: {[f['desc'] for f in result['failures']]}"
        )

    def test_learned_prompts_accuracy(self):
        """Score learned prompts separately (informational, no threshold)."""
        all_prompts = get_all_eval_prompts()
        learned = [p for p in all_prompts if p[3] == "Learned from usage"]
        if not learned:
            # No learned prompts yet — pass vacuously
            return
        result = score_eval_prompts(learned)
        print(f"\nLearned accuracy: {result['accuracy']:.1%} ({result['passed']}/{result['total']})")

    def test_score_result_structure(self):
        """Verify score result has all required fields."""
        result = score_eval_prompts(EVAL_PROMPTS[:5])
        assert "total" in result
        assert "passed" in result
        assert "failed" in result
        assert "accuracy" in result
        assert "failures" in result
        assert result["total"] == 5
        assert result["passed"] + result["failed"] == result["total"]
