"""Investigations of inbox items: the one a person asks for, and its status.

``POST /inbox/{id}/investigate`` used to claim the item and nothing else. The
inbox's background investigation (``inbox._phase_b_investigate``) only picks up
items triage moved to ``agent_reviewing``, and a claimed item is never triaged,
so a task created and then "investigated" never got an investigation and
``GET /inbox/{id}/investigation`` stayed 404 for good.

This module starts the investigation itself, through the same pipeline inbox
triage uses (``investigation_pipeline.investigate``, freeform, read-only),
admitted by the monitor's budget, cooldown and concurrency rules
(``investigation_runner.admit_requested_investigation``). It also keeps one
record of the run per item, in-process and in the item's metadata
(``investigation_run``), which triage's runs use too, so:

* a second request while a run is in progress returns that run, not a new one;
* ``GET /inbox/{id}/investigation`` can say ``queued``/``running``/``done``/
  ``failed`` (with the error) and not only return a finished report.

Reads use the requester's forwarded token when there is one: the run's task is
created inside ``user_token_context``, and the agent loop carries the context
into its tool threads. Without a token (token forwarding off) it reads as the
service account, as the monitor's own investigations do. Either way the run is
offered no write tools.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pulse_agent.inbox")

RUN_KEY = "investigation_run"
ACTIVE_STATES = frozenset(("queued", "running"))
CLOSED_ITEM_STATES = frozenset(("resolved", "archived"))

# How long a run recorded as queued/running in the item's metadata, but not in
# this process, is believed before it is reported as interrupted. Covers the
# investigation timeout plus saving; a restart is the usual cause.
_STALE_GRACE_SECONDS = 120


@dataclass
class InvestigationRun:
    item_id: str
    requested_by: str
    source: str  # "request" (a person) | "triage" (inbox triage)
    state: str = "queued"  # queued | running | done | failed
    queued_at: int = field(default_factory=lambda: int(time.time()))
    started_at: int | None = None
    finished_at: int | None = None
    error: str = ""
    report_id: str = ""
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "state": self.state,
            "source": self.source,
            "requested_by": self.requested_by,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error or None,
            "report_id": self.report_id or None,
        }


class InvestigationRefusedError(Exception):
    """The investigation was not started. ``status_code`` is the HTTP answer."""

    def __init__(self, status_code: int, reason: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message
        self.retry_after = retry_after

    def detail(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, "retry_after": self.retry_after}


class ItemNotFoundError(Exception):
    pass


# Runs in progress in this process, by item id. Triage's runs execute in a
# worker thread on their own event loop, hence a threading lock.
_runs: dict[str, InvestigationRun] = {}
_lock = threading.Lock()


def active_run(item_id: str) -> InvestigationRun | None:
    with _lock:
        return _runs.get(item_id)


def active_count() -> int:
    with _lock:
        return len(_runs)


def reserve(item_id: str, requested_by: str, source: str) -> tuple[InvestigationRun, bool]:
    """The item's run in progress, or a new queued one. ``True`` when this call created it."""
    with _lock:
        existing = _runs.get(item_id)
        if existing is not None:
            return existing, False
        run = InvestigationRun(item_id=item_id, requested_by=requested_by, source=source)
        _runs[item_id] = run
        return run, True


def release(item_id: str) -> None:
    with _lock:
        _runs.pop(item_id, None)


def _reset_for_tests() -> None:
    with _lock:
        _runs.clear()


def persist(run: InvestigationRun) -> None:
    """Record the run on the item (re-read first: other writers update metadata too)."""
    from .inbox import _publish_event, get_inbox_item
    from .repositories.inbox_repo import get_inbox_repo

    item = get_inbox_item(run.item_id)
    if item is None:
        return
    metadata = item.get("metadata") or {}
    metadata[RUN_KEY] = run.public()
    get_inbox_repo().update_metadata(run.item_id, metadata, int(time.time()))
    _publish_event("inbox_item_updated", run.item_id, {RUN_KEY: run.public()})


