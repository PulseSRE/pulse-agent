"""Persisted monitor findings live in inbox_items, not legacy memory findings."""

import json

MONITOR_FINDINGS = """SELECT severity, resources,
    split_part(correlation_key, ':', 1) AS category
    FROM inbox_items WHERE created_by = 'system:monitor'
    AND finding_id IS NOT NULL AND finding_id <> ''"""
ACTIVE = " AND status NOT IN ('resolved', 'archived')"


def graph_findings(rows: list[dict]) -> list[dict]:
    """Expose canonical graph IDs to existing topology consumers."""
    result = []
    for row in rows:
        resources = json.loads(row["resources"] or "[]")
        if not isinstance(resources, list):
            raise ValueError("Monitor finding resources must be a JSON array")
        keys = []
        for resource in resources:
            if not isinstance(resource, dict) or not resource.get("kind") or not resource.get("name"):
                raise ValueError("Monitor finding resource has no kind/name")
            keys.append(f"{resource['kind']}:{resource.get('namespace', '')}:{resource['name']}")
        result.append({**row, "resources": ",".join(keys)})
    return result
