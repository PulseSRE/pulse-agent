"""Process cross-agent handoff requests from the database."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from ..config import get_settings
from ..repositories.monitor_repo import get_monitor_repo
from .confidence import _sanitize_for_prompt
from .investigations import _run_proactive_investigation, _run_security_followup

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def process_handoffs(monitor: ClusterMonitor) -> None:
    """Process pending handoff requests (security scans, SRE investigations)."""
    repo = get_monitor_repo()
    timeout_seconds = get_settings().monitor.investigation_timeout

    cutoff = int(time.time() * 1000) - 300_000
    try:
        rows = await repo.async_get_pending_handoffs(cutoff)
    except Exception:
        try:
            rows = repo.get_pending_handoffs(cutoff)
        except Exception as e:
            logger.error("Failed to query handoff requests: %s", e)
            return

    for row in rows:
        details = row.get("details", "{}")
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (json.JSONDecodeError, TypeError):
                details = {}
        target = details.get("target", "")
        namespace = row.get("namespace", "") or details.get("namespace", "")
        context = _sanitize_for_prompt(details.get("context", ""))

        if target == "security_agent" and namespace:
            finding = {
                "category": "handoff",
                "title": f"Security scan requested for {_sanitize_for_prompt(namespace)}",
                "summary": context,
                "severity": "warning",
                "resources": [{"kind": "Namespace", "name": namespace, "namespace": namespace}],
            }
            try:
                await asyncio.wait_for(
                    _run_security_followup(finding, client=monitor._client),
                    timeout=timeout_seconds,
                )
                logger.info("Handoff security scan completed for %s", namespace)
            except Exception as e:
                logger.error("Handoff security scan failed: %s", e)

        elif target == "sre_agent" and namespace:
            finding = {
                "id": f"f-handoff-{uuid.uuid4().hex[:8]}",
                "category": "handoff",
                "title": f"SRE investigation requested: {_sanitize_for_prompt(details.get('kind', ''))}/{_sanitize_for_prompt(details.get('name', ''))}",
                "summary": context,
                "severity": "warning",
                "resources": [
                    {"kind": details.get("kind", ""), "name": details.get("name", ""), "namespace": namespace}
                ],
            }
            try:
                await asyncio.wait_for(
                    _run_proactive_investigation(finding, client=monitor._client),
                    timeout=timeout_seconds,
                )
                logger.info("Handoff SRE investigation completed for %s", namespace)
            except Exception as e:
                logger.error("Handoff SRE investigation failed: %s", e)

    if rows:
        try:
            await repo.async_delete_processed_handoffs(cutoff)
        except Exception:
            try:
                repo.delete_processed_handoffs(cutoff)
            except Exception as e:
                logger.error("Failed to clean up handoff requests: %s", e)
