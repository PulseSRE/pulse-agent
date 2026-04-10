"""Skill management and analytics REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import verify_token

router = APIRouter()


@router.get("/skills")
async def list_skills(_auth=Depends(verify_token)):
    """List all loaded skills with metadata."""
    from ..skill_loader import list_skills as _list

    return [s.to_dict() for s in _list()]


# Usage endpoints BEFORE /skills/{name} to avoid route conflict
@router.get("/skills/usage")
async def skill_usage_stats(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Aggregated skill usage statistics."""
    from ..skill_analytics import get_skill_stats

    return get_skill_stats(days=days)


@router.get("/skills/usage/handoffs")
async def skill_handoff_flow(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Handoff flow between skills."""
    from ..skill_analytics import get_skill_stats

    stats = get_skill_stats(days=days)
    return {"handoffs": stats.get("handoffs", []), "days": days}


@router.get("/skills/usage/{name}")
async def skill_usage_detail(
    name: str,
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Detailed stats for a specific skill."""
    from ..skill_analytics import get_skill_stats

    stats = get_skill_stats(days=days)
    skill_stats = next((s for s in stats["skills"] if s["name"] == name), None)
    if not skill_stats:
        return {"name": name, "invocations": 0}
    return skill_stats


@router.get("/skills/usage/{name}/trend")
async def skill_usage_trend(
    name: str,
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Skill usage trend with sparkline data."""
    from ..skill_analytics import get_skill_trend

    return get_skill_trend(skill_name=name, days=days)


# Parameterized route AFTER specific routes
@router.get("/skills/{name}")
async def get_skill(name: str, _auth=Depends(verify_token)):
    """Get a specific skill's full details including prompt and file contents."""
    from ..skill_loader import get_skill as _get

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    result = skill.to_dict()
    result["system_prompt"] = skill.system_prompt

    # Include raw file contents for viewing/editing
    for filename, key in [
        ("skill.md", "raw_content"),
        ("evals.yaml", "evals_content"),
        ("mcp.yaml", "mcp_content"),
        ("layouts.yaml", "layouts_content"),
        ("components.yaml", "components_content"),
    ]:
        filepath = skill.path / filename
        if filepath.exists():
            result[key] = filepath.read_text(encoding="utf-8")

    return result


@router.post("/admin/skills/reload")
async def reload_skills(_auth=Depends(verify_token)):
    """Hot reload all skills from disk."""
    from ..skill_loader import reload_skills as _reload

    skills = _reload()
    return {"reloaded": len(skills), "skills": list(skills.keys())}


@router.post("/admin/skills/test")
async def test_skill_routing(
    body: dict,
    _auth=Depends(verify_token),
):
    """Test which skill would handle a given query."""
    from ..skill_loader import classify_query

    query = body.get("query", "")
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query' field")

    skill = classify_query(query)
    return {
        "query": query,
        "skill": skill.name,
        "version": skill.version,
        "description": skill.description,
        "degraded": skill.degraded,
    }


@router.get("/admin/mcp")
async def list_mcp_servers(_auth=Depends(verify_token)):
    """List all MCP server connections with status."""
    from ..mcp_client import list_mcp_connections

    return list_mcp_connections()


@router.get("/components")
async def list_components(_auth=Depends(verify_token)):
    """List all registered component kinds with schemas."""
    from ..component_registry import COMPONENT_REGISTRY

    return {
        name: {
            "description": c.description,
            "category": c.category,
            "required_fields": c.required_fields,
            "optional_fields": c.optional_fields,
            "supports_mutations": c.supports_mutations,
            "example": c.example,
            "is_container": c.is_container,
        }
        for name, c in COMPONENT_REGISTRY.items()
    }
