"""Proactive investigation runner — Claude-powered root-cause analysis for findings."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from ..config import get_settings
from .confidence import _finding_key
from .findings import _ts
from .investigations import _run_proactive_investigation, _run_security_followup
from .registry import SEVERITY_CRITICAL, SEVERITY_WARNING

try:
    from ..observability import (
        COST_BUDGET_EXHAUSTION_TOTAL,
        COST_BUDGET_REMAINING_USD,
        INVESTIGATION_BUDGET_REMAINING,
        INVESTIGATIONS_TOTAL,
        TOKEN_PRICES,
    )

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

if TYPE_CHECKING:
    from .cluster_monitor import ClusterMonitor

logger = logging.getLogger("pulse_agent.monitor")


async def run_investigations(monitor: ClusterMonitor, findings: list[dict]) -> None:
    """Run proactive read-only investigations for critical findings."""
    from ..agent import _circuit_breaker

    if _circuit_breaker.is_open:
        logger.info("Skipping proactive investigations: agent circuit breaker open")
        return

    _settings = get_settings()
    max_daily = _settings.monitor.max_daily_investigations
    if time.time() - monitor._daily_investigation_reset > 86400:
        monitor._daily_investigation_count = 0
        monitor._daily_investigation_reset = time.time()
    if monitor._daily_investigation_count >= max_daily:
        logger.info(
            "Daily investigation budget exhausted (%d/%d)",
            monitor._daily_investigation_count,
            max_daily,
        )
        if _METRICS_AVAILABLE:
            INVESTIGATION_BUDGET_REMAINING.set(0)
        return

    # Cost budget gate (cached — only queries DB every 5 minutes)
    _budget_usd = _settings.cost_budget_usd
    if _budget_usd > 0:
        try:
            now_t = time.time()
            cached = monitor._cost_budget_cache
            if cached and (now_t - cached[0]) < 300:
                today_cost = cached[1]
            else:
                from ..repositories import get_analytics_repo

                totals = get_analytics_repo().fetch_token_totals(1)
                if totals and totals["total_incidents"] > 0:
                    _in = int(totals["total_input"])
                    _out = int(totals["total_output"])
                    today_cost = (
                        (_in * TOKEN_PRICES["input"] + _out * TOKEN_PRICES["output"]) / 1_000_000
                        if _METRICS_AVAILABLE
                        else (_in * 15.0 + _out * 75.0) / 1_000_000
                    )
                else:
                    today_cost = 0.0
                monitor._cost_budget_cache = (now_t, today_cost)
            if _METRICS_AVAILABLE:
                COST_BUDGET_REMAINING_USD.set(max(0, _budget_usd - today_cost))
            if today_cost >= _budget_usd:
                logger.warning(
                    "Daily cost budget exceeded ($%.2f / $%.2f) — pausing investigations",
                    today_cost,
                    _budget_usd,
                )
                if _METRICS_AVAILABLE:
                    COST_BUDGET_EXHAUSTION_TOTAL.inc()
                return
        except Exception:
            logger.debug("Cost budget check failed", exc_info=True)

    max_per_scan = _settings.monitor.investigations_max_per_scan
    timeout_seconds = _settings.monitor.investigation_timeout
    cooldown_seconds = _settings.monitor.investigation_cooldown
    allowed_categories = {
        item.strip() for item in _settings.monitor.investigation_categories.split(",") if item.strip()
    }

    security_followup_enabled = _settings.monitor.security_followup
    security_followup_cooldown = 600
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

        noise_score = finding.get("noiseScore", 0.0)
        if noise_score >= monitor._noise_threshold:
            logger.info(
                "Skipping investigation for noisy finding: %s (noiseScore=%.2f)",
                finding.get("title", "")[:40],
                noise_score,
            )
            monitor._noise_suppressed += 1
            monitor._noise_suppressed_last_scan += 1
            continue

        key = _finding_key(finding)
        last_time = monitor._recent_investigations.get(key, 0.0)
        if now - last_time < cooldown_seconds:
            continue

        from .confidence import _finding_content_hash

        content_hash = _finding_content_hash(finding)
        prev_hash = monitor._investigation_fingerprints.get(key)
        if prev_hash == content_hash:
            logger.info(
                "Skipping investigation for unchanged finding: %s (hash=%s)",
                finding.get("title", "")[:40],
                content_hash,
            )
            continue

        try:
            from ..log_fingerprinter import fingerprint_finding

            fps = fingerprint_finding(finding)
            if fps:
                finding["_log_fingerprints"] = fps
                logger.info(
                    "Log fingerprints for %s: %s",
                    finding.get("title", "")[:40],
                    ", ".join(f"{fp['category']}({fp['count']})" for fp in fps[:3]),
                )
        except Exception:
            logger.debug("Log fingerprinting failed for finding", exc_info=True)

        # Spawn plan-based investigation as background task
        try:
            from ..plan_templates import match_template

            template = match_template(category=finding.get("category", ""))
            if template:
                monitor._investigation_tasks = [t for t in monitor._investigation_tasks if not t.done()]
                if len(monitor._investigation_tasks) >= get_settings().monitor.max_concurrent_investigations:
                    logger.info(
                        "Skipping investigation for %s — %d tasks already running",
                        finding.get("title", "")[:40],
                        len(monitor._investigation_tasks),
                    )
                    continue
                monitor._recent_investigations[key] = now
                monitor._investigation_fingerprints[key] = content_hash
                investigations_run += 1
                monitor._daily_investigation_count += 1
                if _METRICS_AVAILABLE:
                    INVESTIGATIONS_TOTAL.inc()
                    INVESTIGATION_BUDGET_REMAINING.set(max(0, max_daily - monitor._daily_investigation_count))
                task = asyncio.create_task(
                    monitor._try_plan_execution(finding),
                    name=f"plan-{finding.get('id', 'unknown')[:12]}",
                )
                monitor._investigation_tasks.append(task)

                finding_ref = finding

                def _on_plan_done(t: asyncio.Task, f=finding_ref) -> None:
                    try:
                        if t.cancelled() or not t.result():
                            logger.warning(
                                "Plan execution failed for %s — finding may need manual investigation",
                                f.get("title", "")[:40],
                            )
                    except Exception:
                        logger.warning("Plan execution raised for %s", f.get("title", "")[:40], exc_info=True)

                task.add_done_callback(_on_plan_done)
                logger.info(
                    "Spawned async investigation for %s (template=%s)",
                    finding.get("title", "")[:40],
                    template.name,
                )
                continue
        except Exception:
            logger.debug("Plan execution spawn failed", exc_info=True)

        report = {
            "type": "investigation_report",
            "id": f"i-{uuid.uuid4().hex[:12]}",
            "finding_id": finding.get("id", ""),
            "findingId": finding.get("id", ""),
            "category": finding.get("category", ""),
            "status": "failed",
            "summary": "",
            "suspected_cause": "",
            "suspectedCause": "",
            "recommended_fix": "",
            "recommendedFix": "",
            "confidence": 0.0,
            "timestamp": _ts(),
        }
        try:
            result = await asyncio.wait_for(
                _run_proactive_investigation(finding, client=monitor._client),
                timeout=timeout_seconds,
            )
            _sc = result.get("suspected_cause", "")
            _rf = result.get("recommended_fix", "")
            report.update(
                {
                    "status": "completed",
                    "summary": result.get("summary", ""),
                    "suspected_cause": _sc,
                    "suspectedCause": _sc,
                    "recommended_fix": _rf,
                    "recommendedFix": _rf,
                    "confidence": result.get("confidence", 0.0),
                }
            )
            investigations_run += 1
            monitor._daily_investigation_count += 1
            if _METRICS_AVAILABLE:
                INVESTIGATIONS_TOTAL.inc()
                INVESTIGATION_BUDGET_REMAINING.set(max(0, max_daily - monitor._daily_investigation_count))
            monitor._recent_investigations[key] = now
            monitor._investigation_fingerprints[key] = content_hash

            from ..context_bus import ContextEntry, get_context_bus

            bus = get_context_bus()
            bus.publish(
                ContextEntry(
                    source="monitor",
                    category="investigation",
                    summary=f"Investigated {finding.get('category')}: {result.get('summary', '')}",
                    details={
                        "suspected_cause": result.get("suspected_cause", ""),
                        "recommended_fix": result.get("recommended_fix", ""),
                        "confidence": result.get("confidence", 0),
                    },
                    namespace=finding.get("resources", [{}])[0].get("namespace", ""),
                    resources=finding.get("resources", []),
                )
            )

            if (
                security_followup_enabled
                and not security_followup_done_this_scan
                and now - monitor._last_security_followup >= security_followup_cooldown
            ):
                try:
                    sec_result = await asyncio.wait_for(
                        _run_security_followup(finding, client=monitor._client),
                        timeout=timeout_seconds,
                    )
                    report["securityFollowup"] = {
                        "issues": sec_result.get("security_issues", []),
                        "riskLevel": sec_result.get("risk_level", "unknown"),
                    }
                    security_followup_done_this_scan = True
                    monitor._last_security_followup = now
                    logger.info("Security followup completed for finding %s", finding.get("id", ""))
                except TimeoutError:
                    logger.warning("Security followup timed out for finding %s", finding.get("id", ""))
                except Exception as e:
                    logger.warning("Security followup failed for finding %s: %s", finding.get("id", ""), e)

            if get_settings().agent.memory and result.get("confidence", 0) >= 0.7:
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

            from ..plan_templates import match_template as _match_tmpl

            _has_template = _match_tmpl(category=finding.get("category", "")) is not None
            if not _has_template and result.get("confidence", 0) >= 0.75:
                try:
                    from ..skill_scaffolder import (
                        save_scaffolded_skill,
                        scaffold_plan_template,
                        scaffold_skill_from_resolution,
                    )

                    skill_content = scaffold_skill_from_resolution(
                        query=finding.get("title", ""),
                        tools_called=["proactive_investigation"],
                        investigation_summary=result.get("summary", ""),
                        root_cause=result.get("suspected_cause", "unknown"),
                        confidence=result.get("confidence", 0),
                    )
                    tokens = finding.get("title", "unknown").lower().split()[:3]
                    skill_name = "-".join(t for t in tokens if t.isalnum())[:40] or "auto-skill"
                    save_scaffolded_skill(skill_content, skill_name)
                    scaffold_plan_template(
                        skill_name=skill_name,
                        plan_phases=["triage", "diagnose", "remediate", "verify"],
                        incident_type=finding.get("category", "unknown"),
                        confidence=result.get("confidence", 0),
                    )
                    logger.info("Scaffolded skill '%s' from novel flat investigation", skill_name)

                    try:
                        from ..eval_scaffolder import scaffold_eval_from_investigation

                        scaffold_eval_from_investigation(
                            skill_name=skill_name,
                            finding=finding,
                            investigation_result=result,
                        )
                    except Exception:
                        logger.debug("Eval scaffolding from investigation failed", exc_info=True)
                except Exception:
                    logger.debug("Skill scaffolding from flat investigation failed", exc_info=True)

        except TimeoutError:
            report["error"] = f"Investigation timed out after {timeout_seconds}s"
        except Exception as e:
            report["error"] = str(e)

        await monitor._broadcast_raw(report)
        from .actions import save_investigation

        save_investigation(report, finding)
