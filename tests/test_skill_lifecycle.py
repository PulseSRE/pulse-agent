"""A verified trajectory must deepen the skill that exists, not clone it.

Scaffolding keyed new skills on the incident's title words, so two verified
fixes for the same failure produced sibling duplicates and nothing ever reached
version 2. Learning now keys on the finding category: the first verified
trajectory creates a skill stamped with ``incident_type``, later ones refine it
in place — and refinement re-opens the review gate, visibly, via the inbox.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sre_agent.skill_lifecycle import (
    MAX_VERIFIED_CASES,
    _find_skill_for_category,
    learn_from_verified,
    refine_skill,
)
from sre_agent.skill_loader import Skill, _parse_skill_md
from sre_agent.trajectory import LearningCandidate


def _candidate(**overrides) -> LearningCandidate:
    defaults = dict(
        key="crashloop:Pod:payments:api",
        category="crashloop",
        title="CrashLoopBackOff in payments api pods",
        root_cause="Missing DB_HOST env var after config rollout",
        summary="Pods restarted with config error; env var absent from ConfigMap.",
        confidence=0.82,
        evidence=[{"kind": "log", "detail": "DB_HOST not set"}],
        tools_called=["list_pods", "get_pod_logs", "describe_pod"],
    )
    defaults.update(overrides)
    return LearningCandidate(**defaults)


def _write_skill(
    tmp_path: Path,
    *,
    name: str = "crashloop-payments",
    incident_type: str = "crashloop",
    version: int = 3,
    reviewed: bool = True,
    cases: int = 0,
) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "version": version,
        "description": "Auto-generated skill",
        "keywords": ["crashloop", "payments"],
        "categories": ["diagnostics"],
        "requires_tools": ["list_pods"],
        "generated_by": "auto",
        "reviewed": reviewed,
        "incident_type": incident_type,
    }
    body = "\n## Crashloop Payments\n\nInvestigate crashloops.\n"
    for i in range(cases):
        body += f"\n### Verified case — 2026-01-0{i + 1}\n- Root cause: case number {i}\n"
    (skill_dir / "skill.md").write_text(
        f"---\n{yaml.dump(meta, default_flow_style=False, sort_keys=False)}---\n{body}",
        encoding="utf-8",
    )
    return skill_dir / "skill.md"


def _skill_obj(skill_file: Path) -> Skill:
    parsed = _parse_skill_md(skill_file)
    assert parsed is not None
    return parsed


def _frontmatter(skill_file: Path) -> dict:
    return yaml.safe_load(skill_file.read_text().split("---")[1])


class TestFindSkillForCategory:
    def test_matches_auto_skill_with_incident_type(self, tmp_path, monkeypatch):
        skill = _skill_obj(_write_skill(tmp_path))
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [skill])
        assert _find_skill_for_category("crashloop") is skill

    def test_ignores_human_authored_skills(self, tmp_path, monkeypatch):
        skill = _skill_obj(_write_skill(tmp_path))
        skill.generated_by = ""  # a person wrote this; never auto-rewrite it
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [skill])
        assert _find_skill_for_category("crashloop") is None

    def test_empty_category_matches_nothing(self, tmp_path, monkeypatch):
        skill = _skill_obj(_write_skill(tmp_path, incident_type=""))
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [skill])
        assert _find_skill_for_category("") is None


class TestRefineSkill:
    def _refine(self, tmp_path, monkeypatch, *, reviewed=True, cases=0, candidate=None):
        skill_file = _write_skill(tmp_path, reviewed=reviewed, cases=cases)
        skill = _skill_obj(skill_file)
        monkeypatch.setattr("sre_agent.skill_loader.reload_skills", lambda: {})
        inbox_items: list[dict] = []
        monkeypatch.setattr("sre_agent.inbox.upsert_inbox_item", lambda item: inbox_items.append(item) or "id-1")
        result = refine_skill(skill, candidate or _candidate())
        return skill_file, result, inbox_items

    def test_bumps_version_and_reopens_review_gate(self, tmp_path, monkeypatch):
        skill_file, result, _ = self._refine(tmp_path, monkeypatch, reviewed=True)
        assert result == str(skill_file)
        meta = _frontmatter(skill_file)
        assert meta["version"] == 4
        # The body a person approved is not the body on disk any more.
        assert meta["reviewed"] is False

    def test_appends_distilled_case(self, tmp_path, monkeypatch):
        skill_file, _, _ = self._refine(tmp_path, monkeypatch)
        body = skill_file.read_text()
        assert "### Verified case" in body
        assert "Missing DB_HOST env var" in body
        assert "`list_pods` → `get_pod_logs` → `describe_pod`" in body

    def test_merges_keywords_and_tools(self, tmp_path, monkeypatch):
        skill_file, _, _ = self._refine(tmp_path, monkeypatch)
        meta = _frontmatter(skill_file)
        assert "crashloop" in meta["keywords"]  # original kept
        assert len(meta["keywords"]) > 2  # new tokens merged in
        assert "get_pod_logs" in meta["requires_tools"]
        assert meta["requires_tools"][0] == "list_pods"  # originals stay first

    def test_reviewed_refinement_surfaces_rereview_in_inbox(self, tmp_path, monkeypatch):
        _, _, inbox = self._refine(tmp_path, monkeypatch, reviewed=True)
        assert len(inbox) == 1
        assert inbox[0]["correlation_key"] == "skill-rereview:crashloop-payments"
        assert "re-review" in inbox[0]["title"] or "re-approve" in inbox[0]["summary"]

    def test_unreviewed_refinement_stays_quiet(self, tmp_path, monkeypatch):
        # The skill was already awaiting first review; nothing newly changed
        # about its routing status, so no extra inbox item.
        _, _, inbox = self._refine(tmp_path, monkeypatch, reviewed=False)
        assert inbox == []

    def test_case_history_is_capped(self, tmp_path, monkeypatch):
        skill_file, _, _ = self._refine(tmp_path, monkeypatch, cases=MAX_VERIFIED_CASES)
        body = skill_file.read_text()
        assert body.count("### Verified case") == MAX_VERIFIED_CASES
        # Oldest case dropped, newest present.
        assert "case number 0" not in body
        assert "Missing DB_HOST env var" in body

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        skill_file = _write_skill(tmp_path)
        skill = _skill_obj(skill_file)
        skill_file.unlink()
        assert refine_skill(skill, _candidate()) is None


class TestLearnFromVerified:
    def test_existing_category_refines_instead_of_cloning(self, tmp_path, monkeypatch):
        skill_file = _write_skill(tmp_path)
        skill = _skill_obj(skill_file)
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [skill])
        monkeypatch.setattr("sre_agent.skill_loader.reload_skills", lambda: {})
        monkeypatch.setattr("sre_agent.inbox.upsert_inbox_item", lambda item: "id-1")

        created: list[str] = []
        monkeypatch.setattr(
            "sre_agent.skill_scaffolder.save_scaffolded_skill",
            lambda content, name: created.append(name) or "unexpected",
        )

        result = learn_from_verified(_candidate())
        assert result == str(skill_file)
        assert created == []  # no duplicate sibling skill
        assert _frontmatter(skill_file)["version"] == 4

    def test_novel_category_scaffolds_with_incident_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [])

        saved: dict = {}

        def fake_save(content, name):
            saved["content"] = content
            saved["name"] = name
            return f"/tmp/{name}/skill.md"

        monkeypatch.setattr("sre_agent.skill_scaffolder.save_scaffolded_skill", fake_save)
        monkeypatch.setattr(
            "sre_agent.skill_scaffolder.scaffold_plan_template",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "sre_agent.eval_scaffolder.scaffold_eval_from_investigation",
            lambda **kw: None,
        )

        result = learn_from_verified(_candidate())
        assert result is not None
        # The stamp that makes the NEXT verified case a refinement.
        assert "incident_type: crashloop" in saved["content"]


class TestScaffoldedSkillRoundTrip:
    def test_incident_type_survives_parse(self, tmp_path):
        from sre_agent.skill_scaffolder import scaffold_skill_from_resolution

        content = scaffold_skill_from_resolution(
            query="oom killed pods in checkout",
            tools_called=["list_pods", "describe_pod"],
            investigation_summary="Pods exceed memory limit under load.",
            root_cause="Limit too low for peak traffic",
            confidence=0.7,
            incident_type="oom",
        )
        f = tmp_path / "skill.md"
        f.write_text(content)
        parsed = _parse_skill_md(f)
        assert parsed is not None
        assert parsed.incident_type == "oom"
        assert parsed.generated_by == "auto"
        assert parsed.reviewed is False


class TestPreRouteRespectsGates:
    """Trigger patterns are automatic routing too — both gates must hold there."""

    def _route(self, monkeypatch, *, reviewed: bool, quarantined: bool):
        from sre_agent import skill_router
        from tests.conftest import _mock_skill

        skill = _mock_skill(
            "etcdwatch",
            trigger_patterns=[r"etcd"],
            route_priority=10,
            reviewed=reviewed,
            quarantined=quarantined,
        )
        monkeypatch.setattr("sre_agent.skill_loader.list_skills", lambda: [skill])
        monkeypatch.setattr("sre_agent.skill_loader.get_skill", lambda name: skill)
        skill_router.reset_hard_pre_route()
        try:
            return skill_router._hard_pre_route("why is etcd slow")
        finally:
            skill_router.reset_hard_pre_route()

    def test_reviewed_unquarantined_skill_pre_routes(self, monkeypatch):
        assert self._route(monkeypatch, reviewed=True, quarantined=False) is not None

    def test_unreviewed_skill_cannot_pre_route(self, monkeypatch):
        assert self._route(monkeypatch, reviewed=False, quarantined=False) is None

    def test_quarantined_skill_cannot_pre_route(self, monkeypatch):
        assert self._route(monkeypatch, reviewed=True, quarantined=True) is None
