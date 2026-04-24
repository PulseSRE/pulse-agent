"""Pre-deploy change risk scoring.

Scores deployment rollouts by analyzing:
- Image change magnitude
- Resource request/limit changes
- Historical failure rate
- Time of day risk
- Blast radius from dependency graph
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger("pulse_agent.change_risk")


@dataclass
class RiskAssessment:
    """Risk assessment for a deployment change."""

    deployment_name: str
    namespace: str
    score: int  # 0-100
    level: str  # LOW, MEDIUM, HIGH, CRITICAL
    factors: list[str]  # Human-readable risk factors
    recommendation: str  # Action recommendation


def score_deployment_change(
    *,
    deployment_name: str,
    namespace: str,
    old_image: str = "",
    new_image: str = "",
    resource_changes: bool = False,
    config_changes: bool = False,
) -> RiskAssessment:
    """Score the risk of a deployment change."""
    score = 0
    factors: list[str] = []

    # Image change magnitude
    if old_image and new_image and old_image != new_image:
        old_tag = old_image.split(":")[-1] if ":" in old_image else ""
        new_tag = new_image.split(":")[-1] if ":" in new_image else ""

        if old_image.split(":")[0] != new_image.split(":")[0]:
            score += 30
            factors.append("Entirely new image (not just tag change)")
        elif old_tag and new_tag and old_tag != new_tag:
            score += 10
            factors.append(f"Image tag changed: {old_tag} → {new_tag}")

    # Resource changes
    if resource_changes:
        score += 15
        factors.append("Resource requests/limits modified")

    # Config changes
    if config_changes:
        score += 10
        factors.append("ConfigMap or Secret references changed")

    # Time of day risk
    hour = datetime.now(UTC).hour
    if hour < 6 or hour > 22:
        score += 15
        factors.append(f"Off-hours deployment (UTC {hour:02d}:00)")
    elif hour < 9 or hour > 17:
        score += 5
        factors.append(f"Outside core hours (UTC {hour:02d}:00)")

    # Historical failure rate
    try:
        from .db import get_database

        db = get_database()
        row = db.fetchone(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN verification_status = 'still_failing' THEN 1 ELSE 0 END) as failures "
            "FROM actions WHERE category = 'workloads' "
            "AND reasoning LIKE %s "
            "AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days')::BIGINT * 1000",
            (f"%{deployment_name}%",),
        )
        if row and row["total"] > 0:
            failure_rate = row["failures"] / row["total"]
            if failure_rate > 0.3:
                score += 20
                factors.append(f"High historical failure rate: {failure_rate:.0%}")
            elif failure_rate > 0.1:
                score += 10
                factors.append(f"Moderate historical failure rate: {failure_rate:.0%}")
    except Exception:
        logger.debug("Failed to query historical failure rate", exc_info=True)

    # Blast radius from dependency graph
    try:
        from .dependency_graph import get_dependency_graph

        graph = get_dependency_graph()
        blast = graph.downstream_blast_radius("Deployment", namespace, deployment_name)
        if len(blast) > 10:
            score += 15
            factors.append(f"Large blast radius: {len(blast)} downstream resources")
        elif len(blast) > 5:
            score += 5
            factors.append(f"Moderate blast radius: {len(blast)} downstream resources")
    except Exception:
        logger.debug("Failed to compute blast radius", exc_info=True)

    # Clamp score
    score = min(score, 100)

    # Determine level
    if score >= 70:
        level = "CRITICAL"
        recommendation = "Require approval. Consider canary deployment or off-peak timing."
    elif score >= 50:
        level = "HIGH"
        recommendation = "Review before proceeding. Monitor golden signals closely after deploy."
    elif score >= 25:
        level = "MEDIUM"
        recommendation = "Standard deployment. Watch for issues in first 10 minutes."
    else:
        level = "LOW"
        recommendation = "Low risk. Proceed normally."

    return RiskAssessment(
        deployment_name=deployment_name,
        namespace=namespace,
        score=score,
        level=level,
        factors=factors,
        recommendation=recommendation,
    )
