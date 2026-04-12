"""Recorded replay harness for agent evaluation.

Patches the agent's tool map so K8s tools return pre-recorded responses
instead of making real API calls.  Runs the actual agent loop
(run_agent_streaming) and captures the response, tool calls, and timing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from ..agent import run_agent_streaming

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def list_fixtures() -> list[str]:
    """Return names of available fixture files (without .json extension)."""
    return sorted(p.stem for p in _FIXTURES_DIR.glob("*.json"))


def load_fixture(name: str) -> dict:
    """Load a fixture JSON file by name."""
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Replay harness
# ---------------------------------------------------------------------------


class ReplayHarness:
    """Run the agent against recorded K8s tool responses.

    Parameters
    ----------
    recorded_responses : dict[str, str]
        Maps tool name -> return value (string).  When the agent calls
        a tool whose name appears here the recorded value is returned
        instead of executing the real tool.
    """

    def __init__(self, recorded_responses: dict[str, Any]):
        self.recorded_responses = recorded_responses
        self.tool_calls: list[dict] = []

    # ----- public API -----

    def run(
        self,
        client: Any,
        prompt: str,
        system_prompt: str = "You are an SRE agent. Diagnose the issue.",
        tool_defs: list | None = None,
        tool_map: dict | None = None,
        write_tools: set[str] | None = None,
        thinking: dict | None = None,
    ) -> dict:
        """Execute the agent loop and return results.

        Parameters
        ----------
        client : Anthropic-compatible client (can be a mock).
        prompt : The user message to send.
        system_prompt : System prompt for the agent.
        tool_defs : Tool definitions (JSON schemas).  If *None*, minimal
            stubs are generated from *recorded_responses*.
        tool_map : Base tool map.  Recorded responses override entries.
        write_tools : Set of tool names requiring confirmation.

        Returns
        -------
        dict with keys ``response``, ``tool_calls``, ``duration_ms``.
        """
        self.tool_calls = []

        # Build the mock tool map
        effective_map = dict(tool_map or {})
        for name, value in self.recorded_responses.items():
            mock_tool = MagicMock()
            mock_tool.name = name
            mock_tool.call.return_value = value
            effective_map[name] = mock_tool

        # Build minimal tool defs if not provided
        if tool_defs is None:
            tool_defs = self._build_stub_defs()

        # Track every tool invocation via a callback
        def _on_tool_use(tool_name: str) -> None:
            self.tool_calls.append({"name": tool_name, "timestamp": time.time()})

        start = time.monotonic()
        kwargs: dict[str, Any] = {
            "client": client,
            "messages": [{"role": "user", "content": prompt}],
            "system_prompt": system_prompt,
            "tool_defs": tool_defs,
            "tool_map": effective_map,
            "write_tools": write_tools or set(),
            "on_tool_use": _on_tool_use,
        }
        if thinking is not None:
            kwargs["thinking"] = thinking
        response = run_agent_streaming(**kwargs)
        elapsed_ms = (time.monotonic() - start) * 1000

        return {
            "response": response,
            "tool_calls": list(self.tool_calls),
            "duration_ms": elapsed_ms,
        }

    # ----- helpers -----

    def _build_stub_defs(self) -> list[dict]:
        """Generate minimal tool definitions from recorded response keys."""
        defs = []
        for name in self.recorded_responses:
            defs.append(
                {
                    "name": name,
                    "description": f"Recorded stub for {name}",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                }
            )
        return defs


# ---------------------------------------------------------------------------
# Multi-turn replay harness
# ---------------------------------------------------------------------------


class MultiTurnReplayHarness:
    """Run a multi-turn conversation against recorded tool responses.

    Each turn has its own user prompt and can have different recorded
    responses (simulating state changes between turns).

    Parameters
    ----------
    turns : list[dict]
        Each turn has: ``prompt`` (str), ``recorded_responses`` (dict),
        and optionally ``expected`` (dict) for per-turn scoring.
    """

    def __init__(self, turns: list[dict]):
        self.turns = turns
        self.all_tool_calls: list[list[dict]] = []

    def run(
        self,
        client: Any,
        system_prompt: str = "You are an SRE agent. Diagnose the issue.",
        tool_defs: list | None = None,
        write_tools: set[str] | None = None,
        thinking: dict | None = None,
    ) -> dict:
        """Execute multi-turn conversation and return results per turn.

        Returns
        -------
        dict with keys ``turns`` (list of per-turn results), ``total_duration_ms``.
        """
        messages: list[dict] = []
        turn_results: list[dict] = []
        total_start = time.monotonic()

        # Build tool defs once from all turns' tools (reused across turns)
        if tool_defs is None:
            all_tool_names: set[str] = set()
            for t in self.turns:
                all_tool_names.update(t.get("recorded_responses", {}).keys())
            stub_defs = [
                {
                    "name": name,
                    "description": f"Recorded stub for {name}",
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                }
                for name in sorted(all_tool_names)
            ]
        else:
            stub_defs = tool_defs

        for i, turn in enumerate(self.turns):
            turn_tool_calls: list[dict] = []
            recorded = turn.get("recorded_responses", {})

            # Build mock tool map for this turn
            effective_map: dict[str, Any] = {}
            for name, value in recorded.items():
                mock_tool = MagicMock()
                mock_tool.name = name
                mock_tool.call.return_value = value
                effective_map[name] = mock_tool

            def _on_tool_use(tool_name: str) -> None:
                turn_tool_calls.append({"name": tool_name, "timestamp": time.time()})

            # Add user message
            messages.append({"role": "user", "content": turn["prompt"]})

            start = time.monotonic()
            kwargs: dict[str, Any] = {
                "client": client,
                "messages": list(messages),  # copy to avoid mutation
                "system_prompt": system_prompt,
                "tool_defs": stub_defs,
                "tool_map": effective_map,
                "write_tools": write_tools or set(),
                "on_tool_use": _on_tool_use,
            }
            if thinking is not None:
                kwargs["thinking"] = thinking

            response = run_agent_streaming(**kwargs)
            elapsed_ms = (time.monotonic() - start) * 1000

            # Add assistant response to history for next turn
            messages.append({"role": "assistant", "content": response})

            self.all_tool_calls.append(turn_tool_calls)
            turn_results.append(
                {
                    "turn": i + 1,
                    "prompt": turn["prompt"],
                    "response": response,
                    "tool_calls": turn_tool_calls,
                    "duration_ms": elapsed_ms,
                }
            )

        total_elapsed = (time.monotonic() - total_start) * 1000
        return {
            "turns": turn_results,
            "total_duration_ms": total_elapsed,
        }


# Synonym map — keyword can be matched by any synonym
_KEYWORD_SYNONYMS: dict[str, list[str]] = {
    "quota": ["quota", "resource limit", "limit exceeded", "resource constraint"],
    "scaled": ["scaled", "scale", "replicas", "replica count"],
    "memory": ["memory", "mem", "oom", "ram"],
    "cpu": ["cpu", "processor", "cores", "millicores"],
    "insufficient": ["insufficient", "not enough", "exhausted", "exceeded", "no capacity"],
    "database": ["database", "db", "postgres", "mysql", "sql"],
    "restart": ["restart", "rollout restart", "rolling restart"],
}


def _keyword_match(keyword: str, text: str) -> bool:
    """Check if keyword or any of its synonyms appear in text."""
    kw_lower = keyword.lower()
    if kw_lower in text:
        return True
    # Check synonyms
    synonyms = _KEYWORD_SYNONYMS.get(kw_lower, [])
    return any(syn in text for syn in synonyms)


def score_multi_turn(result: dict, expected: dict) -> dict:
    """Score a multi-turn replay result.

    Parameters
    ----------
    result : Return value of ``MultiTurnReplayHarness.run()``.
    expected : Dict with:
        - ``per_turn`` : list[dict] — per-turn expected checks (same format as score_replay)
        - ``overall_should_mention`` : list[str] — keywords in ANY turn response (supports synonyms)
        - ``max_total_tool_calls`` : int — budget across all turns
        - ``should_use_tools_in_order`` : list[str] — tools that must appear in this order across turns
        - ``should_use_tools`` : list[str] — tools that must be called (any order)
    """
    checks: list[dict] = []
    all_responses = " ".join(t["response"].lower() for t in result["turns"])
    all_tool_calls = [tc["name"] for t in result["turns"] for tc in t["tool_calls"]]

    # Per-turn checks
    for i, turn_expected in enumerate(expected.get("per_turn", [])):
        if i >= len(result["turns"]):
            break
        turn = result["turns"][i]
        turn_response = turn["response"].lower()
        turn_tools = [tc["name"] for tc in turn["tool_calls"]]

        for keyword in turn_expected.get("should_mention", []):
            found = _keyword_match(keyword, turn_response)
            checks.append({"check": f"turn {i + 1} mentions '{keyword}'", "passed": found, "weight": 1})

        for tool in turn_expected.get("should_use_tools", []):
            found = tool in turn_tools
            checks.append({"check": f"turn {i + 1} used tool '{tool}'", "passed": found, "weight": 1})

        for tool in turn_expected.get("should_not_use_tools", []):
            found = tool in turn_tools
            checks.append({"check": f"turn {i + 1} avoided tool '{tool}'", "passed": not found, "weight": 1})

    # Overall keyword checks (with synonym support)
    for keyword in expected.get("overall_should_mention", []):
        found = _keyword_match(keyword, all_responses)
        checks.append({"check": f"any turn mentions '{keyword}'", "passed": found, "weight": 1})

    # Required tools (any order) — more flexible than ordered check
    for tool in expected.get("should_use_tools", []):
        found = tool in all_tool_calls
        checks.append({"check": f"used tool '{tool}'", "passed": found, "weight": 1})

    # Tool budget
    max_calls = expected.get("max_total_tool_calls")
    if max_calls is not None:
        within = len(all_tool_calls) <= max_calls
        checks.append(
            {"check": f"total tool calls <= {max_calls} (actual: {len(all_tool_calls)})", "passed": within, "weight": 1}
        )

    # Tool ordering — soft check (weight: 0.5 instead of 1)
    # Failure here reduces score but doesn't cause outright FAIL
    ordered_tools = expected.get("should_use_tools_in_order", [])
    if ordered_tools:
        positions = []
        for tool in ordered_tools:
            try:
                pos = all_tool_calls.index(tool)
                positions.append(pos)
            except ValueError:
                positions.append(-1)
        in_order = all(p >= 0 for p in positions) and positions == sorted(positions)
        checks.append({"check": f"tools in order: {ordered_tools}", "passed": in_order, "weight": 0.5})

    # Compute score
    if not checks:
        return {"passed": True, "score": 100, "checks": [], "turns": len(result["turns"])}

    total_weight = sum(c["weight"] for c in checks)
    earned = sum(c["weight"] for c in checks if c["passed"])
    score = round(earned / total_weight * 100)

    # Pass threshold: 80% (not strict all-must-pass)
    return {
        "passed": score >= 80,
        "score": score,
        "checks": checks,
        "turns": len(result["turns"]),
        "total_tool_calls": all_tool_calls,
    }


# ---------------------------------------------------------------------------
# Deterministic scorer (no LLM needed)
# ---------------------------------------------------------------------------


def score_replay(result: dict, expected: dict) -> dict:
    """Score a replay result against expected criteria.

    Parameters
    ----------
    result : Return value of ``ReplayHarness.run()``.
    expected : Dict with optional keys:
        - ``should_mention``   : list[str] -- keywords that must appear
        - ``should_use_tools`` : list[str] -- tools that must be called
        - ``should_not_use_tools`` : list[str] -- tools that must NOT be called
        - ``max_tool_calls``   : int -- upper bound on total tool calls

    Returns
    -------
    dict with ``passed``, ``score`` (0-100), ``details``.
    """
    response_lower = result["response"].lower()
    called_tools = [tc["name"] for tc in result["tool_calls"]]

    checks: list[dict] = []

    # 1. Keyword mentions (with synonym support)
    for keyword in expected.get("should_mention", []):
        found = _keyword_match(keyword, response_lower)
        checks.append(
            {
                "check": f"mentions '{keyword}'",
                "passed": found,
                "weight": 1,
            }
        )

    # 2. Required tool usage
    for tool in expected.get("should_use_tools", []):
        found = tool in called_tools
        checks.append(
            {
                "check": f"used tool '{tool}'",
                "passed": found,
                "weight": 1,
            }
        )

    # 3. Forbidden tools
    for tool in expected.get("should_not_use_tools", []):
        found = tool in called_tools
        checks.append(
            {
                "check": f"avoided tool '{tool}'",
                "passed": not found,
                "weight": 1,
            }
        )

    # 4. Tool call budget
    max_calls = expected.get("max_tool_calls")
    if max_calls is not None:
        within = len(called_tools) <= max_calls
        checks.append(
            {
                "check": f"tool calls <= {max_calls} (actual: {len(called_tools)})",
                "passed": within,
                "weight": 1,
            }
        )

    # Compute score
    if not checks:
        return {
            "passed": True,
            "score": 100,
            "checks": [],
            "tool_calls": called_tools,
            "response_length": len(result["response"]),
        }

    total_weight = sum(c["weight"] for c in checks)
    earned = sum(c["weight"] for c in checks if c["passed"])
    score = round(earned / total_weight * 100)
    passed = all(c["passed"] for c in checks)

    return {
        "passed": passed,
        "score": score,
        "checks": checks,
        "tool_calls": called_tools,
        "response_length": len(result["response"]),
    }
