"""Shared context bus for cross-agent communication.

Allows the Monitor, SRE Agent, and Security Agent to share recent
findings, investigations, fixes, and diagnoses so each component
can make better-informed decisions.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ContextEntry:
    source: str  # 'monitor', 'sre_agent', 'security_agent'
    category: str  # 'finding', 'investigation', 'fix', 'diagnosis', 'user_resolution', 'verification'
    summary: str
    details: dict
    timestamp: float = field(default_factory=time.time)
    namespace: str = ""
    resources: list = field(default_factory=list)


class ContextBus:
    """Shared context between Monitor, SRE Agent, and Security Agent."""

    def __init__(self, max_entries: int = 100, ttl_seconds: int = 3600):
        self._entries: deque[ContextEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def publish(self, entry: ContextEntry) -> None:
        """Publish a context entry from any agent."""
        with self._lock:
            self._entries.append(entry)

    def get_context_for(self, namespace: str = "", category: str = "", limit: int = 5) -> list[ContextEntry]:
        """Get recent context entries, optionally filtered."""
        with self._lock:
            now = time.time()
            entries = [e for e in self._entries if now - e.timestamp < self._ttl]
            if namespace:
                entries = [e for e in entries if e.namespace == namespace or not e.namespace]
            if category:
                entries = [e for e in entries if e.category == category]
            return sorted(entries, key=lambda e: e.timestamp, reverse=True)[:limit]

    def build_context_prompt(self, namespace: str = "", limit: int = 5) -> str:
        """Build a context injection string for agent system prompts."""
        entries = self.get_context_for(namespace=namespace, limit=limit)
        if not entries:
            return ""
        lines = ["## Recent Agent Activity (shared context)"]
        for e in entries:
            age = int(time.time() - e.timestamp)
            age_str = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
            lines.append(f"- [{e.source}] {e.summary} ({age_str})")
            if e.details.get("suspected_cause"):
                lines.append(f"  Suspected cause: {e.details['suspected_cause']}")
            if e.details.get("fix_applied"):
                lines.append(f"  Fix applied: {e.details['fix_applied']}")
        return "\n".join(lines)


# Singleton
_bus = ContextBus()


def get_context_bus() -> ContextBus:
    return _bus
