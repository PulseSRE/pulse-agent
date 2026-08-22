"""Tests for skill loader."""

from __future__ import annotations

from pathlib import Path

from sre_agent.skill_loader import (
    Skill,
    _parse_skill_md,
    check_handoff,
    classify_query,
    get_mode_categories,
    get_skill,
    list_skills,
    load_skill_evals,
    load_skills,
)


class TestLoadSkills:
    def test_loads_built_in_skills(self):
        skills = load_skills()
        names = set(skills.keys())
        assert "sre" in names
        assert "security" in names
        assert "view_designer" in names

    def test_at_least_3_skills(self):
        skills = load_skills()
        assert len(skills) >= 3

    def test_capacity_planner_loaded(self):
        skills = load_skills()
        assert "capacity_planner" in skills

    def test_list_skills(self):
        result = list_skills()
        assert len(result) >= 3
        assert all(isinstance(s, Skill) for s in result)


class TestParseSkillMd:
    def test_parse_sre(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "sre"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "sre"
        assert skill.version >= 1
        assert skill.write_tools is True
        assert len(skill.keywords) > 5
        assert len(skill.categories) >= 5
        assert "Security" in skill.system_prompt

    def test_parse_security(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "security"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "security"
        assert skill.write_tools is False
        assert "request_sre_investigation" in skill.requires_tools

    def test_parse_view_designer(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "view-designer"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "view_designer"
        assert skill.write_tools is False
        assert "create_dashboard" in skill.requires_tools

    def test_parse_capacity_planner(self):
        skill_dir = Path(__file__).parent.parent / "sre_agent" / "skills" / "capacity-planner"
        skill = _parse_skill_md(skill_dir / "skill.md")
        assert skill is not None
        assert skill.name == "capacity_planner"
        assert len(skill.configurable) >= 2
        assert skill.handoff_to.get("sre") is not None

    def test_invalid_file_returns_none(self, tmp_path):
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("no frontmatter here")
        assert _parse_skill_md(bad_file) is None

    def test_missing_name_returns_none(self, tmp_path):
        bad_file = tmp_path / "noname.md"
        bad_file.write_text("---\nversion: 1\n---\nSome prompt")
        assert _parse_skill_md(bad_file) is None


class TestClassifyQuery:
    def test_sre_query(self):
        skill = classify_query("why is my pod crashlooping in production?")
        assert skill.name == "sre"

    def test_security_query(self):
        skill = classify_query("scan for RBAC vulnerabilities")
        assert skill.name == "security"

    def test_view_designer_query(self):
        skill = classify_query("create a dashboard for production")
        assert skill.name == "view_designer"

    def test_capacity_query(self):
        skill = classify_query("how much headroom do we have on the cluster?")
        assert skill.name == "capacity_planner"

    def test_capacity_forecast(self):
        skill = classify_query("will we run out of CPU?")
        assert skill.name == "capacity_planner"

    def test_fallback_to_sre(self):
        skill = classify_query("hello")
        assert skill.name == "sre"  # default fallback

    def test_longer_keywords_win(self):
        # "create view" is longer than "create" so view_designer should win
        skill = classify_query("create view for monitoring")
        assert skill.name == "view_designer"


class TestHandoff:
    def test_sre_to_view_designer(self):
        sre = get_skill("sre")
        assert sre is not None
        target = check_handoff(sre, "now create a dashboard")
        assert target is not None
        assert target.name == "view_designer"

    def test_sre_to_security(self):
        sre = get_skill("sre")
        target = check_handoff(sre, "scan for vulnerabilities")
        assert target is not None
        assert target.name == "security"

    def test_no_handoff(self):
        sre = get_skill("sre")
        target = check_handoff(sre, "list pods in production")
        assert target is None

    def test_security_to_sre(self):
        security = get_skill("security")
        assert security is not None
        target = check_handoff(security, "fix the RBAC issue")
        assert target is not None
        assert target.name == "sre"

    def test_capacity_to_sre(self):
        cap = get_skill("capacity_planner")
        assert cap is not None
        target = check_handoff(cap, "scale the deployment")
        assert target is not None
        assert target.name == "sre"


class TestModeCategoriesIntegration:
    def test_builds_from_skills(self):
        cats = get_mode_categories()
        assert "sre" in cats
        assert "security" in cats
        assert "view_designer" in cats
        assert "both" in cats
        assert cats["both"] is None  # all tools

    def test_sre_has_expected_categories(self):
        cats = get_mode_categories()
        sre_cats = cats["sre"]
        assert "diagnostics" in sre_cats
        assert "workloads" in sre_cats
        assert "monitoring" in sre_cats

    def test_security_has_limited_categories(self):
        cats = get_mode_categories()
        sec_cats = cats["security"]
        assert "security" in sec_cats
        assert "operations" not in sec_cats


class TestSkillEvals:
    def test_load_sre_evals(self):
        scenarios = load_skill_evals("sre")
        assert len(scenarios) >= 4
        ids = [s["id"] for s in scenarios]
        assert "sre_crashloop" in ids

    def test_load_security_evals(self):
        scenarios = load_skill_evals("security")
        assert len(scenarios) >= 4

    def test_load_capacity_evals(self):
        scenarios = load_skill_evals("capacity_planner")
        assert len(scenarios) >= 4

    def test_load_nonexistent_skill(self):
        scenarios = load_skill_evals("nonexistent_skill_xyz")
        assert scenarios == []


class TestSchemaValidation:
    def test_valid_skill_no_warnings(self):
        from sre_agent.skill_loader import _validate_schema

        meta = {
            "name": "test",
            "version": 1,
            "description": "Test skill",
            "keywords": ["test"],
            "categories": ["diagnostics"],
            "priority": 5,
        }
        errors = _validate_schema(meta, Path("."))
        assert errors == []

    def test_invalid_version(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": -1, "description": "t", "keywords": ["x"]}, Path("."))
        assert any("version" in e for e in errors)

    def test_missing_description(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": 1, "keywords": ["x"]}, Path("."))
        assert any("description" in e for e in errors)

    def test_no_keywords_warning(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema({"name": "t", "version": 1, "description": "t"}, Path("."))
        assert any("keywords" in e for e in errors)

    def test_invalid_category(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "categories": ["invalid_xyz"]},
            Path("."),
        )
        assert any("unknown category" in e for e in errors)

    def test_valid_categories_accepted(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "categories": ["diagnostics", "monitoring", "workloads"],
            },
            Path("."),
        )
        cat_errors = [e for e in errors if "category" in e]
        assert cat_errors == []

    def test_invalid_priority(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "priority": 999},
            Path("."),
        )
        assert any("priority" in e for e in errors)

    def test_invalid_handoff_type(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {"name": "t", "version": 1, "description": "t", "keywords": ["x"], "handoff_to": {"sre": "not_a_list"}},
            Path("."),
        )
        assert any("handoff_to" in e for e in errors)

    def test_invalid_configurable_type(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "invalid_type"}}],
            },
            Path("."),
        )
        assert any("invalid type" in e for e in errors)

    def test_enum_without_options(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "enum"}}],
            },
            Path("."),
        )
        assert any("no options" in e for e in errors)

    def test_number_min_greater_than_max(self):
        from sre_agent.skill_loader import _validate_schema

        errors = _validate_schema(
            {
                "name": "t",
                "version": 1,
                "description": "t",
                "keywords": ["x"],
                "configurable": [{"field1": {"type": "number", "min": 100, "max": 10}}],
            },
            Path("."),
        )
        assert any("min > max" in e for e in errors)

    def test_built_in_skills_pass_validation(self):
        """All built-in skill packages should pass validation cleanly."""
        from sre_agent.skill_loader import _parse_skill_md

        skills_dir = Path(__file__).parent.parent / "sre_agent" / "skills"
        for skill_dir in skills_dir.iterdir():
            skill_file = skill_dir / "skill.md" if skill_dir.is_dir() else None
            if skill_file and skill_file.exists():
                skill = _parse_skill_md(skill_file)
                assert skill is not None, f"Failed to parse {skill_file}"


