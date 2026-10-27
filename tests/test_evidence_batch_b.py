"""Batch B: investigations carry their evidence to the person who claims them.

Each test here fails on the code before this change:

- plan phases reported a fixed 0.7 confidence and dropped their evidence;
- plan runs produced no investigation report and scaffolded skills unverified;
- the monitor report dropped evidence/alternatives, and never reached the inbox;
- claim built views without the user's token, on the event loop, and reused
  another incident's view when two titles matched;
- the view header left out the evidence and hid a 0 confidence;
- view-plan tool calls were all recorded as instant successes;
- chat dashboards merged into another user's view found by finding id, and
  announced a view_spec for a view that was never saved;
- namespace_summary showed unreadable resources as 0 and took any namespace.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sre_agent.evidence import UNSUPPORTED_CONFIDENCE_CAP

# ---------------------------------------------------------------- plan phases


def _phase(**kw):
    from sre_agent.skill_plan import SkillPhase

    return SkillPhase(id=kw.pop("id", "diagnose"), skill_name="sre", produces=kw.pop("produces", ["root_cause"]), **kw)


def _runtime():
    from sre_agent.plan_runtime import PlanRuntime

    return PlanRuntime(client=MagicMock())


class TestPhaseEvidence:
    def test_confidence_comes_from_evidence_not_a_fixed_default(self):
        body = {
            "findings": {"root_cause": "memory limit too low"},
            "evidence": [
                {"observation": "OOMKilled x4 in 10m", "kind": "event", "source": "get_events", "stance": "supports"},
                {"observation": "RSS at limit", "kind": "metric", "source": "container_memory", "stance": "supports"},
            ],
            "confidence": 0.95,
        }
        out = _runtime()._parse_skill_output(f"```json\n{json.dumps(body)}\n```", _phase(), [])
        assert len(out.evidence) == 2
        assert out.evidence[0]["source"] == "get_events"
        assert 0 < out.confidence <= 0.95
        assert out.confidence != 0.7

    def test_cause_without_evidence_is_capped(self):
        body = {"findings": {"root_cause": "bad config"}, "confidence": 0.95}
        out = _runtime()._parse_skill_output(f"```json\n{json.dumps(body)}\n```", _phase(), [])
        assert out.confidence <= UNSUPPORTED_CONFIDENCE_CAP

    def test_prose_answer_is_not_confident(self):
        out = _runtime()._parse_skill_output("I think it is the memory limit.", _phase(), [])
        assert out.confidence <= UNSUPPORTED_CONFIDENCE_CAP

    def test_alternatives_are_kept(self):
        body = {"findings": {"root_cause": "x"}, "alternatives_considered": ["node pressure: nodes fine"]}
        out = _runtime()._parse_skill_output(f"```json\n{json.dumps(body)}\n```", _phase(), [])
        assert out.findings["alternatives_considered"] == ["node pressure: nodes fine"]

    def test_prompt_asks_for_valid_evidence_shape(self):
        from sre_agent.evidence import Evidence

        prompt = _runtime()._build_phase_prompt(_phase(produces=["root_cause"]), {"title": "t"}, "")
        assert '"root_cause"' in prompt and '"evidence"' in prompt
        # Every kind and stance the prompt offers must be accepted by the model,
        # or the item loses its source on parsing.
        kinds = prompt.split('"kind": "', 1)[1].split('"', 1)[0].split("|")
        stances = prompt.split('"stance": "', 1)[1].split('"', 1)[0].split("|")
        for kind in kinds:
            for stance in stances:
                Evidence(observation="o", kind=kind, stance=stance, source="s")


# ---------------------------------------------------------- plan investigations


class TestPlanReport:
    def _result(self, evidence):
        from sre_agent.skill_plan import PlanResult, SkillOutput

        diagnose = SkillOutput(
            skill_id="sre",
            phase_id="diagnose",
            findings={"root_cause": "memory limit too low", "alternatives_considered": ["node pressure"]},
            evidence_summary="Container is OOMKilled at its limit.",
            confidence=0.8,
            evidence=evidence,
        )
        remediate = SkillOutput(
            skill_id="sre",
            phase_id="remediate",
            findings={"recommended_fix": "raise the limit to 512Mi"},
            confidence=0.3,
        )
        return PlanResult(plan_id="p", plan_name="oom", phase_outputs={"diagnose": diagnose, "remediate": remediate})

    def test_plan_run_reports_its_investigation(self, monkeypatch):
        from sre_agent.monitor import actions
        from sre_agent.monitor import plan_executor as pe

        saved, attached = [], []
        monkeypatch.setattr(actions, "save_investigation", lambda r, f: saved.append(r))
        monkeypatch.setattr(
            "sre_agent.inbox.attach_investigation_to_finding", lambda f, r, i: attached.append((r, i)) or True
        )
        monitor = SimpleNamespace(_broadcast_raw=MagicMock(side_effect=lambda r: asyncio.sleep(0)))
        ev = [{"observation": "OOMKilled x4", "kind": "event", "source": "get_events", "stance": "supports"}]

        report = asyncio.run(
            pe._report_plan_investigation(
                monitor, {"id": "f1", "category": "oom"}, SimpleNamespace(id="oom-plan"), self._result(ev)
            )
        )

        assert report["suspected_cause"] == "memory limit too low"
        assert report["recommended_fix"] == "raise the limit to 512Mi"
        assert report["confidence"] == 0.8  # the diagnosing phase, not the last one
        assert report["evidenceDetail"] == ev
        assert report["alternatives_considered"] == ["node pressure"]
        assert saved and saved[0]["id"] == report["id"]
        assert attached and attached[0][0]["evidence_detail"] == ev
        monitor._broadcast_raw.assert_called_once()

    def test_plan_run_no_longer_scaffolds_unverified_skills(self):
        import inspect

        from sre_agent.monitor import plan_executor

        src = inspect.getsource(plan_executor)
        assert "save_scaffolded_skill" not in src
        assert "LearningCandidate" in src


# ------------------------------------------------------- monitor → inbox evidence


@pytest.fixture
def _clean_inbox():
    from sre_agent.db import get_database

    db = get_database()
    db.execute("DELETE FROM inbox_items")
    db.commit()
    yield
    db.execute("DELETE FROM inbox_items")
    db.commit()


@pytest.mark.usefixtures("_clean_inbox")
class TestInboxEvidence:
    def test_monitor_investigation_lands_on_the_inbox_item(self):
        from sre_agent.inbox import attach_investigation_to_finding, bridge_finding_to_inbox, get_inbox_item

        finding = {
            "id": "f-ev-1",
            "title": "OOM killed",
            "severity": "warning",
            "category": "oom",
            "confidence": 0.9,
            "noiseScore": 0.0,
            "resources": [{"kind": "Pod", "name": "web-1", "namespace": "prod"}],
            "namespace": "prod",
        }
        item_id = bridge_finding_to_inbox(finding)
        ev = [{"observation": "OOMKilled x4", "kind": "event", "source": "get_events", "stance": "supports"}]
        result = {
            "summary": "s",
            "suspected_cause": "limit too low",
            "recommended_fix": "raise it",
            "confidence": 0.0,
            "evidence": ["OOMKilled x4"],
            "evidence_detail": ev,
            "alternatives_considered": ["node pressure"],
        }
        assert attach_investigation_to_finding(finding, result, "i-1") is True
        meta = get_inbox_item(item_id)["metadata"]
        assert meta["investigation_id"] == "i-1"
        assert meta["evidence_detail"] == ev
        assert meta["alternatives_considered"] == ["node pressure"]
        assert meta["investigation_confidence"] == 0.0

    def test_attach_without_an_item_is_a_noop(self):
        from sre_agent.inbox import attach_investigation_to_finding

        assert attach_investigation_to_finding({"id": "nope", "category": "x"}, {"summary": "s"}, "i") is False


# ------------------------------------------------------------- claim and views


@pytest.fixture
def _views_db():
    from sre_agent.db import get_database

    db = get_database()
    db.execute("DELETE FROM view_versions")
    db.execute("DELETE FROM views")
    db.commit()
    yield db
    db.execute("DELETE FROM view_versions")
    db.execute("DELETE FROM views")
    db.commit()


@pytest.mark.usefixtures("_views_db")
class TestSaveView:
    def _save(self, owner, view_id, title, **kw):
        from sre_agent import db

        return db.save_view(owner, view_id, title, "d", [{"kind": "key_value", "pairs": []}], **kw)

    def test_title_collision_is_a_failure_not_a_silent_overwrite(self):
        from sre_agent import db

        self._save("alice", "cv-a", "Investigation: OOM", reuse_by_title=False)
        assert self._save("alice", "cv-b", "Investigation: OOM", reuse_by_title=False) is None
        assert db.get_view("cv-b") is None

    def test_chat_views_still_reuse_by_title(self):
        self._save("alice", "cv-a", "My dash")
        assert self._save("alice", "cv-b", "My dash") == "cv-a"

    def test_id_owned_by_someone_else_is_not_reported_saved(self):
        self._save("bob", "cv-x", "Bob's")
        assert self._save("alice", "cv-x", "Alice's", reuse_by_title=False) is None


class TestClaim:
    def test_view_tools_run_with_the_claimant_token(self, monkeypatch):
        from sre_agent import inbox

        seen = {}
        monkeypatch.setattr(
            "sre_agent.view_executor.execute_view_plan",
            lambda plan, item, user_token=None: seen.setdefault("token", user_token) and [{"kind": "x"}],
        )
        monkeypatch.setattr("sre_agent.db.save_view", lambda **kw: seen.setdefault("save", kw) and kw["view_id"])
        repo = MagicMock()
        monkeypatch.setattr(inbox, "get_inbox_repo", lambda: repo)
        monkeypatch.setattr(inbox, "_publish_event", lambda *a, **k: None)

        item = {"id": "it-1", "title": "OOM", "metadata": {"view_plan": [{"kind": "key_value"}]}}
        inbox._generate_view_for_item("it-1", item, "alice", user_token="tok-alice")

        assert seen["token"] == "tok-alice"
        assert seen["save"]["reuse_by_title"] is False
        # Two items titled "OOM" get two views, not one shared one.
        assert seen["save"]["title"].endswith("it-1")
        repo.update_metadata_and_view.assert_called_once()

    def test_unsaved_view_is_marked_failed_not_ready(self, monkeypatch):
        from sre_agent import inbox

        monkeypatch.setattr("sre_agent.view_executor.execute_view_plan", lambda *a, **k: [{"kind": "x"}])
        monkeypatch.setattr("sre_agent.db.save_view", lambda **kw: None)
        repo = MagicMock()
        monkeypatch.setattr(inbox, "get_inbox_repo", lambda: repo)
        monkeypatch.setattr(inbox, "_publish_event", lambda *a, **k: None)

        item = {"id": "it-2", "title": "OOM", "metadata": {"view_plan": [{"kind": "key_value"}]}}
        inbox._generate_view_for_item("it-2", item, "alice")

        repo.update_metadata_and_view.assert_not_called()
        assert repo.update_metadata.call_args.args[1]["view_status"] == "failed"

    def test_rest_claim_runs_off_the_event_loop_with_token(self, monkeypatch):
        import threading

        from sre_agent.api import inbox_rest

        seen = {}

        def fake_claim(item_id, owner, user_token=None):
            seen.update(thread=threading.current_thread(), token=user_token)
            return True

        monkeypatch.setattr(inbox_rest, "claim_item", fake_claim)
        assert asyncio.run(inbox_rest.rest_claim_item("it", owner="alice", user_token="tok")) == {"ok": True}
        assert seen["token"] == "tok"
        assert seen["thread"] is not threading.main_thread()

    def test_fallback_layout_uses_valid_props(self):
        from sre_agent.inbox import _fallback_layout

        layout = _fallback_layout(
            {"namespace": "prod"},
            {
                "investigation_summary": "s",
                "blast_radius": {"affected_count": 3, "affected_resources": ["Service/prod/web", "Route/prod/web"]},
            },
        )
        kinds = [w["kind"] for w in layout]
        assert "resource_counts" not in kinds  # had no items: rendered empty
        dependents = next(w for w in layout if w["title"] == "Downstream Dependents")
        names = [i["name"] for i in dependents["props"]["items"]]
        assert names == ["Service/web", "Route/web", "1 more not shown"]
        assert all(i["status"] == "unknown" for i in dependents["props"]["items"])


# ------------------------------------------------------------ view header + audit


class TestHeader:
    def test_zero_confidence_is_shown(self):
        from sre_agent.view_executor import _build_header_widgets

        header = _build_header_widgets({"metadata": {"investigation_confidence": 0, "investigation_summary": "s"}})
        badge = next(w for w in header if w["kind"] == "confidence_badge")
        assert badge["props"]["score"] == 0

    def test_evidence_and_alternatives_are_shown(self):
        from sre_agent.view_executor import _build_header_widgets

        header = _build_header_widgets(
            {
                "metadata": {
                    "investigation_confidence": 0.6,
                    "suspected_cause": "c",
                    "evidence_detail": [
                        {"observation": "OOMKilled x4", "kind": "event", "source": "get_events", "stance": "supports"},
                        {"observation": "node has headroom", "kind": "metric", "source": "", "stance": "contradicts"},
                    ],
                    "alternatives_considered": ["node pressure: ruled out"],
                }
            }
        )
        evidence = next(w for w in header if w["title"] == "Evidence")
        items = evidence["props"]["items"]
        assert items[0]["status"] == "healthy" and "source: get_events" in items[0]["detail"]
        assert items[1]["status"] == "warning" and "contradicts" in items[1]["detail"]
        assert "source not recorded" in items[1]["detail"]
        alts = next(w for w in header if w["title"] == "Alternatives Considered")
        assert alts["props"]["cards"][0]["value"] == "node pressure: ruled out"

    def test_tool_failures_are_recorded_as_errors(self, monkeypatch):
        from sre_agent import tool_usage, view_executor

        calls = []
        monkeypatch.setattr(tool_usage, "record_tool_call", lambda **kw: calls.append(kw))
        tool = MagicMock()
        tool.call.return_value = "Error (403): forbidden"
        monkeypatch.setattr(view_executor, "_resolve_tool", lambda name: tool)
        view_executor._execute_tool_widget({"tool": "list_pods", "args": {}, "title": "Pods"}, "it")
        assert calls[-1]["status"] == "error"
        assert "403" in calls[-1]["error_message"]

        tool.call.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            view_executor._execute_tool_widget({"tool": "list_pods", "args": {}, "title": "Pods"}, "it")
        assert calls[-1]["status"] == "error" and calls[-1]["error_message"] == "boom"


# ------------------------------------------------------------------ chat views


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


def _run_turn(monkeypatch, current_user, signal):
    from sre_agent.api import agent_ws
    from sre_agent.view_tools import SIGNAL_PREFIX

    components = [{"kind": "key_value", "title": "Facts", "pairs": [{"key": "a", "value": "1"}]}]

    class FakeExecutor:
        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            return SimpleNamespace(text="ok", tools_called=["create_dashboard"], components=components, token_usage={})

    monkeypatch.setattr(agent_ws, "SkillExecutor", FakeExecutor)
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "content": "msg\n" + SIGNAL_PREFIX + json.dumps(signal)}],
        }
    ]
    ws = _WS()
    asyncio.run(
        agent_ws._run_agent_ws_inner(
            ws, messages, "", [], {}, set(), "ws-1", current_user, "sre", 1, "make a dashboard", None, 0.0
        )
    )
    return ws.sent


@pytest.mark.usefixtures("_views_db")
class TestChatViews:
    def test_finding_lookup_does_not_merge_into_another_users_view(self, monkeypatch):
        from sre_agent import db

        db.save_view("bob", "cv-bob", "Bob's view", "d", [{"kind": "key_value", "pairs": []}], finding_id="f-9")
        sent = _run_turn(
            monkeypatch, "alice", {"type": "view_spec", "view_id": "cv-alice", "title": "Alice", "finding_id": "f-9"}
        )
        specs = [m for m in sent if m.get("type") == "view_spec"]
        assert specs and specs[0]["spec"]["id"] == "cv-alice"
        assert len(db.get_view("cv-bob")["layout"]) == 1  # untouched
        assert db.get_view("cv-alice")["owner"] == "alice"

    def test_failed_save_sends_an_error_not_a_view(self, monkeypatch):
        from sre_agent import db

        monkeypatch.setattr(db, "save_view", lambda *a, **k: None)
        sent = _run_turn(monkeypatch, "alice", {"type": "view_spec", "view_id": "cv-1", "title": "Nope"})
        assert not [m for m in sent if m.get("type") == "view_spec"]
        assert any(m.get("type") == "error" and "could not be saved" in m["message"] for m in sent)

    def test_tool_result_does_not_claim_saved(self):
        import inspect

        from sre_agent import view_tools

        src = inspect.getsource(view_tools)
        assert "now saved" not in src
        assert "Do not say it is already saved" in src


# -------------------------------------------------------------- namespace_summary


class TestNamespaceSummary:
    def test_invalid_namespace_rejected(self):
        from sre_agent.view_tools import namespace_summary

        out = namespace_summary.call({"namespace": 'x"} or vector(1) #'})
        assert str(out).startswith("Error")

    def test_unreadable_resources_are_unknown_not_zero(self, monkeypatch):
        from kubernetes.client.rest import ApiException

        from sre_agent import k8s_client
        from sre_agent.view_tools import namespace_summary

        core, apps = MagicMock(), MagicMock()
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
        core.list_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")
        for name in ("service", "config_map", "persistent_volume_claim", "event"):
            getattr(core, f"list_namespaced_{name}").return_value = SimpleNamespace(items=[])
        for name in ("deployment", "stateful_set", "daemon_set"):
            getattr(apps, f"list_namespaced_{name}").return_value = SimpleNamespace(items=[])
        monkeypatch.setattr(k8s_client, "get_core_client", lambda: core)
        monkeypatch.setattr(k8s_client, "get_apps_client", lambda: apps)
        monkeypatch.setattr(k8s_client, "get_custom_client", lambda: MagicMock(side_effect=Exception("no routes")))
        monkeypatch.setattr(k8s_client, "get_networking_client", lambda: MagicMock())

        out = namespace_summary.call({"namespace": "prod"})
        text, component = out if isinstance(out, tuple) else (out, None)
        assert "Secrets: unknown" in text
        assert "Could not read" in text and "secrets" in text
        counts = component["items"][0]
        assert "secrets" not in [i["resource"] for i in counts["items"]]
