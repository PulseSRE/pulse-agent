"""REST endpoints for the Ops Inbox."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..inbox import (
    VALID_TRANSITIONS,
    claim_item,
    create_inbox_item,
    dismiss_item,
    escalate_assessment,
    get_inbox_item,
    get_inbox_stats,
    list_inbox_items,
    pin_item,
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
    severity: str | None = Query(None),
    group_by: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return list_inbox_items(
        item_type=type,
        status=status,
        namespace=namespace,
        claimed_by=claimed_by,
        severity=severity,
        group_by=group_by,
        limit=limit,
        offset=offset,
    )


@router.get("/inbox/stats")
async def rest_inbox_stats():
    return get_inbox_stats()


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
async def rest_unclaim_item(item_id: str):
    unclaim_item(item_id)
    return {"ok": True}


@router.post("/inbox/{item_id}/acknowledge")
async def rest_acknowledge_item(item_id: str):
    ok = update_item_status(item_id, "acknowledged")
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid status transition")
    return {"ok": True}


@router.post("/inbox/{item_id}/snooze")
async def rest_snooze_item(item_id: str, request: Request):
    body = await request.json()
    hours = body.get("hours", 24)
    if hours not in (4, 24, 72, 168):
        raise HTTPException(status_code=400, detail="hours must be 4, 24, 72, or 168")
    ok = snooze_item(item_id, hours)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/inbox/{item_id}/dismiss")
async def rest_dismiss_item(item_id: str):
    ok = dismiss_item(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/inbox/{item_id}/investigate")
async def rest_investigate_item(item_id: str, owner: str = Depends(get_owner)):
    item = get_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    claim_item(item_id, owner)
    if item["item_type"] == "finding" and item["status"] in ("new", "acknowledged"):
        if item["status"] == "new":
            update_item_status(item_id, "acknowledged")
        update_item_status(item_id, "investigating")
    return {"ok": True, "item_id": item_id}


@router.post("/inbox/{item_id}/resolve")
async def rest_resolve_item(item_id: str):
    item = get_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    ok = update_item_status(item_id, "resolved")
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


@router.post("/inbox/{item_id}/pin")
async def rest_pin_item(item_id: str, owner: str = Depends(get_owner)):
    ok = pin_item(item_id, owner)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}
