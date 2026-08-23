"""Regression confirmation for the judged replay gate.

The gate blocked on any fixture that passed in the baseline and failed now.
Because these fixtures are scored by an LLM judge, four consecutive main
commits produced 32, 26, 28 and 27 passes with almost no overlap in *which*
fixtures failed — one run sat above the baseline and still failed the gate.
Main was permanently red for reasons unrelated to the product.
"""

from __future__ import annotations

from unittest.mock import patch

from sre_agent.evals.replay_cli import _confirm_regressions


def _runner(script: dict[str, list[bool]]):
    """Return a run_one that yields scripted pass/fail per fixture, in order."""
    calls: dict[str, int] = {}

    def run_one(name: str) -> dict:
        i = calls.get(name, 0)
        calls[name] = i + 1
        outcomes = script[name]
        passed = outcomes[min(i, len(outcomes) - 1)]
        return {"fixture": name, "score": {"passed": passed}}

    return run_one


def _confirm(suspected, script, retries=2):
    with patch("sre_agent.evals.replay_config.offline_context"):
        return _confirm_regressions(suspected, _runner(script), 1, retries)


class TestConfirmRegressions:
    def test_fixture_that_passes_on_retry_is_flaky_not_regressed(self):
        confirmed, flaky = _confirm(["a"], {"a": [True]})
        assert confirmed == []
        assert flaky == ["a"]

    def test_fixture_that_never_passes_is_confirmed(self):
        confirmed, flaky = _confirm(["a"], {"a": [False]})
        assert confirmed == ["a"]
        assert flaky == []

    def test_one_late_pass_still_clears_the_fixture(self):
        """Fails the first retry, passes the second — still noise, not a regression."""
        confirmed, flaky = _confirm(["a"], {"a": [False, True]}, retries=2)
        assert confirmed == []
        assert flaky == ["a"]

    def test_real_regression_survives_alongside_flake(self):
        confirmed, flaky = _confirm(["good", "bad"], {"good": [True], "bad": [False]})
        assert confirmed == ["bad"]
        assert flaky == ["good"]

    def test_retries_zero_is_handled_by_caller_not_here(self):
        """With no retries the suspected list is returned unchanged."""
        confirmed, flaky = _confirm(["a"], {"a": [True]}, retries=0)
        assert confirmed == ["a"]
        assert flaky == []

    def test_order_is_preserved(self):
        confirmed, _ = _confirm(["x", "y", "z"], {"x": [False], "y": [False], "z": [False]})
        assert confirmed == ["x", "y", "z"]

    def test_confirmed_fixtures_are_retried_not_the_whole_suite(self):
        """Only the suspects re-run; a clean run must cost nothing extra."""
        seen: list[str] = []

        def run_one(name: str) -> dict:
            seen.append(name)
            return {"fixture": name, "score": {"passed": False}}

        with patch("sre_agent.evals.replay_config.offline_context"):
            _confirm_regressions(["a"], run_one, 1, 2)
        assert set(seen) == {"a"}
