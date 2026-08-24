"""Run pulse-agent against SRE-Bench's simulated cluster fixtures.

This adapter drives the real production agent — orchestrated skill config,
system prompt, tool definitions, write-tool confirmation gates, and the full
``run_agent_streaming`` loop — with one substitution: every tool's
*implementation* is backed by the bench's ``SimCluster`` instead of a live
cluster. The sim observes the run and sets the integrity flags
(``verification_passed``, ``had_policy_violation``, ``hallucinated_tool``,
rejections), so Pulse's score comes from the observed lane, exactly like the
plain-model baseline it is compared against.

Usage, from an environment with pulse-agent installed, Vertex credentials,
and ``sre_bench`` on ``PYTHONPATH``::

    python3 -m sre_bench.cli run \
        --adapter sre_agent.evals.srebench_adapter:factory \
        --all --sim --out pulse-sim.json --score

Notes on fidelity:

- Tool defs are restricted to names in the bench's canonical registry, so the
  hallucination check stays meaningful (a canonical-only menu means any
  out-of-registry call really is invented).
- Verification contracts are suspended for the run: their precondition and
  postcondition probes would query the *real* cluster about the fixture's
  fictional resources and wrongly veto writes. The sim itself performs the
  affirmative post-check observation that contracts provide in production.
- Confirmation requests are auto-approved (a maximally permissive human), and
  approved write-tool calls carry ``confirmed=true`` into the sim. Refusing
  outright — the correct behavior on trap tasks — must come from the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class _SimBackedTool:
    """A pulse tool whose implementation is the bench's simulated cluster.

    Implements pulse's full tool protocol (``name``, ``to_dict()``, ``call()``)
    — parts of the loop (skill routing) rebuild tool defs from the tool map,
    so the wrapper must carry the original definition, not just the callable.
    """

    def __init__(self, tool_def: dict, backend, is_write: bool):
        self.name = tool_def["name"]
        self._def = tool_def
        self._backend = backend
        self._is_write = is_write

    def to_dict(self) -> dict:
        return self._def

    def call(self, input_data: dict) -> str:
        args = dict(input_data or {})
        if self._is_write:
            # The agent loop only executes write tools after its confirmation
            # gate; carry that approval into the sim's destructive-tool gate.
            args["confirmed"] = True
        return json.dumps(self._backend.call(self.name, **args))


class PulseAgentAdapter:
    def run(self, task, backend=None):
        from sre_bench.fixtures.registry import CANONICAL_TOOLS
        from sre_bench.types import Trajectory

        if backend is None:
            return Trajectory(scenario_id=task.scenario_id, completed=False, final_response="sim backend required")

        from sre_agent import tool_contracts
        from sre_agent.agent import create_async_client, run_agent_streaming
        from sre_agent.orchestrator import build_orchestrated_config

        config = build_orchestrated_config("sre", query=task.task)
        write_tools = set(config.get("write_tools") or set())
        tool_defs = [d for d in config["tool_defs"] if d.get("name") in CANONICAL_TOOLS]
        offered = {d["name"] for d in tool_defs}
        tool_map = {d["name"]: _SimBackedTool(d, backend, d["name"] in write_tools) for d in tool_defs}

        async def on_confirm(tool_name: str, tool_input: dict) -> bool:
            return True

        async def _run() -> str:
            client = create_async_client()
            try:
                return await run_agent_streaming(
                    client=client,
                    messages=[{"role": "user", "content": task.task}],
                    system_prompt=config["system_prompt"],
                    tool_defs=tool_defs,
                    tool_map=tool_map,
                    write_tools=write_tools & offered,
                    on_confirm=on_confirm,
                    mode="sre",
                )
            finally:
                try:
                    await client.close()
                except Exception:
                    logger.debug("client close failed", exc_info=True)

        try:
            with tool_contracts.suspended():
                text = asyncio.run(_run())
            return Trajectory(scenario_id=task.scenario_id, final_response=text or "", completed=bool(text))
        except Exception as exc:
            logger.warning("pulse-agent bench run failed for %s", task.scenario_id, exc_info=True)
            return Trajectory(
                scenario_id=task.scenario_id,
                completed=False,
                final_response=f"agent run failed: {type(exc).__name__}: {exc}",
            )


def factory() -> PulseAgentAdapter:
    return PulseAgentAdapter()
