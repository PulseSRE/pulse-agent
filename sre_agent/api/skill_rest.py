"""Skill management and analytics REST endpoints."""

from __future__ import annotations

import difflib
import shutil
from datetime import UTC, datetime
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
        raise HTTPException(
            status_code=400,
            detail="Content must include YAML frontmatter (--- delimiters)",
        )

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


_BUILTIN_SKILLS = {"sre", "security", "view_designer"}


@router.delete("/admin/skills/{name}")
async def delete_skill_endpoint(name: str, _auth=Depends(verify_token)):
    """Delete a user-created skill. Built-in skills cannot be deleted."""
    from ..skill_loader import get_skill as _get
    from ..skill_loader import reload_skills as _reload

    skill = _get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    if name in _BUILTIN_SKILLS:
        raise HTTPException(status_code=403, detail=f"Cannot delete built-in skill '{name}'")

    skill_dir = skill.path
    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    _reload()
    return {"name": name, "deleted": True}


@router.post("/admin/skills/{name}/clone")
async def clone_skill(name: str, body: dict, _auth=Depends(verify_token)):
    """Clone an existing skill as a template for a new one."""
    from ..skill_loader import _SKILLS_DIR
    from ..skill_loader import get_skill as _get
    from ..skill_loader import reload_skills as _reload

    source = _get(name)
    if not source:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    new_name = body.get("new_name", "")
    if not new_name:
        raise HTTPException(status_code=400, detail="new_name is required")

    import re as _re

    if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", new_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid skill name — must be lowercase alphanumeric with hyphens/underscores",
        )

    new_description = body.get("description", source.description)

    # Read source skill.md
    source_file = source.path / "skill.md"
    if not source_file.exists():
        raise HTTPException(status_code=404, detail="Source skill.md not found")

    content = source_file.read_text(encoding="utf-8")

    # Replace name and description in frontmatter
    import re

    content = re.sub(r"^name:\s*.*$", f"name: {new_name}", content, count=1, flags=re.MULTILINE)
    content = re.sub(
        r"^description:\s*.*$",
        f"description: {new_description}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    content = re.sub(r"^version:\s*\d+", "version: 1", content, count=1, flags=re.MULTILINE)

    # Write to new directory
    new_dir = _SKILLS_DIR / new_name.replace("_", "-")
    if new_dir.exists():
        raise HTTPException(status_code=409, detail=f"Skill '{new_name}' already exists")

    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "skill.md").write_text(content, encoding="utf-8")

    # Copy evals.yaml if exists
    source_evals = source.path / "evals.yaml"
    if source_evals.exists():
        shutil.copy2(source_evals, new_dir / "evals.yaml")

    _reload()
    new_skill = _get(new_name)

    return {
        "name": new_name,
        "cloned_from": name,
        "version": new_skill.version if new_skill else 1,
        "created": True,
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
                "timestamp": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
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
                        "timestamp": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
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
        versions_dir = skill.path / ".versions"
        p = (versions_dir / filename).resolve()
        if not str(p).startswith(str(versions_dir.resolve())):
            raise HTTPException(status_code=400, detail="Invalid version filename")
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Version not found: {filename}")
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
    ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    archive_name = f"skill_v{version}_{ts}.md"
    shutil.copy2(skill_file, versions_dir / archive_name)


@router.get("/prompt/stats")
async def prompt_stats(
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Aggregated prompt stats: avg tokens by skill, cache hit rate, section breakdown."""
    from ..prompt_log import get_prompt_stats

    return get_prompt_stats(days=days)


@router.get("/prompt/versions/{skill}")
async def prompt_versions(
    skill: str,
    days: int = Query(30, ge=1, le=365),
    _auth=Depends(verify_token),
):
    """Track prompt hash changes over time for a skill."""
    from ..prompt_log import get_prompt_versions

    return {
        "skill": skill,
        "versions": get_prompt_versions(skill, days=days),
        "days": days,
    }


@router.get("/prompt/log")
async def prompt_log(
    session_id: str = Query(..., description="Session ID to retrieve prompt logs for"),
    _auth=Depends(verify_token),
):
    """Get prompt log entries for a session."""
    from ..prompt_log import get_prompt_log

    return {"session_id": session_id, "entries": get_prompt_log(session_id)}


# All known MCP server toolsets (from kubernetes-mcp-server --help)
_MCP_AVAILABLE_TOOLSETS = [
    "core",
    "config",
    "helm",
    "observability",
    "openshift",
    "ossm",
    "netedge",
    "tekton",
    "kiali",
    "kubevirt",
    "kcp",
]


@router.get("/admin/mcp")
async def list_mcp_servers(_auth=Depends(verify_token)):
    """List all MCP server connections with status + available toolsets."""
    from ..mcp_client import list_mcp_connections

    connections = list_mcp_connections()
    return {
        "connections": connections,
        "available_toolsets": _MCP_AVAILABLE_TOOLSETS,
    }


@router.post("/admin/mcp/toolsets")
async def update_mcp_toolsets(body: dict, _auth=Depends(verify_token)):
    """Update MCP server toolsets by patching the deployment and reconnecting.

    Expects: {"toolsets": ["core", "config", "helm", ...]}
    """
    import time

    toolsets = body.get("toolsets", [])
    if not toolsets or not isinstance(toolsets, list):
        raise HTTPException(status_code=400, detail="toolsets must be a non-empty list")

    invalid = [t for t in toolsets if t not in _MCP_AVAILABLE_TOOLSETS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid toolsets: {invalid}. Valid: {_MCP_AVAILABLE_TOOLSETS}",
        )

    # Patch the MCP deployment args
    try:
        from ..k8s_client import get_apps_client

        apps = get_apps_client()

        # Find the MCP deployment (name ends with -mcp)
        ns = _detect_namespace()
        deps = apps.list_namespaced_deployment(namespace=ns)
        mcp_deploy = None
        for d in deps.items:
            if d.metadata.name.endswith("-mcp") and "mcp-server" in str(d.spec.template.metadata.labels):
                mcp_deploy = d
                break

        if not mcp_deploy:
            raise HTTPException(status_code=404, detail="MCP server deployment not found in cluster")

        deploy_name = mcp_deploy.metadata.name

        # Save old args for rollback if new toolsets crash
        old_args = list(mcp_deploy.spec.template.spec.containers[0].args or [])

        # Build new args with updated toolsets
        new_args = [
            "--port",
            "8081",
            "--toolsets",
            ",".join(toolsets),
            "--cluster-provider",
            "in-cluster",
            "--stateless",
        ]

        # Patch container args — use field_manager="helm" to avoid conflicts
        # on next Helm upgrade (default "OpenAPI-Generator" creates a second owner)
        apps.patch_namespaced_deployment(
            name=deploy_name,
            namespace=ns,
            body={"spec": {"template": {"spec": {"containers": [{"name": "mcp-server", "args": new_args}]}}}},
            field_manager="helm",
        )

        # Wait for rollout — detect crashloop and revert if needed
        healthy = False
        for attempt in range(20):
            time.sleep(2)
            dep = apps.read_namespaced_deployment(deploy_name, ns)

            # Check for successful rollout
            if (
                dep.status.ready_replicas
                and dep.status.ready_replicas >= 1
                and dep.status.updated_replicas
                and dep.status.updated_replicas >= 1
            ):
                healthy = True
                break

            # Check for crashloop after initial grace period
            if attempt >= 5:
                from ..k8s_client import get_core_client

                core = get_core_client()
                pods = core.list_namespaced_pod(
                    namespace=ns,
                    label_selector="app.kubernetes.io/component=mcp-server",
                )
                for pod in pods.items:
                    for cs in pod.status.container_statuses or []:
                        waiting = cs.state.waiting if cs.state else None
                        if waiting and waiting.reason in (
                            "CrashLoopBackOff",
                            "Error",
                            "RunContainerError",
                        ):
                            # Revert to old args
                            apps.patch_namespaced_deployment(
                                name=deploy_name,
                                namespace=ns,
                                body={
                                    "spec": {
                                        "template": {
                                            "spec": {
                                                "containers": [
                                                    {
                                                        "name": "mcp-server",
                                                        "args": old_args,
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                },
                                field_manager="helm",
                            )
                            # Extract which toolsets were added
                            old_toolset_str = ""
                            for j, arg in enumerate(old_args):
                                if arg == "--toolsets" and j + 1 < len(old_args):
                                    old_toolset_str = old_args[j + 1]
                            old_ts = set(old_toolset_str.split(",")) if old_toolset_str else set()
                            new_ts = set(toolsets) - old_ts
                            raise HTTPException(
                                status_code=400,
                                detail=f"MCP server crashed with toolsets {list(new_ts)}. "
                                f"These likely require operators not installed on this cluster. "
                                f"Reverted to: {sorted(old_ts)}",
                            )

        if not healthy:
            raise HTTPException(
                status_code=500,
                detail="MCP server did not become ready within 40 seconds. Check pod logs.",
            )

        # Give MCP server time to fully initialize after becoming ready
        time.sleep(3)

        # Reconnect MCP client to pick up new tools
        from ..mcp_client import disconnect_all

        disconnect_all()

        # Re-connect MCP for skills that have mcp.yaml
        from ..mcp_client import connect_skill_mcp
        from ..skill_loader import list_skills as _list_skills

        tool_count = 0
        tool_names: list[str] = []
        for skill in _list_skills():
            if (skill.path / "mcp.yaml").exists():
                conn = connect_skill_mcp(skill.name, skill.path)
                if conn and conn.connected:
                    # Override toolsets to match what the deployment is actually running
                    conn.toolsets = toolsets
                    tool_count = len(conn.tools)
                    tool_names = list(conn.tools)

        return {
            "toolsets": toolsets,
            "deployment": deploy_name,
            "tools_registered": tool_count,
            "tools": tool_names,
            "success": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update toolsets: {e}") from e


@router.post("/admin/mcp")
async def add_mcp_server(body: dict, _auth=Depends(verify_token)):
    """Add a standalone MCP server connection.

    Expects: {"name": "my-server", "url": "http://...", "transport": "sse"}
    """
    import ipaddress
    import urllib.parse

    from ..mcp_client import add_standalone_server

    name = body.get("name", "").strip()
    url = body.get("url", "").strip()
    transport = body.get("transport", "sse").strip()

    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    # Security: disable stdio transport for user-added servers (command injection risk)
    if transport == "stdio":
        raise HTTPException(
            status_code=400,
            detail="stdio transport is disabled for security. Use 'sse' transport.",
        )
    if transport != "sse":
        raise HTTPException(status_code=400, detail="transport must be 'sse'")

    # Security: validate URL to prevent SSRF
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must use http or https")

    hostname = parsed.hostname or ""

    # Block cloud metadata endpoints
    _BLOCKED_HOSTS = {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.internal",
    }
    if hostname in _BLOCKED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="URL blocked: cloud metadata endpoints are not allowed",
        )

    # Block private/internal IP ranges
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise HTTPException(
                status_code=400,
                detail="URL blocked: private/internal IP addresses are not allowed",
            )
    except ValueError:
        pass  # Hostname is a DNS name, not an IP — allow it

    conn = add_standalone_server(name, url, transport)

    return {
        "name": conn.name,
        "url": conn.url,
        "transport": conn.transport,
        "connected": conn.connected,
        "tools_count": len(conn.tools),
        "tools": conn.tools,
        "error": conn.error,
    }


@router.delete("/admin/mcp/{name}")
async def remove_mcp_server(name: str, _auth=Depends(verify_token)):
    """Remove a standalone MCP server connection."""
    from ..mcp_client import remove_standalone_server

    if not remove_standalone_server(name):
        raise HTTPException(status_code=404, detail=f"Standalone server '{name}' not found")

    return {"removed": name}


@router.post("/admin/mcp/test")
async def test_mcp_server(body: dict, _auth=Depends(verify_token)):
    """Test connectivity to an MCP server without registering it.

    Expects: {"url": "http://...", "transport": "sse"}
    Returns: {"connected": bool, "tools_count": int, "tools": [...], "error": str}
    """
    import ipaddress
    import urllib.parse

    from ..mcp_client import test_mcp_connection

    url = body.get("url", "").strip()
    transport = body.get("transport", "sse").strip()

    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if transport == "stdio":
        raise HTTPException(status_code=400, detail="stdio transport is disabled for security")
    if transport != "sse":
        raise HTTPException(status_code=400, detail="transport must be 'sse'")

    # SSRF validation (same as add_mcp_server)
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must use http or https")
    hostname = parsed.hostname or ""
    if hostname in {"169.254.169.254", "metadata.google.internal", "metadata.internal"}:
        raise HTTPException(status_code=400, detail="URL blocked: cloud metadata endpoints")
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise HTTPException(status_code=400, detail="URL blocked: private/internal IP")
    except ValueError:
        pass

    return test_mcp_connection(url, transport)


def _detect_namespace() -> str:
    """Detect the current namespace (in-cluster or default)."""
    try:
        return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
    except Exception:
        return "openshiftpulse"


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
