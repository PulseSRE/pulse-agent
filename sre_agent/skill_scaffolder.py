"""Auto-scaffold new skills from novel incident resolutions.

When a novel incident is resolved (no plan template matched, dynamic plan was used):
1. Extract reasoning trace from resolution
2. Auto-draft skill.md with triggers, tool sequence, framework
3. Store as generated_by="auto", reviewed=false
4. Surface in Toolbox UI for SRE review
"""

from __future__ import annotations

import logging

logger = logging.getLogger("pulse_agent.skill_scaffolder")


def scaffold_skill_from_resolution(
    *,
    query: str,
    tools_called: list[str],
    investigation_summary: str,
    root_cause: str,
    confidence: float,
    plan_phases: list[str] | None = None,
) -> str:
    """Auto-draft a skill.md from a novel incident resolution.

    Returns the skill.md content as a string.
    """
    from .tool_predictor import extract_tokens

    tokens = extract_tokens(query)
    keywords = tokens[:10] if tokens else ["unknown"]

    # Determine categories from tools used
    categories: set[str] = set()
    tool_category_hints = {
        "list_pods": "diagnostics",
        "describe_pod": "diagnostics",
        "get_pod_logs": "diagnostics",
        "get_events": "diagnostics",
        "scale_deployment": "workloads",
        "restart_deployment": "workloads",
        "get_prometheus_query": "monitoring",
        "get_firing_alerts": "monitoring",
        "scan_rbac_risks": "security",
        "scan_pod_security": "security",
        "drain_node": "operations",
        "cordon_node": "operations",
    }
    for tool in tools_called:
        cat = tool_category_hints.get(tool)
        if cat:
            categories.add(cat)
    if not categories:
        categories = {"diagnostics"}

    skill_name = "-".join(keywords[:3]).replace(" ", "-")

    content = f"""---
name: {skill_name}
version: 1
description: Auto-generated skill for: {query[:100]}
keywords:
{chr(10).join(f"  - {kw}" for kw in keywords)}
categories:
{chr(10).join(f"  - {cat}" for cat in sorted(categories))}
write_tools: false
priority: 5
alert_triggers: []
cluster_components: []
examples: []
success_criteria: ""
risk_level: low
conflicts_with: []
supported_components: []
generated_by: auto
reviewed: false
---

## {skill_name.replace("-", " ").title()}

This skill was auto-generated from a resolved incident.

### Root Cause Pattern
{root_cause}

### Investigation Framework
{investigation_summary[:500]}

### Tool Sequence
{chr(10).join(f"1. `{tool}`" for tool in tools_called[:10])}

### Confidence
This diagnosis was made with {confidence:.0%} confidence.
"""

    return content


def scaffold_plan_template(
    *,
    skill_name: str,
    plan_phases: list[str],
    incident_type: str,
    confidence: float,
) -> str | None:
    """Save an auto-scaffolded plan template YAML and hot-reload the registry.

    Returns the path if saved, None on failure.
    """
    import re

    # Validate skill_name
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", skill_name):
        logger.warning("Invalid skill_name for plan template: %s", skill_name[:50])
        return None

    try:
        from pathlib import Path

        import yaml

        from .plan_templates import get_template

        # Don't overwrite existing templates for this incident type
        if get_template(incident_type):
            logger.info("Plan template already exists for '%s', skipping scaffold", incident_type)
            return None

        templates_dir = Path(__file__).parent / "plan_templates"
        if not templates_dir.exists():
            logger.warning("Plan templates directory not found")
            return None

        # Verify resolved path stays within templates directory
        template_path = (templates_dir / f"{skill_name}.yaml").resolve()
        if not str(template_path).startswith(str(templates_dir.resolve())):
            logger.warning("Path traversal blocked for plan template: %s", skill_name)
            return None

        # Build phases from the provided phase IDs
        phases = []
        prev_id = None
        for phase_id in plan_phases:
            phase = {
                "id": phase_id,
                "skill_name": "sre",
                "required": phase_id != "postmortem",
                "timeout_seconds": 120,
                "runs": "always" if phase_id == "postmortem" else "on_success",
            }
            if prev_id:
                phase["depends_on"] = [prev_id]
            phases.append(phase)
            prev_id = phase_id

        template = {
            "id": f"auto-{skill_name}",
            "name": skill_name.replace("-", " ").title(),
            "incident_type": incident_type,
            "max_total_duration": 1800,
            "generated_by": "auto",
            "confidence": round(confidence, 2),
            "phases": phases,
        }

        with open(template_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(template, f, default_flow_style=False, sort_keys=False)

        # Hot-reload template registry
        from .plan_templates import load_templates

        load_templates()

        logger.info("Scaffolded plan template: %s (%d phases)", template_path, len(phases))
        return str(template_path)

    except Exception:
        logger.debug("Failed to save scaffolded plan template", exc_info=True)
        return None


def validate_scaffolded_skill(skill_content: str) -> list[str]:
    """Validate an auto-scaffolded skill before saving.

    Returns list of validation errors (empty = valid).
    """
    errors: list[str] = []

    # Check frontmatter exists
    if not skill_content.startswith("---"):
        errors.append("Missing frontmatter (must start with ---)")
        return errors

    parts = skill_content.split("---", 2)
    if len(parts) < 3:
        errors.append("Malformed frontmatter")
        return errors

    try:
        import yaml

        meta = yaml.safe_load(parts[1])
    except Exception as e:
        errors.append(f"Invalid YAML frontmatter: {e}")
        return errors

    if not meta:
        errors.append("Empty frontmatter")
        return errors

    # Required fields
    if not meta.get("name"):
        errors.append("Missing 'name' field")
    if not meta.get("description"):
        errors.append("Missing 'description' field")
    if not meta.get("keywords"):
        errors.append("Missing 'keywords' field")
    elif not isinstance(meta["keywords"], list) or len(meta["keywords"]) < 1:
        errors.append("'keywords' must be a non-empty list")
    if not meta.get("categories"):
        errors.append("Missing 'categories' field")

    # Body must have content
    body = parts[2].strip()
    if len(body) < 50:
        errors.append("Skill body too short (need at least 50 characters of instructions)")

    return errors


def save_scaffolded_skill(skill_content: str, skill_name: str) -> str | None:
    """Save an auto-scaffolded skill to the skills directory.

    Returns the path if saved, None on failure.
    """
    import re

    # Validate skill_name — prevent path traversal
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", skill_name):
        logger.warning("Invalid skill_name rejected: %s", skill_name[:50])
        return None

    # Validate skill content before saving
    validation_errors = validate_scaffolded_skill(skill_content)
    if validation_errors:
        logger.warning("Scaffolded skill '%s' failed validation: %s", skill_name, validation_errors)
        return None

    try:
        from pathlib import Path

        skills_dir = Path(__file__).parent / "skills" / skill_name

        # Verify resolved path stays within skills directory
        base = (Path(__file__).parent / "skills").resolve()
        if not str(skills_dir.resolve()).startswith(str(base)):
            logger.warning("Path traversal blocked for skill: %s", skill_name)
            return None

        skills_dir.mkdir(parents=True, exist_ok=True)

        skill_path = skills_dir / "skill.md"
        skill_path.write_text(skill_content, encoding="utf-8")

        logger.info("Scaffolded new skill: %s", skill_path)
        return str(skill_path)

    except Exception:
        logger.debug("Failed to save scaffolded skill", exc_info=True)
        return None
