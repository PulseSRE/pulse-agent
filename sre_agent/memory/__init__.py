"""Self-improving agent layer — orchestrates memory, evaluation, and learning."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .evaluation import evaluate_interaction
from .memory_tools import MEMORY_TOOLS, set_store
from .patterns import detect_patterns
from .retrieval import build_memory_context
from .runbooks import extract_runbook, is_duplicate_runbook
from .store import IncidentStore

logger = logging.getLogger("pulse_agent")

__all__ = [
    "MemoryManager",
    "get_manager",
    "is_memory_enabled",
    "set_manager",
]

# Module-level singleton so monitor.py can access memory without plumbing
_manager_instance: MemoryManager | None = None


def get_manager() -> MemoryManager | None:
    """Return the global MemoryManager singleton, or None if memory is disabled."""
    return _manager_instance


def set_manager(manager: MemoryManager | None) -> None:
    """Set the global MemoryManager singleton."""
    global _manager_instance
    _manager_instance = manager


def is_memory_enabled() -> bool:
    from ..config import get_settings

    return get_settings().agent.memory


_DEFAULT_SESSION = "default"


@dataclass
class _TurnState:
    """One chat session's turn, kept so later feedback scores the right turn."""

    turn_start: float = 0
    tool_calls: list[dict] = field(default_factory=list)
    rejected_count: int = 0
    last_incident_id: int | None = None
    prev_tool_calls: list[dict] = field(default_factory=list)
    prev_rejected_count: int = 0
    prev_turn_duration: float = 0


