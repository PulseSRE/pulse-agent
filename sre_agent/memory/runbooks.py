"""Runbook extraction from resolved incidents."""

from __future__ import annotations

import json

from .store import IncidentStore


def extract_runbook(store: IncidentStore, incident_id: int,
                    name: str | None = None) -> int | None:
    """Extract a reusable runbook from a resolved incident.

    Returns runbook ID or None if not suitable.
    """
    rows = store.db.fetchall(
        "SELECT * FROM incidents WHERE id = ? AND outcome = 'resolved'",
        (incident_id,)
    )
    if not rows:
        return None

    incident = rows[0]
    tool_sequence = json.loads(incident["tool_sequence"])

    if len(tool_sequence) < 2:
        return None

    if is_duplicate_runbook(store, tool_sequence):
        return None

    if not name:
        parts = []
        if incident["error_type"]:
            parts.append(incident["error_type"])
        if incident["resource_type"]:
            parts.append(incident["resource_type"])
        parts.append("runbook")
        name = "-".join(parts) or f"runbook-{incident_id}"

    description = f"Learned from: \"{incident['query'][:120]}\""

    trigger_kws = incident["query_keywords"]
    if incident["error_type"]:
        trigger_kws += " " + incident["error_type"].lower()
    if incident["resource_type"]:
        trigger_kws += " " + incident["resource_type"].lower()

    return store.save_runbook(
        name=name,
        description=description,
        trigger_keywords=trigger_kws,
        tool_sequence=tool_sequence,
        source_incident_id=incident_id,
    )


def is_duplicate_runbook(store: IncidentStore, tool_sequence: list[dict]) -> bool:
    """Check if a runbook with the same tool name sequence exists."""
    tool_names = tuple(t["name"] for t in tool_sequence)
    existing = store.db.fetchall("SELECT tool_sequence FROM runbooks")
    for row in existing:
        existing_names = tuple(t["name"] for t in json.loads(row["tool_sequence"]))
        if existing_names == tool_names:
            return True
    return False
