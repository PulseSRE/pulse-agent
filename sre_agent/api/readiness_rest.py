"""Authenticated installation diagnostics, separate from process liveness.

Also the readiness gate waivers: an administrator's record that a gate's risk
is accepted for this cluster, with the reason, who accepted it and when.
"""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..readiness import collect_readiness
from .auth import require_admin, verify_token

logger = logging.getLogger("pulse_agent.api")

router = APIRouter(tags=["readiness"])
_lock = asyncio.Lock()
_cached = None
_expires = 0.0

# Gate ids are defined by the UI's readiness checks (``etcd-backup``,
# ``network-policies``, ...). The agent does not keep that list, so it checks
# the shape only: the id is a path segment and an audit-log key.
_GATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
MAX_REASON_LENGTH = 2000


@router.get("/readiness")
async def installation_readiness(_auth=Depends(verify_token)):
    return await get_installation_report()


async def get_installation_report() -> dict:
    global _cached, _expires
    # Single flight plus a short cache bounds repeated read-only cluster probes.
    async with _lock:
        if _cached is None or time.monotonic() >= _expires:
            _cached = await collect_readiness()
            _expires = time.monotonic() + 60
        return _cached


# -- Waivers -----------------------------------------------------------------


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _to_json(row: dict) -> dict:
    return {
        "gate_id": row["gate_id"],
        "reason": row["reason"],
        "waived_by": row["waived_by"],
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(row["expires_at"]),
    }


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _storage_unavailable(action: str) -> JSONResponse:
    # The exception may carry a DSN or SQL; it goes to the log, not the caller.
    logger.warning("Readiness waiver %s failed", action, exc_info=True)
    return _error(503, "Waiver storage is unavailable; the database could not be reached.")


def _parse_expiry(value: object, now_ms: int) -> int | str | None:
    """Epoch ms for an ISO 8601 ``expires_at``, None for none, or an error message."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return "'expires_at' must be an ISO 8601 timestamp with a time zone, or null"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return "'expires_at' must be an ISO 8601 timestamp with a time zone, or null"
    if parsed.tzinfo is None:
        # A naive time means a different instant to every reader.
        return "'expires_at' must include a time zone (for example 2027-01-31T00:00:00Z)"
    ms = int(parsed.timestamp() * 1000)
    if ms <= now_ms:
        return "'expires_at' must be in the future"
    return ms


@router.get("/readiness/waivers")
async def list_readiness_waivers(_auth=Depends(verify_token)):
    """Active waivers for this cluster. Expired waivers are not returned."""
    from ..repositories import get_readiness_waiver_repo

    try:
        rows = await asyncio.to_thread(get_readiness_waiver_repo().fetch_active, int(time.time() * 1000))
    except Exception:
        return _storage_unavailable("list")
    return {"waivers": [_to_json(r) for r in rows]}


@router.put("/readiness/waivers/{gate_id}")
async def waive_readiness_gate(gate_id: str, request: Request, actor: str = Depends(require_admin)):
    """Waive a gate: the risk is accepted, with a reason, by the signed-in admin.

    Same authority as approving a fix. The author is the authenticated user
    from ``require_admin``; any author the client sends is ignored.
    """
    if not _GATE_ID_RE.match(gate_id):
        return _error(400, "Invalid gate id")
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _error(400, "Body must be a JSON object with a non-empty 'reason' string")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error(400, "Body must include a non-empty 'reason' string")
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        return _error(400, f"'reason' must be at most {MAX_REASON_LENGTH} characters")

    now_ms = int(time.time() * 1000)
    expires = _parse_expiry(body.get("expires_at"), now_ms)
    if isinstance(expires, str):
        return _error(400, expires)

    from ..inbox import record_interaction
    from ..repositories import get_readiness_waiver_repo

    try:
        row = await asyncio.to_thread(get_readiness_waiver_repo().upsert, gate_id, reason, actor, now_ms, expires)
    except Exception:
        return _storage_unavailable("save")
    waiver = _to_json(row)
    await asyncio.to_thread(
        record_interaction,
        actor=actor,
        interaction_type="waive_readiness_gate",
        item_id=gate_id,
        decision="waived",
        metadata={"reason": reason, "expires_at": waiver["expires_at"]},
    )
    logger.info("Readiness gate %s waived by %s", gate_id, actor)
    return waiver


@router.delete("/readiness/waivers/{gate_id}")
async def unwaive_readiness_gate(gate_id: str, actor: str = Depends(require_admin)):
    """Lift a gate's waiver. Idempotent: lifting a waiver that is not there is 200 with ``deleted: false``.

    Not 404, so a client can tell "no such waiver" from "this agent has no
    waiver endpoint" (an older agent answers 404 for the route itself).
    """
    if not _GATE_ID_RE.match(gate_id):
        return _error(400, "Invalid gate id")

    from ..inbox import record_interaction
    from ..repositories import get_readiness_waiver_repo

    try:
        lifted = await asyncio.to_thread(get_readiness_waiver_repo().delete, gate_id)
    except Exception:
        return _storage_unavailable("delete")
    deleted = lifted is not None
    if lifted is not None:
        # The row is gone, so the audit record keeps what was lifted.
        was = _to_json(lifted)
        await asyncio.to_thread(
            record_interaction,
            actor=actor,
            interaction_type="unwaive_readiness_gate",
            item_id=gate_id,
            decision="unwaived",
            metadata={
                "reason": was["reason"],
                "waived_by": was["waived_by"],
                "created_at": was["created_at"],
                "expires_at": was["expires_at"],
            },
        )
        logger.info("Readiness gate %s waiver lifted by %s", gate_id, actor)
    return {"gate_id": gate_id, "deleted": deleted}