class MemoryManager:
    """Orchestrates the self-improving agent layer."""

    # Sessions whose turn state is kept for feedback; oldest dropped beyond this.
    _MAX_SESSIONS = 500

    def __init__(self, db_path: str | None = None):
        self.store = IncidentStore(db_path)
        set_store(self.store)
        # Turn state per chat session. It was one set of fields on this
        # process-wide object, so a thumbs-up in one session scored whichever
        # session's turn had finished last.
        self._sessions: dict[str, _TurnState] = {}

    def _state(self, session_id: str) -> _TurnState:
        state = self._sessions.get(session_id)
        if state is None:
            if len(self._sessions) >= self._MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            state = self._sessions[session_id] = _TurnState()
        return state

    def store_incident(self, incident: dict, confirmed: bool = False) -> int | None:
        """Store a learned incident from auto-fix or investigation.

        Args:
            incident: dict with keys query, tool_sequence, resolution,
                      namespace, resource_type, error_type.
            confirmed: True if the fix was verified (sets outcome='resolved').
        """
        outcome = "resolved" if confirmed else "unknown"
        tool_sequence = incident.get("tool_sequence", [])
        # Normalise tool_sequence to list-of-dicts expected by record_incident
        normalised = []
        for t in tool_sequence:
            if isinstance(t, str):
                normalised.append({"name": t})
            else:
                normalised.append(t)

        incident_id = self.store.record_incident(
            query=incident.get("query", ""),
            tool_sequence=normalised,
            resolution=incident.get("resolution", ""),
            outcome=outcome,
            namespace=incident.get("namespace", ""),
            resource_type=incident.get("resource_type", ""),
            error_type=incident.get("error_type", ""),
            score=0.8 if confirmed else 0.5,
        )
        if incident_id and incident_id > 0 and confirmed and len(normalised) >= 1:
            if not is_duplicate_runbook(self.store, normalised):
                extract_runbook(self.store, incident_id)
        return incident_id

    def record_fix_regression(
        self,
        category: str,
        tool: str,
        namespace: str = "",
        resource_type: str = "",
        recurrence_minutes: int = 0,
    ) -> dict:
        """A verified auto-fix did not hold — retract what was learned from it.

        ``store_incident(confirmed=True)`` recorded the fix as resolved the
        moment verification passed. A verdict has a time horizon: when the same
        condition returns inside the recurrence window, this walks that record
        back — the confirmed incident is demoted to "regressed", any runbook it
        seeded takes a failure count, and a low-score incident is stored so the
        anti-pattern surface warns about the fix instead of recommending it.
        """
        demoted_id = self.store.mark_recent_autofix_regressed(category, namespace)
        runbooks_penalized = 0
        if demoted_id is not None:
            runbooks_penalized = self.store.record_runbook_failure(demoted_id)

        when = f" {recurrence_minutes} min later" if recurrence_minutes else " shortly after verification"
        incident_id = self.store.record_incident(
            query=f"Auto-fix for {category} finding recurred",
            tool_sequence=[{"name": tool or category}],
            resolution=(
                f"Applied {tool or category} — verified healthy, then the same condition "
                f"returned{when}. This fix treats a symptom; it does not hold for this condition."
            ),
            outcome="regressed",
            namespace=namespace,
            resource_type=resource_type,
            error_type=category,
            score=0.2,
        )
        return {
            "demoted_incident_id": demoted_id,
            "runbooks_penalized": runbooks_penalized,
            "regression_incident_id": incident_id,
        }

    def augment_prompt(self, base_prompt: str | list[dict], user_query: str) -> str | list[dict]:
        memory_context = build_memory_context(self.store, user_query)
        if not memory_context:
            return base_prompt
        # Handle cache block format (list of content dicts from build_cached_system_prompt)
        if isinstance(base_prompt, list):
            blocks = list(base_prompt)
            blocks.append({"type": "text", "text": memory_context})
            return blocks
        return base_prompt + memory_context

    def get_extra_tools(self) -> list:
        return MEMORY_TOOLS

    def start_turn(self, session_id: str = _DEFAULT_SESSION):
        state = self._state(session_id)
        # Preserve previous turn's data before resetting
        state.prev_tool_calls = state.tool_calls[:]
        state.prev_rejected_count = state.rejected_count
        state.prev_turn_duration = time.time() - state.turn_start if state.turn_start else 0
        # Reset for new turn
        state.turn_start = time.time()
        state.tool_calls = []
        state.rejected_count = 0

    def record_tool_call(
        self, name: str, input_data: dict, was_rejected: bool = False, session_id: str = _DEFAULT_SESSION
    ):
        state = self._state(session_id)
        state.tool_calls.append(
            {
                "name": name,
                "input_summary": {k: str(v)[:50] for k, v in input_data.items()},
            }
        )
        if was_rejected:
            state.rejected_count += 1

    def finish_turn(
        self,
        user_query: str,
        final_response: str,
        user_confirmed: bool | None = None,
        session_id: str = _DEFAULT_SESSION,
    ) -> dict:
        state = self._state(session_id)
        duration = time.time() - state.turn_start

        eval_result = evaluate_interaction(
            tool_calls=state.tool_calls,
            rejected_count=state.rejected_count,
            user_confirmed_resolution=user_confirmed,
            duration_seconds=duration,
            final_response=final_response,
        )

        namespace = _extract_namespace(user_query)
        resource_type = _extract_resource_type(user_query)
        error_type = _extract_error_type(user_query + " " + final_response)
        outcome = "resolved" if user_confirmed else ("unresolved" if user_confirmed is False else "unknown")

        incident_id = self.store.record_incident(
            query=user_query,
            tool_sequence=state.tool_calls,
            resolution=final_response,
            outcome=outcome,
            namespace=namespace,
            resource_type=resource_type,
            error_type=error_type,
            tool_count=eval_result.tool_count,
            rejected_tools=eval_result.rejected_tools,
            duration_seconds=duration,
            score=eval_result.score,
        )
        state.last_incident_id = incident_id

        self.store.record_metric("interaction_score", eval_result.score)
        self.store.record_metric("tool_count", float(eval_result.tool_count))

        runbook_id = None
        # Create runbook from explicit thumbs-up OR high-scoring interactions with 2+ tools
        # Score is 0-1 scale (0.7 = 7/10 in the UI)
        should_learn = (outcome == "resolved" and len(state.tool_calls) >= 2) or (
            eval_result.score >= 0.7 and len(state.tool_calls) >= 2 and outcome != "unresolved"
        )
        if should_learn:
            if not is_duplicate_runbook(self.store, state.tool_calls):
                runbook_id = extract_runbook(self.store, incident_id)

        # Run pattern detection on a background thread every 10 incidents
        if self.store.get_incident_count() % 10 == 0:
            import threading

            threading.Thread(target=detect_patterns, args=(self.store,), daemon=True).start()

        return {
            "incident_id": incident_id,
            "score": eval_result.score,
            "breakdown": eval_result.breakdown,
            "runbook_id": runbook_id,
        }

    def update_last_outcome(self, resolved: bool, session_id: str = _DEFAULT_SESSION) -> dict | None:
        """Record a person's verdict on the most recent turn of *this* session.

        Uses preserved data from the previous turn to avoid stale-state bugs.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return None
        incident_id = state.last_incident_id
        if incident_id is None:
            return None

        # Use preserved previous turn data (not current turn which may have reset)
        tool_calls = state.prev_tool_calls or state.tool_calls
        rejected = state.prev_rejected_count if state.prev_tool_calls else state.rejected_count
        duration = state.prev_turn_duration if state.prev_tool_calls else (time.time() - state.turn_start)

        eval_result = evaluate_interaction(
            tool_calls=tool_calls,
            rejected_count=rejected,
            user_confirmed_resolution=resolved,
            duration_seconds=duration,
            final_response="",
        )
        self.store.update_incident_outcome(incident_id, "resolved" if resolved else "unresolved", eval_result.score)

        runbook_id = None
        if resolved and len(tool_calls) >= 2:
            if not is_duplicate_runbook(self.store, tool_calls):
                runbook_id = extract_runbook(self.store, incident_id)

        return {"incident_id": incident_id, "score": eval_result.score, "runbook_id": runbook_id}

    def close(self):
        self.store.close()


def _extract_namespace(text: str) -> str:
    """Extract namespace from query text."""
    patterns = [
        r"namespace[s]?\s+([a-zA-Z0-9][\w.-]*)",
        r"in\s+([a-zA-Z0-9][\w.-]*)\s+namespace",
        r"ns[/:]([a-zA-Z0-9][\w.-]*)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1).strip("'\"")
    return ""


def _extract_resource_type(text: str) -> str:
    resource_keywords = {
        "pod": "pod",
        "pods": "pod",
        "deployment": "deployment",
        "deploy": "deployment",
        "node": "node",
        "nodes": "node",
        "service": "service",
        "svc": "service",
        "pvc": "pvc",
        "volume": "pvc",
        "secret": "secret",
        "configmap": "configmap",
        "statefulset": "statefulset",
        "daemonset": "daemonset",
    }
    for word in text.lower().split():
        word = word.strip(".,?!")
        if word in resource_keywords:
            return resource_keywords[word]
    return ""


def _extract_error_type(text: str) -> str:
    error_patterns = [
        "CrashLoopBackOff",
        "OOMKilled",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "Pending",
        "Evicted",
        "NodeNotReady",
        "FailedScheduling",
        "BackOff",
        "Unhealthy",
    ]
    text_lower = text.lower()
    for ep in error_patterns:
        if ep.lower() in text_lower:
            return ep
    return ""
