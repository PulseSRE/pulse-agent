"""Self-improving agent layer — orchestrates memory, evaluation, and learning."""

from __future__ import annotations

import logging
import os
import re
import time

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
    val = os.environ.get("PULSE_AGENT_MEMORY", "1").lower()
    return val in ("1", "true", "yes")


class MemoryManager:
    """Orchestrates the self-improving agent layer."""

    def __init__(self, db_path: str | None = None):
        self.store = IncidentStore(db_path)
        set_store(self.store)
        self._turn_start: float = 0
        self._tool_calls: list[dict] = []
        self._rejected_count: int = 0
        self._last_incident_id: int | None = None
        # Preserved copies for feedback after a new turn starts
        self._prev_tool_calls: list[dict] = []
        self._prev_rejected_count: int = 0
        self._prev_turn_duration: float = 0

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

    def augment_prompt(self, base_prompt: str, user_query: str) -> str:
        memory_context = build_memory_context(self.store, user_query)
        if memory_context:
            return base_prompt + memory_context
        return base_prompt

    def get_extra_tools(self) -> list:
        return MEMORY_TOOLS

    def start_turn(self):
        # Preserve previous turn's data before resetting
        self._prev_tool_calls = self._tool_calls[:]
        self._prev_rejected_count = self._rejected_count
        self._prev_turn_duration = time.time() - self._turn_start if self._turn_start else 0
        # Reset for new turn
        self._turn_start = time.time()
        self._tool_calls = []
        self._rejected_count = 0

    def record_tool_call(self, name: str, input_data: dict, was_rejected: bool = False):
        self._tool_calls.append(
            {
                "name": name,
                "input_summary": {k: str(v)[:50] for k, v in input_data.items()},
            }
        )
        if was_rejected:
            self._rejected_count += 1

    def finish_turn(self, user_query: str, final_response: str, user_confirmed: bool | None = None) -> dict:
        duration = time.time() - self._turn_start

        eval_result = evaluate_interaction(
            tool_calls=self._tool_calls,
            rejected_count=self._rejected_count,
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
            tool_sequence=self._tool_calls,
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
        self._last_incident_id = incident_id

        self.store.record_metric("interaction_score", eval_result.score)
        self.store.record_metric("tool_count", float(eval_result.tool_count))

        runbook_id = None
        if outcome == "resolved" and len(self._tool_calls) >= 2:
            if not is_duplicate_runbook(self.store, self._tool_calls):
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

    def update_last_outcome(self, resolved: bool) -> dict | None:
        """Update the last incident's outcome after user feedback.

        Uses preserved data from the previous turn to avoid stale-state bugs.
        """
        if self._last_incident_id is None:
            return None

        # Use preserved previous turn data (not current turn which may have reset)
        tool_calls = self._prev_tool_calls or self._tool_calls
        rejected = self._prev_rejected_count if self._prev_tool_calls else self._rejected_count
        duration = self._prev_turn_duration if self._prev_tool_calls else (time.time() - self._turn_start)

        eval_result = evaluate_interaction(
            tool_calls=tool_calls,
            rejected_count=rejected,
            user_confirmed_resolution=resolved,
            duration_seconds=duration,
            final_response="",
        )
        self.store.update_incident_outcome(
            self._last_incident_id, "resolved" if resolved else "unresolved", eval_result.score
        )

        runbook_id = None
        if resolved and len(tool_calls) >= 2:
            if not is_duplicate_runbook(self.store, tool_calls):
                runbook_id = extract_runbook(self.store, self._last_incident_id)

        return {"incident_id": self._last_incident_id, "score": eval_result.score, "runbook_id": runbook_id}

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
