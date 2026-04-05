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
    k8s_tools,  # noqa: F401
    predict_tools,  # noqa: F401
    security_tools,  # noqa: F401
    timeline_tools,  # noqa: F401
    view_tools,  # noqa: F401
)
from sre_agent.tool_registry import TOOL_REGISTRY
from tests.eval_prompts import EVAL_PROMPTS, EXCLUDED_FROM_EVAL


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