class TestSkillToDict:
    def test_serialization(self):
        skill = get_skill("sre")
        d = skill.to_dict()
        assert d["name"] == "sre"
        assert d["version"] >= 1
        assert isinstance(d["keywords"], list)
        assert isinstance(d["prompt_length"], int)
        assert d["prompt_length"] > 0


class TestFallbackIsNotArgmax:
    """When no channel clears the threshold, routing must be deterministic.

    The fallback previously returned the highest-scoring skill even though the
    threshold check had just declared that score insufficient. The temporal
    channel is learned from usage, so on a cluster where one skill had been
    used recently, an unrelated query would route to it on a score of 0.01
    against a 0.45 threshold — and the same code would route differently in
    CI and locally, which is exactly how it stayed hidden.
    """

    def test_an_unmatched_query_routes_to_the_default(self):
        from sre_agent.skill_loader import classify_query

        assert classify_query("hello").name == "sre"

    def test_the_fallback_ignores_a_below_threshold_winner(self):
        from sre_agent.skill_loader import _get_selector

        result = _get_selector().select("hello")
        assert result.source == "fallback"
        assert result.skill_name == "sre"
        # ...even though something did score highest.
        if result.fused_scores:
            best = max(result.fused_scores.values())
            assert best < result.threshold_used

    def test_a_matched_query_still_routes_on_its_own_evidence(self):
        """The fix must not flatten real routing into the default."""
        from sre_agent.skill_loader import classify_query

        assert classify_query("audit cluster-admin bindings for privilege escalation").name == "security"
        assert classify_query("forecast capacity for next quarter").name == "capacity_planner"


