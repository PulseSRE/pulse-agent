"""REST endpoints for the Ops Inbox."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger("pulse_agent.api")

from ..inbox import (
    VALID_TRANSITIONS,
    async_get_inbox_stats,
    async_list_inbox_items,
    claim_item,
    create_inbox_item,
    dismiss_item,
    escalate_assessment,
    get_inbox_item,
    pin_item,
    record_interaction,
    snooze_item,
    unclaim_item,
    update_item_status,
)
from .auth import get_owner, verify_token

router = APIRouter(tags=["inbox"], dependencies=[Depends(verify_token)])


@router.get("/inbox")
async def rest_list_inbox(
    type: str | None = Query(None),
    status: str | None = Query(None),
    namespace: str | None = Query(None),
    claimed_by: str | None = Query(None),
    created_by: str | None = Query(None),
    severity: str | None = Query(None),
    group_by: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    owner: str = Depends(get_owner),
):
    resolved_claimed = claimed_by
    if claimed_by == "__current_user__":
        resolved_claimed = owner
    elif claimed_by == "__unclaimed__":
        resolved_claimed = "__null__"

    resolved_created = created_by
    if created_by == "__user__":
        resolved_created = "__not_system__"

    result = await async_list_inbox_items(
        item_type=type,
        status=status,
        namespace=namespace,
        claimed_by=resolved_claimed,
        created_by=resolved_created,
        severity=severity,
        group_by=group_by,
        limit=limit,
        offset=offset,
    )
    result["current_user"] = owner
    return result


@router.get("/inbox/stats")
async def rest_inbox_stats():
    return await async_get_inbox_stats()


# Declared before /inbox/{item_id}: FastAPI matches in definition order, so a
# literal path registered after the parameterised one is unreachable — item_id
# would simply capture "mutes". /inbox/stats above is placed the same way.
@router.get("/inbox/mutes")
async def list_muted_conditions(_auth=Depends(verify_token)):
    from ..repositories.inbox_repo import get_inbox_repo

    return {"mutes": [dict(r) for r in get_inbox_repo().list_mutes()]}


@router.get("/inbox/{item_id}")
async def rest_get_inbox_item(item_id: str):
    item = get_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.post("/inbox", status_code=201)
async def rest_create_inbox_item(
    request: Request,
    owner: str = Depends(get_owner),
):
    body = await request.json()
    title = body.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    item = {
        "item_type": body.get("item_type", "task"),
        "title": title,
        "summary": body.get("summary", ""),
        "severity": body.get("severity"),
        "namespace": body.get("namespace"),
        "due_date": body.get("due_date"),
        "created_by": owner,
        "resources": body.get("resources", []),
        "metadata": body.get("metadata", {}),
    }
    item_id = create_inbox_item(item)
    return {"id": item_id, "item_type": item["item_type"], "status": "new"}


@router.patch("/inbox/{item_id}")
async def rest_update_inbox_item(item_id: str, request: Request):
    body = await request.json()
    new_status = body.get("status")
    if new_status:
        ok = update_item_status(item_id, new_status)
        if not ok:
            raise HTTPException(status_code=400, detail="Invalid status transition")
    return {"ok": True}


@router.post("/inbox/{item_id}/claim")
async def rest_claim_item(item_id: str, owner: str = Depends(get_owner)):
    ok = claim_item(item_id, owner)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.delete("/inbox/{item_id}/claim")
async def rest_unclaim_item(item_id: str, owner: str = Depends(get_owner)):
    unclaim_item(item_id, actor=owner)
    return {"ok": True}


@router.post("/inbox/{item_id}/acknowledge")
async def rest_acknowledge_item(item_id: str, owner: str = Depends(get_owner)):
    ok = update_item_status(item_id, "triaged", actor=owner)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid status transition")
    return {"ok": True}


@router.post("/inbox/{item_id}/snooze")
async def rest_snooze_item(item_id: str, request: Request, owner: str = Depends(get_owner)):
    body = await request.json()
    hours = body.get("hours", 24)
    if hours not in (4, 24, 72, 168):
        raise HTTPException(status_code=400, detail="hours must be 4, 24, 72, or 168")
    ok = snooze_item(item_id, hours, actor=owner)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/inbox/{item_id}/dismiss")
async def rest_dismiss_item(item_id: str, owner: str = Depends(get_owner)):
    ok = dismiss_item(item_id, actor=owner)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/inbox/{item_id}/investigate")
async def rest_investigate_item(item_id: str, owner: str = Depends(get_owner)):
    from ..inbox import claim_and_investigate

    ok = claim_and_investigate(item_id, owner)
    if not ok:
        raise HTTPException(status_code=409, detail="Item not found or already claimed by another user")
    return {"ok": True, "item_id": item_id}


@router.post("/inbox/{item_id}/resolve")
async def rest_resolve_item(item_id: str, owner: str = Depends(get_owner)):
    item = get_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    ok = update_item_status(item_id, "resolved", actor=owner)
    if not ok:
        valid_next = VALID_TRANSITIONS.get(item["item_type"], {}).get(item["status"], [])
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resolve from status '{item['status']}'. Valid next: {valid_next}",
        )
    return {"ok": True}


@router.post("/inbox/{item_id}/escalate")
async def rest_escalate_item(item_id: str):
    finding_id = escalate_assessment(item_id)
    if finding_id is None:
        raise HTTPException(status_code=400, detail="Item is not an assessment or not found")
    return {"ok": True, "finding_id": finding_id}


@router.post("/inbox/{item_id}/restore")
async def rest_restore_item(item_id: str, owner: str = Depends(get_owner)):
    from ..inbox import restore_item

    ok = restore_item(item_id, actor=owner)
    if not ok:
        raise HTTPException(status_code=400, detail="Item is not agent_cleared or not found")
    return {"ok": True}


@router.post("/inbox/{item_id}/step")
async def rest_record_step(item_id: str, request: Request, owner: str = Depends(get_owner)):
    body = await request.json()
    action = body.get("action", "")
    if action not in ("execute", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'execute' or 'skip'")
    record_interaction(
        actor=owner,
        interaction_type=f"{action}_step",
        item_id=item_id,
        metadata={"step_index": body.get("step_index", 0), "step_title": body.get("step_title", "")},
    )
    return {"ok": True}


@router.get("/inbox/{item_id}/investigation")
async def rest_get_investigation(item_id: str):
    item = get_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    inv_id = (item.get("metadata") or {}).get("investigation_id")
    if not inv_id:
        raise HTTPException(status_code=404, detail="No investigation linked")
    from ..db import get_database as _get_db

    db = _get_db()
    row = db.fetchone("SELECT * FROM investigations WHERE id = ?", (inv_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    result = dict(row)
    for field in ("resources", "evidence", "alternatives_considered"):
        if field in result and isinstance(result[field], str):
            import json as _json

            try:
                result[field] = _json.loads(result[field])
            except (ValueError, TypeError):
                logger.debug("Failed to parse JSON field '%s' in investigation result", field)
    return result


@router.post("/inbox/{item_id}/pin")
async def rest_pin_item(item_id: str, owner: str = Depends(get_owner)):
    ok = pin_item(item_id, owner)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/inbox/mute")
async def mute_condition(body: dict, owner: str = Depends(get_owner)):
    """Silence a correlation key so its items stop being raised.

    Takes a correlation_key rather than an item id on purpose: muting one item
    would be pointless, because the next scan simply raises another for the same
    condition. The key is what recurs.
    """
    from ..inbox import mute_correlation_key

    key = (body.get("correlation_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="correlation_key is required")
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(
            status_code=400, detail="reason is required — a mute with no reason becomes permanent by accident"
        )

    hours = body.get("hours")
    mute_correlation_key(key, muted_by=owner, reason=reason, hours=float(hours) if hours else None)
    return {"correlation_key": key, "muted": True, "hours": hours, "muted_by": owner}


@router.delete("/inbox/mute/{correlation_key:path}")
async def unmute_condition(correlation_key: str, _owner: str = Depends(get_owner)):
    from ..inbox import unmute_correlation_key

    unmute_correlation_key(correlation_key)
    return {"correlation_key": correlation_key, "muted": False}
