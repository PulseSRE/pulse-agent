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
    with open(path, "r", encoding="utf-8") as fh:
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
        response = run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            tool_map=effective_map,
            write_tools=write_tools or set(),
            on_tool_use=_on_tool_use,
        )
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
            defs.append({
                "name": name,
                "description": f"Recorded stub for {name}",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            })
        return defs


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

    # 1. Keyword mentions
    for keyword in expected.get("should_mention", []):
        found = keyword.lower() in response_lower
        checks.append({
            "check": f"mentions '{keyword}'",
            "passed": found,
            "weight": 1,
        })

    # 2. Required tool usage
    for tool in expected.get("should_use_tools", []):
        found = tool in called_tools
        checks.append({
            "check": f"used tool '{tool}'",
            "passed": found,
            "weight": 1,
        })

    # 3. Forbidden tools
    for tool in expected.get("should_not_use_tools", []):
        found = tool in called_tools
        checks.append({
            "check": f"avoided tool '{tool}'",
            "passed": not found,
            "weight": 1,
        })

    # 4. Tool call budget
    max_calls = expected.get("max_tool_calls")
    if max_calls is not None:
        within = len(called_tools) <= max_calls
        checks.append({
            "check": f"tool calls <= {max_calls} (actual: {len(called_tools)})",
            "passed": within,
            "weight": 1,
        })

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
