"""Skill management and analytics REST endpoints."""

from __future__ import annotations

import difflib
import shutil
from datetime import datetime
from pathlib import Path

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


@router.put("/admin/skills/{name}")
async def update_skill(name: str, body: dict, _auth=Depends(verify_token)):
    """Save updated skill.md content. Archives the old version and hot-reloads."""
    from ..skill_loader import get_skill as _get
    from ..skill_loader import reload_skills as _reload

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    content = body.get("content", "")
    if not content or "---" not in content:
        raise HTTPException(status_code=400, detail="Content must include YAML frontmatter (--- delimiters)")

    skill_file = skill.path / "skill.md"
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="skill.md not found on disk")

    # Archive current version before overwriting
    _archive_version(skill.path, skill.version)

    # Write new content
    skill_file.write_text(content, encoding="utf-8")

    # Hot-reload all skills
    _reload()
    updated = _get(name)
    if not updated:
        raise HTTPException(status_code=500, detail="Skill failed to reload after save")

    return {
        "name": updated.name,
        "version": updated.version,
        "saved": True,
    }


@router.get("/admin/skills/{name}/versions")
async def list_skill_versions(name: str, _auth=Depends(verify_token)):
    """List all archived versions of a skill."""
    from ..skill_loader import get_skill as _get

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    versions_dir = skill.path / ".versions"
    versions = []

    # Current version
    skill_file = skill.path / "skill.md"
    if skill_file.exists():
        stat = skill_file.stat()
        versions.append(
            {
                "version": skill.version,
                "label": f"v{skill.version} (current)",
                "filename": "skill.md",
                "timestamp": datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC).isoformat(),
                "current": True,
            }
        )

    # Archived versions
    if versions_dir.exists():
        for f in sorted(versions_dir.iterdir(), reverse=True):
            if f.name.startswith("skill_v") and f.name.endswith(".md"):
                ver_str = f.name.removeprefix("skill_v").removesuffix(".md")
                try:
                    ver_num = int(ver_str.split("_")[0])
                except ValueError:
                    continue
                stat = f.stat()
                versions.append(
                    {
                        "version": ver_num,
                        "label": f"v{ver_num}",
                        "filename": f.name,
                        "timestamp": datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC).isoformat(),
                        "current": False,
                    }
                )

    return {"name": name, "versions": versions}


@router.get("/admin/skills/{name}/diff")
async def skill_version_diff(
    name: str,
    v1: str = Query(..., description="Filename of version A (e.g. skill_v1.md)"),
    v2: str = Query(..., description="Filename of version B (e.g. skill.md for current)"),
    _auth=Depends(verify_token),
):
    """Unified diff between two versions of a skill."""
    from ..skill_loader import get_skill as _get

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    def _resolve_path(filename: str) -> Path:
        if filename == "skill.md":
            return skill.path / "skill.md"
        p = skill.path / ".versions" / filename
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Version file not found: {filename}")
        return p

    path_a = _resolve_path(v1)
    path_b = _resolve_path(v2)

    lines_a = path_a.read_text(encoding="utf-8").splitlines(keepends=True)
    lines_b = path_b.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=v1, tofile=v2))

    return {
        "name": name,
        "v1": v1,
        "v2": v2,
        "diff": "".join(diff),
        "has_changes": len(diff) > 0,
    }


def _archive_version(skill_path: Path, version: int) -> None:
    """Copy current skill.md to .versions/skill_v{version}.md."""
    skill_file = skill_path / "skill.md"
    if not skill_file.exists():
        return

    versions_dir = skill_path / ".versions"
    versions_dir.mkdir(exist_ok=True)

    # Include timestamp to avoid collision if same version is saved multiple times
    ts = datetime.now(tz=datetime.UTC).strftime("%Y%m%d%H%M%S")
    archive_name = f"skill_v{version}_{ts}.md"
    shutil.copy2(skill_file, versions_dir / archive_name)


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
