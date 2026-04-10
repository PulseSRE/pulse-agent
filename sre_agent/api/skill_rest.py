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


@router.get("/skills/{name}")
async def get_skill(name: str, _auth=Depends(verify_token)):
    """Get a specific skill's details."""
    from ..skill_loader import get_skill as _get

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill.to_dict()


@router.post("/admin/skills/reload")
async def reload_skills(_auth=Depends(verify_token)):
    """Hot reload all skills from disk."""
    from ..skill_loader import reload_skills as _reload

    skills = _reload()
    return {"reloaded": len(skills), "skills": list(skills.keys())}


@router.get("/skills/usage")
async def skill_usage_stats(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Aggregated skill usage statistics."""
    from ..skill_analytics import get_skill_stats

    return get_skill_stats(days=days)


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


@router.get("/skills/usage/handoffs")
async def skill_handoff_flow(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Handoff flow between skills."""
    from ..skill_analytics import get_skill_stats

    stats = get_skill_stats(days=days)
    return {"handoffs": stats.get("handoffs", []), "days": days}


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
