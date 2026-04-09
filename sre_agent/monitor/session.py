"""MonitorSession — manages a single /ws/monitor connection with periodic scanning."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from .. import db_schema
from ..config import get_settings
from ..db import get_database
from ..k8s_client import get_core_client, safe
from .actions import save_action, save_investigation, update_action_verification
from .autofix import AUTO_FIX_HANDLERS, _autofix_paused
from .confidence import _estimate_auto_fix_confidence, _estimate_finding_confidence, _finding_key
from .findings import _make_action_report, _ts
from .investigations import (
    _run_proactive_investigation_sync,
    _run_security_followup_sync,
    _sanitize_for_prompt,
)
from .registry import SCANNER_REGISTRY, SEVERITY_CRITICAL, SEVERITY_WARNING
from .scanners import ALL_SCANNERS, _get_all_scanners
from .webhook import _send_webhook

logger = logging.getLogger("pulse_agent.monitor")


# ── Monitor Loop ───────────────────────────────────────────────────────────


class MonitorSession:
    """Manages a single /ws/monitor connection with periodic scanning."""

    def __init__(self, websocket, trust_level: int = 1, auto_fix_categories: list[str] | None = None):
        self.websocket = websocket
        self.trust_level = trust_level
        self.auto_fix_categories = set(auto_fix_categories or [])
        self.running = True
        self.scan_interval = get_settings().scan_interval
        self._last_findings: dict[str, dict] = {}  # deduplicate by title+category
        self._recent_fixes: dict[str, float] = {}  # resource_key -> timestamp for cooldown
        self._pending_action_approvals: dict[str, asyncio.Future] = {}
        self._recent_investigations: dict[str, float] = {}
        self._scan_counter = 0
        self._pending_verifications: dict[str, dict[str, Any]] = {}
        self._daily_investigation_count = 0
        self._daily_investigation_reset = time.time()
        self._scan_lock = asyncio.Lock()  # H1: prevent concurrent scans
        self._last_security_followup: float = 0.0  # cooldown tracker
        self._recent_fix_ids: set[str] = set()  # finding IDs that were auto-fixed (for resolution attribution)
        # Noise learning: track findings that appear then quickly disappear
        self._transient_counts: dict[str, int] = {}  # finding_key -> count of transient appearances
        self._noise_threshold = get_settings().noise_threshold
        self._session_id = f"mon-{uuid.uuid4().hex[:12]}"  # Unique session ID for DB tracking

    def resolve_action_response(self, action_id: str, approved: bool) -> bool:
        """Resolve an outstanding action approval request."""
        future = self._pending_action_approvals.get(action_id)
        if not future or future.done():
            return False
        future.set_result(bool(approved))
        return True

    async def send(self, data: dict) -> bool:
        """Send JSON, return False if connection lost."""
        try:
            await self.websocket.send_json(data)
            return True
        except Exception:
            self.running = False
            return False

    async def auto_fix(self, findings: list[dict]) -> None:
        """Attempt to auto-fix findings when trust level permits.

        Safety guardrails (confirmation gate is NOT used here — by design for
        autonomous operation, see SECURITY.md):
        - Rate limit: max 3 auto-fixes per scan cycle
        - Cooldown: skip resources fixed in the last 5 minutes
        - Bare pod protection: never delete pods without ownerReferences
        """
        if _autofix_paused:
            logger.info("Auto-fix paused — skipping")
            return

        if not get_settings().autofix_enabled:
            logger.info("Auto-fix disabled via PULSE_AGENT_AUTOFIX_ENABLED — skipping")
            return

        fixes_this_cycle = 0
        MAX_FIXES_PER_CYCLE = 3

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

            # Trust level 3: only fix categories in self.auto_fix_categories
            # Trust level 4: fix ALL auto-fixable findings
            if self.trust_level == 3 and category not in self.auto_fix_categories:
                continue

            handler = AUTO_FIX_HANDLERS.get(category)
            if not handler:
                continue

            # Cooldown: skip resources fixed in the last 5 minutes
            resources = finding.get("resources", [])
            resource_key = ""
            if resources:
                r = resources[0]
                resource_key = f"{r.get('kind', '')}:{r.get('namespace', '')}:{r.get('name', '')}"
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

            # Bare pod protection: don't delete pods that have no ownerReferences
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
                        logger.warning(
                            "Auto-fix skipped: could not verify ownerReferences for %s: %s", r.get("name"), e
                        )
                        continue

            confidence = _estimate_auto_fix_confidence(finding, self._recent_fixes)
            action_report = _make_action_report(
                finding_id=finding["id"],
                tool="",
                inp={"category": category, "resources": resources},
                status="proposed" if self.trust_level == 2 else "executing",
                reasoning=f"Auto-fix for {category}: {finding.get('title', '')} (confidence={confidence:.2f})",
                confidence=confidence,
            )

            # Ask-first mode: emit proposal and wait for explicit decision.
            if self.trust_level == 2:
                await self.send(action_report)
                loop = asyncio.get_running_loop()
                approval_future = loop.create_future()
                self._pending_action_approvals[action_report["id"]] = approval_future
                try:
                    approved = bool(await asyncio.wait_for(approval_future, timeout=120))
                except TimeoutError:
                    approved = False
                finally:
                    self._pending_action_approvals.pop(action_report["id"], None)

                if not approved:
                    action_report["status"] = "failed"
                    action_report["error"] = "Rejected by user or approval timed out"
                    await self.send(action_report)
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
                    self.trust_level,
                    category,
                    resource_key,
                )

            # Send executing report
            await self.send(action_report)

            start_ms = _ts()
            try:
                tool, before_state, after_state = await asyncio.to_thread(handler, finding)
                duration_ms = _ts() - start_ms

                # Update report with success
                action_report["tool"] = tool
                action_report["status"] = "completed"
                action_report["beforeState"] = before_state
                action_report["afterState"] = after_state
                action_report["durationMs"] = duration_ms
                fixes_this_cycle += 1
                self._recent_fix_ids.add(finding["id"])

                # Record cooldown timestamp
                if resource_key:
                    self._recent_fixes[resource_key] = time.time()
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

                # Publish fix to shared context bus
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
                action_report["status"] = "failed"
                action_report["error"] = str(e)
                action_report["durationMs"] = duration_ms

                logger.info(
                    "Auto-fix failed: category=%s finding=%s error=%s",
                    category,
                    finding["id"],
                    e,
                )

            # Send completed/failed report
            await self.send(action_report)

            # Persist to fix history
            save_action(
                action_report,
                category=category,
                resources=resources,
                finding=finding,
            )

    async def run_investigations(self, findings: list[dict]) -> None:
        """Run proactive read-only investigations for critical findings."""
        from ..agent import _circuit_breaker

        if _circuit_breaker.is_open:
            logger.info("Skipping proactive investigations: agent circuit breaker open")
            return

        # Daily investigation budget
        _settings = get_settings()
        max_daily = _settings.max_daily_investigations
        if time.time() - self._daily_investigation_reset > 86400:
            self._daily_investigation_count = 0
            self._daily_investigation_reset = time.time()
        if self._daily_investigation_count >= max_daily:
            logger.info(
                "Daily investigation budget exhausted (%d/%d)",
                self._daily_investigation_count,
                max_daily,
            )
            return

        max_per_scan = _settings.investigations_max_per_scan
        timeout_seconds = _settings.investigation_timeout
        cooldown_seconds = _settings.investigation_cooldown
        allowed_categories = {
            item.strip()
            for item in os.environ.get(
                "PULSE_AGENT_INVESTIGATION_CATEGORIES",
                "crashloop,workloads,nodes,alerts,cert_expiry,scheduling,oom,image_pull,operators,daemonsets,hpa",
            ).split(",")
            if item.strip()
        }

        security_followup_enabled = _settings.security_followup
        security_followup_cooldown = 600  # 10 minutes
        security_followup_done_this_scan = False

        investigations_run = 0
        now = time.time()
        for finding in findings:
            if investigations_run >= max_per_scan:
                break
            if finding.get("severity") not in (SEVERITY_CRITICAL, SEVERITY_WARNING):
                continue
            if finding.get("category") not in allowed_categories:
                continue

            key = _finding_key(finding)
            last_time = self._recent_investigations.get(key, 0.0)
            if now - last_time < cooldown_seconds:
                continue

            report = {
                "type": "investigation_report",
                "id": f"i-{uuid.uuid4().hex[:12]}",
                "findingId": finding.get("id", ""),
                "category": finding.get("category", ""),
                "status": "failed",
                "summary": "",
                "suspectedCause": "",
                "recommendedFix": "",
                "confidence": 0.0,
                "timestamp": _ts(),
            }
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_run_proactive_investigation_sync, finding),
                    timeout=timeout_seconds,
                )
                report.update(
                    {
                        "status": "completed",
                        "summary": result.get("summary", ""),
                        "suspectedCause": result.get("suspectedCause", ""),
                        "recommendedFix": result.get("recommendedFix", ""),
                        "confidence": result.get("confidence", 0.0),
                    }
                )
                investigations_run += 1
                self._daily_investigation_count += 1
                self._recent_investigations[key] = now

                # Publish investigation result to shared context bus
                from ..context_bus import ContextEntry, get_context_bus

                bus = get_context_bus()
                bus.publish(
                    ContextEntry(
                        source="monitor",
                        category="investigation",
                        summary=f"Investigated {finding.get('category')}: {result.get('summary', '')}",
                        details={
                            "suspected_cause": result.get("suspectedCause", ""),
                            "recommended_fix": result.get("recommendedFix", ""),
                            "confidence": result.get("confidence", 0),
                        },
                        namespace=finding.get("resources", [{}])[0].get("namespace", ""),
                        resources=finding.get("resources", []),
                    )
                )

                # Security followup: max 1 per scan, 10min cooldown
                if (
                    security_followup_enabled
                    and not security_followup_done_this_scan
                    and now - self._last_security_followup >= security_followup_cooldown
                ):
                    try:
                        sec_result = await asyncio.wait_for(
                            asyncio.to_thread(_run_security_followup_sync, finding),
                            timeout=timeout_seconds,
                        )
                        report["securityFollowup"] = {
                            "issues": sec_result.get("security_issues", []),
                            "riskLevel": sec_result.get("risk_level", "unknown"),
                        }
                        security_followup_done_this_scan = True
                        self._last_security_followup = now
                        logger.info("Security followup completed for finding %s", finding.get("id", ""))
                    except TimeoutError:
                        logger.warning("Security followup timed out for finding %s", finding.get("id", ""))
                    except Exception as e:
                        logger.warning("Security followup failed for finding %s: %s", finding.get("id", ""), e)

                # Auto-learn from high-confidence investigations (Improvement #2)
                if get_settings().memory and result.get("confidence", 0) >= 0.7:
                    try:
                        from ..memory import get_manager

                        manager = get_manager()
                        if manager:
                            inv_namespace = ""
                            inv_resource_type = ""
                            f_resources = finding.get("resources", [])
                            if f_resources:
                                inv_namespace = f_resources[0].get("namespace", "")
                                inv_resource_type = f_resources[0].get("kind", "").lower()
                            inv_incident = {
                                "query": f"Investigation: {finding.get('title', '')}",
                                "tool_sequence": ["proactive_investigation"],
                                "resolution": result.get("summary", ""),
                                "namespace": inv_namespace,
                                "resource_type": inv_resource_type,
                                "error_type": finding.get("category", ""),
                            }
                            manager.store_incident(inv_incident, confirmed=False)
                            logger.info(
                                "Stored high-confidence investigation: %s (confidence=%.2f)",
                                finding.get("title", ""),
                                result.get("confidence", 0),
                            )
                    except Exception as e:
                        logger.warning("Failed to store investigation: %s", e)

            except TimeoutError:
                report["error"] = f"Investigation timed out after {timeout_seconds}s"
            except Exception as e:
                report["error"] = str(e)

            await self.send(report)
            save_investigation(report, finding)

    async def process_verifications(self, findings: list[dict]) -> None:
        """Verify whether previously applied fixes remained healthy on next scan."""
        if not self._pending_verifications:
            return

        active_by_category: dict[str, set[str]] = {}
        active_ns_category: dict[str, set[str]] = {}  # "ns:category" -> set of resource keys
        for finding in findings:
            category = str(finding.get("category", ""))
            active_by_category.setdefault(category, set())
            for resource in finding.get("resources", []):
                rkey = f"{resource.get('kind', '')}:{resource.get('namespace', '')}:{resource.get('name', '')}"
                active_by_category[category].add(rkey)
                ns_key = f"{resource.get('namespace', '')}:{category}"
                active_ns_category.setdefault(ns_key, set()).add(rkey)

        completed_ids: list[str] = []
        for action_id, payload in self._pending_verifications.items():
            if self._scan_counter < int(payload.get("target_scan", 0)):
                continue

            category = str(payload.get("category", ""))
            resources = payload.get("resources", [])
            matches_active = False
            matched_resource = ""
            ns_improved = False
            for resource in resources:
                key = f"{resource.get('kind', '')}:{resource.get('namespace', '')}:{resource.get('name', '')}"
                if key in active_by_category.get(category, set()):
                    matches_active = True
                    matched_resource = key
                    break
                # Check namespace-level: if any resource in same ns+category is still failing
                # This catches renamed pods (e.g. after a restart)
                ns_key = f"{resource.get('namespace', '')}:{category}"
                ns_active = active_ns_category.get(ns_key, set())
                if ns_active:
                    # Count how many resources the original fix targeted in this namespace
                    orig_ns_count = sum(1 for r in resources if r.get("namespace", "") == resource.get("namespace", ""))
                    if len(ns_active) < orig_ns_count:
                        # Namespace improved — fewer failures despite pod rename
                        ns_improved = True
                        matched_resource = f"{ns_key} (namespace count {orig_ns_count} -> {len(ns_active)})"
                    else:
                        matches_active = True
                        matched_resource = f"{ns_key} (namespace-level match)"
                    break

            if matches_active:
                status = "still_failing"
                evidence = f"Resource still appears in active {category} findings: {matched_resource}"
            elif ns_improved:
                status = "improved"
                evidence = f"Namespace-level improvement in {category} findings: {matched_resource}"
            else:
                status = "verified"
                evidence = f"No active {category} findings for affected resources on verification scan"

            verification_report = {
                "type": "verification_report",
                "id": f"v-{uuid.uuid4().hex[:12]}",
                "actionId": action_id,
                "findingId": payload.get("finding_id", ""),
                "status": status,
                "evidence": evidence,
                "timestamp": _ts(),
            }
            await self.send(verification_report)
            update_action_verification(action_id, status, evidence)

            # Confidence calibration: update investigation confidence based on verification outcome
            try:
                db = get_database()
                finding_id = payload.get("finding_id", "")
                if finding_id:
                    inv = db.fetchone("SELECT id, confidence FROM investigations WHERE finding_id = ?", (finding_id,))
                    if inv:
                        if status == "verified":
                            new_conf = min(1.0, (inv["confidence"] or 0.5) + 0.05)
                        else:  # still_failing
                            new_conf = max(0.0, (inv["confidence"] or 0.5) - 0.1)
                        db.execute("UPDATE investigations SET confidence = ? WHERE id = ?", (new_conf, inv["id"]))
                        db.commit()
            except Exception as e:
                logger.debug("Failed to update investigation confidence: %s", e)

            # Publish verification to shared context bus
            from ..context_bus import ContextEntry, get_context_bus

            bus = get_context_bus()
            bus.publish(
                ContextEntry(
                    source="monitor",
                    category="verification",
                    summary=f"Verification {status}: {evidence}",
                    details={"status": status, "evidence": evidence},
                )
            )

            # Auto-learn from verified fixes (Improvement #1)
            if status == "verified" and get_settings().memory:
                try:
                    from ..memory import get_manager

                    manager = get_manager()
                    if manager:
                        # Extract namespace/resource_type from resources
                        namespace = ""
                        resource_type = ""
                        if resources:
                            r0 = resources[0]
                            namespace = r0.get("namespace", "")
                            resource_type = r0.get("kind", "").lower()
                        incident = {
                            "query": f"Auto-fix for {category} finding",
                            "tool_sequence": [payload.get("tool", "unknown") if payload.get("tool") else category],
                            "resolution": f"Applied {payload.get('tool', category)} — verified healthy on next scan",
                            "namespace": namespace,
                            "resource_type": resource_type,
                            "error_type": category,
                        }
                        manager.store_incident(incident, confirmed=True)
                        logger.info("Auto-learned runbook from verified fix: %s", category)
                except Exception as e:
                    logger.warning("Failed to auto-learn from fix: %s", e)

            completed_ids.append(action_id)

        for action_id in completed_ids:
            self._pending_verifications.pop(action_id, None)

    async def run_scan(self) -> None:
        """Run all scanners and push new findings."""
        async with self._scan_lock:  # H1: prevent re-entrant scans
            await self._run_scan_locked()

    async def _run_scan_locked(self) -> None:
        """Internal scan body — must be called under self._scan_lock."""
        logger.info("Running cluster scan...")
        scan_start = time.time()
        self._scan_counter += 1

        # Evict stale entries older than 1 hour from cooldown/investigation caches
        eviction_cutoff = scan_start - 3600
        self._recent_fixes = {k: v for k, v in self._recent_fixes.items() if v > eviction_cutoff}
        self._recent_investigations = {k: v for k, v in self._recent_investigations.items() if v > eviction_cutoff}
        all_findings: list[dict] = []
        scanner_results: list[dict] = []

        # Fetch pods once and share across pod-based scanners (H1)
        _POD_SCANNERS = {"crashloop", "oom", "image_pull"}
        shared_pods = None
        try:
            shared_pods = await asyncio.to_thread(lambda: safe(lambda: get_core_client().list_pod_for_all_namespaces()))
        except Exception as e:
            logger.error("Failed to fetch shared pod list: %s", e)

        # Run all standard scanners with timing
        for category, scanner in _get_all_scanners():
            scanner_start = time.monotonic()
            try:
                if category in _POD_SCANNERS and shared_pods is not None:
                    findings = await asyncio.to_thread(scanner, shared_pods)
                else:
                    findings = await asyncio.to_thread(scanner)
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                registry = SCANNER_REGISTRY.get(category, {})
                scanner_results.append(
                    {
                        "name": category,
                        "displayName": registry.get("displayName", category),
                        "description": registry.get("description", ""),
                        "duration_ms": elapsed_ms,
                        "findings_count": len(findings) if isinstance(findings, list) else 0,
                        "checks": registry.get("checks", []),
                        "status": "warning" if findings else "clean",
                    }
                )
                all_findings.extend(findings)
            except Exception as e:
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                logger.error("Scanner %s failed: %s", category, e)
                scanner_results.append(
                    {
                        "name": category,
                        "displayName": SCANNER_REGISTRY.get(category, {}).get("displayName", category),
                        "description": SCANNER_REGISTRY.get(category, {}).get("description", ""),
                        "duration_ms": elapsed_ms,
                        "findings_count": 0,
                        "status": "error",
                        "error": str(e)[:100],
                        "checks": SCANNER_REGISTRY.get(category, {}).get("checks", []),
                    }
                )

        # Run security posture check every 3rd scan
        if self._scan_counter % 3 == 0:
            from ..security_tools import get_security_summary

            scanner_start = time.monotonic()
            try:
                sec_result = await asyncio.to_thread(get_security_summary)
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                # Parse security summary to extract findings count
                findings_count = 0
                if "CRITICAL:" in sec_result or "WARNING:" in sec_result:
                    findings_count = sec_result.count("CRITICAL:") + sec_result.count("WARNING:")
                registry = SCANNER_REGISTRY.get("security", {})
                scanner_results.append(
                    {
                        "name": "security",
                        "displayName": registry.get("displayName", "Security Posture"),
                        "description": registry.get("description", ""),
                        "duration_ms": elapsed_ms,
                        "findings_count": findings_count,
                        "checks": registry.get("checks", []),
                        "status": "warning" if findings_count > 0 else "clean",
                    }
                )
            except Exception as e:
                elapsed_ms = int((time.monotonic() - scanner_start) * 1000)
                logger.error("Security scanner failed: %s", e)
                scanner_results.append(
                    {
                        "name": "security",
                        "displayName": "Security Posture",
                        "description": "Comprehensive security check",
                        "duration_ms": elapsed_ms,
                        "findings_count": 0,
                        "status": "error",
                        "error": str(e)[:100],
                        "checks": SCANNER_REGISTRY.get("security", {}).get("checks", []),
                    }
                )

        # Deduplicate: only send new/changed findings
        current_keys = set()
        new_findings = []
        for f in all_findings:
            key = _finding_key(f)
            current_keys.add(key)
            if key not in self._last_findings:
                new_findings.append(f)
                self._last_findings[key] = f

        # Remove stale findings (resolved since last scan) and emit resolution events
        stale_keys = set(self._last_findings.keys()) - current_keys
        for key in stale_keys:
            resolved_finding = self._last_findings.pop(key)
            # Determine how it was resolved
            resolved_by = "self-healed"
            finding_id = resolved_finding.get("id", "")
            if finding_id in self._recent_fix_ids:
                resolved_by = "auto-fix"
                self._recent_fix_ids.discard(finding_id)
            await self.send(
                {
                    "type": "resolution",
                    "findingId": finding_id,
                    "category": resolved_finding.get("category", ""),
                    "title": f"{resolved_finding.get('title', 'Issue')} resolved",
                    "resolvedBy": resolved_by,
                    "timestamp": _ts(),
                }
            )

        # Track transient findings for noise learning
        for key in stale_keys:
            self._transient_counts[key] = self._transient_counts.get(key, 0) + 1

        # Cap _recent_fix_ids to prevent unbounded growth on long-running sessions
        if len(self._recent_fix_ids) > 500:
            self._recent_fix_ids = set(list(self._recent_fix_ids)[-500:])
        # Cap transient counts
        if len(self._transient_counts) > 1000:
            # Keep only the most frequent
            sorted_keys = sorted(self._transient_counts, key=self._transient_counts.get, reverse=True)  # type: ignore[arg-type]
            self._transient_counts = {k: self._transient_counts[k] for k in sorted_keys[:500]}

        # Enrich findings with confidence and noise scores (before context bus so consumers get scores)
        for f in new_findings:
            if "confidence" not in f:
                f["confidence"] = _estimate_finding_confidence(f)
            # Compute noise score from transient history
            fkey = _finding_key(f)
            transient_count = self._transient_counts.get(fkey, 0)
            if transient_count >= 3:
                f["noiseScore"] = min(1.0, round(transient_count * 0.2, 2))
            elif transient_count > 0:
                f["noiseScore"] = round(transient_count * 0.1, 2)

        # Publish critical new findings to shared context bus
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

        # Push new findings and send webhook for critical ones
        for f in new_findings:
            if not await self.send(f):
                return
            if f.get("severity") == SEVERITY_CRITICAL:
                await _send_webhook(f)

        # Send snapshot of all active finding IDs so UI can remove stale ones
        # H2: cap activeIds to 500 entries to prevent unbounded growth
        active_ids = [f["id"] for f in all_findings][:500]
        await self.send(
            {
                "type": "findings_snapshot",
                "activeIds": active_ids,
                "timestamp": _ts(),
            }
        )

        # Push monitor status
        scan_duration = time.time() - scan_start
        scan_duration_ms = int(scan_duration * 1000)
        await self.send(
            {
                "type": "monitor_status",
                "activeWatches": [cat for cat, _ in ALL_SCANNERS],
                "lastScan": _ts(),
                "findingsCount": len(self._last_findings),
                "nextScan": _ts() + self.scan_interval * 1000,
            }
        )

        # Save scan run to database
        try:
            db = get_database()
            db.executescript(db_schema.SCAN_RUNS_SCHEMA)  # Ensure table exists
            db.execute(
                "INSERT INTO scan_runs (duration_ms, total_findings, scanner_results, session_id) VALUES (?, ?, ?, ?)",
                (scan_duration_ms, len(all_findings), json.dumps(scanner_results), self._session_id),
            )
            db.commit()
        except Exception as e:
            logger.debug("Failed to save scan run: %s", e, exc_info=True)

        # Emit scan report WebSocket message
        await self.send(
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

        # Ask-first and auto-fix modes
        if self.trust_level >= 2:
            await self.auto_fix(all_findings)

        await self.process_verifications(all_findings)

        await self.process_handoffs()

    async def process_handoffs(self) -> None:
        """Process agent-to-agent handoff requests from the context bus."""
        db = get_database()
        timeout_seconds = get_settings().investigation_timeout

        # Find recent handoff requests (last 5 minutes)
        cutoff = int(time.time() * 1000) - 300_000
        try:
            rows = db.fetchall(
                "SELECT * FROM context_entries WHERE category = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 5",
                ("handoff_request", cutoff),
            )
        except Exception as e:
            logger.error("Failed to query handoff requests: %s", e)
            return

        for row in rows:
            details = row.get("details", "{}")
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except (json.JSONDecodeError, TypeError):
                    details = {}
            target = details.get("target", "")
            namespace = row.get("namespace", "") or details.get("namespace", "")
            context = _sanitize_for_prompt(details.get("context", ""))

            if target == "security_agent" and namespace:
                finding = {
                    "category": "handoff",
                    "title": f"Security scan requested for {_sanitize_for_prompt(namespace)}",
                    "summary": context,
                    "severity": "warning",
                    "resources": [{"kind": "Namespace", "name": namespace, "namespace": namespace}],
                }
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_run_security_followup_sync, finding),
                        timeout=timeout_seconds,
                    )
                    logger.info("Handoff security scan completed for %s", namespace)
                except Exception as e:
                    logger.error("Handoff security scan failed: %s", e)

            elif target == "sre_agent" and namespace:
                finding = {
                    "id": f"f-handoff-{uuid.uuid4().hex[:8]}",
                    "category": "handoff",
                    "title": f"SRE investigation requested: {_sanitize_for_prompt(details.get('kind', ''))}/{_sanitize_for_prompt(details.get('name', ''))}",
                    "summary": context,
                    "severity": "warning",
                    "resources": [
                        {"kind": details.get("kind", ""), "name": details.get("name", ""), "namespace": namespace}
                    ],
                }
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_run_proactive_investigation_sync, finding),
                        timeout=timeout_seconds,
                    )
                    logger.info("Handoff SRE investigation completed for %s", namespace)
                except Exception as e:
                    logger.error("Handoff SRE investigation failed: %s", e)

        # Clean up processed requests
        if rows:
            try:
                db.execute(
                    "DELETE FROM context_entries WHERE category = ? AND timestamp > ?", ("handoff_request", cutoff)
                )
                db.commit()
            except Exception as e:
                logger.error("Failed to clean up handoff requests: %s", e)

    async def run_loop(self) -> None:
        """Main monitor loop — scan periodically until disconnected."""
        # Initial scan immediately
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
                await asyncio.sleep(30)  # Back off on error