class TestToolCategoryCoverage:
    """Every registered tool must be reachable, or the agent can never call it."""

    def test_no_unreachable_tools(self):
        import re
        from pathlib import Path as _Path

        from sre_agent.tool_categories import ALWAYS_INCLUDE, TOOL_CATEGORIES

        # A tool is reachable if some category offers it, or if it is always
        # included regardless of category.
        categorized: set[str] = set(ALWAYS_INCLUDE)
        for cat in TOOL_CATEGORIES.values():
            categorized.update(cat.get("tools", []))

        defined: set[str] = set()
        for path in _Path("sre_agent").rglob("*.py"):
            for match in re.finditer(r"@beta_tool[^\n]*\ndef ([a-z_][a-z0-9_]*)", path.read_text(encoding="utf-8")):
                defined.add(match.group(1))

        # list_resources / describe_resource supersede the per-kind listing tools.
        # Those remain defined for direct use but are deliberately absent from
        # ALL_TOOLS and from every category — being unreachable is the intent.
        superseded = {
            "list_nodes",
            "list_namespaces",
            "list_deployments",
            "list_statefulsets",
            "list_daemonsets",
            "list_replicasets",
            "list_limit_ranges",
            "get_services",
            "get_resource_quotas",
            "get_persistent_volume_claims",
            "get_pod_disruption_budgets",
        }

        orphaned = defined - categorized - superseded
        assert not orphaned, f"tools defined but unreachable (no category, not always-included): {sorted(orphaned)}"

    def test_memory_tools_are_reachable(self):
        from sre_agent.tool_categories import MODE_CATEGORIES, TOOL_CATEGORIES

        sre_tools: set[str] = set()
        for cat_name in MODE_CATEGORIES["sre"]:
            sre_tools.update(TOOL_CATEGORIES.get(cat_name, {}).get("tools", []))

        # Without these the agent cannot consult anything it has previously learned.
        for tool in ("search_past_incidents", "get_learned_runbooks", "get_cluster_patterns"):
            assert tool in sre_tools, f"{tool} unreachable in sre mode"


class TestBudgetRelevance:
    """The budget cut must drop the least relevant tools, not the last-registered ones."""

    @staticmethod
    def _tool(name: str):
        class _T:
            def __init__(self, n: str) -> None:
                self.name = n

        return _T(name)

    def test_query_named_tool_survives_truncation(self):
        from sre_agent.skill_loader import _rank_by_relevance

        # list_nodes sits last, exactly the position the old slice discarded.
        tools = [self._tool(f"unrelated_tool_{i}") for i in range(60)] + [self._tool("list_nodes")]
        ranked = _rank_by_relevance(tools, "One of our cluster nodes is showing as NotReady")
        assert ranked[0].name == "list_nodes"

    def test_singular_plural_still_matches(self):
        from sre_agent.skill_loader import _rank_by_relevance

        tools = [self._tool("unrelated_a"), self._tool("describe_node")]
        assert _rank_by_relevance(tools, "why are the nodes unhealthy")[0].name == "describe_node"

    def test_ties_keep_original_order(self):
        from sre_agent.skill_loader import _rank_by_relevance

        tools = [self._tool("alpha_thing"), self._tool("beta_thing"), self._tool("gamma_thing")]
        ranked = _rank_by_relevance(tools, "completely unrelated wording")
        assert [t.name for t in ranked] == ["alpha_thing", "beta_thing", "gamma_thing"]

    def test_empty_query_is_a_no_op(self):
        from sre_agent.skill_loader import _rank_by_relevance

        tools = [self._tool("a_tool"), self._tool("b_tool")]
        assert [t.name for t in _rank_by_relevance(tools, "")] == ["a_tool", "b_tool"]


