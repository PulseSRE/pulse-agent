"""Ops Inbox — unified SRE worklist with CRUD, lifecycle, priority, and dedup."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from .repositories.inbox_repo import get_inbox_repo

logger = logging.getLogger("pulse_agent.inbox")
_inbox_logger = logger


def _resource_exists(resource: dict[str, str]) -> bool:
    """Quick K8s API check — returns False if the resource is gone (404)."""
    kind = resource.get("kind", "").lower()
    name = resource.get("name", "")
    ns = resource.get("namespace", "default")
    if not kind or not name:
        return True

    from kubernetes.client.rest import ApiException

    from .k8s_client import get_apps_client, get_core_client

    try:
        if kind == "pod":
            get_core_client().read_namespaced_pod(name, ns)
        elif kind == "deployment":
            get_apps_client().read_namespaced_deployment(name, ns)
        elif kind == "statefulset":
            get_apps_client().read_namespaced_stateful_set(name, ns)
        elif kind == "daemonset":
            get_apps_client().read_namespaced_daemon_set(name, ns)
        elif kind == "service":
            get_core_client().read_namespaced_service(name, ns)
        elif kind == "node":
            get_core_client().read_node(name)
        else:
            return True
        return True
    except ApiException as e:
        return e.status != 404
    except Exception:
        return True


def _publish_event(event_type: str, item_id: str, data: dict[str, Any] | None = None) -> None:
    from .api.view_events import publish_view_event

    publish_view_event(event_type, item_id, "system", data)


def record_interaction(
    *,
    actor: str,
    interaction_type: str,
    item_id: str | None = None,
    action_id: str | None = None,
    decision: str = "",
    metadata: dict | None = None,
) -> None:
    """Fire-and-forget audit record for human-in-the-loop decisions."""
    try:
        repo = get_inbox_repo()
        repo.record_interaction(actor, interaction_type, item_id, action_id, decision, json.dumps(metadata or {}))
    except Exception:
        logger.debug("Failed to record interaction", exc_info=True)


# -- Simplified lifecycle: New → Triaged → Claimed → In Progress → Resolved --
# All item types share the same transition map.

_TRANSITIONS: dict[str, list[str]] = {
    "new": ["agent_reviewing", "triaged", "agent_cleared", "agent_review_failed"],
    "agent_reviewing": ["triaged", "agent_cleared", "agent_review_failed"],
    "agent_review_failed": ["new", "triaged", "archived"],
    "triaged": ["claimed", "in_progress", "new"],
    "claimed": ["in_progress", "resolved", "archived", "new"],
    "in_progress": ["resolved", "archived", "new"],
    "resolved": ["archived", "new"],
    "agent_cleared": ["new", "triaged", "archived"],
}

VALID_TRANSITIONS: dict[str, dict[str, list[str]]] = {
    "task": dict(_TRANSITIONS),
}


def _get_transitions(item_type: str) -> dict[str, list[str]]:
    return VALID_TRANSITIONS.get(item_type, VALID_TRANSITIONS["task"])


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
    item_id = _gen_id()
    now = int(time.time())
    priority = compute_priority_score(
        severity=item.get("severity"),
        confidence=item.get("confidence", 0),
        noise_score=item.get("noise_score", 0),
        created_at=now,
        due_date=item.get("due_date"),
    )

    cluster_id = item.get("cluster_id") or _get_cluster_id()
    get_inbox_repo().insert_item(item_id, item, priority, cluster_id, now)
    _publish_event(
        "inbox_item_created",
        item_id,
        {"title": item["title"], "severity": item.get("severity"), "item_type": item["item_type"]},
    )
    return item_id


def get_inbox_item(item_id: str) -> dict[str, Any] | None:
    row = get_inbox_repo().fetch_item(item_id)
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
    exclude_clause = None
    if status == "archived":
        exclude_clause = None
    elif status == "agent_cleared":
        exclude_clause = "status NOT IN ('archived')"
    elif status == "__needs_attention__":
        exclude_clause = "status NOT IN ('archived', 'agent_cleared', 'new', 'agent_reviewing', 'agent_review_failed') AND (severity IS NULL OR severity != 'info')"
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
    rows = get_inbox_repo().query_items(where, tuple(params))
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


_NEEDS_ATTENTION_EXCLUDE = frozenset({"archived", "agent_cleared", "new", "agent_reviewing", "agent_review_failed"})


def get_inbox_stats() -> dict[str, int]:
    now = int(time.time())
    rows = get_inbox_repo().get_stats_rows(now)
    stats: dict[str, int] = {}
    total = 0
    cleared = 0
    archived = 0
    needs_attention = 0
    unique_total = 0
    for row in rows:
        stats[row["status"]] = row["cnt"]
        unique_cnt = row.get("unique_cnt", row["cnt"])
        if row["status"] == "agent_cleared":
            cleared += row["cnt"]
        elif row["status"] == "archived":
            archived += row["cnt"]
        else:
            total += row["cnt"]
            unique_total += unique_cnt
        if row["status"] not in _NEEDS_ATTENTION_EXCLUDE:
            needs_attention += row["cnt"]
    stats["total"] = total
    stats["agent_cleared"] = cleared
    stats["archived"] = archived
    stats["needs_attention"] = needs_attention
    stats["unique_issues"] = unique_total
    return stats


_STALE_THRESHOLD = 300  # 5 minutes


def sweep_stale_items() -> int:
    """Reset items stuck in agent_reviewing after restart (>5 min stale)."""
    repo = get_inbox_repo()
    now = int(time.time())
    stale_cutoff = now - _STALE_THRESHOLD
    rows = repo.fetch_stale_agent_reviewing(stale_cutoff)
    swept = 0
    for row in rows:
        item = _deserialize_row(row)
        metadata = item.get("metadata", {})
        metadata.pop("triaged", None)
        repo.update_triage_result(row["id"], "new", metadata, item.get("summary", ""), now)
        swept += 1
    if swept:
        _inbox_logger.info("Startup sweep: reset %d stale agent_reviewing items to new", swept)
    return swept


def update_item_status(item_id: str, new_status: str, actor: str = "") -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    item_type = item["item_type"]
    current_status = item["status"]
    transitions = _get_transitions(item_type)
    valid_next = transitions.get(current_status, [])

    if new_status not in valid_next:
        return False

    now = int(time.time())
    resolved_at = now if new_status == "resolved" else item.get("resolved_at")
    get_inbox_repo().update_status(item_id, new_status, now, resolved_at)
    event_type = "inbox_item_resolved" if new_status == "resolved" else "inbox_item_updated"
    _publish_event(event_type, item_id, {"status": new_status})
    if actor:
        record_interaction(actor=actor, interaction_type=new_status, item_id=item_id, decision=new_status)
    return True


def claim_item(item_id: str, username: str) -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    now = int(time.time())
    get_inbox_repo().update_claim(item_id, username, now)
    _publish_event("inbox_item_claimed", item_id, {"claimed_by": username, "claimed_at": now})

    current = item["status"]
    if current in ("triaged", "new"):
        update_item_status(item_id, "claimed")

    _generate_view_for_item(item_id, item, username)

    return True


_COMPONENT_KINDS = [
    "topology",
    "metric_card",
    "stat_card",
    "chart",
    "data_table",
    "info_card_grid",
    "status_list",
    "log_viewer",
    "yaml_viewer",
    "timeline",
    "blast_radius",
    "resource_counts",
    "key_value",
    "badge_list",
    "progress_list",
]

_VIEW_LAYOUT_PROMPT = """You are designing a dashboard for an SRE investigating this issue:
Title: {title}
Summary: {summary}
Investigation: {investigation}
Suspected cause: {cause}
Recommended fix: {fix}
Namespace: {namespace}
Resources: {resources}

