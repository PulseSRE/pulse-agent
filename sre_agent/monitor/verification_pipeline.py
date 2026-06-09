"""Post-fix verification pipeline — checks whether applied fixes stayed healthy."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from ..config import get_settings
from ..repositories.monitor_repo import get_monitor_repo

try:
    import asyncpg

    _ASYNC_DB_ERRORS: tuple[type[Exception], ...] = (asyncpg.PostgresError, OSError, ConnectionError)
except ImportError:
    _ASYNC_DB_ERRORS = (OSError, ConnectionError)
from .actions import update_action_verification as _sync_update_action_verification
from .findings import _ts

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def process_verifications(monitor: ClusterMonitor, findings: list[dict]) -> None:
    """Verify whether previously applied fixes remained healthy on next scan."""
    if not monitor._pending_verifications:
        return

    active_by_category: dict[str, set[str]] = {}
    active_ns_category: dict[str, set[str]] = {}
    for finding in findings:
        category = str(finding.get("category", ""))
        active_by_category.setdefault(category, set())
        for resource in finding.get("resources", []):
            rkey = f"{resource.get('kind', '')}:{resource.get('namespace', '')}:{resource.get('name', '')}"
            active_by_category[category].add(rkey)
            ns_key = f"{resource.get('namespace', '')}:{category}"
            active_ns_category.setdefault(ns_key, set()).add(rkey)

    completed_ids: list[str] = []
    for action_id, payload in monitor._pending_verifications.items():
        if monitor._scan_counter < int(payload.get("target_scan", 0)):
            continue

        category = str(payload.get("category", ""))
        resources = payload.get("resources", [])
        matches_active = False
        matched_resource = ""
        ns_improved = False
        for resource in resources:
            key = f"{resource.get('kind', '')}:{resource.get('namespace', '')}:{resource.get('name', '')}"
            if key in active_by_category.get(category, set()):
                matches_active = True
                matched_resource = key
                break
            ns_key = f"{resource.get('namespace', '')}:{category}"
            ns_active = active_ns_category.get(ns_key, set())
            if ns_active:
                orig_ns_count = sum(1 for r in resources if r.get("namespace", "") == resource.get("namespace", ""))
                if len(ns_active) < orig_ns_count:
                    ns_improved = True
                    matched_resource = f"{ns_key} (namespace count {orig_ns_count} -> {len(ns_active)})"
                else:
                    matches_active = True
                    matched_resource = f"{ns_key} (namespace-level match)"
                break

        if matches_active:
            status = "still_failing"
            evidence = f"Resource still appears in active {category} findings: {matched_resource}"
        elif ns_improved:
            status = "improved"
            evidence = f"Namespace-level improvement in {category} findings: {matched_resource}"
        else:
            status = "verified"
            evidence = f"No active {category} findings for affected resources on verification scan"

        verification_report = {
            "type": "verification_report",
            "id": f"v-{uuid.uuid4().hex[:12]}",
            "actionId": action_id,
            "findingId": payload.get("finding_id", ""),
            "status": status,
            "evidence": evidence,
            "timestamp": _ts(),
        }
        await monitor._broadcast_raw(verification_report)
        try:
            repo = get_monitor_repo()
            await repo.async_update_action_verification(action_id, status, evidence, _ts())
        except _ASYNC_DB_ERRORS:
            _sync_update_action_verification(action_id, status, evidence)

        try:
            repo = get_monitor_repo()
            finding_id = payload.get("finding_id", "")
            if finding_id:
                inv = await repo.async_get_investigation_by_finding_id(finding_id)
                if inv:
                    if status == "verified":
                        new_conf = min(1.0, (inv["confidence"] or 0.5) + 0.05)
                    else:
                        new_conf = max(0.0, (inv["confidence"] or 0.5) - 0.1)
                    await repo.async_update_investigation_confidence(inv["id"], new_conf)
        except Exception as e:
            logger.debug("Failed to update investigation confidence: %s", e)

        from ..context_bus import ContextEntry, get_context_bus

        bus = get_context_bus()
        bus.publish(
            ContextEntry(
                source="monitor",
                category="verification",
                summary=f"Verification {status}: {evidence}",
                details={"status": status, "evidence": evidence},
            )
        )

        if status == "verified" and get_settings().agent.memory:
            try:
                from ..memory import get_manager

                manager = get_manager()
                if manager:
                    namespace = ""
                    resource_type = ""
                    if resources:
                        r0 = resources[0]
                        namespace = r0.get("namespace", "")
                        resource_type = r0.get("kind", "").lower()
                    incident = {
                        "query": f"Auto-fix for {category} finding",
                        "tool_sequence": [payload.get("tool", "unknown") if payload.get("tool") else category],
                        "resolution": f"Applied {payload.get('tool', category)} — verified healthy on next scan",
                        "namespace": namespace,
                        "resource_type": resource_type,
                        "error_type": category,
                    }
                    manager.store_incident(incident, confirmed=True)
                    logger.info("Auto-learned runbook from verified fix: %s", category)
            except Exception as e:
                logger.warning("Failed to auto-learn from fix: %s", e)

        completed_ids.append(action_id)

    for action_id in completed_ids:
        monitor._pending_verifications.pop(action_id, None)
