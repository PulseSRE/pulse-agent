"""Ops Inbox — unified SRE worklist with CRUD, lifecycle, priority, and dedup."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .db import get_database


def _publish_event(event_type: str, item_id: str, data: dict[str, Any] | None = None) -> None:
    from .api.view_events import publish_view_event

    publish_view_event(event_type, item_id, "system", data)


# -- Status lifecycles per item type --

VALID_TRANSITIONS: dict[str, dict[str, list[str]]] = {
    "finding": {
        "new": ["acknowledged", "agent_reviewing", "agent_cleared", "agent_review_failed"],
        "agent_reviewing": ["acknowledged", "agent_cleared", "agent_review_failed"],
        "agent_review_failed": ["new", "acknowledged", "archived"],
        "acknowledged": ["investigating", "agent_reviewing", "new"],
        "investigating": ["action_taken"],
        "action_taken": ["verifying"],
        "verifying": ["resolved", "investigating"],
        "resolved": ["archived"],
        "agent_cleared": ["new", "acknowledged", "archived"],
    },
    "task": {
        "new": ["in_progress", "agent_reviewing", "agent_cleared", "agent_review_failed"],
        "agent_reviewing": ["in_progress", "agent_cleared", "agent_review_failed"],
        "agent_review_failed": ["new", "in_progress", "archived"],
        "in_progress": ["resolved"],
        "resolved": ["archived"],
        "agent_cleared": ["new", "in_progress", "archived"],
    },
    "alert": {
        "new": ["acknowledged", "agent_reviewing", "agent_cleared", "agent_review_failed"],
        "agent_reviewing": ["acknowledged", "agent_cleared", "agent_review_failed"],
        "agent_review_failed": ["new", "acknowledged", "archived"],
        "acknowledged": ["resolved", "new"],
        "resolved": ["archived"],
        "agent_cleared": ["new", "acknowledged", "archived"],
    },
    "assessment": {
        "new": ["acknowledged", "agent_reviewing", "agent_cleared", "agent_review_failed"],
        "agent_reviewing": ["acknowledged", "agent_cleared", "agent_review_failed"],
        "agent_review_failed": ["new", "acknowledged", "archived"],
        "acknowledged": ["escalated", "new"],
        "agent_cleared": ["new", "acknowledged", "archived"],
    },
}

SEVERITY_WEIGHTS = {"critical": 4, "warning": 2, "info": 1}
AGE_BONUS_CAP = 2.0
AGE_BONUS_PER_HOUR = 0.1


def _gen_id() -> str:
    return f"inb-{uuid.uuid4().hex[:12]}"


def _get_cluster_id() -> str | None:
    try:
        from .config import get_settings

        return getattr(get_settings(), "cluster_id", None)
    except Exception:
        return None


def compute_priority_score(
    severity: str | None,
    confidence: float,
    noise_score: float,
    created_at: int,
    due_date: int | None,
) -> float:
    weight = SEVERITY_WEIGHTS.get(severity or "info", 1)
    base = weight * confidence * (1 - noise_score)

    age_hours = (time.time() - created_at) / 3600
    age_bonus = min(age_hours * AGE_BONUS_PER_HOUR, AGE_BONUS_CAP)

    due_bonus = 0.0
    if due_date is not None:
        hours_until = (due_date - time.time()) / 3600
        if hours_until <= 24:
            due_bonus = 2.0
        elif hours_until <= 72:
            due_bonus = 1.0

    return base + age_bonus + due_bonus


def create_inbox_item(item: dict[str, Any]) -> str:
    db = get_database()
    item_id = _gen_id()
    now = int(time.time())
    resources = item.get("resources", [])
    metadata = item.get("metadata", {})
    priority = compute_priority_score(
        severity=item.get("severity"),
        confidence=item.get("confidence", 0),
        noise_score=item.get("noise_score", 0),
        created_at=now,
        due_date=item.get("due_date"),
    )

    db.execute(
        """INSERT INTO inbox_items
        (id, item_type, status, title, summary, severity, priority_score,
         confidence, noise_score, namespace, resources, correlation_key,
         created_by, due_date, finding_id, view_id, cluster_id,
         pinned_by, metadata, created_at, updated_at)
        VALUES (?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
        (
            item_id,
            item["item_type"],
            item["title"],
            item.get("summary", ""),
            item.get("severity"),
            priority,
            item.get("confidence", 0),
            item.get("noise_score", 0),
            item.get("namespace"),
            json.dumps(resources),
            item.get("correlation_key"),
            item["created_by"],
            item.get("due_date"),
            item.get("finding_id"),
            item.get("view_id"),
            item.get("cluster_id") or _get_cluster_id(),
            json.dumps(metadata),
            now,
            now,
        ),
    )
    db.commit()
    _publish_event(
        "inbox_item_created",
        item_id,
        {"title": item["title"], "severity": item.get("severity"), "item_type": item["item_type"]},
    )
    return item_id


