"""Chat-derived learning — Hermes's cadence through Pulse's gates.

The monitor's trajectory learning only ever sees incidents the scanners
found: everything an SRE does in chat (the investigations, the corrections,
the confirmed fixes) evaporated when the session ended. Hermes reviews every
conversation for learnable material — but writes skills on the model's own
judgment of the turn. This module takes the cadence without the
self-assessment: a chat session becomes a ``LearningCandidate`` only when
the human clicks "resolved" (the WebSocket feedback message), and the
candidate then flows through the exact same pipeline as monitor-verified
fixes — ``TrajectoryLearner`` for the persistent record, ``skill_lifecycle``
for refine-vs-scaffold, and the human review gate before anything routes.

The verification gate is the affirmative human signal, not the model's
opinion of itself. Sessions that never get a thumbs-up teach nothing.
"""

from __future__ import annotations

import logging
import time

from .trajectory import LearningCandidate

logger = logging.getLogger("pulse_agent.learning")

# A session must look like an actual investigation before it can teach:
# a couple of tool calls means the agent looked at the cluster, not that it
# answered from memory.
MIN_TOOL_CALLS = 3

# Human-confirmed resolution. Above MIN_CONFIDENCE_TO_LEARN (0.6) by design:
# a person saying "that fixed it" outranks any self-assessed score, but stays
# below the certainty we grant a monitor-verified fix (the finding observably
# cleared), which this signal is a human report of, not a measurement of.
CHAT_CONFIDENCE = 0.75

# Conversation topic -> finding category, the same taxonomy the monitor's
# scanners stamp on findings. Learning keys on category (skill_lifecycle
# refines the skill whose incident_type matches), so chat about crashloops
# deepens the same skill crashloop findings do. Deliberately incomplete, like
# alert_plans: an unmatched conversation teaches nothing rather than
# scaffolding a skill for a topic nobody classified.
_TOPIC_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("crashloop", ("crashloop", "crash loop", "crashloopbackoff")),
    ("oom", ("oomkill", "oom kill", "out of memory", "memory limit")),
    ("etcd", ("etcd",)),
    ("control_plane", ("control plane", "control-plane", "apiserver", "kube-controller", "kube-scheduler")),
    ("nodes", ("node pressure", "notready", "not ready", "node memory", "disk pressure", "cordon", "drain")),
    ("operators", ("cluster operator", "clusteroperator", "operator degraded", "degraded operator")),
    ("cert_expiry", ("certificat", "tls expir", "cert expir")),
    ("hpa", ("hpa", "autoscal", "horizontal pod")),
    ("image_pull", ("imagepull", "image pull", "errimagepull")),
    ("scheduling", ("unschedulable", "pending pod", "scheduling fail", "taint", "affinity")),
    ("workloads", ("rollout", "deployment stuck", "replicas mismatch", "statefulset")),
    ("network", ("networkpolicy", "network policy", "ingress", "route", "dns", "endpoint")),
    ("storage", ("pvc", "persistentvolume", "storage class", "volume")),
]


def _text_of(content) -> str:
    """Flatten a message's content (string or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tools_called(messages: list[dict]) -> list[str]:
    """Tool names invoked across the session, in order, deduplicated."""
    seen: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                if name and name not in seen:
                    seen.append(name)
    return seen


def infer_category(text: str) -> str:
    """The finding category this conversation was about, or '' if unclassified."""
    lowered = text.lower()
    for category, needles in _TOPIC_CATEGORIES:
        if any(n in lowered for n in needles):
            return category
    return ""


def candidate_from_chat(session_id: str, messages: list[dict]) -> LearningCandidate | None:
    """Distill a resolved chat session into a learning candidate, or explain why not.

    Returns None (with a debug log naming the gate) when the session doesn't
    qualify: too few tool calls, no classifiable topic, or no substantive
    conclusion to learn from.
    """
    user_texts = [_text_of(m.get("content")) for m in messages if m.get("role") == "user"]
    user_texts = [t for t in user_texts if t.strip()]
    assistant_texts = [
        _text_of(m.get("content")) for m in messages if m.get("role") == "assistant" and _text_of(m.get("content"))
    ]
    if not user_texts or not assistant_texts:
        logger.debug("Chat session %s: no learnable exchange", session_id)
        return None

    tools = _tools_called(messages)
    if len(tools) < MIN_TOOL_CALLS:
        logger.debug(
            "Chat session %s: only %d tool calls (< %d) — answered from memory, nothing cluster-verified to learn",
            session_id,
            len(tools),
            MIN_TOOL_CALLS,
        )
        return None

    category = infer_category(" ".join(user_texts) + " " + assistant_texts[-1])
    if not category:
        logger.debug("Chat session %s: no classifiable incident topic", session_id)
        return None

    conclusion = assistant_texts[-1].strip()
    if len(conclusion) < 80:
        logger.debug("Chat session %s: conclusion too thin to learn from", session_id)
        return None

    title = user_texts[0].strip()[:120]
    return LearningCandidate(
        key=f"chat:{category}:{session_id[:12]}",
        category=category,
        title=title,
        root_cause=conclusion[:500],
        summary=f"Chat-resolved: {title}",
        confidence=CHAT_CONFIDENCE,
        evidence=[
            {
                "type": "chat_session",
                "session_id": session_id,
                "messages": len(messages),
                "confirmed_at": int(time.time() * 1000),
            }
        ],
        tools_called=tools,
    )


def learn_from_chat_feedback(session_id: str, messages: list[dict]) -> str | None:
    """A human marked this session resolved — run it through the learning pipeline.

    Called from the WebSocket feedback handler (in an executor; everything here
    is sync and must never raise into the caller). Returns the skill name that
    was created or refined, or None when the session didn't qualify.
    """
    try:
        candidate = candidate_from_chat(session_id, messages)
        if candidate is None:
            return None

        # Same record→promote→learn chain as monitor-verified fixes, so
        # chat-taught lessons appear in the same stats/history and hit the
        # same review gate. The thumbs-up IS the verification here, so
        # promotion is immediate rather than waiting on a later scan.
        from .skill_lifecycle import learn_from_verified
        from .trajectory import get_learner

        learner = get_learner()
        learner.record(candidate)
        promoted = learner.promote(candidate.key)
        if promoted is None:
            return None
        skill_name = learn_from_verified(promoted)
        if skill_name:
            logger.info(
                "Chat session %s taught skill '%s' (category=%s, %d tools)",
                session_id,
                skill_name,
                candidate.category,
                len(candidate.tools_called),
            )
        return skill_name
    except Exception:
        logger.debug("Chat learning failed for session %s", session_id, exc_info=True)
        return None
