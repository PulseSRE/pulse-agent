"""Chat-derived learning: Hermes's cadence through Pulse's gates.

A chat session may only teach when a human marked it resolved AND it looks
like a real investigation (tool calls, classifiable topic, substantive
conclusion). The candidate then rides the same record->promote->learn chain
as monitor-verified fixes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.chat_learning import (
    CHAT_CONFIDENCE,
    candidate_from_chat,
    infer_category,
    learn_from_chat_feedback,
)


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _tool_call(name: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}],
    }


_CONCLUSION = (
    "The pods were crashlooping because the liveness probe pointed at a port the "
    "container stopped listening on after the config change. Reverting the probe "
    "port to 8080 resolved it; all replicas are Ready."
)


def _investigation_messages() -> list[dict]:
    return [
        _msg("user", "why are the payment pods crashlooping in prod?"),
        _tool_call("list_pods"),
        _tool_call("get_pod_logs"),
        _tool_call("describe_pod"),
        _msg("assistant", _CONCLUSION),
    ]


class TestInferCategory:
    def test_maps_topics_to_finding_categories(self):
        assert infer_category("pods stuck in CrashLoopBackOff") == "crashloop"
        assert infer_category("etcd DB is huge") == "etcd"
        assert infer_category("apiserver feels slow") == "control_plane"

    def test_unclassified_topic_returns_empty(self):
        """No guessing: an unmatched conversation teaches nothing rather than
        scaffolding a skill for a topic nobody classified."""
        assert infer_category("please summarize yesterday's activity") == ""


class TestCandidateGates:
    def test_qualifying_session_produces_candidate(self):
        c = candidate_from_chat("sess-abc-123", _investigation_messages())
        assert c is not None
        assert c.category == "crashloop"
        assert c.confidence == CHAT_CONFIDENCE
        assert c.tools_called == ["list_pods", "get_pod_logs", "describe_pod"]
        assert c.key.startswith("chat:crashloop:")
        assert c.evidence and c.evidence[0]["type"] == "chat_session"
        learnable, reason = c.is_learnable()
        assert learnable, reason

    def test_too_few_tool_calls_teaches_nothing(self):
        """Answered-from-memory sessions have nothing cluster-verified to learn."""
        msgs = [
            _msg("user", "why do pods crashloop generally?"),
            _tool_call("list_pods"),
            _msg("assistant", _CONCLUSION),
        ]
        assert candidate_from_chat("s1", msgs) is None

    def test_unclassifiable_topic_teaches_nothing(self):
        msgs = [
            _msg("user", "write me a haiku about the cluster"),
            _tool_call("list_pods"),
            _tool_call("get_events"),
            _tool_call("get_node_metrics"),
            _msg("assistant", "Here is a haiku that is definitely longer than eighty characters of conclusion text."),
        ]
        assert candidate_from_chat("s2", msgs) is None

    def test_thin_conclusion_teaches_nothing(self):
        msgs = _investigation_messages()[:-1] + [_msg("assistant", "Fixed the crashloop.")]
        assert candidate_from_chat("s3", msgs) is None

    def test_plain_string_content_supported(self):
        msgs = [
            {"role": "user", "content": "oomkilled pods in the batch namespace, why?"},
            _tool_call("list_pods"),
            _tool_call("get_pod_logs"),
            _tool_call("get_pod_metrics"),
            {"role": "assistant", "content": _CONCLUSION.replace("crashlooping", "OOMKilled")},
        ]
        c = candidate_from_chat("s4", msgs)
        assert c is not None and c.category == "oom"


class TestLearnFromChatFeedback:
    def test_rides_the_verified_learning_chain(self):
        learner = MagicMock()
        promoted = MagicMock()
        learner.promote.return_value = promoted
        with (
            patch("sre_agent.trajectory.get_learner", return_value=learner),
            patch("sre_agent.skill_lifecycle.learn_from_verified", return_value="crashloop-skill") as lfv,
        ):
            name = learn_from_chat_feedback("sess-abc-123", _investigation_messages())
        assert name == "crashloop-skill"
        learner.record.assert_called_once()
        recorded = learner.record.call_args[0][0]
        learner.promote.assert_called_once_with(recorded.key)
        lfv.assert_called_once_with(promoted)

    def test_unqualified_session_never_touches_learner(self):
        learner = MagicMock()
        with patch("sre_agent.trajectory.get_learner", return_value=learner):
            assert learn_from_chat_feedback("s5", [_msg("user", "hi")]) is None
        learner.record.assert_not_called()

    def test_never_raises_into_the_feedback_handler(self):
        with patch("sre_agent.trajectory.get_learner", side_effect=RuntimeError("db down")):
            assert learn_from_chat_feedback("s6", _investigation_messages()) is None