def get_inbox_item(item_id: str) -> dict[str, Any] | None:
    db = get_database()
    row = db.fetchone("SELECT * FROM inbox_items WHERE id = ?", (item_id,))
    if row is None:
        return None
    return _deserialize_row(row)


def list_inbox_items(
    item_type: str | None = None,
    status: str | None = None,
    namespace: str | None = None,
    claimed_by: str | None = None,
    severity: str | None = None,
    group_by: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    db = get_database()
    exclude_clause = None
    if status == "archived":
        exclude_clause = None
    elif status == "agent_cleared":
        exclude_clause = "status NOT IN ('archived')"
    elif status == "__needs_attention__":
        exclude_clause = "status NOT IN ('archived', 'agent_cleared', 'new', 'agent_reviewing')"
        status = None
    else:
        exclude_clause = "status NOT IN ('archived', 'agent_cleared')"
    where_parts = [
        "(snoozed_until IS NULL OR snoozed_until <= ?)",
    ]
    if exclude_clause:
        where_parts.append(exclude_clause)
    params: list[Any] = [int(time.time())]

    if item_type:
        where_parts.append("item_type = ?")
        params.append(item_type)
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if namespace:
        where_parts.append("namespace = ?")
        params.append(namespace)
    if claimed_by == "__null__":
        where_parts.append("claimed_by IS NULL")
    elif claimed_by:
        where_parts.append("claimed_by = ?")
        params.append(claimed_by)
    if severity:
        where_parts.append("severity = ?")
        params.append(severity)

    where = " AND ".join(where_parts)
    params.extend([limit, offset])
    rows = db.fetchall(
        f"SELECT * FROM inbox_items WHERE {where} ORDER BY priority_score DESC LIMIT ? OFFSET ?",
        tuple(params),
    )
    items = [_deserialize_row(r) for r in rows]

    groups: list[dict[str, Any]] = []
    ungrouped: list[dict[str, Any]] = []

    if group_by == "correlation":
        corr_map: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            key = item.get("correlation_key")
            if key:
                corr_map.setdefault(key, []).append(item)
            else:
                ungrouped.append(item)

        for key, group_items in corr_map.items():
            if len(group_items) >= 2:
                severities = [i.get("severity") for i in group_items if i.get("severity")]
                top = "critical" if "critical" in severities else ("warning" if "warning" in severities else "info")
                groups.append(
                    {
                        "correlation_key": key,
                        "items": group_items,
                        "count": len(group_items),
                        "top_severity": top,
                    }
                )
            else:
                ungrouped.extend(group_items)
    else:
        ungrouped = items

    all_for_stats = ungrouped + [i for g in groups for i in g["items"]]
    return {
        "items": ungrouped,
        "groups": groups,
        "stats": _compute_stats(all_for_stats),
        "total": len(all_for_stats),
    }


def _compute_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for item in items:
        s = item.get("status", "new")
        stats[s] = stats.get(s, 0) + 1
    stats["total"] = len(items)
    return stats


def get_inbox_stats() -> dict[str, int]:
    db = get_database()
    now = int(time.time())
    rows = db.fetchall(
        """SELECT status, COUNT(*) as cnt FROM inbox_items
        WHERE (snoozed_until IS NULL OR snoozed_until <= ?)
        GROUP BY status""",
        (now,),
    )
    stats: dict[str, int] = {}
    total = 0
    cleared = 0
    archived = 0
    for row in rows:
        stats[row["status"]] = row["cnt"]
        if row["status"] == "agent_cleared":
            cleared += row["cnt"]
        elif row["status"] == "archived":
            archived += row["cnt"]
        else:
            total += row["cnt"]
    stats["total"] = total
    stats["agent_cleared"] = cleared
    stats["archived"] = archived
    return stats


def update_item_status(item_id: str, new_status: str) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    item_type = item["item_type"]
    current_status = item["status"]
    transitions = VALID_TRANSITIONS.get(item_type, {})
    valid_next = transitions.get(current_status, [])

    if new_status not in valid_next:
        return False

    db = get_database()
    now = int(time.time())
    resolved_at = now if new_status == "resolved" else item.get("resolved_at")
    db.execute(
        "UPDATE inbox_items SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
        (new_status, now, resolved_at, item_id),
    )
    db.commit()
    event_type = "inbox_item_resolved" if new_status == "resolved" else "inbox_item_updated"
    _publish_event(event_type, item_id, {"status": new_status})
    return True


def claim_item(item_id: str, username: str) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    db = get_database()
    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET claimed_by = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        (username, now, now, item_id),
    )
    db.commit()
    _publish_event("inbox_item_claimed", item_id, {"claimed_by": username, "claimed_at": now})

    if item["status"] == "new":
        if item["item_type"] == "task":
            update_item_status(item_id, "in_progress")
        elif item["item_type"] == "finding":
            update_item_status(item_id, "acknowledged")

    _generate_view_for_item(item_id, item)

    return True


