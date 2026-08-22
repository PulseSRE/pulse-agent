"""Tests for inner-loop budgets and context compaction."""

from __future__ import annotations

from sre_agent.loop_budget import (
    MIN_COMPACTABLE_CHARS,
    LoopBudget,
    compact_tool_results,
)


def _tool_result_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}


class TestExhaustion:
    def test_fresh_budget_is_not_exhausted(self):
        assert LoopBudget(max_iterations=25).exhausted() == ""

    def test_iteration_limit_is_named(self):
        b = LoopBudget(max_iterations=2)
        b.record_iteration()
        b.record_iteration()
        assert "iteration limit" in b.exhausted()

    def test_token_budget_is_named(self):
        b = LoopBudget(max_iterations=100, max_input_tokens=1000)
        b.record_iteration(600)
        assert b.exhausted() == ""
        b.record_iteration(600)
        assert "token budget" in b.exhausted()

    def test_no_token_ceiling_by_default(self):
        b = LoopBudget(max_iterations=100)
        b.record_iteration(10_000_000)
        assert b.exhausted() == ""

    def test_negative_usage_does_not_reduce_spend(self):
        b = LoopBudget(max_iterations=100, max_input_tokens=100)
        b.record_iteration(50)
        b.record_iteration(-40)
        assert b.input_tokens == 50


class TestWrapUpWarning:
    def test_warns_before_the_limit_not_at_it(self):
        b = LoopBudget(max_iterations=10)
        for _ in range(7):
            b.record_iteration()
        assert b.should_warn() is False
        b.record_iteration()  # 8th of 10 = 80%
        assert b.should_warn() is True
        # a turn remains in which to actually conclude
        assert b.exhausted() == ""

    def test_warns_only_once(self):
        b = LoopBudget(max_iterations=10)
        for _ in range(9):
            b.record_iteration()
        assert b.should_warn() is True
        assert b.should_warn() is False

    def test_notice_asks_for_a_conclusion_and_honesty_about_gaps(self):
        notice = LoopBudget().wrap_up_notice()
        assert "conclusion" in notice
        assert "could not check" in notice

    def test_token_pressure_also_warns(self):
        b = LoopBudget(max_iterations=1000, max_input_tokens=1000)
        b.record_iteration(850)
        assert b.should_warn() is True


class TestCutoffNotice:
    """A truncated answer still reads as an answer, so it must say it was truncated."""

    def test_notice_names_the_limit_and_the_work_done(self):
        b = LoopBudget(max_iterations=3)
        for _ in range(3):
            b.record_iteration()
        notice = b.cutoff_notice(b.exhausted())
        assert "stopped early" in notice
        assert "iteration limit" in notice
        assert "3 steps" in notice

    def test_notice_warns_the_findings_may_be_incomplete(self):
        notice = LoopBudget().cutoff_notice("iteration limit (25)")
        assert "incomplete" in notice
        assert "not ruled out" in notice


class TestCompaction:
    def test_short_conversations_are_untouched(self):
        msgs = [_tool_result_message("x" * 50_000) for _ in range(3)]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert out == msgs
        assert reclaimed == 0

    def test_recent_results_are_kept_raw(self):
        big = "x" * 50_000
        msgs = [_tool_result_message(big) for _ in range(8)]
        out, _ = compact_tool_results(msgs, keep_recent=6)
        for msg in out[-6:]:
            assert msg["content"][0]["content"] == big

    def test_older_results_are_compacted(self):
        big = "x" * 50_000
        msgs = [_tool_result_message(big) for _ in range(8)]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert reclaimed > 0
        assert len(out[0]["content"][0]["content"]) < 2000

    def test_compaction_says_what_it_dropped(self):
        msgs = [_tool_result_message("x" * 50_000) for _ in range(8)]
        out, _ = compact_tool_results(msgs, keep_recent=6)
        compacted = out[0]["content"][0]["content"]
        assert "compacted" in compacted
        assert "50,000 characters" in compacted
        assert "Re-run the tool" in compacted

    def test_small_results_are_not_worth_compacting(self):
        small = "y" * (MIN_COMPACTABLE_CHARS - 1)
        msgs = [_tool_result_message(small) for _ in range(8)]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert reclaimed == 0
        assert out[0]["content"][0]["content"] == small

    def test_assistant_messages_are_left_alone(self):
        msgs = [{"role": "assistant", "content": "z" * 50_000} for _ in range(8)]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert reclaimed == 0
        assert out == msgs

    def test_non_tool_result_blocks_survive(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "w" * 50_000}]} for _ in range(8)
        ]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert reclaimed == 0
        assert out[0]["content"][0]["text"] == "w" * 50_000

    def test_plain_string_content_is_safe(self):
        msgs = [{"role": "user", "content": "just text"} for _ in range(8)]
        out, reclaimed = compact_tool_results(msgs, keep_recent=6)
        assert reclaimed == 0
        assert out == msgs
