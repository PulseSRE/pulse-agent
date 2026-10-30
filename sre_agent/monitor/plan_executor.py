"""Plan-based investigation of a finding.

The plan strategy of the investigation pipeline (``investigation_pipeline``):
pick the template for the finding's category and investigate with it. Running,
reporting, saving and learning are the pipeline's, shared with the freeform
strategy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .investigation_pipeline import diagnosing_output, investigate

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")

# Kept under its old name for callers outside the monitor.
_diagnosing_output = diagnosing_output


def template_for(finding: dict):
    """The plan template for a finding, or None when it has no plan."""
    from ..plan_templates import match_template
    from .alert_plans import plan_category_for

    # plan_category_for, not the raw category: alert findings arrive as
    # category="alerts", which no template matches — so alert-borne incidents
    # (this cluster's dominant kind) never engaged a plan.
    return match_template(category=plan_category_for(finding))


async def try_plan_execution(monitor: ClusterMonitor, finding: dict) -> bool:
    """Investigate ``finding`` with its plan. False when it has none or the investigation failed."""
    try:
        template = template_for(finding)
    except Exception:
        logger.debug("Plan template lookup failed", exc_info=True)
        return False
    if not template:
        return False
    outcome = await investigate(monitor, finding, template)
    logger.info(
        "Plan investigation complete: %s status=%s confidence=%.2f",
        template.name,
        outcome.status,
        outcome.confidence,
    )
    return outcome.completed