def _generate_view_for_item(item_id: str, item: dict[str, Any]) -> None:
    """Generate an investigation view when a user claims an item."""
    metadata = item.get("metadata", {})
    if not metadata.get("investigation_summary") and not metadata.get("action_plan"):
        return

    if item.get("view_id"):
        return

    try:
        metadata["view_status"] = "generating"
        db = get_database()
        db.execute(
            "UPDATE inbox_items SET metadata = ? WHERE id = ?",
            (json.dumps(metadata), item_id),
        )
        db.commit()

        from .view_tools import create_dashboard

        title = f"Investigation: {item['title'][:60]}"
        view_type = "incident" if item["item_type"] == "finding" else "plan"

        result = create_dashboard(
            title=title,
            description=item.get("summary", ""),
            view_type=view_type,
            trigger_source="agent",
            finding_id=item.get("finding_id") or item_id,
            visibility="team",
        )

        view_id = None
        if isinstance(result, tuple):
            _, signal = result
            if isinstance(signal, dict):
                view_id = signal.get("view_id") or signal.get("id")
        elif isinstance(result, str) and "cv-" in result:
            import re as _re2

            match = _re2.search(r"(cv-[a-f0-9]+)", result)
            if match:
                view_id = match.group(1)

        if view_id:
            metadata["view_status"] = "ready"
            now = int(time.time())
            db.execute(
                "UPDATE inbox_items SET view_id = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (view_id, json.dumps(metadata), now, item_id),
            )
            db.commit()
            _publish_event("inbox_item_updated", item_id, {"view_id": view_id})
            _inbox_logger.info("Generated view %s for inbox item %s", view_id, item_id)
        else:
            metadata["view_status"] = "failed"
            db.execute(
                "UPDATE inbox_items SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), item_id),
            )
            db.commit()
            _inbox_logger.warning("View generation returned no view_id for %s", item_id)
    except Exception:
        _inbox_logger.exception("View generation failed for %s", item_id)
        metadata["view_status"] = "failed"
        try:
            db = get_database()
            db.execute(
                "UPDATE inbox_items SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), item_id),
            )
            db.commit()
        except Exception:
            _inbox_logger.exception("Failed to update view_status for %s", item_id)