class TestRoutingPrecision:
    """Fixtures that were landing in the wrong skill on soft signals alone."""

    @staticmethod
    def _sre_patterns():
        import re

        from sre_agent.skill_loader import get_skill

        skill = get_skill("sre")
        assert skill is not None
        return [re.compile(p, re.I) for p in skill.trigger_patterns]

    def test_argocd_drift_routes_to_sre(self):
        # gitops is in the sre skill's categories and it owns get_argo_applications,
        # but nothing claimed the words — this was scoring into capacity_planner
        q = "Check if our ArgoCD applications are in sync and report any drift"
        assert any(p.search(q) for p in self._sre_patterns())

    def test_task_creation_routes_to_sre(self):
        # create_inbox_task is an sre tool with no skill of its own, so "rotate
        # the certs" was pulling this into security
        q = "Add a task to remind me to rotate TLS certificates before Friday"
        assert any(p.search(q) for p in self._sre_patterns())

    def test_does_not_hijack_other_skills(self):
        for q in (
            "run a security scan on the payments namespace",
            "forecast capacity for the next quarter",
            "build me a dashboard for production",
            "create a skill for diagnosing etcd",
        ):
            assert not any(p.search(q) for p in self._sre_patterns()), f"sre hijacked: {q}"

    def test_every_trigger_pattern_compiles(self):
        # a broken regex here silently disables hard pre-routing for the whole skill
        self._sre_patterns()


class TestDestructiveGuidance:
    """The skill must say which tool restarts a workload, or the agent guesses."""

    @staticmethod
    def _sre_prompt() -> str:
        from sre_agent.skill_loader import get_skill

        skill = get_skill("sre")
        assert skill is not None
        return skill.system_prompt

    def test_restart_is_named_as_the_way_to_pick_up_a_change(self):
        prompt = self._sre_prompt()
        assert "restart_deployment" in prompt
        assert "delete_pod" in prompt

    def test_delete_pod_is_explicitly_ruled_out_for_restarts(self):
        # the agent had both tools and no guidance, and reached for delete_pod
        # twice to make pods pick up an RBAC change
        prompt = self._sre_prompt().lower()
        assert "never use `delete_pod`" in prompt or "never use delete_pod" in prompt

    def test_scoped_requests_are_respected(self):
        assert "start with step 1" in self._sre_prompt()


class TestContinuationStickiness:
    """A follow-up stays with the skill already serving the conversation."""

    def test_back_references_are_continuations(self):
        from sre_agent.skill_router import is_continuation

        for q in (
            "Scale it back to 3 then, we don't have the capacity",
            "Did it work? Are all pods running?",
            "Add a memory chart to it",
            "do that again",
            "check the previous one instead",
        ):
            assert is_continuation(q), f"should be a continuation: {q}"

    def test_new_tasks_are_not_continuations(self):
        from sre_agent.skill_router import is_continuation

        for q in (
            "run a security scan on the payments namespace",
            "forecast capacity for next quarter",
            "why is checkout-api slow",
            "build me a dashboard for production",
        ):
            assert not is_continuation(q), f"should not be a continuation: {q}"

    def test_a_followup_does_not_switch_specialists(self):
        """The worst result in the eval suite came from this switching.

        "Scale it back to 3 then, we don't have the capacity" matched
        capacity_planner's trigger. The turn re-routed to a skill without
        scale_deployment, which then correctly disowned an action the
        conversation had taken two turns earlier — reading, to the operator, as
        the assistant denying its own work.
        """
        from sre_agent.evals.replay_config import resolve_mode

        assert resolve_mode("Scale it back to 3 then, we don't have the capacity", last_mode="sre") == "sre"

    def test_a_declared_handoff_beats_stickiness(self):
        """"build me a dashboard of these findings" must reach view_designer.

        My first version of this rule pinned any back-reference to the current
        skill, so this turn stayed in security — which has no create_dashboard —
        and the agent called no tools at all. The back-reference is about the
        data; the request is for a capability the current skill lacks, and the
        skills already declare that in handoff_to.
        """
        from sre_agent.evals.replay_config import resolve_mode

        assert resolve_mode("build me a dashboard of these findings", last_mode="security") != "security"

    def test_a_continuation_without_a_handoff_still_sticks(self):
        from sre_agent.evals.replay_config import resolve_mode

        assert resolve_mode("Scale it back to 3 then, we don't have the capacity", last_mode="sre") == "sre"

    def test_a_hard_switch_still_switches(self):
        # stickiness must not trap a conversation that genuinely changed subject
        from sre_agent.evals.replay_config import resolve_mode

        assert resolve_mode("check that for rbac issues", last_mode="sre") != "sre"
