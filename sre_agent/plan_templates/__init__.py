"""Plan template loading and incident-to-template matching."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..skill_plan import SkillPhase, SkillPlan, validate_plan

logger = logging.getLogger("pulse_agent.plan_templates")

_TEMPLATES_DIR = Path(__file__).parent
_templates: dict[str, SkillPlan] = {}


def load_templates() -> dict[str, SkillPlan]:
    """Load all plan templates from YAML files."""
    global _templates
    _templates.clear()

    if not _TEMPLATES_DIR.exists():
        logger.warning("Plan templates directory not found: %s", _TEMPLATES_DIR)
        return _templates

    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            phases = []
            for p in data.get("phases", []):
                phases.append(
                    SkillPhase(
                        id=p["id"],
                        skill_name=p.get("skill_name", "sre"),
                        required=p.get("required", True),
                        depends_on=p.get("depends_on", []),
                        timeout_seconds=p.get("timeout_seconds", 120),
                        produces=p.get("produces", []),
                        branch_on=p.get("branch_on"),
                        branches=p.get("branches", {}),
                        parallel_with=p.get("parallel_with"),
                        approval_required=p.get("approval_required", False),
                        runs=p.get("runs", "on_success"),
                    )
                )

            plan = SkillPlan(
                id=data["id"],
                name=data["name"],
                phases=phases,
                incident_type=data.get("incident_type", ""),
                max_total_duration=data.get("max_total_duration", 1800),
            )

            errors = validate_plan(plan)
            if errors:
                logger.warning("Plan template '%s' has validation errors: %s", path.name, errors)
                continue

            # Auto-generated templates must not override built-in templates
            if plan.incident_type in _templates and data.get("generated_by") == "auto":
                logger.debug(
                    "Skipping auto-generated template '%s' — built-in '%s' already covers incident_type '%s'",
                    path.name,
                    _templates[plan.incident_type].id,
                    plan.incident_type,
                )
                continue

            _templates[plan.incident_type] = plan
            logger.info("Loaded plan template: %s (%s, %d phases)", plan.name, plan.incident_type, len(plan.phases))

        except Exception:
            logger.warning("Failed to load plan template: %s", path.name, exc_info=True)

    return _templates


def get_template(incident_type: str) -> SkillPlan | None:
    """Get a plan template by incident type."""
    if not _templates:
        load_templates()
    return _templates.get(incident_type)


def match_template(*, category: str = "", keywords: list[str] | None = None) -> SkillPlan | None:
    """Find the best matching template for an incident.

    Matches by:
    1. Exact category match (e.g., "crashloop" -> crashloop-resolution)
    2. Keyword overlap with incident_type
    """
    if not _templates:
        load_templates()

    # Exact match on category
    if category in _templates:
        return _templates[category]

    # Fuzzy match: check if category is a substring of any incident_type
    for incident_type, plan in _templates.items():
        if category and category in incident_type:
            return plan

    # Keyword match
    if keywords:
        best_match = None
        best_score = 0
        for incident_type, plan in _templates.items():
            score = sum(1 for kw in keywords if kw in incident_type)
            if score > best_score:
                best_score = score
                best_match = plan
        if best_match and best_score > 0:
            return best_match

    return None


def list_templates() -> list[dict]:
    """List all available templates."""
    if not _templates:
        load_templates()
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "incident_type": plan.incident_type,
            "phases": len(plan.phases),
            "max_duration": plan.max_total_duration,
        }
        for plan in _templates.values()
    ]


__all__ = ["get_template", "list_templates", "load_templates", "match_template"]