def claim_and_investigate(item_id: str, username: str) -> bool:
    """Atomically claim an item and transition to investigating status."""
    item = get_inbox_item(item_id)
    if item is None:
        return False

    db = get_database()
    now = int(time.time())

    target_status = item["status"]
    if item["item_type"] == "finding":
        if item["status"] == "new":
            target_status = "acknowledged"
        if item["status"] in ("new", "acknowledged"):
            target_status = "investigating"

    db.execute(
        "UPDATE inbox_items SET claimed_by = ?, claimed_at = ?, status = ?, updated_at = ? WHERE id = ? AND (claimed_by IS NULL OR claimed_by = ?)",
        (username, now, target_status, now, item_id, username),
    )
    db.commit()

    updated = get_inbox_item(item_id)
    if updated and updated["claimed_by"] == username:
        _publish_event("inbox_item_claimed", item_id, {"claimed_by": username, "claimed_at": now})
        _publish_event("inbox_item_updated", item_id, {"status": target_status})
        return True
    return False


def unclaim_item(item_id: str) -> bool:
    db = get_database()
    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET claimed_by = NULL, claimed_at = NULL, updated_at = ? WHERE id = ?",
        (now, item_id),
    )
    db.commit()
    _publish_event("inbox_item_updated", item_id, {"claimed_by": None})
    return True


def snooze_item(item_id: str, hours: float) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    db = get_database()
    now = int(time.time())
    snoozed_until = now + int(hours * 3600)

    metadata = item.get("metadata", {})
    metadata["pre_snooze_status"] = item["status"]

    db.execute(
        "UPDATE inbox_items SET snoozed_until = ?, metadata = ?, updated_at = ? WHERE id = ?",
        (snoozed_until, json.dumps(metadata), now, item_id),
    )
    db.commit()
    return True


def unsnooze_expired() -> int:
    db = get_database()
    now = int(time.time())
    rows = db.fetchall(
        "SELECT id, metadata FROM inbox_items WHERE snoozed_until IS NOT NULL AND snoozed_until <= ?",
        (now,),
    )
    count = 0
    for row in rows:
        raw = row["metadata"]
        metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
        pre_status = metadata.pop("pre_snooze_status", "new")
        db.execute(
            "UPDATE inbox_items SET snoozed_until = NULL, status = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (pre_status, json.dumps(metadata), now, row["id"]),
        )
        count += 1
    if count:
        db.commit()
    return count


def upsert_inbox_item(item: dict[str, Any]) -> str:
    db = get_database()
    corr_key = item.get("correlation_key")
    item_type = item["item_type"]

    existing = None
    if corr_key:
        row = db.fetchone(
            "SELECT * FROM inbox_items WHERE correlation_key = ? AND item_type = ? AND status NOT IN ('resolved', 'archived')",
            (corr_key, item_type),
        )
        if row:
            existing = _deserialize_row(row)

    if existing is None:
        return create_inbox_item(item)

    merged_resources = _merge_resources(existing.get("resources", []), item.get("resources", []))

    now = int(time.time())
    priority = compute_priority_score(
        severity=item.get("severity", existing.get("severity")),
        confidence=item.get("confidence", existing.get("confidence", 0)),
        noise_score=item.get("noise_score", existing.get("noise_score", 0)),
        created_at=existing["created_at"],
        due_date=item.get("due_date", existing.get("due_date")),
    )

    db.execute(
        "UPDATE inbox_items SET resources = ?, priority_score = ?, updated_at = ? WHERE id = ?",
        (json.dumps(merged_resources), priority, now, existing["id"]),
    )
    db.commit()
    return existing["id"]


def escalate_assessment(item_id: str) -> str | None:
    item = get_inbox_item(item_id)
    if item is None or item["item_type"] != "assessment":
        return None

    db = get_database()
    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET status = 'escalated', updated_at = ? WHERE id = ?",
        (now, item_id),
    )
    db.commit()

    finding_item = {
        "item_type": "finding",
        "title": item["title"],
        "summary": item.get("summary", ""),
        "severity": item.get("severity", "warning"),
        "confidence": item.get("confidence", 0),
        "noise_score": 0,
        "namespace": item.get("namespace"),
        "resources": item.get("resources", []),
        "correlation_key": item.get("correlation_key"),
        "created_by": "system:monitor",
        "metadata": {"escalated_from": item_id},
    }
    finding_id = create_inbox_item(finding_item)

    metadata = item.get("metadata", {})
    metadata["escalated_to"] = finding_id
    db.execute(
        "UPDATE inbox_items SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata), now, item_id),
    )
    db.commit()

    return finding_id