def run_status(item: dict[str, Any]) -> dict[str, Any] | None:
    """The item's latest run: the one in progress here, else the one recorded on the item."""
    live = active_run(item["id"])
    if live is not None:
        return live.public()
    recorded = (item.get("metadata") or {}).get(RUN_KEY)
    if not isinstance(recorded, dict):
        return None
    recorded = dict(recorded)
    if recorded.get("state") in ACTIVE_STATES:
        # Recorded as in progress, but not running in this process: it was cut
        # off (a restart). Said once the investigation could no longer be
        # finishing, so a run that is just being saved is not called failed.
        from .config import get_settings

        since = int(recorded.get("started_at") or recorded.get("queued_at") or 0)
        limit = int(get_settings().monitor.investigation_timeout) + _STALE_GRACE_SECONDS
        if time.time() - since > limit:
            recorded["state"] = "failed"
            recorded["error"] = "Interrupted: the agent restarted before the investigation finished."
    return recorded


async def request_investigation(item_id: str, requested_by: str, user_token: str | None = None) -> dict[str, Any]:
    """Start (or return) the investigation of an inbox item. Returns the run's public status.

    Raises ``ItemNotFoundError`` or ``InvestigationRefusedError`` (closed item, budget,
    cost budget, circuit breaker, cooldown or concurrency).
    """
    from .inbox import claim_and_investigate, finding_for_item, get_inbox_item
    from .monitor import get_cluster_monitor
    from .monitor.investigation_runner import admit_requested_investigation

    item = await asyncio.to_thread(get_inbox_item, item_id)
    if item is None:
        raise ItemNotFoundError(item_id)

    live = active_run(item_id)
    if live is not None:
        return live.public()

    if item.get("status") in CLOSED_ITEM_STATES:
        raise InvestigationRefusedError(
            409, "item_closed", f"The item is {item['status']}; reopen it to investigate it again."
        )

    finding = finding_for_item(item)
    monitor = await get_cluster_monitor()

    # Reserve before admitting, with no await in between: two requests racing
    # each other get one run, and the loser is answered with its status.
    run, created = reserve(item_id, requested_by, "request")
    if not created:
        return run.public()
    try:
        refusal = admit_requested_investigation(monitor, finding, running_elsewhere=active_count() - 1)
    except Exception:
        release(item_id)
        raise
    if refusal is not None:
        release(item_id)
        raise InvestigationRefusedError(429, refusal.reason, refusal.message, refusal.retry_after)

    try:
        # Claim it as before, but never take it from someone who already has
        # it: the investigation is read-only and useful to whoever owns it.
        if not item.get("claimed_by") or item.get("claimed_by") == requested_by:
            await asyncio.to_thread(claim_and_investigate, item_id, requested_by)
        await asyncio.to_thread(persist, run)
    except Exception:
        release(item_id)
        raise

    from .k8s_client import user_token_context

    # The task copies the current context when it is created, so the token
    # set here is what its reads (and the agent loop's tool threads) see.
    with user_token_context(user_token):
        run.task = asyncio.create_task(_execute(run, finding), name=f"inbox-investigate-{item_id}")
    return run.public()


async def _execute(run: InvestigationRun, finding: dict[str, Any]) -> None:
    from .monitor import investigation_pipeline

    run.state = "running"
    run.started_at = int(time.time())
    try:
        await asyncio.to_thread(persist, run)
    except Exception:
        logger.debug("Recording investigation start for %s failed", run.item_id, exc_info=True)

    try:
        outcome = await investigation_pipeline.investigate(None, finding, attach_to_inbox=False)
        run.report_id = outcome.report_id
        if outcome.completed:
            await asyncio.to_thread(_attach_result, run, outcome.to_inbox_result())
            run.state = "done"
        else:
            run.state = "failed"
            run.error = outcome.error or "The investigation failed."
    except Exception as e:
        logger.exception("Requested investigation failed for %s", run.item_id)
        run.state = "failed"
        run.error = str(e) or type(e).__name__
    finally:
        run.finished_at = int(time.time())
        try:
            await asyncio.to_thread(persist, run)
        except Exception:
            logger.warning("Recording investigation result for %s failed", run.item_id, exc_info=True)
        finally:
            release(run.item_id)


def _attach_result(run: InvestigationRun, result: dict[str, Any]) -> None:
    """Link the report to the item. Status is left alone: a person asked, and owns what happens next."""
    from .inbox import get_inbox_item, investigation_metadata
    from .repositories.inbox_repo import get_inbox_repo

    item = get_inbox_item(run.item_id)
    if item is None:
        return
    metadata = item.get("metadata") or {}
    metadata.update(investigation_metadata(result, run.report_id))
    metadata["skill_used"] = "sre"
    get_inbox_repo().update_metadata(run.item_id, metadata, int(time.time()))
