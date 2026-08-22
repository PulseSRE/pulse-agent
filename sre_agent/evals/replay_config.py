"""Real Pulse configuration for the replay harness.

The replay gate runs the real model, so whatever configuration it hands the
agent loop is what the judge ends up scoring.  With the harness defaults —
a one-line system prompt and parameterless ``{"name": X}`` tool stubs — the
gate measured bare Sonnet with a K8s-flavoured prompt, not Pulse: no skill
prompt, no runbooks, no intent prefix, no component catalog, no tool
descriptions, no tool parameters.

This module rebuilds the configuration the ``/ws/agent`` endpoint uses
(``skill_router`` → ``build_orchestrated_config`` → ``assemble_prompt`` →
``build_cached_system_prompt``) and then makes it safe to replay:

* every entry of the tool map is replaced by a recorded stub, so there is no
  object in the map that can reach a cluster (see ``shadow_tool_map``);
* everything that reads a live cluster while the prompt is being built is
  patched out for the duration of the run (see ``offline_context``).

``build_replay_config(..., stub=True)`` returns the old stub configuration so
the two can be compared side by side.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any
from unittest.mock import patch

logger = logging.getLogger("pulse_agent.evals.replay_config")

# The pre-existing stub prompt, kept so --stub-config reproduces the old gate.
STUB_SYSTEM_PROMPT = "You are an SRE agent. Diagnose the issue."

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

_NO_RECORDING_TEMPLATE = (
    "REPLAY HARNESS: there is no recorded response for '{name}'. Nothing was executed — "
    "this evaluation runs against recorded data with no cluster attached. Treat this tool as "
    "unavailable for the rest of this session and continue from the evidence you already have; "
    "do not retry it."
)

# Mirrors sre_agent.api.ws_endpoints._HARD_SWITCH_* — used only if that module
# cannot be imported (it pulls in the whole web layer).
_FALLBACK_HARD_SWITCH_SRE = {
    "crash",
    "oom",
    "pending",
    "drain",
    "cordon",
    "crashloop",
    "node not ready",
    "why are",
    "what's wrong",
}
_FALLBACK_HARD_SWITCH_SEC = {"rbac", "scc", "vulnerability", "compliance", "privilege", "security audit"}

_BUILTIN_MODES = ("sre", "security", "view_designer", "both")


def no_recording_message(name: str) -> str:
    """Text returned when the agent calls a tool the fixture never recorded."""
    return _NO_RECORDING_TEMPLATE.format(name=name)


class RecordedTool:
    """Stand-in for a real tool that returns a recorded value.

    Carries the *real* tool's JSON schema when one is available, so that a
    caller which rebuilds ``tool_defs`` from the tool map (``run_agent_streaming``
    does this when the harness is enabled) still shows the model the real
    parameters rather than an empty object schema.
    """

    missing = False

    def __init__(self, name: str, value: Any, schema: dict | None = None):
        self.name = name
        self.value = value
        self.schema = schema
        self.calls: list[dict] = []

    def to_dict(self) -> dict:
        if self.schema:
            return dict(self.schema)
        return {
            "name": self.name,
            "description": f"Recorded stub for {self.name}",
            "input_schema": dict(_EMPTY_SCHEMA),
        }

    def call(self, input_data: dict) -> Any:
        self.calls.append(dict(input_data or {}))
        return self.value


class MissingRecordingTool(RecordedTool):
    """Stand-in for an offered tool the fixture never recorded.

    Returning a sentinel is deliberate: the real config offers far more tools
    than any one fixture recorded, and executing the real tool would put a live
    K8s/Prometheus call inside the eval.  The agent is told the tool is
    unavailable and its calls are reported so the fixture's coverage gaps are
    visible instead of silent.
    """

    missing = True

    def __init__(self, name: str, schema: dict | None = None):
        super().__init__(name, no_recording_message(name), schema)


def _schema_of(tool: Any) -> dict | None:
    """Return a tool's JSON schema, or None if it cannot be produced."""
    to_dict = getattr(tool, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        schema = to_dict()
    except Exception:
        logger.debug("Could not read schema for tool %r", getattr(tool, "name", tool), exc_info=True)
        return None
    return schema if isinstance(schema, dict) else None


def shadow_tool_map(base_map: dict, recorded_responses: dict[str, Any]) -> dict[str, RecordedTool]:
    """Replace every tool with a recorded stub — the single choke point for safety.

    Every key of *base_map* is rebuilt as a ``RecordedTool`` (when the fixture
    recorded it) or a ``MissingRecordingTool`` (when it did not), so no object
    capable of touching a cluster survives into the map the agent loop executes.
    Recorded tools absent from *base_map* are added, keeping the pre-existing
    behaviour that a recording is always honoured.
    """
    shadowed: dict[str, RecordedTool] = {}
    for name, tool in base_map.items():
        schema = _schema_of(tool)
        if name in recorded_responses:
            shadowed[name] = RecordedTool(name, recorded_responses[name], schema)
        else:
            shadowed[name] = MissingRecordingTool(name, schema)
    for name, value in recorded_responses.items():
        if name not in shadowed:
            shadowed[name] = RecordedTool(name, value)
    return shadowed


def _offline_cluster_context(*_args: Any, **_kwargs: Any) -> str:
    """Replacement for cluster-context gathering — never touches a cluster."""
    return ""


def _no_llm_tool_picker(*_args: Any, **_kwargs: Any) -> list[str]:
    """Replacement for the LLM tool picker — forces the category fallback."""
    return []


def _offline_slo_values(*_args: Any, **_kwargs: Any) -> dict[str, float]:
    """Replacement for the SLO registry's Prometheus read."""
    return {}


# (module-or-class path, attribute, replacement, required)
_ISOLATION_TARGETS: list[tuple[str, str, Any, bool]] = [
    ("sre_agent.harness", "get_cluster_context", _offline_cluster_context, True),
    ("sre_agent.harness", "gather_cluster_context", _offline_cluster_context, True),
    ("sre_agent.agent", "get_cluster_context", _offline_cluster_context, True),
    ("sre_agent.slo_registry.SLORegistry", "query_prometheus_values", _offline_slo_values, True),
]

_LLM_PICKER_TARGET: tuple[str, str, Any, bool] = (
    "sre_agent.tool_predictor",
    "llm_pick_tools",
    _no_llm_tool_picker,
    False,
)


# Isolation is process-wide because it patches module attributes, so concurrent
# fixtures must share one set of patches. Refcounted: the first entry applies
# them, the last exit removes them. Without this, one fixture finishing would
# restore the live cluster reads underneath every fixture still running.
_isolation_lock = threading.Lock()
_isolation_depth = 0
_isolation_stack: contextlib.ExitStack | None = None
_isolation_mode: bool | None = None


def _apply_isolation(allow_llm_tool_picker: bool) -> contextlib.ExitStack:
    """Apply every isolation patch, returning the stack that undoes them."""
    targets = list(_ISOLATION_TARGETS)
    if not allow_llm_tool_picker:
        targets.append(_LLM_PICKER_TARGET)

    stack = contextlib.ExitStack()
    try:
        for module, attr, replacement, required in targets:
            try:
                stack.enter_context(patch(f"{module}.{attr}", replacement))
            except (AttributeError, ImportError) as exc:
                if required:
                    raise RuntimeError(
                        f"Replay isolation failed: cannot patch {module}.{attr} ({exc}). "
                        "Replay refuses to run without it — a live cluster read could reach the eval."
                    ) from exc
                logger.debug("Replay isolation: could not patch %s.%s", module, attr, exc_info=True)
    except Exception:
        stack.close()
        raise
    return stack


@contextlib.contextmanager
def offline_context(*, allow_llm_tool_picker: bool = False):
    """Disable every live dependency the real config would otherwise reach.

    1. Cluster-context injection. ``prompt_builder.assemble_prompt`` (and the
       in-loop assembly inside ``run_agent_streaming``) calls
       ``harness.get_cluster_context``, which lists nodes, namespaces, pods and
       alerts against the live cluster at prompt-build time. Both the
       ``harness`` definition and the module-level alias imported into
       ``agent`` are patched, because either name may be the one called.
    2. The SLO registry's Prometheus read. Skill routing asks
       ``SLORegistry.get_context_for_selector()`` for burn-rate context, which
       runs a live PromQL query per registered SLO — and defaults are
       registered on first use, so this fires on every routed query.
    3. The LLM tool picker. ``select_tools_adaptive`` falls back to a live
       Claude call whenever the TF-IDF table is cold — which, in CI, is every
       fixture. That makes the offered tool set depend on a second model call
       that is not part of the fixture. Suppressing it falls back to the
       skill's category tool set, i.e. the superset the predictor picks from.
       Pass ``allow_llm_tool_picker=True`` to keep the live picker.

    A required patch that cannot be applied raises: silently losing cluster
    isolation because a symbol moved is the one failure this module exists to
    prevent, so it must break the eval rather than leak into it.
    """
    global _isolation_depth, _isolation_stack, _isolation_mode

    with _isolation_lock:
        if _isolation_depth == 0:
            _isolation_mode = allow_llm_tool_picker
            _isolation_stack = _apply_isolation(allow_llm_tool_picker)
        elif _isolation_mode != allow_llm_tool_picker:
            # Two concurrent entries disagreeing about the picker would leave one
            # of them running under isolation it did not ask for. Refuse rather
            # than silently applying the wrong one.
            raise RuntimeError(
                "Replay isolation is already active with "
                f"allow_llm_tool_picker={_isolation_mode!r}; cannot nest a run requesting "
                f"{allow_llm_tool_picker!r}. Run these separately."
            )
        _isolation_depth += 1

    try:
        yield
    finally:
        with _isolation_lock:
            _isolation_depth -= 1
            if _isolation_depth == 0 and _isolation_stack is not None:
                _isolation_stack.close()
                _isolation_stack = None
                _isolation_mode = None


def _hard_switch_keywords() -> tuple[set[str], set[str]]:
    """Return the ws endpoint's sticky-mode keyword sets, with a local fallback."""
    try:
        from ..api.ws_endpoints import _HARD_SWITCH_SEC, _HARD_SWITCH_SRE

        return set(_HARD_SWITCH_SRE), set(_HARD_SWITCH_SEC)
    except Exception:
        logger.debug("Could not import ws_endpoints hard-switch keywords; using local copy", exc_info=True)
        return set(_FALLBACK_HARD_SWITCH_SRE), set(_FALLBACK_HARD_SWITCH_SEC)


def classify_mode(query: str) -> str:
    """Route a query to a skill exactly the way the agent endpoint does."""
    try:
        from ..skill_router import classify_query

        skill = classify_query(query)
        if skill is not None:
            return skill.name
    except Exception:
        logger.debug("Skill routing failed for replay query; falling back", exc_info=True)

    try:
        from ..orchestrator import classify_intent

        return classify_intent(query)[0]
    except Exception:
        logger.debug("Legacy intent classification failed for replay query", exc_info=True)
        return "sre"


def resolve_mode(query: str, last_mode: str | None = None) -> str:
    """Classify *query*, applying the endpoint's sticky-mode rules for follow-ups.

    Multi-turn fixtures depend on this: "Add a memory chart to it" classifies as
    SRE on its own, but in a real session it stays in view_designer. Without the
    stickiness a multi-turn replay would swap the agent's whole toolset mid-
    conversation and measure something the product never does.
    """
    intent = classify_mode(query)
    if not last_mode or last_mode == intent:
        return intent

    q_lower = query.lower()
    hard_sre, hard_sec = _hard_switch_keywords()

    if last_mode == "view_designer":
        try:
            from ..skill_loader import get_skill

            current = get_skill(last_mode)
            is_conflicting = bool(current and intent in (current.conflicts_with or []))
        except Exception:
            is_conflicting = False
        is_custom_skill = intent not in _BUILTIN_MODES and not is_conflicting
        has_hard = any(kw in q_lower for kw in hard_sre) or any(kw in q_lower for kw in hard_sec)
        if not has_hard and not is_custom_skill:
            return "view_designer"
        return intent

    if last_mode not in _BUILTIN_MODES:
        # Custom skills are sticky unless the skill itself declares a handoff.
        try:
            from ..skill_loader import check_handoff, get_skill

            current = get_skill(last_mode)
            if current and not check_handoff(current, query):
                return last_mode
        except Exception:
            logger.debug("Skill handoff check failed during replay", exc_info=True)

    return intent


def build_stub_config(recorded_responses: dict[str, Any]) -> dict:
    """The pre-existing gate configuration: one-line prompt, parameterless stubs."""
    tool_defs = [
        {
            "name": name,
            "description": f"Recorded stub for {name}",
            "input_schema": dict(_EMPTY_SCHEMA),
        }
        for name in sorted(recorded_responses)
    ]
    return {
        "mode": "stub",
        "stub": True,
        "system_prompt": STUB_SYSTEM_PROMPT,
        "tool_defs": tool_defs,
        "tool_map": shadow_tool_map({}, recorded_responses),
        "write_tools": set(),
        "unoffered_recorded_tools": [],
    }


def _assemble_system_prompt(config: dict, mode: str, query: str, tool_names: list[str]) -> list[dict]:
    """Build the same system prompt the /ws/agent endpoint sends.

    Returns the cache-structured block list from ``build_cached_system_prompt``
    (static skill prompt + intent prefix + component catalog, then the dynamic
    runbook section). ``run_agent_streaming`` passes a list straight through to
    the API, so the agent sees exactly what production sees minus the live
    cluster state, which ``offline_context`` blanks out.
    """
    from ..harness import build_cached_system_prompt

    try:
        from ..prompt_builder import assemble_prompt
        from ..skill_loader import get_skill

        skill = get_skill(mode)
        if skill is not None:
            static, dynamic = assemble_prompt(skill, query, mode, tool_names)
            return build_cached_system_prompt(static, dynamic)
    except Exception:
        logger.warning("Replay prompt assembly failed for mode=%s; using the raw skill prompt", mode, exc_info=True)

    # Legacy modes (e.g. "both") have no skill file — use the config's own prompt.
    base = config.get("system_prompt") or STUB_SYSTEM_PROMPT
    hint = config.get("component_hint") or ""
    return build_cached_system_prompt(base + (f"\n\n{hint}" if hint else ""), "")


_registry_ready = False


def ensure_tool_registry() -> int:
    """Populate TOOL_REGISTRY the way the server does, before selecting tools.

    ``discover_tools()`` is called in the FastAPI lifespan and nowhere else, so the
    eval process ran with an empty registry. ``build_config_from_skill`` then fell
    back to the curated static maps in ``agent.py`` — a different, smaller tool
    universe than production. Replay was measuring an agent with a different set of
    tools than the one that actually ships, which is exactly what replay exists to
    rule out.

    Idempotent, and failure is not fatal: an eval that cannot import every tool
    module is still worth running against whatever registered, but it says so.
    """
    global _registry_ready
    if _registry_ready:
        from ..tool_registry import TOOL_REGISTRY

        return len(TOOL_REGISTRY)

    try:
        from ..tool_discovery import discover_tools

        discover_tools()
        _registry_ready = True
    except Exception:
        logger.warning("Tool discovery failed; replay will use the curated static tool maps", exc_info=True)

    from ..tool_registry import TOOL_REGISTRY

    count = len(TOOL_REGISTRY)
    logger.info("Replay tool registry: %d tools available", count)
    return count


def build_replay_config(
    query: str,
    recorded_responses: dict[str, Any],
    *,
    mode: str | None = None,
    last_mode: str | None = None,
    stub: bool = False,
    allow_llm_tool_picker: bool = False,
) -> dict:
    """Build the configuration a replay turn should run with.

    Args:
        query: The user prompt for this turn (drives routing and tool selection).
        recorded_responses: Fixture recordings, keyed by tool name.
        mode: Force a skill instead of routing the query.
        last_mode: Previous turn's mode, for the sticky-mode rules.
        stub: Reproduce the old one-line-prompt configuration.
        allow_llm_tool_picker: Let tool selection make its live fallback call.

    Returns a dict with ``mode``, ``system_prompt``, ``tool_defs``, ``tool_map``,
    ``write_tools``, ``unoffered_recorded_tools`` and ``stub``.  The ``tool_map``
    contains only recorded stubs.
    """
    if stub:
        return build_stub_config(recorded_responses)

    with offline_context(allow_llm_tool_picker=allow_llm_tool_picker):
        resolved = mode or resolve_mode(query, last_mode)

        from ..orchestrator import build_orchestrated_config

        ensure_tool_registry()
        config = build_orchestrated_config(resolved, query=query)
        real_map = dict(config.get("tool_map") or {})
        tool_defs = list(config.get("tool_defs") or [])
        write_tools = set(config.get("write_tools") or set())

        tool_map = shadow_tool_map(real_map, recorded_responses)
        # Recorded tools the real config did not offer: the model cannot call
        # them (they are absent from tool_defs), so surface them rather than
        # inventing a definition, which would hide a tool-selection miss.
        unoffered = sorted(name for name in recorded_responses if name not in real_map)

        system_prompt = _assemble_system_prompt(config, resolved, query, list(tool_map))

    return {
        "mode": resolved,
        "stub": False,
        "system_prompt": system_prompt,
        "tool_defs": tool_defs,
        "tool_map": tool_map,
        "write_tools": write_tools,
        "unoffered_recorded_tools": unoffered,
    }