def pin_item(item_id: str, username: str) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    pinned = item.get("pinned_by", [])
    if username in pinned:
        pinned.remove(username)
    else:
        pinned.append(username)

    db = get_database()
    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET pinned_by = ?, updated_at = ? WHERE id = ?",
        (json.dumps(pinned), now, item_id),
    )
    db.commit()
    return True


def restore_item(item_id: str) -> bool:
    """Restore an agent-cleared item back to new status (user override)."""
    return update_item_status(item_id, "new")


def dismiss_item(item_id: str) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False
    db = get_database()
    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET status = 'archived', updated_at = ?, resolved_at = ? WHERE id = ?",
        (now, now, item_id),
    )
    db.commit()
    _publish_event("inbox_item_updated", item_id, {"status": "archived"})
    return True


_last_prune_time = 0
_PRUNE_INTERVAL = 86400


def prune_old_items(max_age_days: int = 30) -> int:
    global _last_prune_time
    now = time.time()
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return 0
    _last_prune_time = now

    db = get_database()
    cutoff = int(now) - max_age_days * 86400
    cur = db.execute(
        "DELETE FROM inbox_items WHERE status IN ('resolved', 'archived') AND resolved_at IS NOT NULL AND resolved_at < ?",
        (cutoff,),
    )
    db.commit()

    global _last_investigate_time
    _last_investigate_time = {k: v for k, v in _last_investigate_time.items() if v > cutoff}

    return cur.rowcount if hasattr(cur, "rowcount") else 0


