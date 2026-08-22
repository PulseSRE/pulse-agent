"""Recorded replay harness for agent evaluation.

Runs the real agent loop (``run_agent_streaming``) against Pulse's real
configuration — the routed skill's system prompt, runbooks, component catalog
and full tool schemas — with every tool replaced by a recorded response, so
the model does the real work and no cluster is ever touched.

Safety, in one place: ``replay_config.shadow_tool_map`` rebuilds *every* entry
of the tool map as a stub before the loop starts, and
``replay_config.offline_context`` patches out cluster reads that happen while
the prompt is assembled.  See ``replay_config`` for the details.

Pass ``stub_config=True`` to reproduce the old configuration (one-line prompt,
parameterless tool stubs) for comparison.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..agent import run_agent_streaming
from .replay_config import build_replay_config, offline_context, shadow_tool_map

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


async def _replay_confirm(tool_name: str, input_data: dict) -> bool:
    """Approve write tools during replay.

    The real config hands the agent write tools behind a confirmation gate.
    With no callback the loop denies them, so fixtures that record a write
    (scale_deployment, rollback_deployment) would measure a refusal rather
    than the recorded outcome.  Approving is safe here precisely because the
    tool map contains nothing but recorded stubs.
    """
    return True


def _unrecorded_calls(effective_map: dict) -> list[str]:
    """Names of tools the agent called that the fixture never recorded."""
    return sorted(name for name, tool in effective_map.items() if getattr(tool, "missing", False) and tool.calls)


class ReplayHarness:
    """Run the agent against recorded K8s tool responses.

    Parameters
    ----------
    recorded_responses : dict[str, str]
        Maps tool name -> return value (string).  When the agent calls
        a tool whose name appears here the recorded value is returned
        instead of executing the real tool.
    mode : str | None
        Force a skill instead of routing the prompt.
    stub_config : bool
        Reproduce the pre-existing configuration (one-line system prompt,
        parameterless tool stubs) instead of Pulse's real one.
    allow_llm_tool_picker : bool
        Let adaptive tool selection make its live Claude fallback call.
    """

    def __init__(
        self,
        recorded_responses: dict[str, Any],
        *,
        mode: str | None = None,
        stub_config: bool = False,
        allow_llm_tool_picker: bool = False,
    ):
        self.recorded_responses = recorded_responses
        self.mode = mode
        self.stub_config = stub_config
        self.allow_llm_tool_picker = allow_llm_tool_picker
        self.tool_calls: list[dict] = []
        self.config: dict | None = None

    # ----- public API -----

    def run(
        self,
        client: Any,
        prompt: str,
        system_prompt: Any = None,
        tool_defs: list | None = None,
        tool_map: dict | None = None,
        write_tools: set[str] | None = None,
        thinking: dict | None = None,
        config: dict | None = None,
    ) -> dict:
        """Execute the agent loop and return results.

        Parameters
        ----------
        client : Anthropic-compatible client (can be a mock).
        prompt : The user message to send.
        system_prompt : Overrides the configuration's system prompt.
        tool_defs : Overrides the configuration's tool definitions.
        tool_map : Extra tools to merge in.  Every entry is replaced by a
            recorded stub before execution — a real tool passed here is
            never called.
        write_tools : Overrides the configuration's write-tool set.
        config : A pre-built config from ``build_replay_config``.

        Returns
        -------
        dict with keys ``response``, ``tool_calls``, ``duration_ms``, ``mode``,
        ``unrecorded_tool_calls`` and ``unoffered_recorded_tools``.
        """
        self.tool_calls = []

        cfg = config or build_replay_config(
            prompt,
            self.recorded_responses,
            mode=self.mode,
            stub=self.stub_config,
            allow_llm_tool_picker=self.allow_llm_tool_picker,
        )
        self.config = cfg

        # Recorded responses shadow everything: the config's map is already
        # stubbed, and any caller-supplied map is re-shadowed here so no real
        # tool object can reach the agent loop.
        base_map = dict(cfg["tool_map"])
        if tool_map:
            base_map.update(tool_map)
        effective_map = shadow_tool_map(base_map, self.recorded_responses)

        # Track every tool invocation via a callback
        async def _on_tool_use(tool_name: str) -> None:
            self.tool_calls.append({"name": tool_name, "timestamp": time.time()})

        start = time.monotonic()
        kwargs: dict[str, Any] = {
            "client": client,
            "messages": [{"role": "user", "content": prompt}],
            "system_prompt": cfg["system_prompt"] if system_prompt is None else system_prompt,
            "tool_defs": cfg["tool_defs"] if tool_defs is None else tool_defs,
            "tool_map": effective_map,
            "write_tools": set(cfg["write_tools"]) if write_tools is None else write_tools,
            "on_tool_use": _on_tool_use,
            "on_confirm": _replay_confirm,
            "mode": cfg["mode"],
        }
        if thinking is not None:
            kwargs["thinking"] = thinking
        with offline_context(allow_llm_tool_picker=self.allow_llm_tool_picker):
            response = asyncio.run(run_agent_streaming(**kwargs))
        elapsed_ms = (time.monotonic() - start) * 1000

        return {
            "response": response,
            "tool_calls": list(self.tool_calls),
            "duration_ms": elapsed_ms,
            "mode": cfg["mode"],
            "offered_tool_count": len(cfg["tool_defs"]),
            "unrecorded_tool_calls": _unrecorded_calls(effective_map),
            "unoffered_recorded_tools": list(cfg.get("unoffered_recorded_tools", [])),
        }

    # ----- helpers -----

    def _build_stub_defs(self) -> list[dict]:
        """Generate minimal tool definitions from recorded response keys."""
        from .replay_config import build_stub_config

        return build_stub_config(self.recorded_responses)["tool_defs"]


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

    def __init__(
        self,
        turns: list[dict],
        *,
        mode: str | None = None,
        stub_config: bool = False,
        allow_llm_tool_picker: bool = False,
    ):
        self.turns = turns
        self.mode = mode
        self.stub_config = stub_config
        self.allow_llm_tool_picker = allow_llm_tool_picker
        self.all_tool_calls: list[list[dict]] = []
        self.configs: list[dict] = []

    def run(
        self,
        client: Any,
        system_prompt: Any = None,
        tool_defs: list | None = None,
        write_tools: set[str] | None = None,
        thinking: dict | None = None,
    ) -> dict:
        """Execute multi-turn conversation and return results per turn.

        Each turn is configured the way a real session would be: the turn's
        prompt is routed to a skill (with the endpoint's sticky-mode rules
        applied to follow-ups), and that skill's prompt and tools are used.

        Returns
        -------
        dict with keys ``turns`` (list of per-turn results), ``total_duration_ms``.
        """
        messages: list[dict] = []
        turn_results: list[dict] = []
        total_start = time.monotonic()
        self.configs = []
        last_mode: str | None = None

        for i, turn in enumerate(self.turns):
            turn_tool_calls: list[dict] = []
            recorded = turn.get("recorded_responses", {})

            cfg = build_replay_config(
                turn["prompt"],
                recorded,
                mode=self.mode,
                last_mode=last_mode,
                stub=self.stub_config,
                allow_llm_tool_picker=self.allow_llm_tool_picker,
            )
            last_mode = cfg["mode"]
            self.configs.append(cfg)

            # Recorded responses shadow every tool the config offers.
            effective_map = shadow_tool_map(dict(cfg["tool_map"]), recorded)

            async def _on_tool_use(tool_name: str, _calls: list[dict] = turn_tool_calls) -> None:
                _calls.append({"name": tool_name, "timestamp": time.time()})

            # Add user message
            messages.append({"role": "user", "content": turn["prompt"]})

            start = time.monotonic()
            kwargs: dict[str, Any] = {
                "client": client,
                # The live list, not a copy. run_agent_streaming appends this turn's
                # assistant tool_use and user tool_result blocks to it, and a real
                # conversation carries those into the next turn. Copying here meant
                # turn 2 saw only turn 1's final prose — the agent was asked follow-up
                # questions about data it could no longer see, which is why the
                # multi-turn fixtures scored so far below the single-turn ones.
                "messages": messages,
                "system_prompt": cfg["system_prompt"] if system_prompt is None else system_prompt,
                "tool_defs": cfg["tool_defs"] if tool_defs is None else tool_defs,
                "tool_map": effective_map,
                "write_tools": set(cfg["write_tools"]) if write_tools is None else write_tools,
                "on_tool_use": _on_tool_use,
                "on_confirm": _replay_confirm,
                "mode": cfg["mode"],
            }
            if thinking is not None:
                kwargs["thinking"] = thinking

            with offline_context(allow_llm_tool_picker=self.allow_llm_tool_picker):
                response = asyncio.run(run_agent_streaming(**kwargs))
            elapsed_ms = (time.monotonic() - start) * 1000

            # run_agent_streaming has already appended the assistant turn (including
            # tool_use blocks) and the tool_result messages. Only append the final
            # text if it did not, so history is neither lost nor duplicated.
            if not (messages and messages[-1].get("role") == "assistant"):
                messages.append({"role": "assistant", "content": response})

            self.all_tool_calls.append(turn_tool_calls)
            turn_results.append(
                {
                    "turn": i + 1,
                    "prompt": turn["prompt"],
                    "response": response,
                    "tool_calls": turn_tool_calls,
                    "duration_ms": elapsed_ms,
                    "mode": cfg["mode"],
                    "unrecorded_tool_calls": _unrecorded_calls(effective_map),
                    "unoffered_recorded_tools": list(cfg.get("unoffered_recorded_tools", [])),
                }
            )

        total_elapsed = (time.monotonic() - total_start) * 1000
        return {
            "turns": turn_results,
            "total_duration_ms": total_elapsed,
            "modes": [c["mode"] for c in self.configs],
            "unrecorded_tool_calls": sorted({n for t in turn_results for n in t["unrecorded_tool_calls"]}),
            "unoffered_recorded_tools": sorted({n for t in turn_results for n in t["unoffered_recorded_tools"]}),
        }


# Synonym map — keyword can be matched by any synonym
_KEYWORD_SYNONYMS: dict[str, list[str]] = {
    "quota": ["quota", "resource limit", "limit exceeded", "resource constraint", "forbidden", "exceeded"],
    "exceeded": ["exceeded", "exhausted", "over limit", "forbidden", "quota"],
    "scaled": ["scaled", "scale", "replicas", "replica count"],
    "memory": ["memory", "mem", "oom", "ram"],
    "cpu": ["cpu", "processor", "cores", "millicores"],
    "insufficient": ["insufficient", "not enough", "exhausted", "exceeded", "no capacity"],
    "database": ["database", "db", "postgres", "mysql", "sql"],
    "restart": ["restart", "rollout restart", "rolling restart"],
    "connection": ["connection", "connect", "connectivity", "refused", "unreachable"],
    # Orthography only. "RoleBinding" is the literal Kubernetes API kind and the
    # more correct spelling, and a fixture demanding "role binding" with a space
    # failed an answer the judge scored 91. Matching is plain substring, so
    # "rolebinding" also covers clusterrolebinding — harmless, since every
    # fixture using the term is about RBAC bindings.
    "role binding": ["role binding", "rolebinding", "role-binding"],
    # Restatements of the same term of art, and nothing weaker: "over-privileged"
    # and "dangerous" are different claims and are deliberately absent, so this
    # cannot manufacture a pass for an answer that never named the risk.
    "privilege escalation": [
        "privilege escalation",
        "privilege-escalation",
        "privesc",
        "escalation of privilege",
        "escalate privileges",
        "escalated privileges",
    ],
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
            checks.append(
                {"check": f"turn {i + 1} mentions '{keyword}'", "passed": found, "weight": 1, "kind": "content"}
            )

        for tool in turn_expected.get("should_use_tools", []):
            found = tool in turn_tools
            checks.append(
                {"check": f"turn {i + 1} used tool '{tool}'", "passed": found, "weight": 1, "kind": "structure"}
            )

        for tool in turn_expected.get("should_not_use_tools", []):
            found = tool in turn_tools
            checks.append(
                {"check": f"turn {i + 1} avoided tool '{tool}'", "passed": not found, "weight": 1, "kind": "structure"}
            )

    # Overall keyword checks (with synonym support)
    for keyword in expected.get("overall_should_mention", []):
        found = _keyword_match(keyword, all_responses)
        checks.append({"check": f"any turn mentions '{keyword}'", "passed": found, "weight": 1, "kind": "content"})

    # Required tools (any order) — more flexible than ordered check
    for tool in expected.get("should_use_tools", []):
        found = tool in all_tool_calls
        checks.append({"check": f"used tool '{tool}'", "passed": found, "weight": 1, "kind": "structure"})

    # Tool budget
    max_calls = expected.get("max_total_tool_calls")
    if max_calls is not None:
        within = len(all_tool_calls) <= max_calls
        checks.append(
            {
                "check": f"total tool calls <= {max_calls} (actual: {len(all_tool_calls)})",
                "passed": within,
                "weight": 1,
                "kind": "structure",
            }
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
        checks.append(
            {"check": f"tools in order: {ordered_tools}", "passed": in_order, "weight": 0.5, "kind": "structure"}
        )

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
                "kind": "content",
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
                "kind": "structure",
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
                "kind": "structure",
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
                "kind": "structure",
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
