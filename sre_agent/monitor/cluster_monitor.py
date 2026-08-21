"""ClusterMonitor — singleton that owns the scan loop and broadcasts to all subscribers.

Multiple /ws/monitor WebSocket clients share a single ClusterMonitor instance
instead of each running their own scan loop. This eliminates duplicate K8s API
calls, Claude API calls, and memory usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..config import get_settings
from ..k8s_client import get_core_client
from ..repositories.monitor_repo import get_monitor_repo
from .actions import mark_finding_actions_resolved, save_action
from .autofix import is_autofix_paused
from .confidence import _estimate_auto_fix_confidence, _estimate_finding_confidence, _finding_key
from .findings import _make_action_report, _ts
from .registry import SEVERITY_CRITICAL
from .scanners import ALL_SCANNERS, get_all_scanner_instances

try:
    from ..observability import (
        ACTIVE_FINDINGS,
        AUTOFIX_TOTAL,
        INVESTIGATION_BUDGET_MAX,
        SCAN_DURATION_SECONDS,
        SCANNER_RUNS_TOTAL,
    )

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False
from .scanner_health import get_failure as get_reported_failure
from .scanner_health import reset as reset_reported_failures
from .scanner_health import scanning
from .webhook import _send_webhook, notify_episode_opened, notify_fix_proposed

if TYPE_CHECKING:
    from .session import MonitorClient

logger = logging.getLogger("pulse_agent.monitor")


def _resolve_finding_inbox(finding_id: str, finding: dict | None = None) -> None:
    """Resolve inbox items linked to a resolved finding."""
    try:
        from ..inbox import resolve_finding_inbox_item

        resolve_finding_inbox_item(finding_id, finding)
    except Exception:
        logger.debug("Failed to resolve inbox item for finding %s", finding_id, exc_info=True)


def _close_episode_for(finding: dict) -> None:
    """Close the episode this finding heads, if it heads one."""
    try:
        from ..inbox import _finding_corr_key
        from .episodes import close_for_correlation

        key = _finding_corr_key(finding)
        if key:
            close_for_correlation(key)
    except Exception:
        logger.debug("Could not close episode for finding", exc_info=True)


class ClusterMonitor:
    """Singleton that owns the scan loop and investigation pipeline.

    Subscribers (MonitorClient instances) are notified of all events via broadcast().
    Per-client filtering (e.g. disabled scanners) is handled by each MonitorClient.on_event().
    """

    _MAX_FINDINGS = 500

    def _correlate_episodes(self, findings: list[dict]) -> list[tuple[str, dict]]:
        """Open episodes for cause-capable findings and attach what they explain.

        Called with every finding currently standing, so a cause found three
        cycles ago can still absorb a symptom that only appeared this cycle.

        Returns the episodes opened for the first time this cycle, paired with
        their cause, so the caller can notify about them. Newly-opened is
        distinguished from touched-again by the ids seen on previous cycles:
        an episode announces itself once, not on every scan for as long as it
        stays open.
        """
        from ..inbox import _finding_corr_key
        from .episodes import attach_symptoms, open_or_touch, symptom_keys_by_episode
        from .layers import layer_for_finding

        first_seen = {}
        for f in findings:
            key = _finding_corr_key(f)
            if key:
                first_seen[key] = self._first_seen.get(_finding_key(f), int(time.time()))

        # Deepest cause first, and among equals the one that started earliest.
        # Order decides ownership: a symptom belongs to the first episode that
        # claims it, and the answer to "what explains this TargetDown" should
        # be the control plane underneath it, not the operator that is itself
        # a symptom of the same thing.
        def _depth(f: dict) -> tuple[int, int]:
            return (layer_for_finding(f), int(f.get("startedAt") or first_seen.get(_finding_corr_key(f), 0) or 0))

        if not self._episodes_seeded:
            # Whatever is already open was announced by whoever was running
            # before this process. Restarting the agent is not news.
            try:
                from .episodes import list_open

                self._known_episodes.update(e["id"] for e in list_open())
            except Exception:
                logger.debug("Could not seed known episodes", exc_info=True)
            self._episodes_seeded = True

        claimed = symptom_keys_by_episode()
        opened: list[tuple[str, dict]] = []
        for f in sorted(findings, key=_depth):
            episode_id = open_or_touch(f, claimed)
            if episode_id:
                if episode_id not in self._known_episodes:
                    self._known_episodes.add(episode_id)
                    opened.append((episode_id, f))
                # The finding, not just its category: it carries the declared
                # layer and the condition's own onset, both of which the
                # category alone throws away.
                attach_symptoms(episode_id, f, findings, first_seen, claimed)
        return opened

    def __init__(self) -> None:
        self.running = False
        self.scan_interval = get_settings().monitor.scan_interval
        self._subscribers: list[MonitorClient] = []
        # When each condition was first observed. Episodes need it to tell a
        # symptom from something that was already broken before the cause.
        self._first_seen: dict[str, int] = {}
        # Episodes already announced. Seeded from the database on first use so
        # a restart does not re-announce every open episode on the cluster.
        self._known_episodes: set[str] = set()
        self._episodes_seeded = False
        self._subscribers_lock = asyncio.Lock()

        # Scan state — previously owned by MonitorSession
        self._last_findings: dict[str, dict] = {}
        self._recent_fixes: dict[str, float] = {}
        self._fix_attempt_counts: dict[str, int] = {}
        self._MAX_FIX_ATTEMPTS = 2
        self._recent_investigations: dict[str, float] = {}
        self._investigation_fingerprints: dict[str, str] = {}
        self._scan_counter = 0
        self._pending_verifications: dict[str, dict[str, Any]] = {}
        self._daily_investigation_count = 0
        self._daily_investigation_reset = time.time()
        self._scan_lock = asyncio.Lock()
        self._last_security_followup: float = 0.0
        self._recent_fix_ids: set[str] = set()
        self._investigation_tasks: list[asyncio.Task] = []
        self._generator_task: asyncio.Task | None = None
        self._last_daily_run: float = 0.0
        self._last_weekly_run: float = 0.0
        self._transient_counts: dict[str, int] = {}
        self._noise_threshold = get_settings().monitor.noise_threshold
        self._noise_suppressed = 0
        self._noise_suppressed_last_scan = 0
        self._cost_budget_cache: tuple[float, float] | None = None
        self._session_id = f"mon-{uuid.uuid4().hex[:12]}"

        # Shared Anthropic client (async). NOT used by the proactive
        # investigation path in investigation_runner.py — that path wraps
        # each call in asyncio.wait_for() and needs a fresh, disposable client
        # per attempt so a timeout can't corrupt a connection pool that later
        # investigations depend on. This shared instance remains for callers
        # that don't cancel it mid-stream (e.g. handoff/plan execution).
        from ..agent import create_async_client

        self._client = create_async_client()

        # Initialize database schema once
        get_monitor_repo().ensure_scan_runs_table()

    # ── Subscriber management ─────────────────────────────────────────────

    async def subscribe(self, client: MonitorClient) -> None:
        async with self._subscribers_lock:
            if client not in self._subscribers:
                self._subscribers.append(client)
                logger.info(
                    "ClusterMonitor: client subscribed (total=%d, trust=%d)",
                    len(self._subscribers),
                    client.trust_level,
                )

    async def unsubscribe(self, client: MonitorClient) -> None:
        async with self._subscribers_lock:
            try:
                self._subscribers.remove(client)
            except ValueError:
                logger.debug("Attempted to unsubscribe client that was not registered")
            logger.info("ClusterMonitor: client unsubscribed (total=%d)", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def get_investigation_budget(self) -> tuple[int, int]:
        """Return (used_today, max_daily) for the investigation budget."""
        return (self._daily_investigation_count, get_settings().monitor.max_daily_investigations)

    @property
    def effective_trust_level(self) -> int:
        """The server's configured trust level, raised by any subscriber above it.

        It used to be "max among subscribers, or 1 if none" — and subscribers
        are browser tabs. Since ``auto_fix`` is only called at trust >= 2, that
        made remediation depend on somebody having the UI open: no tab meant
        trust 1, ``auto_fix`` was never entered, and the agent quietly did
        nothing about problems it had correctly diagnosed. Measured on the
        reference cluster after days of running: 2,528 investigations, zero
        actions, and not one auto-fix line in the logs.

        This is the same bug as the scan loop only running while a client was
        connected. That half was fixed; this half was left behind.

        A subscriber may still raise the level above the configured one — that
        is a human electing to supervise more closely — but it can no longer
        lower it by being absent.
        """
        configured = get_settings().monitor.max_trust_level
        if not self._subscribers:
            return configured
        return max(configured, max(c.trust_level for c in self._subscribers))

    @property
    def effective_auto_fix_categories(self) -> set[str]:
        """What may be auto-fixed: everything the server can do, plus subscribers'.

        Same reasoning as the trust level. An empty union meant that with no tab
        open the allowed set was empty, so a trust-3 deployment filtered every
        category out and fixed nothing.
        """
        from .autofix import AUTO_FIX_HANDLERS

        result: set[str] = set(AUTO_FIX_HANDLERS)
        for c in self._subscribers:
            result |= c.auto_fix_categories
        return result

    @property
    def effective_disabled_scanners(self) -> set[str]:
        """Intersection of all subscribers' disabled scanners.

        A scanner is only disabled globally if ALL subscribers have disabled it.
        If any subscriber still wants it, we run it and let per-client filtering handle the rest.
        """
        if not self._subscribers:
            return set()
        result = set(self._subscribers[0].disabled_scanners)
        for c in self._subscribers[1:]:
            result &= c.disabled_scanners
        return result

    async def broadcast(self, data: dict) -> None:
        """Send data to all subscribers via their on_event() method."""
        async with self._subscribers_lock:
            subs = list(self._subscribers)
        for client in subs:
            try:
                await client.on_event(data)
            except Exception:
                logger.debug("Failed to send to subscriber", exc_info=True)

    async def _broadcast_raw(self, data: dict) -> None:
        """Send data to all subscribers without per-client filtering (for non-finding events)."""
        async with self._subscribers_lock:
            subs = list(self._subscribers)
        for client in subs:
            try:
                await client.send(data)
            except Exception:
                logger.debug("Failed to send to subscriber", exc_info=True)

    # ── Memory stats ──────────────────────────────────────────────────────

    def memory_stats(self) -> dict:
        return {
            "last_findings": len(self._last_findings),
            "recent_fixes": len(self._recent_fixes),
            "fix_attempt_counts": len(self._fix_attempt_counts),
            "recent_investigations": len(self._recent_investigations),
            "investigation_fingerprints": len(self._investigation_fingerprints),
            "pending_verifications": len(self._pending_verifications),
            "transient_counts": len(self._transient_counts),
            "recent_fix_ids": len(self._recent_fix_ids),
            "investigation_tasks": len(self._investigation_tasks),
            "scan_counter": self._scan_counter,
            "noise_suppressed": self._noise_suppressed,
            "noise_suppressed_last_scan": self._noise_suppressed_last_scan,
            "subscribers": len(self._subscribers),
        }

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def cancel_pending_investigations(self) -> None:
        for task in self._investigation_tasks:
            if not task.done():
                task.cancel()
        self._investigation_tasks.clear()
        try:
            await self._client.close()
        except Exception:
            logger.debug("Failed to close client", exc_info=True)

    # ── Auto-fix ──────────────────────────────────────────────────────────

    async def auto_fix(self, findings: list[dict]) -> None:
        """Attempt to auto-fix findings when trust level permits."""
        # Always call the accessor. It reads the persisted operational_flags row,
        # so the pause is visible across replicas and survives a restart; and
        # reading it through a function rather than a module-level name avoids
        # re-introducing the by-value import bug that made this check see a
        # permanently-False copy while /health reported the pause as active.
        if is_autofix_paused():
            logger.info("Auto-fix paused — skipping")
            return

        if not get_settings().monitor.autofix_enabled:
            logger.info("Auto-fix disabled via PULSE_AGENT_AUTOFIX_ENABLED — skipping")
            return

        trust_level = self.effective_trust_level
        auto_fix_categories = self.effective_auto_fix_categories

        fixes_this_cycle = 0
        MAX_FIXES_PER_CYCLE = 3

        fixable = [f for f in findings if f.get("autoFixable")]
        logger.info(
            "Auto-fix: %d/%d findings are auto-fixable, trust=%d, categories=%s",
            len(fixable),
            len(findings),
            trust_level,
            auto_fix_categories,
        )

        for finding in findings:
            if fixes_this_cycle >= MAX_FIXES_PER_CYCLE:
                logger.info(
                    "Auto-fix rate limit reached (%d/%d), skipping remaining findings",
                    fixes_this_cycle,
                    MAX_FIXES_PER_CYCLE,
                )
                break

            if not finding.get("autoFixable"):
                continue

            category = finding.get("category", "")

            if trust_level == 3 and category not in auto_fix_categories:
                logger.info("Auto-fix: skipping %s (category %s not in allowed list)", finding.get("title"), category)
                continue

            logger.info("Auto-fix: attempting fix for %s (category=%s)", finding.get("title"), category)

            resources = finding.get("resources", [])
            resource_key = ""
            if resources:
                r = resources[0]
                name = r.get("name", "")
                kind = r.get("kind", "")
                if kind == "Pod":
                    from .confidence import _strip_pod_hash

                    name = _strip_pod_hash(name)
                resource_key = f"{kind}:{r.get('namespace', '')}:{name}"

            from .fix_planner import (
                default_fix_plan,
                get_investigation_for_finding,
                plan_fix,
            )
            from .fix_planner import (
                execute_fix as execute_targeted_fix,
            )

            investigation = get_investigation_for_finding(finding.get("id", ""))
            targeted_plan = None
            if investigation:
                targeted_plan = plan_fix(investigation, finding)
                if targeted_plan:
                    logger.info(
                        "Intelligent fix available: strategy=%s cause=%s confidence=%.2f for %s",
                        targeted_plan.strategy,
                        targeted_plan.cause_category,
                        targeted_plan.confidence,
                        resource_key,
                    )

            if not targeted_plan:
                targeted_plan = default_fix_plan(category, finding)
                if targeted_plan:
                    logger.info(
                        "Fast-path fix: strategy=%s for %s (no investigation needed)",
                        targeted_plan.strategy,
                        resource_key,
                    )

            if not targeted_plan:
                if investigation:
                    logger.info(
                        "Auto-fix skipped: investigation exists but no targeted strategy (confidence=%.2f) for %s",
                        float(investigation.get("confidence", 0)),
                        resource_key,
                    )
                continue
            if resource_key and resource_key in self._recent_fixes:
                cooldown_remaining = 300 - (time.time() - self._recent_fixes[resource_key])
                if cooldown_remaining > 0:
                    logger.info(
                        "Auto-fix cooldown: %s was fixed %.0fs ago, skipping (%.0fs remaining)",
                        resource_key,
                        time.time() - self._recent_fixes[resource_key],
                        cooldown_remaining,
                    )
                    continue

            if resource_key and self._fix_attempt_counts.get(resource_key, 0) >= self._MAX_FIX_ATTEMPTS:
                logger.info(
                    "Auto-fix exhausted: %s already attempted %d times — needs manual intervention",
                    resource_key,
                    self._fix_attempt_counts[resource_key],
                )
                continue

            if category == "crashloop" and resources:
                r = resources[0]
                if r.get("kind") == "Pod":
                    try:
                        core = get_core_client()
                        pod = core.read_namespaced_pod(r["name"], r.get("namespace", "default"))
                        if not pod.metadata.owner_references:
                            logger.warning(
                                "Auto-fix skipped: Pod %s/%s has no ownerReferences (bare pod, won't be recreated)",
                                r.get("namespace", "default"),
                                r["name"],
                            )
                            continue
                    except Exception as e:
                        from kubernetes.client.rest import ApiException as _ApiEx

                        if isinstance(e, _ApiEx) and e.status == 404:
                            logger.info("Auto-fix: pod gone (404) for %s — resolving", finding["id"])
                            _resolve_finding_inbox(finding.get("id", ""), finding)
                        else:
                            logger.warning(
                                "Auto-fix skipped: could not verify ownerReferences for %s: %s", r.get("name"), e
                            )
                        continue

            confidence = _estimate_auto_fix_confidence(finding, self._recent_fixes)

            if targeted_plan and targeted_plan.strategy == "require_human_review":
                try:
                    existing = get_monitor_repo().check_existing_human_review(finding["id"])
                    if existing:
                        continue
                except Exception:
                    logger.debug("Failed to check for existing human_review action", exc_info=True)
                action_report = _make_action_report(
                    finding_id=finding["id"],
                    tool="require_human_review",
                    inp={"category": category, "resources": resources},
                    status="proposed",
                    reasoning=f"Manual fix required: {targeted_plan.description}",
                    confidence=confidence,
                )
                action_report["fixStrategy"] = targeted_plan.strategy
                action_report["causeCategory"] = targeted_plan.cause_category
                action_report["fixDescription"] = targeted_plan.description
                await self._broadcast_raw(action_report)
                save_action(action_report, category=category, resources=resources, finding=finding)
                continue

            action_report = _make_action_report(
                finding_id=finding["id"],
                tool="",
                inp={"category": category, "resources": resources},
                status="proposed" if trust_level == 2 else "executing",
                reasoning=f"Auto-fix for {category}: {finding.get('title', '')} (confidence={confidence:.2f})",
                confidence=confidence,
            )
            if targeted_plan:
                action_report["fixStrategy"] = targeted_plan.strategy
                action_report["causeCategory"] = targeted_plan.cause_category
                action_report["fixDescription"] = targeted_plan.description

            # Ask-first mode: broadcast proposal and wait for first approval from ANY subscriber
            if trust_level == 2:
                async with self._subscribers_lock:
                    nobody_to_ask = not self._subscribers
                if nobody_to_ask:
                    # There is no one to answer. Record the proposal and move
                    # on: waiting 120 seconds for an approval that cannot
                    # arrive would stall a 65-second scan loop, and executing
                    # unsupervised because nobody is watching is the opposite
                    # of what trust level 2 means. The proposal persists in fix
                    # history, where an operator can approve it later.
                    action_report["reasoning"] += " — proposed while nobody was connected to approve it"
                    await self._broadcast_raw(action_report)
                    save_action(action_report, category=category, resources=resources, finding=finding)
                    # The one notification that asks for something back. If
                    # nobody was connected to approve it, nobody is going to
                    # find it by looking either.
                    await notify_fix_proposed(action_report, finding)
                    logger.info(
                        "Auto-fix proposed (no subscriber to approve): %s",
                        finding.get("title", "")[:80],
                    )
                    continue

                await self._broadcast_raw(action_report)
                loop = asyncio.get_running_loop()
                approval_future = loop.create_future()
                # Register the pending approval on ALL subscribers
                async with self._subscribers_lock:
                    for client in self._subscribers:
                        client._pending_action_approvals[action_report["id"]] = approval_future

                try:
                    approved = bool(await asyncio.wait_for(approval_future, timeout=120))
                except TimeoutError:
                    approved = False
                finally:
                    # Clean up from all subscribers
                    async with self._subscribers_lock:
                        for client in self._subscribers:
                            client._pending_action_approvals.pop(action_report["id"], None)

                if not approved:
                    action_report["status"] = "failed"
                    action_report["error"] = "Rejected by user or approval timed out"
                    if _METRICS_AVAILABLE:
                        AUTOFIX_TOTAL.labels(outcome="skipped").inc()
                    await self._broadcast_raw(action_report)
                    save_action(
                        action_report,
                        category=category,
                        resources=resources,
                        finding=finding,
                    )
                    continue

                action_report["status"] = "executing"
            else:
                logger.warning(
                    "Auto-fix executing WITHOUT confirmation gate (trust_level=%d, category=%s, resource=%s). "
                    "This is by design for autonomous operation.",
                    trust_level,
                    category,
                    resource_key,
                )

            await self._broadcast_raw(action_report)

            start_ms = _ts()
            try:
                tool, before_state, after_state = await asyncio.to_thread(execute_targeted_fix, targeted_plan)
                duration_ms = _ts() - start_ms

                action_report["tool"] = tool
                action_report["status"] = "completed"
                action_report["beforeState"] = before_state
                action_report["afterState"] = after_state
                action_report["durationMs"] = duration_ms
                if _METRICS_AVAILABLE:
                    AUTOFIX_TOTAL.labels(outcome="success").inc()
                fixes_this_cycle += 1
                self._recent_fix_ids.add(finding["id"])

                if resource_key:
                    self._recent_fixes[resource_key] = time.time()
                    self._fix_attempt_counts[resource_key] = self._fix_attempt_counts.get(resource_key, 0) + 1
                self._pending_verifications[action_report["id"]] = {
                    "action_id": action_report["id"],
                    "finding_id": finding["id"],
                    "category": category,
                    "resources": resources,
                    "target_scan": self._scan_counter + 1,
                }

                logger.info(
                    "Auto-fix completed: category=%s finding=%s tool=%s duration=%dms (%d/%d this cycle)",
                    category,
                    finding["id"],
                    tool,
                    duration_ms,
                    fixes_this_cycle,
                    MAX_FIXES_PER_CYCLE,
                )

                from ..context_bus import ContextEntry, get_context_bus

                bus = get_context_bus()
                bus.publish(
                    ContextEntry(
                        source="monitor",
                        category="fix",
                        summary=f"Auto-fixed {category}: {finding.get('title', '')}",
                        details={"fix_applied": tool, "before_state": before_state, "after_state": after_state},
                        namespace=resources[0].get("namespace", "") if resources else "",
                        resources=resources,
                    )
                )
            except Exception as e:
                duration_ms = _ts() - start_ms

                from kubernetes.client.rest import ApiException as _ApiException

                if isinstance(e, _ApiException) and e.status == 404:
                    logger.info(
                        "Auto-fix: resource gone (404) for %s — resolving finding",
                        finding["id"],
                    )
                    action_report["status"] = "completed"
                    action_report["afterState"] = "Resource no longer exists — resolved"
                    action_report["durationMs"] = duration_ms
                    self._recent_fix_ids.add(finding["id"])
                    _resolve_finding_inbox(finding["id"], finding)
                    if _METRICS_AVAILABLE:
                        AUTOFIX_TOTAL.labels(outcome="success").inc()
                else:
                    action_report["status"] = "failed"
                    action_report["error"] = str(e)
                    action_report["durationMs"] = duration_ms
                    if _METRICS_AVAILABLE:
                        AUTOFIX_TOTAL.labels(outcome="failure").inc()

                    logger.info(
                        "Auto-fix failed: category=%s finding=%s error=%s",
                        category,
                        finding["id"],
                        e,
                    )

            await self._broadcast_raw(action_report)

            save_action(
                action_report,
                category=category,
                resources=resources,
                finding=finding,
            )

    # ── Plan execution ────────────────────────────────────────────────────

    async def _try_plan_execution(self, finding: dict) -> bool:
        from .plan_executor import try_plan_execution

        return await try_plan_execution(self, finding)

    # ── Investigations ────────────────────────────────────────────────────

    async def run_investigations(self, findings: list[dict]) -> None:
        from .investigation_runner import run_investigations

        await run_investigations(self, findings)

    # ── Verification ──────────────────────────────────────────────────────

    async def process_verifications(self, findings: list[dict]) -> None:
        from .verification_pipeline import process_verifications

        await process_verifications(self, findings)

    # ── Scan ──────────────────────────────────────────────────────────────

    async def run_scan(self) -> None:
        async with self._scan_lock:
            await self._run_scan_locked()

    async def _run_scan_locked(self) -> None:
        logger.info("Running cluster scan...")
        scan_start = time.time()
        self._scan_counter += 1
        self._noise_suppressed_last_scan = 0

        self._investigation_tasks = [t for t in self._investigation_tasks if not t.done()]

        try:
            from ..dependency_graph import get_dependency_graph

            get_dependency_graph().refresh_from_cluster()
        except Exception:
            logger.debug("Dependency graph refresh failed", exc_info=True)

        eviction_cutoff = scan_start - 3600
        self._recent_fixes = {k: v for k, v in self._recent_fixes.items() if v > eviction_cutoff}
        self._fix_attempt_counts = {k: v for k, v in self._fix_attempt_counts.items() if k in self._recent_fixes}
        self._recent_investigations = {k: v for k, v in self._recent_investigations.items() if v > eviction_cutoff}
        all_findings: list[dict] = []
        scanner_results: list[dict] = []

        shared_resources: dict = {}
        try:
            from ..async_k8s import get_async_core_client, safe_async

            async_core = await get_async_core_client()
            shared_pods = await safe_async(async_core.list_pod_for_all_namespaces())
            if shared_pods is not None and not isinstance(shared_pods, str):
                shared_resources["pods"] = shared_pods
        except Exception as e:
            logger.error("Failed to fetch shared pod list: %s", e)

        # Use intersection of all subscribers' disabled scanners for the global filter
        globally_disabled = self.effective_disabled_scanners

        active_scanners = [
            s
            for s in get_all_scanner_instances()
            if s.meta.name not in globally_disabled and self._scan_counter % s.meta.scan_every == 0
        ]

        async def _run_scanner(scanner):
            scanner_start = time.monotonic()
            meta = scanner.meta
            try:
                with scanning(meta.name):
                    if getattr(scanner, "is_async", False):
                        findings = await scanner.async_scan(shared_resources)
                    else:
                        findings = await asyncio.to_thread(scanner.scan, shared_resources)
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                # A scanner that caught its own error still returns a list, and
                # an empty list is what a healthy scan of a healthy cluster
                # returns too. Only the scanner knows which happened, so take
                # its word for it rather than reading "clean" off the shape.
                self_reported = get_reported_failure(meta.name)
                if self_reported:
                    status = "error"
                elif findings:
                    status = "warning"
                else:
                    status = "clean"
                result = {
                    "name": meta.name,
                    "displayName": meta.display_name,
                    "description": meta.description,
                    "duration_ms": elapsed_ms,
                    "findings_count": len(findings) if isinstance(findings, list) else 0,
                    "checks": list(meta.checks),
                    "status": status,
                }
                if self_reported:
                    result["error"] = self_reported
                return {
                    "result": result,
                    "findings": findings if isinstance(findings, list) else [],
                }
            except Exception as e:
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                logger.error("Scanner %s failed: %s", meta.name, e)
                return {
                    "result": {
                        "name": meta.name,
                        "displayName": meta.display_name,
                        "description": meta.description,
                        "duration_ms": elapsed_ms,
                        "findings_count": 0,
                        "status": "error",
                        "error": str(e)[:100],
                        "checks": list(meta.checks),
                    },
                    "findings": [],
                }

        reset_reported_failures()
        parallel_results = await asyncio.gather(*[_run_scanner(s) for s in active_scanners])
        for pr in parallel_results:
            scanner_results.append(pr["result"])
            all_findings.extend(pr["findings"])
            if _METRICS_AVAILABLE:
                SCANNER_RUNS_TOTAL.labels(scanner=pr["result"]["name"]).inc()

        # Deduplicate
        current_keys = set()
        new_findings = []
        for f in all_findings:
            key = _finding_key(f)
            current_keys.add(key)
            if key not in self._last_findings:
                transient_count = self._transient_counts.get(key, 0)
                if transient_count >= 3:
                    noise_score = min(1.0, round(transient_count * 0.2, 2))
                elif transient_count > 0:
                    noise_score = round(transient_count * 0.1, 2)
                else:
                    noise_score = 0.0
                f["noiseScore"] = noise_score

                if noise_score >= self._noise_threshold:
                    logger.debug(
                        "Suppressing noisy finding: %s (noiseScore=%.2f, transient_count=%d)",
                        f.get("title", "")[:40],
                        noise_score,
                        transient_count,
                    )
                    self._noise_suppressed += 1
                    self._noise_suppressed_last_scan += 1
                    self._last_findings[key] = f
                    continue

                new_findings.append(f)
                self._last_findings[key] = f
                self._first_seen.setdefault(key, int(time.time()))

        if len(self._last_findings) > self._MAX_FINDINGS:
            excess = len(self._last_findings) - self._MAX_FINDINGS
            oldest_keys = list(self._last_findings.keys())[:excess]
            for k in oldest_keys:
                del self._last_findings[k]

        # Episode correlation. Runs on every finding still standing this cycle,
        # not just the new ones: a cause detected three cycles ago must still
        # be able to absorb a symptom that only appeared now.
        try:
            opened = await asyncio.to_thread(self._correlate_episodes, list(self._last_findings.values()))
            for episode_id, cause in opened:
                # One message for one event. The symptoms underneath it stay
                # silent — see the suppression in webhook._send_webhook.
                await notify_episode_opened(episode_id, cause)
        except Exception:
            logger.exception("Episode correlation failed")

        # Resolution events
        stale_keys = set(self._last_findings.keys()) - current_keys
        for key in stale_keys:
            resolved_finding = self._last_findings.pop(key)
            resolved_by = "self-healed"
            finding_id = resolved_finding.get("id", "")
            if finding_id in self._recent_fix_ids:
                resolved_by = "auto-fix"
                self._recent_fix_ids.discard(finding_id)
            await self._broadcast_raw(
                {
                    "type": "resolution",
                    "findingId": finding_id,
                    "category": resolved_finding.get("category", ""),
                    "title": f"{resolved_finding.get('title', 'Issue')} resolved",
                    "resolvedBy": resolved_by,
                    "timestamp": _ts(),
                }
            )
            self._first_seen.pop(key, None)
            asyncio.get_running_loop().run_in_executor(None, _close_episode_for, resolved_finding)
            if finding_id:
                asyncio.get_running_loop().run_in_executor(None, mark_finding_actions_resolved, finding_id)
                asyncio.get_running_loop().run_in_executor(None, _resolve_finding_inbox, finding_id, resolved_finding)

        # Track transient findings
        for key in stale_keys:
            self._transient_counts[key] = self._transient_counts.get(key, 0) + 1
            self._investigation_fingerprints.pop(key, None)

        if len(self._recent_fix_ids) > 500:
            self._recent_fix_ids = set(list(self._recent_fix_ids)[-500:])
        if len(self._transient_counts) > 1000:
            sorted_keys = sorted(self._transient_counts, key=lambda k: self._transient_counts[k], reverse=True)
            self._transient_counts = {k: self._transient_counts[k] for k in sorted_keys[:500]}
        if len(self._investigation_fingerprints) > 1000:
            self._investigation_fingerprints = {
                k: v for k, v in self._investigation_fingerprints.items() if k in self._last_findings
            }

        for f in new_findings:
            if "confidence" not in f:
                f["confidence"] = _estimate_finding_confidence(f)

        from ..context_bus import ContextEntry, get_context_bus

        bus = get_context_bus()
        for f in new_findings:
            if f.get("severity") == SEVERITY_CRITICAL:
                bus.publish(
                    ContextEntry(
                        source="monitor",
                        category="finding",
                        summary=f"Critical finding: {f.get('title', '')}",
                        details={"severity": f.get("severity"), "category": f.get("category")},
                        namespace=f.get("resources", [{}])[0].get("namespace", ""),
                        resources=f.get("resources", []),
                    )
                )

        # Push new findings via broadcast (per-client scanner filtering applies)
        for f in new_findings:
            await self.broadcast(f)
            if f.get("severity") == SEVERITY_CRITICAL:
                await _send_webhook(f)

        active_ids = [f["id"] for f in all_findings][:500]
        await self._broadcast_raw(
            {
                "type": "findings_snapshot",
                "activeIds": active_ids,
                "timestamp": _ts(),
            }
        )

        scan_duration = time.time() - scan_start
        scan_duration_ms = int(scan_duration * 1000)
        if _METRICS_AVAILABLE:
            SCAN_DURATION_SECONDS.set(scan_duration)
            ACTIVE_FINDINGS.set(len(self._last_findings))
            INVESTIGATION_BUDGET_MAX.set(get_settings().monitor.max_daily_investigations)
        await self._broadcast_raw(
            {
                "type": "monitor_status",
                "activeWatches": [cat for cat, _ in ALL_SCANNERS],
                "lastScan": _ts(),
                "findingsCount": len(self._last_findings),
                "nextScan": _ts() + self.scan_interval * 1000,
            }
        )

        try:
            await get_monitor_repo().async_save_scan_run(
                scan_duration_ms, len(all_findings), json.dumps(scanner_results), self._session_id
            )
        except Exception:
            try:
                get_monitor_repo().save_scan_run(
                    scan_duration_ms, len(all_findings), json.dumps(scanner_results), self._session_id
                )
            except Exception as e:
                logger.debug("Failed to save scan run: %s", e, exc_info=True)

        await self._broadcast_raw(
            {
                "type": "scan_report",
                "scanId": self._scan_counter,
                "duration_ms": scan_duration_ms,
                "total_findings": len(all_findings),
                "scanners": scanner_results,
            }
        )

        logger.info(
            "Scan complete: %d total findings (%d new) in %.1fs",
            len(self._last_findings),
            len(new_findings),
            scan_duration,
        )

        await self.run_investigations(all_findings)

        if self.effective_trust_level >= 2:
            await self.auto_fix(all_findings)

        await self.process_verifications(all_findings)

        await self.process_handoffs()

        try:
            from ..inbox import bridge_finding_to_inbox

            for finding in new_findings:
                bridge_finding_to_inbox(finding)
        except Exception:
            logger.exception("Failed to bridge findings to inbox")

        try:
            from ..inbox import run_generator_cycle

            if self._generator_task is None or self._generator_task.done():
                self._generator_task = asyncio.create_task(asyncio.to_thread(run_generator_cycle))

                def _on_generator_done(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning("Inbox generator cycle failed: %s", exc)

                self._generator_task.add_done_callback(_on_generator_done)
        except Exception:
            logger.exception("Failed to start inbox generator cycle")

        await self._run_flywheel()

    # ── Flywheel ──────────────────────────────────────────────────────────

    async def _run_flywheel(self) -> None:
        from .flywheel import run_flywheel

        await run_flywheel(self)

    # ── Handoffs ──────────────────────────────────────────────────────────

    async def process_handoffs(self) -> None:
        from .handoff_processor import process_handoffs

        await process_handoffs(self)

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run_loop(self) -> None:
        """Main monitor loop — scan periodically until stopped."""
        self.running = True
        await self.run_scan()

        while self.running:
            try:
                await asyncio.sleep(self.scan_interval)
                if self.running:
                    await self.run_scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor loop error: %s", e)
                await asyncio.sleep(30)


# ── Module-level singleton ────────────────────────────────────────────────

_cluster_monitor: ClusterMonitor | None = None
_cluster_monitor_lock = asyncio.Lock()


async def get_cluster_monitor() -> ClusterMonitor:
    """Get or create the singleton ClusterMonitor instance."""
    global _cluster_monitor
    if _cluster_monitor is None:
        async with _cluster_monitor_lock:
            if _cluster_monitor is None:
                _cluster_monitor = ClusterMonitor()
    return _cluster_monitor


def get_cluster_monitor_sync() -> ClusterMonitor | None:
    """Get the singleton ClusterMonitor if it exists (non-async, no creation)."""
    return _cluster_monitor


def reset_cluster_monitor() -> None:
    """Reset the singleton (for testing)."""
    global _cluster_monitor
    _cluster_monitor = None