def _merge_resources(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {(r["kind"], r["name"], r["namespace"]) for r in existing}
    merged = list(existing)
    for r in new:
        if (r["kind"], r["name"], r["namespace"]) not in seen:
            merged.append(r)
    return merged


def _deserialize_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    for json_field in ("resources", "pinned_by", "metadata"):
        if json_field in d and isinstance(d[json_field], str):
            d[json_field] = json.loads(d[json_field])
    return d


# -- Monitor integration --


def bridge_finding_to_inbox(finding: dict[str, Any]) -> str:
    """Create or update an inbox item from a monitor finding."""
    finding_id = finding.get("id", "")
    db = get_database()

    existing = db.fetchone(
        "SELECT * FROM inbox_items WHERE finding_id = ? AND status NOT IN ('resolved', 'archived')",
        (finding_id,),
    )

    if existing:
        existing_item = _deserialize_row(existing)
        merged_resources = _merge_resources(existing_item.get("resources", []), finding.get("resources", []))

        now = int(time.time())
        priority = compute_priority_score(
            severity=finding.get("severity"),
            confidence=finding.get("confidence", 0),
            noise_score=finding.get("noiseScore", 0),
            created_at=existing_item["created_at"],
            due_date=None,
        )
        db.execute(
            "UPDATE inbox_items SET resources = ?, priority_score = ?, updated_at = ? WHERE id = ?",
            (json.dumps(merged_resources), priority, now, existing_item["id"]),
        )
        db.commit()
        return existing_item["id"]

    item = {
        "item_type": "finding",
        "title": finding.get("title", "Unknown finding"),
        "summary": finding.get("summary", ""),
        "severity": finding.get("severity", "warning"),
        "confidence": finding.get("confidence", 0),
        "noise_score": finding.get("noiseScore", 0),
        "namespace": finding.get("namespace"),
        "resources": finding.get("resources", []),
        "correlation_key": f"{finding.get('category', 'unknown')}:{finding.get('namespace', '')}",
        "created_by": "system:monitor",
        "finding_id": finding_id,
    }
    return create_inbox_item(item)


_last_investigate_time: dict[str, float] = {}
_INVESTIGATE_COOLDOWN = 120

import logging as _logging
import re as _re

_inbox_logger = _logging.getLogger("pulse_agent.inbox")


def agent_process_inbox() -> None:
    """Three-phase agent pipeline: triage → investigate → plan."""
    _phase_a_triage()
    _phase_b_investigate()
    _phase_c_plan()


def _phase_a_triage() -> int:
    """Triage new items: classify as investigate/dismiss/monitor and act."""
    db = get_database()
    rows = db.fetchall(
        """SELECT * FROM inbox_items
        WHERE status = 'new'
        AND (metadata NOT LIKE ? OR metadata NOT LIKE ?)
        ORDER BY priority_score DESC
        LIMIT 5""",
        ('%"triaged"%', "%true%"),
    )
    if not rows:
        return 0

    try:
        from .config import get_settings

        model = get_settings().model
    except Exception:
        _inbox_logger.exception("Failed to get settings for triage")
        return 0

    triaged = 0
    for row in rows:
        item = _deserialize_row(row)
        if item.get("metadata", {}).get("triaged"):
            continue

        is_user_created = item["created_by"] not in ("system:monitor", "system:agent")
        resources_str = ", ".join(f"{r['kind']}/{r['name']}" for r in item.get("resources", []))
        prompt = (
            f"Triage this {item['item_type']}: {item['title']}. "
            f"{item.get('summary', '')} "
            f"Resources: {resources_str or 'none'}. "
            f"Namespace: {item.get('namespace') or 'cluster-wide'}. "
            f"Severity: {item.get('severity', 'unknown')}. "
            f"{'This was manually created by a user — default to investigate, do not dismiss.' if is_user_created else ''}"
            f"Provide: (1) a one-sentence assessment, (2) recommended action (investigate/dismiss/monitor), "
            f"(3) urgency (immediate/soon/can-wait), (4) confidence 0-1. Reply in JSON: "
            f'{{"assessment": "...", "action": "investigate|dismiss|monitor", "urgency": "immediate|soon|can-wait", "confidence": 0.8}}'
        )

        try:
            from .agent import create_client

            client = create_client()
            response = client.messages.create(
                model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()

            match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if not match:
                continue

            triage = json.loads(match.group())
            metadata = item.get("metadata", {})
            metadata["triaged"] = True
            metadata["triage_assessment"] = triage.get("assessment", "")
            metadata["triage_action"] = triage.get("action", "monitor")
            metadata["triage_urgency"] = triage.get("urgency", "can-wait")
            metadata["triage_confidence"] = triage.get("confidence", 0.5)

            action = triage.get("action", "monitor")
            confidence = float(triage.get("confidence", 0.5))

            if action == "dismiss" and confidence >= 0.7 and not is_user_created:
                new_status = "agent_cleared"
                metadata["dismiss_reason"] = triage.get("assessment", "")
            elif action == "investigate":
                new_status = "agent_reviewing"
            else:
                new_status = "acknowledged"

            now = int(time.time())
            db.execute(
                "UPDATE inbox_items SET status = ?, metadata = ?, summary = ?, updated_at = ? WHERE id = ?",
                (new_status, json.dumps(metadata), triage.get("assessment", item.get("summary", "")), now, item["id"]),
            )
            db.commit()
            _publish_event("inbox_item_updated", item["id"], {"status": new_status})
            triaged += 1
            _inbox_logger.info("Triaged %s → %s (%s)", item["id"], new_status, action)
        except Exception:
            _inbox_logger.exception("Triage failed for %s", item["id"])
            update_item_status(item["id"], "agent_review_failed")

    return triaged


def _phase_b_investigate() -> int:
    """Investigate items the triage flagged for review using the SRE agent."""
    db = get_database()
    rows = db.fetchall(
        """SELECT * FROM inbox_items
        WHERE status = 'agent_reviewing'
        ORDER BY priority_score DESC
        LIMIT 3""",
    )
    if not rows:
        return 0

    investigated = 0
    now = time.time()

    for row in rows:
        item = _deserialize_row(row)

        if now - _last_investigate_time.get(item["id"], 0) < _INVESTIGATE_COOLDOWN:
            continue

        _last_investigate_time[item["id"]] = now

        finding_dict = {
            "id": item.get("finding_id") or item["id"],
            "title": item["title"],
            "summary": item.get("summary", ""),
            "severity": item.get("severity", "warning"),
            "category": item.get("metadata", {}).get("generator", item.get("item_type", "unknown")),
            "resources": item.get("resources", []),
            "namespace": item.get("namespace", ""),
            "confidence": item.get("confidence", 0.5),
        }

        try:
            from .monitor.actions import save_investigation
            from .monitor.investigations import _run_proactive_investigation_sync

            result = _run_proactive_investigation_sync(finding_dict)

            if result.get("summary"):
                investigation_id = result.get("id", f"inv-{item['id']}")
                save_investigation(result)

                metadata = item.get("metadata", {})
                metadata["investigation_id"] = investigation_id
                metadata["investigation_summary"] = result.get("summary", "")
                metadata["suspected_cause"] = result.get("suspected_cause", "")
                metadata["recommended_fix"] = result.get("recommended_fix", "")
                metadata["investigation_confidence"] = result.get("confidence", 0)
                metadata["evidence"] = result.get("evidence", [])

                try:
                    from .dependency_graph import get_graph

                    resources = item.get("resources", [])
                    graph = get_graph()
                    if resources and graph:
                        r = resources[0]
                        affected = graph.downstream_blast_radius(
                            r.get("kind", ""), r.get("namespace", ""), r.get("name", "")
                        )
                        if affected:
                            metadata["blast_radius"] = {
                                "affected_count": len(affected),
                                "affected_resources": affected[:10],
                            }
                except Exception:
                    _inbox_logger.debug("Blast radius enrichment failed for %s", item["id"], exc_info=True)

                inv_confidence = float(result.get("confidence", 0))
                recommended_fix = result.get("recommended_fix", "")
                no_action = any(
                    phrase in recommended_fix.lower()
                    for phrase in ["no action", "no issue", "expected behavior", "working as intended", "by design"]
                )

                if inv_confidence >= 0.85 and no_action:
                    new_status = "agent_cleared"
                    metadata["dismiss_reason"] = f"Investigation found no issue: {result.get('summary', '')}"
                else:
                    new_status = "acknowledged"

                ts = int(time.time())
                db.execute(
                    "UPDATE inbox_items SET status = ?, metadata = ?, updated_at = ? WHERE id = ?",
                    (new_status, json.dumps(metadata), ts, item["id"]),
                )
                db.commit()
                _publish_event("inbox_item_updated", item["id"], {"status": new_status})
                investigated += 1
                _inbox_logger.info("Investigated %s → %s (confidence: %.2f)", item["id"], new_status, inv_confidence)
        except Exception:
            _inbox_logger.exception("Investigation failed for %s", item["id"])
            update_item_status(item["id"], "agent_review_failed")

    return investigated


def _phase_c_plan() -> int:
    """Generate step-by-step action plans for investigated items."""
    db = get_database()
    rows = db.fetchall(
        """SELECT * FROM inbox_items
        WHERE status = 'acknowledged'
        AND metadata NOT LIKE ?
        ORDER BY priority_score DESC
        LIMIT 3""",
        ("%action_plan%",),
    )
    if not rows:
        return 0

    try:
        from .config import get_settings

        model = get_settings().model
    except Exception:
        _inbox_logger.exception("Failed to get settings for plan generation")
        return 0

    planned = 0
    for row in rows:
        item = _deserialize_row(row)
        if item.get("metadata", {}).get("action_plan"):
            continue

        investigation = item.get("metadata", {}).get("investigation_summary", "")
        cause = item.get("metadata", {}).get("suspected_cause", "")
        fix = item.get("metadata", {}).get("recommended_fix", "")
        resources_str = ", ".join(f"{r['kind']}/{r['name']}" for r in item.get("resources", []))

        if not investigation and not fix:
            continue

        prompt = (
            f"Based on this investigation of '{item['title']}':\n"
            f"- Summary: {investigation}\n"
            f"- Suspected cause: {cause}\n"
            f"- Recommended fix: {fix}\n"
            f"- Resources: {resources_str or 'none'}\n"
            f"- Namespace: {item.get('namespace') or 'cluster-wide'}\n\n"
            f"Generate a step-by-step action plan. Each step should be concrete and actionable. "
            f"If a step can be executed by a K8s tool, include the tool name and input. "
            f"Reply in JSON:\n"
            f'{{"steps": [{{"title": "...", "description": "...", "tool": "tool_name_or_null", '
            f'"tool_input": {{}} , "risk": "low|medium|high"}}]}}'
        )

        try:
            from .agent import create_client

            client = create_client()
            response = client.messages.create(
                model=model, max_tokens=500, messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()

            match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if not match:
                continue

            plan_data = json.loads(match.group())
            steps = plan_data.get("steps", [])
            if not steps:
                continue

            for step in steps:
                step["status"] = "pending"

            metadata = item.get("metadata", {})
            metadata["action_plan"] = steps

            now = int(time.time())
            db.execute(
                "UPDATE inbox_items SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata), now, item["id"]),
            )
            db.commit()
            _publish_event("inbox_item_updated", item["id"], {"has_plan": True})
            planned += 1
            _inbox_logger.info("Generated %d-step plan for %s", len(steps), item["id"])
        except Exception:
            _inbox_logger.exception("Plan generation failed for %s", item["id"])

    return planned


def resolve_finding_inbox_item(finding_id: str) -> bool:
    """Resolve an inbox item when its linked finding resolves."""
    db = get_database()
    row = db.fetchone(
        "SELECT * FROM inbox_items WHERE finding_id = ? AND status NOT IN ('resolved', 'archived')",
        (finding_id,),
    )
    if row is None:
        return False

    item = _deserialize_row(row)
    if item["status"] == "verifying":
        return update_item_status(item["id"], "resolved")

    now = int(time.time())
    db.execute(
        "UPDATE inbox_items SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
        (now, now, item["id"]),
    )
    db.commit()
    _publish_event("inbox_item_resolved", item["id"], {"resolved_at": now})
    return True


def run_generator_cycle() -> None:
    """Run all generators, upsert items, auto-resolve cleared conditions."""
    from .inbox_generators import run_all_generators

    generated = run_all_generators()

    generated_keys: set[str] = set()
    for item in generated:
        corr_key = item.get("correlation_key", "")
        if corr_key:
            generated_keys.add(corr_key)
        upsert_inbox_item(item)

    db = get_database()
    rows = db.fetchall(
        """SELECT id, correlation_key, metadata FROM inbox_items
        WHERE item_type = 'assessment'
        AND status IN ('new', 'acknowledged')""",
    )
    generator_rows = [r for r in rows if _deserialize_row(r).get("metadata", {}).get("generator")]
    now = int(time.time())
    for row in generator_rows:
        if row["correlation_key"] and row["correlation_key"] not in generated_keys:
            db.execute(
                "UPDATE inbox_items SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
    db.commit()

    unsnooze_expired()
    prune_old_items()

    try:
        agent_process_inbox()
    except Exception:
        _inbox_logger.exception("agent_process_inbox failed")


# -- Agent tool --

from .decorators import beta_tool
from .tool_registry import register_tool

URGENCY_MAP = {"today": 8, "this_week": 168, "this_month": 720}


@beta_tool
def create_inbox_task(
    title: str,
    detail: str = "",
    urgency: str = "this_week",
    namespace: str = "",
    resource_name: str = "",
    resource_kind: str = "",
) -> str:
    """Add a task to the ops inbox.

    Use when the user asks to track, remind, or follow up on something.
    Examples: "remind me to rotate certs", "add task: review HPA config",
    "track the CoreDNS upgrade".

    Args:
        title: Short description
        detail: Actionable guidance on what to do
        urgency: today (8h), this_week (168h), this_month (720h)
        namespace: Optional K8s namespace
        resource_name: Optional resource name
        resource_kind: Optional resource kind (Deployment, Node, etc.)
    """
    if urgency not in URGENCY_MAP:
        return f"Error: invalid urgency '{urgency}'. Use: today, this_week, this_month"

    hours = URGENCY_MAP[urgency]
    now = int(time.time())

    resources = []
    if resource_name and resource_kind:
        resources.append({"kind": resource_kind, "name": resource_name, "namespace": namespace or "default"})

    item = {
        "item_type": "task",
        "title": title,
        "summary": detail,
        "severity": "warning" if hours <= 8 else "info",
        "confidence": 1.0,
        "noise_score": 0,
        "namespace": namespace or None,
        "resources": resources,
        "created_by": "system:agent",
        "due_date": now + hours * 3600,
        "metadata": {"urgency_hours": hours, "generator": "agent"},
    }
    item_id = create_inbox_item(item)
    return f"Created inbox task: {title} (id: {item_id}, due: {urgency})"


register_tool(create_inbox_task)