Available component kinds: {kinds}

Design 3-5 dashboard components. For topology, use props like {{"kinds": ["Pod","Service","NetworkPolicy"], "namespace": "X", "perspective": "network"}}.
For yaml_viewer, include the recommended YAML (e.g. a NetworkPolicy). For metric_card/stat_card, include a PromQL query.
Reply ONLY with valid JSON, no markdown:
{{"components": [{{"kind": "...", "title": "...", "props": {{...}}}}]}}"""


def _generate_view_for_item(item_id: str, item: dict[str, Any], owner: str = "system") -> None:
    """Generate an investigation view when a user claims an item."""
    metadata = item.get("metadata", {})
    if not metadata.get("investigation_summary") and not metadata.get("action_plan") and not metadata.get("view_plan"):
        return

    if item.get("view_id"):
        return

    repo = get_inbox_repo()
    try:
        metadata["view_status"] = "generating"
        now = int(time.time())
        repo.update_metadata(item_id, metadata, now)

        view_plan = metadata.get("view_plan", [])
        if view_plan:
            from .view_executor import execute_view_plan

            layout = execute_view_plan(view_plan, item)
            if not layout:
                layout = _fallback_layout(item, metadata)
        else:
            layout = _generate_smart_layout(item, metadata)

        from .db import save_view

        view_id = f"cv-{uuid.uuid4().hex[:12]}"
        title = f"Investigation: {item['title'][:60]}"
        view_type = "incident" if item.get("severity") in ("critical", "warning") else "plan"

        save_view(
            owner=owner,
            view_id=view_id,
            title=title,
            description=item.get("summary", ""),
            layout=layout,
            view_type=view_type,
            status="active",
            trigger_source="agent",
            finding_id=item.get("finding_id") or item_id,
            visibility="team",
        )

        metadata["view_status"] = "ready"
        now = int(time.time())
        repo.update_metadata_and_view(item_id, view_id, metadata, now)
        _publish_event("inbox_item_updated", item_id, {"view_id": view_id})
        _inbox_logger.info("Generated view %s for inbox item %s", view_id, item_id)
    except Exception:
        _inbox_logger.exception("View generation failed for %s", item_id)
        metadata["view_status"] = "failed"
        try:
            now = int(time.time())
            repo.update_metadata(item_id, metadata, now)
        except Exception:
            _inbox_logger.exception("Failed to update view_status for %s", item_id)


def _generate_smart_layout(item: dict[str, Any], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask Claude to design the investigation dashboard layout."""
    resources_str = ", ".join(f"{r['kind']}/{r['name']}" for r in item.get("resources", []))
    prompt = _VIEW_LAYOUT_PROMPT.format(
        title=item.get("title", ""),
        summary=item.get("summary", ""),
        investigation=metadata.get("investigation_summary", ""),
        cause=metadata.get("suspected_cause", ""),
        fix=metadata.get("recommended_fix", ""),
        namespace=item.get("namespace") or "cluster-wide",
        resources=resources_str or "none",
        kinds=", ".join(_COMPONENT_KINDS),
    )

    try:
        from .agent import borrow_client
        from .config import get_settings

        with borrow_client() as client:
            response = client.messages.create(
                model=get_settings().agent.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            match = _re.search(r"\{.*\}", text, _re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                    components = data.get("components", [])
                    if components:
                        valid = [c for c in components if c.get("kind") in _COMPONENT_KINDS]
                        if valid:
                            _inbox_logger.info("Agent designed %d-component view layout", len(valid))
                            return valid
                except json.JSONDecodeError:
                    _inbox_logger.warning("View layout JSON parse failed, using fallback")
    except Exception:
        _inbox_logger.exception("Smart layout generation failed, using fallback")

    return _fallback_layout(item, metadata)


def _fallback_layout(item: dict[str, Any], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Simple fallback layout when agent layout generation fails."""
    layout: list[dict[str, Any]] = []
    if metadata.get("investigation_summary"):
        layout.append(
            {
                "kind": "info_card_grid",
                "title": "Investigation",
                "props": {
                    "cards": [
                        {"label": "Summary", "value": str(metadata["investigation_summary"])},
                        {"label": "Suspected Cause", "value": str(metadata.get("suspected_cause", "Unknown"))},
                        {"label": "Recommended Fix", "value": str(metadata.get("recommended_fix", "N/A"))},
                    ],
                },
            }
        )
    if item.get("namespace"):
        layout.append(
            {
                "kind": "resource_counts",
                "title": f"Resources in {item['namespace']}",
                "props": {"namespace": item["namespace"]},
            }
        )
    if metadata.get("blast_radius"):
        layout.append(
            {
                "kind": "blast_radius",
                "title": "Blast Radius",
                "props": metadata["blast_radius"],
            }
        )
    return layout


def claim_and_investigate(item_id: str, username: str) -> bool:
    """Atomically claim an item and transition to in_progress."""
    item = get_inbox_item(item_id)
    if item is None:
        return False

    now = int(time.time())
    target_status = "in_progress" if item["status"] in ("triaged", "claimed") else "claimed"

    get_inbox_repo().update_claim_and_status(item_id, username, target_status, now)

    updated = get_inbox_item(item_id)
    if updated and updated["claimed_by"] == username:
        _publish_event("inbox_item_claimed", item_id, {"claimed_by": username, "claimed_at": now})
        _publish_event("inbox_item_updated", item_id, {"status": target_status})
        return True
    return False


def unclaim_item(item_id: str, actor: str = "") -> bool:
    now = int(time.time())
    get_inbox_repo().clear_claim(item_id, now)
    _publish_event("inbox_item_updated", item_id, {"claimed_by": None})
    if actor:
        record_interaction(actor=actor, interaction_type="unclaim", item_id=item_id)
    return True


def snooze_item(item_id: str, hours: float, actor: str = "") -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False

    now = int(time.time())
    snoozed_until = now + int(hours * 3600)

    metadata = item.get("metadata", {})
    metadata["pre_snooze_status"] = item["status"]

    get_inbox_repo().set_snooze(item_id, snoozed_until, metadata, now)
    if actor:
        record_interaction(actor=actor, interaction_type="snooze", item_id=item_id, metadata={"hours": hours})
    return True


def unsnooze_expired() -> int:
    now = int(time.time())
    repo = get_inbox_repo()
    rows = repo.fetch_expired_snoozed(now)
    count = 0
    for row in rows:
        raw = row["metadata"]
        metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
        pre_status = metadata.pop("pre_snooze_status", "new")
        repo.clear_snooze(row["id"], pre_status, metadata, now)
        count += 1
    if count:
        repo.commit()
    return count


def upsert_inbox_item(item: dict[str, Any]) -> str:
    repo = get_inbox_repo()
    corr_key = item.get("correlation_key")
    item_type = item["item_type"]

    existing = None
    if corr_key:
        row = repo.find_active_by_correlation(corr_key, item_type)
        if row:
            existing = _deserialize_row(row)
        else:
            recently_resolved = repo.find_recently_resolved(corr_key, item_type, int(time.time()) - 86400)
            if recently_resolved:
                return recently_resolved["id"]

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

    repo.update_resources_and_priority(existing["id"], merged_resources, priority, now)
    return existing["id"]


def escalate_assessment(item_id: str) -> str | None:
    item = get_inbox_item(item_id)
    if item is None or item["item_type"] != "task":
        return None

    repo = get_inbox_repo()
    now = int(time.time())
    repo.resolve_item(item_id, now)

    finding_item = {
        "item_type": "task",
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
    repo.update_metadata(item_id, metadata, now)

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

    now = int(time.time())
    get_inbox_repo().update_pinned_by(item_id, pinned, now)
    return True


def restore_item(item_id: str, actor: str = "") -> bool:
    """Restore an agent-cleared item back to new status (user override)."""
    ok = update_item_status(item_id, "new")
    if ok and actor:
        record_interaction(actor=actor, interaction_type="restore", item_id=item_id, decision="new")
    return ok


def dismiss_item(item_id: str, actor: str = "") -> bool:
    item = get_inbox_item(item_id)
    if item is None:
        return False
    now = int(time.time())
    get_inbox_repo().archive_item(item_id, now)
    _publish_event("inbox_item_updated", item_id, {"status": "archived"})
    if actor:
        record_interaction(actor=actor, interaction_type="dismiss", item_id=item_id, decision="archived")
    return True


_last_prune_time: float = 0
_PRUNE_INTERVAL = 86400


def prune_old_items(max_age_days: int = 30) -> int:
    global _last_prune_time
    now = time.time()
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return 0
    _last_prune_time = now

    cutoff = int(now) - max_age_days * 86400
    count = get_inbox_repo().delete_old_resolved(cutoff)

    global _last_investigate_time
    _last_investigate_time = {k: v for k, v in _last_investigate_time.items() if v > cutoff}

    return count


_MAX_RESOURCES = 10


def _merge_resources(existing: list[dict], new: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict] = []
    for r in new:
        key = (r.get("kind", ""), r.get("name", ""), r.get("namespace", ""))
        if key not in seen:
            seen.add(key)
            merged.append(r)
    for r in existing:
        key = (r.get("kind", ""), r.get("name", ""), r.get("namespace", ""))
        if key not in seen:
            seen.add(key)
            merged.append(r)
    return merged[:_MAX_RESOURCES]


def _deserialize_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    for json_field in ("resources", "pinned_by", "metadata"):
        if json_field in d and isinstance(d[json_field], str):
            d[json_field] = json.loads(d[json_field])
    return d


# -- Monitor integration --


def _finding_corr_key(finding: dict[str, Any]) -> str:
    """Build a correlation key scoped to category + namespace + primary resource."""
    category = finding.get("category", "unknown")
    namespace = finding.get("namespace", "")
    resources = finding.get("resources", [])
    if resources:
        r = resources[0]
        name = r.get("name", "")
        kind = r.get("kind", "")
        if kind == "Pod":
            from .monitor.confidence import _strip_pod_hash

            name = _strip_pod_hash(name)
        return f"{category}:{namespace}:{kind}/{name}"
    return f"{category}:{namespace}"


def bridge_finding_to_inbox(finding: dict[str, Any]) -> str:
    """Create or update an inbox item from a monitor finding."""
    finding_id = finding.get("id", "")
    repo = get_inbox_repo()

    existing = repo.find_active_by_finding_id(finding_id)

    if existing is None:
        corr_key = _finding_corr_key(finding)
        if corr_key:
            existing = repo.find_active_by_correlation_task(corr_key)

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
        repo.update_resources_and_priority(existing_item["id"], merged_resources, priority, now)
        return existing_item["id"]

    item = {
        "item_type": "task",
        "title": finding.get("title", "Unknown finding"),
        "summary": finding.get("summary", ""),
        "severity": finding.get("severity", "warning"),
        "confidence": finding.get("confidence", 0),
        "noise_score": finding.get("noiseScore", 0),
        "namespace": finding.get("namespace"),
        "resources": finding.get("resources", []),
        "correlation_key": _finding_corr_key(finding),
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
    repo = get_inbox_repo()
    rows = repo.fetch_new_for_triage()
    if not rows:
        return 0

    try:
        from .config import get_settings

        model = get_settings().agent.model
    except Exception:
        _inbox_logger.exception("Failed to get settings for triage")
        return 0

    from .agent import borrow_client

    triaged = 0
    with borrow_client() as client:
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
                else:
                    new_status = "agent_reviewing"

                now = int(time.time())
                repo.update_triage_result(
                    item["id"],
                    new_status,
                    metadata,
                    triage.get("assessment", item.get("summary", "")),
                    now,
                )
                _publish_event("inbox_item_updated", item["id"], {"status": new_status})
                triaged += 1
                _inbox_logger.info("Triaged %s → %s (%s)", item["id"], new_status, action)
            except Exception:
                _inbox_logger.exception("Triage failed for %s", item["id"])
                update_item_status(item["id"], "agent_review_failed")

    return triaged


def _phase_b_investigate() -> int:
    """Investigate items the triage flagged for review using the SRE agent."""
    repo = get_inbox_repo()
    rows = repo.fetch_agent_reviewing()
    if not rows:
        return 0

    investigated = 0
    now = time.time()

    for row in rows:
        item = _deserialize_row(row)

        if now - _last_investigate_time.get(item["id"], 0) < _INVESTIGATE_COOLDOWN:
            continue

        resources = item.get("resources", [])
        if resources:
            alive = [r for r in resources if _resource_exists(r)]
            if not alive:
                _inbox_logger.info("All resources gone (404) for %s — auto-resolving", item["id"])
                ts = int(time.time())
                metadata = item.get("metadata", {})
                metadata["dismiss_reason"] = "Resource no longer exists"
                repo.resolve_item(item["id"], ts, metadata=metadata)
                _publish_event("inbox_item_resolved", item["id"], {"resolved_at": ts})
                continue
            if len(alive) < len(resources):
                _inbox_logger.info("Pruned %d dead resources from %s", len(resources) - len(alive), item["id"])
                repo.update_resources(item["id"], alive, int(time.time()))
                item["resources"] = alive

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
            import asyncio as _asyncio

            from .monitor.actions import save_investigation
            from .monitor.investigations import _run_proactive_investigation

            result = _asyncio.run(_run_proactive_investigation(finding_dict))
            tools_offered: list[str] = []
            try:
                from .agent import TOOL_MAP as SRE_TOOL_MAP
                from .agent import WRITE_TOOLS as SRE_WRITE_TOOLS
                from .skill_loader import select_tools

                readonly_map = {n: t for n, t in SRE_TOOL_MAP.items() if n not in SRE_WRITE_TOOLS}
                inv_prompt = f"Investigate: {item['title']} in {item.get('namespace', 'cluster')}"
                _, _, tools_offered = select_tools(inv_prompt, list(readonly_map.values()), readonly_map)
            except Exception:
                logger.debug("Failed to select tools for inbox investigation", exc_info=True)

            if result.get("summary"):
                investigation_id = result.get("id", f"inv-{item['id']}")
                save_investigation(result, finding_dict)

                metadata = item.get("metadata", {})
                metadata["investigation_id"] = investigation_id
                metadata["investigation_summary"] = result.get("summary", "")
                metadata["suspected_cause"] = result.get("suspected_cause", "")
                metadata["recommended_fix"] = result.get("recommended_fix", "")
                metadata["investigation_confidence"] = result.get("confidence", 0)
                metadata["evidence"] = result.get("evidence", [])
                metadata["skill_used"] = "sre"
                metadata["tools_offered"] = tools_offered[:20]

                raw_view_plan = result.get("viewPlan", [])
                if isinstance(raw_view_plan, list) and raw_view_plan:
                    from .view_executor import validate_view_plan

                    metadata["view_plan"] = validate_view_plan(raw_view_plan)
                    if metadata["view_plan"]:
                        metadata["view_plan_at"] = int(time.time())

                try:
                    from .dependency_graph import get_dependency_graph

                    resources = item.get("resources", [])
                    graph = get_dependency_graph()
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
                    new_status = "triaged"

                ts = int(time.time())
                repo.update_investigation_result(item["id"], new_status, metadata, ts)
                _publish_event("inbox_item_updated", item["id"], {"status": new_status})
                investigated += 1
                _inbox_logger.info("Investigated %s → %s (confidence: %.2f)", item["id"], new_status, inv_confidence)
        except Exception:
            _inbox_logger.exception("Investigation failed for %s", item["id"])
            update_item_status(item["id"], "agent_review_failed")

    return investigated


def _phase_c_plan() -> int:
    """Generate step-by-step action plans for investigated items."""
    repo = get_inbox_repo()
    rows = repo.fetch_triaged_without_plan()
    if not rows:
        return 0

    try:
        from .config import get_settings

        model = get_settings().agent.model
    except Exception:
        _inbox_logger.exception("Failed to get settings for plan generation")
        return 0

    from .agent import borrow_client

    planned = 0
    with borrow_client() as client:
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
                f"- Cause: {cause}\n"
                f"- Fix: {fix}\n"
                f"- Resources: {resources_str or 'none'}\n"
                f"- Namespace: {item.get('namespace') or 'cluster-wide'}\n\n"
                f"Generate 2-4 action steps. Reply ONLY with valid JSON, no markdown:\n"
                f'{{"steps": [{{"title": "short title", "description": "what to do", '
                f'"tool": null, "risk": "low"}}]}}'
            )

            try:
                response = client.messages.create(
                    model=model, max_tokens=1000, messages=[{"role": "user", "content": prompt}]
                )
                text = response.content[0].text.strip()

                match = _re.search(r"\{.*\}", text, _re.DOTALL)
                if not match:
                    continue

                try:
                    plan_data = json.loads(match.group())
                except json.JSONDecodeError:
                    _inbox_logger.warning("Plan JSON parse failed for %s, skipping", item["id"])
                    continue
                steps = plan_data.get("steps", [])
                if not steps:
                    continue

                for step in steps:
                    step["status"] = "pending"

                metadata = item.get("metadata", {})
                metadata["action_plan"] = steps

                now = int(time.time())
                repo.update_plan_metadata(item["id"], metadata, now)
                _publish_event("inbox_item_updated", item["id"], {"has_plan": True})
                planned += 1
                _inbox_logger.info("Generated %d-step plan for %s", len(steps), item["id"])
            except Exception:
                _inbox_logger.exception("Plan generation failed for %s", item["id"])

    return planned


def resolve_finding_inbox_item(finding_id: str, finding: dict[str, Any] | None = None) -> bool:
    """Resolve an inbox item when its linked finding resolves.

    Matches by finding_id first, then falls back to correlation_key so
    items claimed during a previous scan cycle still get resolved.
    """
    repo = get_inbox_repo()
    row = repo.find_active_by_finding_id(finding_id)

    if row is None and finding:
        corr_key = _finding_corr_key(finding)
        if corr_key:
            row = repo.find_active_by_correlation_task(corr_key)

    if row is None:
        return False

    item = _deserialize_row(row)
    now = int(time.time())
    repo.resolve_item(item["id"], now)
    _publish_event("inbox_item_resolved", item["id"], {"resolved_at": now})
    return True


_PRUNABLE_KINDS = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}
_prune_counter = 0


def _prune_stale_resources() -> None:
    """Remove dead Pod/Deployment resources from open inbox items (every 5th call)."""
    global _prune_counter
    _prune_counter += 1
    if _prune_counter % 5 != 0:
        return

    repo = get_inbox_repo()
    rows = repo.fetch_open_items_with_resources()
    pruned_count = 0
    resolved_count = 0
    for row in rows:
        item_resources = json.loads(row["resources"] or "[]")
        prunable = [r for r in item_resources if r.get("kind") in _PRUNABLE_KINDS]
        if not prunable:
            continue
        alive = [r for r in item_resources if r.get("kind") not in _PRUNABLE_KINDS or _resource_exists(r)]
        if len(alive) < len(item_resources):
            if not alive and prunable:
                now = int(time.time())
                repo.resolve_item(row["id"], now)
                _publish_event("inbox_item_resolved", row["id"], {"resolved_at": now, "reason": "resources_gone"})
                resolved_count += 1
            else:
                repo.update_item_resources_raw(row["id"], json.dumps(alive))
            pruned_count += len(item_resources) - len(alive)
    if pruned_count or resolved_count:
        repo.commit()
        _inbox_logger.info("Pruned %d stale resources, auto-resolved %d items", pruned_count, resolved_count)


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

    repo = get_inbox_repo()
    rows = repo.fetch_generator_items()
    generator_rows = [r for r in rows if _deserialize_row(r).get("metadata", {}).get("generator")]
    now = int(time.time())
    for row in generator_rows:
        if row["correlation_key"] and row["correlation_key"] not in generated_keys:
            repo.auto_resolve_generator_item(row["id"], now)
    repo.commit()

    _prune_stale_resources()

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
