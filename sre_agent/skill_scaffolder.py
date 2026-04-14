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


def save_scaffolded_skill(skill_content: str, skill_name: str) -> str | None:
    """Save an auto-scaffolded skill to the skills directory.

    Returns the path if saved, None on failure.
    """
    import re

    # Validate skill_name — prevent path traversal
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", skill_name):
        logger.warning("Invalid skill_name rejected: %s", skill_name[:50])
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
