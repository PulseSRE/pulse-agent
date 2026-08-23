"""Every skill, tool, and plan template holds its declared contract.

Requested after "Create a skill called etcd-defrag ... It should check member
DB sizes" was read as a *continuation* (the incidental "It"), pinned to the
thread's previous specialist, and answered by an agent that had no
create_skill tool — three declared contracts (trigger patterns, requires_tools,
continuation semantics) all silently not holding at once.

These are deterministic contract checks, deliberately cheap enough to run on
every commit — the LLM-judged eval suites measure quality; this file measures
whether the wiring the skills declare actually exists.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import yaml

from sre_agent.skill_loader import list_skills
from sre_agent.skill_router import is_authoring_request, is_continuation
from sre_agent.tool_categories import TOOL_CATEGORIES

SKILLS_DIR = Path("sre_agent/skills")
TEMPLATES_DIR = Path("sre_agent/plan_templates")


def _all_registered_tool_names() -> set[str]:
    """The same tool universe build_config_from_skill resolves against."""
    from sre_agent.agent import TOOL_MAP as SRE_MAP
    from sre_agent.memory.memory_tools import MEMORY_TOOLS
    from sre_agent.security_agent import TOOL_MAP as SEC_MAP
    from sre_agent.tool_discovery import discover_tools
    from sre_agent.tool_registry import TOOL_REGISTRY
    from sre_agent.view_designer import TOOL_MAP as VD_MAP

    discover_tools()
    names = set(SRE_MAP) | set(SEC_MAP) | set(VD_MAP) | set(TOOL_REGISTRY)
    # Memory tools register only when the memory manager starts; they are
    # still legitimate category members.
    names |= {t.name for t in MEMORY_TOOLS}
    return names


class TestSkillContracts:
    def test_every_skill_parses_and_has_routing_signal(self):
        skills = list_skills()
        assert len(skills) >= 7, "built-in skills missing"
        for s in skills:
            assert s.keywords, f"skill '{s.name}' declares no keywords — unroutable by TF-IDF"
            assert s.description, f"skill '{s.name}' has no description"

    def test_every_trigger_pattern_compiles(self):
        for s in list_skills():
            for pat in s.trigger_patterns:
                try:
                    re.compile(pat, re.IGNORECASE)
                except re.error as e:
                    raise AssertionError(f"skill '{s.name}' trigger pattern {pat!r} does not compile: {e}") from e

    def test_every_required_tool_exists(self):
        registered = _all_registered_tool_names()
        for s in list_skills():
            missing = [t for t in s.requires_tools if t not in registered]
            assert not missing, (
                f"skill '{s.name}' requires tools that do not exist: {missing} — "
                "the skill would load while silently lacking its own declared core tools"
            )

    def test_conflicts_and_handoffs_name_real_skills(self):
        names = {s.name for s in list_skills()}
        for s in list_skills():
            for c in s.conflicts_with:
                assert c in names, f"skill '{s.name}' conflicts_with unknown skill '{c}'"
            for target in s.handoff_to or {}:
                assert target in names, f"skill '{s.name}' hands off to unknown skill '{target}'"


class TestToolContracts:
    def test_every_categorized_tool_is_registered(self):
        """A rename that orphans a category entry must fail loudly, not route to nothing."""
        registered = _all_registered_tool_names()
        for cat_name, cat in TOOL_CATEGORIES.items():
            if not isinstance(cat, dict):
                continue
            missing = [t for t in cat.get("tools", []) if t not in registered]
            assert not missing, f"category '{cat_name}' lists unregistered tools: {missing}"

    def test_every_registered_tool_serializes(self):
        from sre_agent.tool_discovery import discover_tools
        from sre_agent.tool_registry import TOOL_REGISTRY

        discover_tools()
        for name, tool in TOOL_REGISTRY.items():
            d = tool.to_dict()
            assert d.get("name") == name
            assert d.get("description"), f"tool '{name}' has no description — the model cannot choose it"


class TestPlanTemplateContracts:
    def test_every_template_loads_and_references_real_skills(self):
        skill_names = {s.name for s in list_skills()}
        templates = sorted(TEMPLATES_DIR.glob("*.yaml"))
        assert templates, "no plan templates found"
        for path in templates:
            data = yaml.safe_load(path.read_text())
            assert data.get("id"), f"{path.name}: template has no id"
            assert data.get("incident_type"), f"{path.name}: no incident_type — unmatchable"
            phases = data.get("phases") or []
            assert phases, f"{path.name}: template has no phases"
            ids = {p["id"] for p in phases}
            for p in phases:
                sk = p.get("skill_name")
                assert sk in skill_names, f"{path.name}: phase '{p['id']}' names unknown skill '{sk}'"
                for dep in p.get("depends_on", []):
                    assert dep in ids, f"{path.name}: phase '{p['id']}' depends on unknown phase '{dep}'"


class TestRoutingSemantics:
    """The two sentences that must never be confused again."""

    AUTHORING = (
        "Create a skill called etcd-defrag for diagnosing etcd fragmentation and "
        "slow disk syncs. It should check member DB sizes with get_prometheus_query."
    )
    CONTINUATION = "Scale it back to 3 then, we don't have the capacity"

    def test_authoring_imperative_is_a_hard_switch(self):
        assert is_authoring_request(self.AUTHORING), (
            "an explicit 'create a skill' must break continuation stickiness — "
            "the incidental 'It' pinned this to the wrong specialist"
        )

    def test_scale_it_back_stays_a_continuation(self):
        assert is_continuation(self.CONTINUATION)
        assert not is_authoring_request(self.CONTINUATION), (
            "bare topic words must not break stickiness — re-routing this "
            "mid-action was the original continuation regression"
        )

    def test_other_authoring_phrasings(self):
        for q in (
            "build me a runbook for etcd defrag",
            "edit the skill for slo checks",
            "write a plan template for oom",
        ):
            assert is_authoring_request(q), q
        for q in ("delete the pod", "create a namespace called test", "make it three replicas"):
            assert not is_authoring_request(q), q


class TestSkillPathToolInclusion:
    """The orchestrated skill path must honor capability queries like the mode path."""

    def _config_for(self, write_tools: bool, query: str):
        from sre_agent.skill_loader import Skill, build_config_from_skill

        skill = Skill(
            name="probe",
            version=1,
            description="probe",
            keywords=["probe"],
            categories=["monitoring"],
            write_tools=write_tools,
            priority=10,
            system_prompt="probe",
            path=Path("."),
        )
        return build_config_from_skill(skill, query=query)

    def test_capability_query_gets_read_only_self_describe_anywhere(self):
        cfg = self._config_for(False, "what tools do you have?")
        assert "describe_tools" in cfg["tool_map"]
        assert "create_skill" not in cfg["tool_map"], "write gate must still strip skill mutation"

    def test_create_skill_reaches_write_skills(self):
        cfg = self._config_for(True, "create a skill for etcd defrag")
        assert "create_skill" in cfg["tool_map"], (
            "a write-enabled skill asked to create a skill must actually hold create_skill — "
            "the apology-with-markdown failure mode"
        )


class TestRoutingEvals:
    """One unambiguous query per specialist must reach that specialist.

    We have seven skills; a routing regression that silently sends SLO work to
    the SRE generalist (or skill-authoring to slo_management, as actually
    happened) degrades answers without any error to notice. These run the real
    classify_query — hard pre-route, typo correction, ORCA fusion — with only
    the DB-backed channels degrading to empty.
    """

    CASES: typing.ClassVar = [
        ("pod payments-api is in CrashLoopBackOff and keeps restarting", "sre"),
        ("scan the cluster for rbac risks and privileged pods", "security"),
        ("create a dashboard showing node health and pod density", "view_designer"),
        ("build a skill for diagnosing etcd fragmentation", "plan_builder"),
        ("define an slo for checkout availability with a 99.9 target", "slo_management"),
        ("write a postmortem for yesterday's control plane outage", "postmortem"),
        ("do we have enough capacity headroom for 20% growth next quarter", "capacity_planner"),
    ]

    def test_each_specialist_wins_its_own_ground(self):
        from sre_agent.skill_router import classify_query

        misses = []
        for query, expected in self.CASES:
            skill = classify_query(query)
            if skill.name != expected:
                misses.append(f"{query!r} → {skill.name} (expected {expected})")
        assert not misses, "routing regressions:\n" + "\n".join(misses)

    def test_authoring_beats_stickiness_end_to_end(self):
        """The etcd-defrag failure, replayed: even mid-conversation, an explicit
        authoring request must classify to plan_builder."""
        from sre_agent.skill_router import classify_query

        q = (
            "Create a skill called etcd-defrag for diagnosing etcd fragmentation "
            "and slow disk syncs. It should check member DB sizes."
        )
        assert classify_query(q).name == "plan_builder"


class TestPlanInvocation:
    """Every plan template must be matchable AND executable — not just parseable.

    A template can pass every schema check and still never run: fuzzy substring
    matching can shadow it behind another template, or its phase graph can be
    un-executable in ways loading does not catch. These run each template
    through the real matcher and the real runtime control flow (phase
    execution stubbed to honor its declared `produces` contract — the LLM
    call is what the judged suites cover).
    """

    def test_every_template_matches_its_own_incident_type(self):
        from sre_agent.plan_templates import list_templates, load_templates, match_template

        load_templates()
        for meta in list_templates():
            incident_type = meta["incident_type"]
            matched = match_template(category=incident_type)
            assert matched is not None, f"template '{meta['id']}' unmatchable for its own incident_type"
            assert matched.incident_type == incident_type, (
                f"incident '{incident_type}' matched template '{matched.id}' instead of "
                f"'{meta['id']}' — substring shadowing in match_template"
            )

    def test_every_template_executes_through_the_runtime(self):
        import asyncio
        from unittest.mock import patch

        from sre_agent.plan_runtime import PlanRuntime
        from sre_agent.plan_templates import get_template, list_templates, load_templates
        from sre_agent.skill_plan import SkillOutput

        load_templates()

        async def fake_phase(self, phase, incident, prior_outputs):
            produced = {f: f"stub-{f}" for f in (getattr(phase, "produces", None) or [])}
            return SkillOutput(
                skill_id=phase.skill_name,
                phase_id=phase.id,
                status="complete",
                findings=produced,
                confidence=0.9,
            )

        incident = {"id": "f-eval", "category": "eval", "title": "plan invocation eval"}
        for meta in list_templates():
            plan = get_template(meta["incident_type"])
            started: list[str] = []
            with patch.object(PlanRuntime, "_execute_phase", fake_phase):
                result = asyncio.run(
                    PlanRuntime().execute(plan, incident, on_phase_start=lambda pid, sk: started.append(pid))
                )
            # An approval-gated phase escalating to a human is the runtime
            # working, not the plan failing — security-incident-v1 does this
            # by design. What must hold: every required phase produced an
            # output, and any non-complete output is a legitimate escalation
            # on a phase that actually declares an approval gate (or a
            # high-risk skill), never a silent skip.
            for phase in plan.phases:
                if not getattr(phase, "required", True):
                    continue
                out = result.phase_outputs.get(phase.id)
                assert out is not None, f"template '{plan.id}': required phase '{phase.id}' never ran"
                if out.status != "complete":
                    gated = getattr(phase, "approval_required", False)
                    assert out.status == "needs_escalation" and gated, (
                        f"template '{plan.id}': phase '{phase.id}' ended '{out.status}' "
                        "without an approval gate to justify it"
                    )


class TestToolReachability:
    """Every tool must be offerable by at least one skill.

    The generalized create_skill bug: a tool can exist, be registered, and be
    categorized — and still be reachable by no skill that would ever hold it,
    which means it exists only in theory. Memory tools are exempt (the memory
    manager injects them globally), as are MCP tools (dynamic).
    """

    def test_no_tool_is_stranded(self):
        from sre_agent.memory.memory_tools import MEMORY_TOOLS
        from sre_agent.skill_loader import build_config_from_skill, list_skills
        from sre_agent.tool_categories import _SELF_DESCRIBE_TOOLS

        universe = _all_registered_tool_names() - {t.name for t in MEMORY_TOOLS}

        offered: set[str] = set(_SELF_DESCRIBE_TOOLS)
        for skill in list_skills():
            cfg = build_config_from_skill(skill, query="")
            offered.update(cfg["tool_map"].keys())
            # Write tools are stripped for read-only skills but offerable
            # wherever write_tools is true.
            offered.update(cfg["write_tools"])

        stranded = sorted(universe - offered)
        assert not stranded, (
            f"tools no skill can ever offer: {stranded} — each needs a category one "
            "of the skills carries, a requires_tools entry, or deletion"
        )


class TestEvalScenarioToolNames:
    """Eval suites must reference tools that exist.

    When list_nodes was consolidated into list_resources, any scenario or
    replay fixture still expecting list_nodes would have kept scoring against
    a tool the agent can no longer call — evals quietly measuring a ghost.
    """

    def _universe(self) -> set[str]:
        names = _all_registered_tool_names()
        # Historical eval data may reference the superseded per-kind tools;
        # scenarios describe past interactions, fixtures describe expected
        # future ones. Only fixtures gate here.
        return names

    def test_fixture_expectations_name_real_tools(self):
        import json

        universe = self._universe()
        bad: list[str] = []
        for path in Path("sre_agent/evals/fixtures").glob("*.json"):
            d = json.loads(path.read_text())
            exp = d.get("expected") or {}
            for key in ("should_use_tools", "should_not_use_tools"):
                for name in exp.get(key) or []:
                    if name not in universe:
                        bad.append(f"{path.name}: {key} names unknown tool '{name}'")
        assert not bad, "replay fixtures reference nonexistent tools:\n" + "\n".join(bad)

    def test_scenario_tool_calls_name_real_tools(self):
        import json

        universe = self._universe()
        bad: list[str] = []
        for path in Path("sre_agent/evals/scenarios_data").glob("*.json"):
            d = json.loads(path.read_text())
            for sc in d.get("scenarios", []):
                for entry in sc.get("tool_calls") or []:
                    name = entry.get("name") if isinstance(entry, dict) else entry
                    if isinstance(name, str) and name and name not in universe:
                        bad.append(f"{path.name}:{sc.get('scenario_id', '?')} calls unknown tool '{name}'")
        assert not bad, "eval scenarios reference nonexistent tools:\n" + "\n".join(bad)
