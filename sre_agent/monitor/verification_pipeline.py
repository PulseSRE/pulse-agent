"""Post-fix verification pipeline — checks whether applied fixes stayed healthy."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ..async_db import ASYNC_DB_ERRORS as _ASYNC_DB_ERRORS
from ..config import get_settings
from ..repositories.monitor_repo import get_monitor_repo
from . import health_gate
from .actions import update_action_verification as _sync_update_action_verification
from .findings import _ts

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


def _scaffold_from_verified(candidate) -> None:
    """Turn a verified trajectory into skill knowledge.

    The trajectory reaching here has had its fix applied and the finding
    confirmed gone, so what gets generalised is something that worked. The
    first verified case for a category creates a skill; every later one
    refines it in place (``skill_lifecycle.learn_from_verified``) instead of
    scaffolding a duplicate sibling.
    """
    try:
        from ..skill_lifecycle import learn_from_verified

        learn_from_verified(candidate)
    except Exception:
        logger.debug("Scaffolding from verified trajectory failed", exc_info=True)


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

        probe = payload.get("probe")
        if probe:
            # A tool-contract postcondition: the check is tool-specific (a
            # scale-to-0 verifies as 0 ready replicas, a rollback verifies the
            # revision moved), so the generic finding-correlation above does
            # not apply. Rollouts are not instant — a FAIL inside the grace
            # window stays pending for another scan instead of becoming the
            # recorded verdict; PASS and UNVERIFIABLE resolve immediately.
            from .. import tool_contracts

            gate_status, gate_evidence = await asyncio.to_thread(tool_contracts.run_probe, probe)
            grace_left = int(payload.get("grace_scans", 0))
            if gate_status == health_gate.FAIL and grace_left > 0:
                payload["grace_scans"] = grace_left - 1
                payload["target_scan"] = monitor._scan_counter + 1
                continue
            if gate_status == health_gate.PASS:
                status = "verified"
                evidence = f"Postcondition probe passed: {gate_evidence}"
            elif gate_status == health_gate.FAIL:
                status = "still_failing"
                evidence = f"Postcondition probe failed: {gate_evidence}"
            else:
                status = "unverifiable"
                evidence = f"Postcondition could not be confirmed: {gate_evidence}"
        elif matches_active:
            status = "still_failing"
            evidence = f"Resource still appears in active {category} findings: {matched_resource}"
        elif ns_improved:
            status = "improved"
            evidence = f"Namespace-level improvement in {category} findings: {matched_resource}"
        else:
            # The finding is gone. That is necessary but not sufficient: it also
            # goes away when the scanner failed or the workload was deleted, so
            # confirm the resource is affirmatively healthy before calling this
            # a verified fix. The gate's own reading becomes the evidence.
            # Prefer the owning workload when the fix deleted a pod: the pod's
            # own name no longer resolves, but the thing it belonged to does.
            gate_targets = payload.get("verify_resources") or resources
            gate_status, gate_evidence = await asyncio.to_thread(health_gate.check_resources, gate_targets)
            if gate_status == health_gate.PASS:
                status = "verified"
                evidence = f"No active {category} findings, and health check passed: {gate_evidence}"
            elif gate_status == health_gate.FAIL:
                status = "still_failing"
                evidence = f"No active {category} findings, but health check failed: {gate_evidence}"
            else:
                status = "unverifiable"
                evidence = f"No active {category} findings, but health could not be confirmed: {gate_evidence}"

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
                    elif status == "unverifiable":
                        # We could not read the cluster. That is a fact about the
                        # check, not about the diagnosis, so it moves nothing.
                        new_conf = None
                    else:
                        new_conf = max(0.0, (inv["confidence"] or 0.5) - 0.1)
                    if new_conf is not None:
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

        # Contract probes for interactive writes have no finding, no auto-fix
        # runbook to learn, and no held trajectory — record the verdict above
        # and stop. Learning from a chat-initiated write would file it under
        # "Auto-fix", which it was not.
        if not payload.get("finding_id"):
            completed_ids.append(action_id)
            continue

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

        # The trajectory gate. A held investigation becomes a skill only now, once
        # the finding it diagnosed is confirmed resolved. A fix that did not hold
        # drops its candidate unlearned rather than teaching the wrong lesson.
        try:
            from ..trajectory import candidate_key, get_learner

            key = candidate_key(category, resources)
            if status == "verified":
                promoted = get_learner().promote(key)
                if promoted is not None:
                    _scaffold_from_verified(promoted)
            elif status == "unverifiable":
                # Neither outcome was demonstrated. Discarding here would record
                # a judgement the check never made; the candidate is left pending
                # and expires on its own TTL if no verdict ever arrives.
                logger.info("Learning candidate %s left pending: %s", key, evidence)
            else:
                get_learner().discard(key, f"verification {status}")
        except Exception:
            logger.debug("Trajectory learning gate failed", exc_info=True)

        completed_ids.append(action_id)

    for action_id in completed_ids:
        monitor._pending_verifications.pop(action_id, None)
