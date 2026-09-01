"""Runtime plan writes go to a writable directory, never the package.

Found by the sre-bench durable probe's first live run: POST /plan-templates
wrote its YAML into site-packages, which is read-only under OpenShift's
arbitrary UID, so creating a plan from the UI had been returning 500 on the
cluster while every local test passed. The properties pinned here: every
runtime write targets ``plans_dir()`` (settings-controlled, writable), the
loader reads the writable dir on top of the bundled seeds with the runtime
copy winning, and deletability is "lives in the writable dir", not an id
prefix — the old ``auto-`` rule blocked users from deleting plans they had
just created themselves.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def writable_plans(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_USER_PLANS_DIR", str(tmp_path / "plans"))
    from sre_agent import config as config_mod

    config_mod._reset_settings()
    yield tmp_path / "plans"
    config_mod._reset_settings()


class FakeReq:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _create(body):
    from sre_agent.api import monitor_rest

    with patch("sre_agent.plan_store.persist_plan", return_value=True):
        return asyncio.run(monitor_rest.create_plan_template(FakeReq(body), _auth=None))


class TestCreateWritesTheWritableDir:
    def test_yaml_lands_outside_the_package(self, writable_plans):
        from sre_agent import plan_templates as pt

        try:
            _create(
                {
                    "incident_type": "writable-probe",
                    "phases": [{"id": "p1", "skill_name": "sre"}],
                }
            )
            written = writable_plans / "writable-probe.yaml"
            assert written.exists(), "the plan must land in the configured writable dir"
            package_dir = pt._TEMPLATES_DIR
            assert not (package_dir / "writable-probe.yaml").exists(), (
                "nothing may be written into the installed package"
            )
        finally:
            pt.load_templates()  # drop the probe template from the cache

    def test_graph_features_survive_the_round_trip(self, writable_plans):
        from sre_agent import plan_templates as pt

        try:
            _create(
                {
                    "incident_type": "graphy-probe",
                    "phases": [
                        {"id": "triage", "skill_name": "sre"},
                        {
                            "id": "fix",
                            "skill_name": "sre",
                            "depends_on": ["triage"],
                            "branch_on": "cause",
                            "branches": {"oom": ["oom-skill"]},
                            "subplan": "",
                        },
                    ],
                }
            )
            data = yaml.safe_load((writable_plans / "graphy-probe.yaml").read_text())
            fix = data["phases"][1]
            assert fix["depends_on"] == ["triage"]
            assert fix["branch_on"] == "cause"
            assert fix["branches"] == {"oom": ["oom-skill"]}
            assert "subplan" not in fix, "empty graph fields are dropped, not stored"
        finally:
            pt.load_templates()


class TestLoaderReadsBothDirs:
    def test_runtime_plan_is_loaded_and_overrides_nothing_bundled(self, writable_plans):
        from sre_agent import plan_templates as pt

        writable_plans.mkdir(parents=True, exist_ok=True)
        (writable_plans / "runtime-only.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "runtime-only",
                    "name": "Runtime Only",
                    "incident_type": "runtime-only",
                    "phases": [{"id": "p1", "skill_name": "sre"}],
                }
            )
        )
        try:
            templates = pt.load_templates()
            assert "runtime-only" in templates
            # Bundled seeds are still present alongside.
            assert "crashloop" in templates
        finally:
            (writable_plans / "runtime-only.yaml").unlink()
            pt.load_templates()

    def test_user_edit_of_a_bundled_plan_wins(self, writable_plans):
        """The override mechanism that replaces rewriting the read-only seed."""
        from sre_agent import plan_templates as pt

        bundled = pt.load_templates()["crashloop"]
        writable_plans.mkdir(parents=True, exist_ok=True)
        (writable_plans / "crashloop.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": bundled.id,
                    "name": "Crashloop (edited)",
                    "incident_type": "crashloop",
                    "generated_by": "user",
                    "phases": [{"id": "p1", "skill_name": "sre"}],
                }
            )
        )
        try:
            assert pt.load_templates()["crashloop"].name == "Crashloop (edited)"
        finally:
            (writable_plans / "crashloop.yaml").unlink()
            pt.load_templates()


class TestDeleteSemantics:
    def test_user_created_plan_is_deletable(self, writable_plans):
        from sre_agent.api import monitor_rest

        _create({"incident_type": "deletable-probe", "phases": [{"id": "p1", "skill_name": "sre"}]})
        with patch("sre_agent.plan_store.forget_plan", return_value=True):
            out = asyncio.run(monitor_rest.delete_plan_template("deletable-probe", _auth=None))
        assert out["status"] == "deleted"
        assert not (writable_plans / "deletable-probe.yaml").exists()

    def test_bundled_plan_is_protected(self, writable_plans):
        from fastapi import HTTPException

        from sre_agent.api import monitor_rest

        with pytest.raises(HTTPException) as exc:
            asyncio.run(monitor_rest.delete_plan_template("crashloop", _auth=None))
        assert exc.value.status_code == 403
