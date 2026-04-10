"""Tool listing and usage REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from ..agent import (
    ALL_TOOLS as SRE_ALL_TOOLS,
)
from ..agent import WRITE_TOOLS
from ..security_agent import (
    ALL_TOOLS as SEC_ALL_TOOLS,
)
from .auth import verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter()


@router.get("/tools")
async def list_tools(_auth=Depends(verify_token)):
    """List all available tools grouped by mode, with write-op flags."""
    from ..harness import get_tool_category

    return {
        "sre": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": t.name in WRITE_TOOLS,
                "category": get_tool_category(t.name),
            }
            for t in SRE_ALL_TOOLS
        ],
        "security": [
            {
                "name": t.name,
                "description": t.description,
                "requires_confirmation": False,
                "category": get_tool_category(t.name),
            }
            for t in SEC_ALL_TOOLS
        ],
        "write_tools": sorted(WRITE_TOOLS),
    }


@router.get("/agents")
async def list_agents(_auth=Depends(verify_token)):
    """List all agent modes with metadata."""
    from ..tool_usage import get_agents_metadata

    return get_agents_metadata()


@router.get("/tools/usage/stats")
async def get_tools_usage_stats(
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    _auth=Depends(verify_token),
):
    """Aggregated tool usage statistics."""
    from ..tool_usage import get_usage_stats

    return get_usage_stats(time_from=time_from, time_to=time_to)


@router.get("/tools/usage/chains")
async def get_tools_usage_chains(_auth=Depends(verify_token)):
    """Discovered tool call chains (common sequences)."""
    from ..tool_chains import discover_chains

    return discover_chains()


@router.get("/tools/usage")
async def get_tools_usage(
    tool_name: str | None = Query(None),
    agent_mode: str | None = Query(None),
    status: str | None = Query(None),
    session_id: str | None = Query(None),
    time_from: str | None = Query(None, alias="from"),
    time_to: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _auth=Depends(verify_token),
):
    """Paginated audit log of tool invocations."""
    from ..tool_usage import query_usage

    return query_usage(
        tool_name=tool_name,
        agent_mode=agent_mode,
        status=status,
        session_id=session_id,
        time_from=time_from,
        time_to=time_to,
        page=page,
        per_page=per_page,
    )
