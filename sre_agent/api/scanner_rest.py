"""Scanner-related REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from ..config import get_settings
from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["scanner"])


# ── Scanner Categories ─────────────────────────────────────────────────────

_SCANNER_CATEGORIES = {
    "pod_health": ["crashloop", "pending", "oom", "image_pull"],
    "node_pressure": ["nodes"],
    "workload_health": ["workloads", "daemonsets", "hpa"],
    "security_audit": ["audit_rbac", "audit_auth", "audit_config"],
    "certificate_expiry": ["cert_expiry"],
    "alerts": ["alerts"],
    "deployment_audit": ["audit_deployment", "audit_events"],
    "operator_health": ["operators"],
}


def get_scanner_coverage(days: int = 7) -> dict:
    """Get scanner coverage statistics.

    Args:
        days: Number of days to look back for finding stats (1-90).

    Returns:
        Dictionary with coverage metrics:
        - active_scanners: count of enabled scanners
        - total_scanners: total available scanners
        - coverage_pct: percentage of categories covered (0.0-1.0)
        - categories: list of {name, covered, scanners}
        - per_scanner: list of {name, enabled, finding_count, actionable_count, noise_pct}
    """
    from ..monitor import _get_all_scanners

    # Get all scanners
    all_scanners = _get_all_scanners()
    scanner_ids = {scanner_id for scanner_id, _ in all_scanners}
    total_scanners = len(all_scanners)

    # All scanners are currently always enabled (no toggle mechanism yet)
    active_scanners = total_scanners

    # Compute category coverage
    categories = []
    covered_count = 0
    total_categories = len(_SCANNER_CATEGORIES)

    for category_name, scanner_list in _SCANNER_CATEGORIES.items():
        # A category is covered if at least one of its scanners is enabled
        covered = any(s in scanner_ids for s in scanner_list)
        if covered:
            covered_count += 1

        # Get the list of enabled scanners for this category
        enabled_scanners = [s for s in scanner_list if s in scanner_ids]

        categories.append(
            {
                "name": category_name,
                "covered": covered,
                "scanners": enabled_scanners,
            }
        )

    coverage_pct = round(covered_count / total_categories * 100, 1) if total_categories > 0 else 0.0

    # Try to get per-scanner finding stats from the database
    per_scanner = []
    try:
        from .. import db

        database = db.get_database()

        # Single batch query instead of N+1 per-scanner queries
        stats_rows = database.fetchall(
            "SELECT category, "
            "  COUNT(*) AS total_count, "
            "  COUNT(*) FILTER (WHERE severity IN ('critical', 'warning')) AS actionable_count "
            "FROM findings "
            "WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day' * ?)::BIGINT * 1000 "
            "GROUP BY category",
            (days,),
        )
        stats_by_cat = {r["category"]: r for r in stats_rows} if stats_rows else {}

        for scanner_id, _ in all_scanners:
            row = stats_by_cat.get(scanner_id, {})
            finding_count = row.get("total_count", 0)
            actionable_count = row.get("actionable_count", 0)
            noise_pct = round((finding_count - actionable_count) / finding_count, 2) if finding_count > 0 else 0.0

            per_scanner.append(
                {
                    "name": scanner_id,
                    "enabled": True,
                    "finding_count": finding_count,
                    "actionable_count": actionable_count,
                    "noise_pct": noise_pct,
                }
            )
    except Exception as e:
        logger.debug("Failed to get per-scanner stats: %s", e)
        for scanner_id, _ in all_scanners:
            per_scanner.append(
                {
                    "name": scanner_id,
                    "enabled": True,
                    "finding_count": 0,
                    "actionable_count": 0,
                    "noise_pct": 0.0,
                }
            )

    return {
        "active_scanners": active_scanners,
        "total_scanners": total_scanners,
        "coverage_pct": coverage_pct,
        "categories": categories,
        "per_scanner": per_scanner,
    }


@router.get("/monitor/scanners")
async def rest_list_scanners(_auth=Depends(verify_token)):
    """List all scanners with metadata and current configuration."""
    from ..monitor import SCANNER_REGISTRY

    return {
        "scanners": [
            {
                "name": k,
                "display_name": v.get("displayName", k),
                "description": v.get("description", ""),
                "category": v.get("category", ""),
                "checks": v.get("checks", []),
                "auto_fixable": v.get("auto_fixable", False),
                "enabled": True,
            }
            for k, v in SCANNER_REGISTRY.items()
        ]
    }


@router.get("/monitor/capabilities")
async def monitor_capabilities(_auth=Depends(verify_token)):
    """Expose monitor trust/capability limits so UI can align controls."""
    from ..monitor import AUTO_FIX_HANDLERS

    max_trust_level = get_settings().max_trust_level
    return {
        "max_trust_level": max(0, min(max_trust_level, 4)),
        "supported_auto_fix_categories": sorted(AUTO_FIX_HANDLERS.keys()),
    }


@router.post("/monitor/pause")
async def pause_autofix(_auth=Depends(verify_token)):
    """Emergency kill switch -- pause all auto-fix actions."""
    from ..monitor import set_autofix_paused

    set_autofix_paused(True)
    logger.warning("Auto-fix PAUSED via /monitor/pause")
    return {"autofix_paused": True}


@router.post("/monitor/resume")
async def resume_autofix(_auth=Depends(verify_token)):
    """Resume auto-fix actions after a pause."""
    from ..monitor import set_autofix_paused

    set_autofix_paused(False)
    logger.info("Auto-fix RESUMED via /monitor/resume")
    return {"autofix_paused": False}


@router.get("/monitor/coverage")
async def scanner_coverage(
    days: int = Query(7, ge=1, le=90),
    _auth=Depends(verify_token),
):
    """Scanner coverage statistics showing which failure modes are monitored. Requires token auth."""
    return get_scanner_coverage(days)
