"""Autonomous cluster monitor — scans the cluster on configurable intervals,
pushes findings/predictions/action reports to connected /ws/monitor clients.

Protocol v2 addition. See API_CONTRACT.md for the full specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .k8s_client import get_core_client, get_apps_client, get_custom_client, safe
from .errors import ToolError

logger = logging.getLogger("pulse_agent.monitor")

# ── Types ──────────────────────────────────────────────────────────────────

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def _ts() -> int:
    return int(time.time() * 1000)


def _make_finding(
    severity: str,
    category: str,
    title: str,
    summary: str,
    resources: list[dict],
    auto_fixable: bool = False,
    runbook_id: str | None = None,
) -> dict:
    return {
        "type": "finding",
        "id": f"f-{uuid.uuid4().hex[:12]}",
        "severity": severity,
        "category": category,
        "title": title,
        "summary": summary,
        "resources": resources,
        "autoFixable": auto_fixable,
        "runbookId": runbook_id,
        "timestamp": _ts(),
    }


def _make_prediction(
    category: str,
    title: str,
    detail: str,
    eta: str,
    confidence: float,
    resources: list[dict],
    recommended_action: str | None = None,
) -> dict:
    return {
        "type": "prediction",
        "id": f"p-{uuid.uuid4().hex[:12]}",
        "category": category,
        "title": title,
        "detail": detail,
        "eta": eta,
        "confidence": confidence,
        "resources": resources,
        "recommendedAction": recommended_action,
        "timestamp": _ts(),
    }


def _make_action_report(
    finding_id: str,
    tool: str,
    inp: dict,
    status: str,
    before_state: str = "",
    after_state: str = "",
    error: str | None = None,
    reasoning: str = "",
    duration_ms: int = 0,
) -> dict:
    return {
        "type": "action_report",
        "id": f"a-{uuid.uuid4().hex[:12]}",
        "findingId": finding_id,
        "tool": tool,
        "input": inp,
        "status": status,
        "beforeState": before_state,
        "afterState": after_state,
        "error": error,
        "timestamp": _ts(),
        "reasoning": reasoning,
        "durationMs": duration_ms,
    }


# ── Fix History (SQLite) ──────────────────────────────────────────────────

import sqlite3
import os

_FIX_DB_PATH = os.environ.get("PULSE_AGENT_FIX_DB", os.path.expanduser("~/.pulse_agent/fix_history.db"))

_FIX_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    finding_id TEXT,
    timestamp INTEGER,
    category TEXT,
    tool TEXT,
    input TEXT,
    status TEXT,
    before_state TEXT,
    after_state TEXT,
    error TEXT,
    reasoning TEXT,
    duration_ms INTEGER,
    rollback_available INTEGER DEFAULT 0,
    rollback_action TEXT,
    resources TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_category ON actions(category);
"""


def _get_fix_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_FIX_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_FIX_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_FIX_SCHEMA)
    return conn


def save_action(action: dict, category: str = "", resources: list[dict] | None = None) -> None:
    """Persist an action report to SQLite."""
    try:
        conn = _get_fix_db()
        conn.execute(
            """INSERT OR REPLACE INTO actions
               (id, finding_id, timestamp, category, tool, input, status,
                before_state, after_state, error, reasoning, duration_ms,
                rollback_available, rollback_action, resources)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action["id"],
                action.get("findingId", ""),
                action.get("timestamp", _ts()),
                category,
                action.get("tool", ""),
                json.dumps(action.get("input", {})),
                action.get("status", ""),
                action.get("beforeState", ""),
                action.get("afterState", ""),
                action.get("error"),
                action.get("reasoning", ""),
                action.get("durationMs", 0),
                0,
                "",
                json.dumps(resources or []),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to save action: %s", e)


def get_fix_history(page: int = 1, page_size: int = 20, filters: dict | None = None) -> dict:
    """Retrieve paginated fix history from SQLite."""
    try:
        conn = _get_fix_db()
        where_parts = []
        params: list[Any] = []

        if filters:
            if filters.get("status"):
                where_parts.append("status = ?")
                params.append(filters["status"])
            if filters.get("category"):
                where_parts.append("category = ?")
                params.append(filters["category"])
            if filters.get("since"):
                where_parts.append("timestamp >= ?")
                params.append(filters["since"])
            if filters.get("search"):
                where_parts.append("(tool LIKE ? OR reasoning LIKE ?)")
                params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])

        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        total = conn.execute(f"SELECT COUNT(*) FROM actions {where}", params).fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT * FROM actions {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

        actions = []
        for r in rows:
            actions.append({
                "id": r["id"],
                "findingId": r["finding_id"],
                "timestamp": r["timestamp"],
                "category": r["category"],
                "tool": r["tool"],
                "input": json.loads(r["input"]) if r["input"] else {},
                "status": r["status"],
                "beforeState": r["before_state"],
                "afterState": r["after_state"],
                "error": r["error"],
                "reasoning": r["reasoning"],
                "durationMs": r["duration_ms"],
                "rollbackAvailable": bool(r["rollback_available"]),
                "rollbackAction": json.loads(r["rollback_action"]) if r["rollback_action"] else None,
                "resources": json.loads(r["resources"]) if r["resources"] else [],
            })

        conn.close()
        return {"actions": actions, "total": total, "page": page, "pageSize": page_size}
    except Exception as e:
        logger.error("Failed to get fix history: %s", e)
        return {"actions": [], "total": 0, "page": page, "pageSize": page_size}


def get_action_detail(action_id: str) -> dict | None:
    """Get a single action by ID."""
    try:
        conn = _get_fix_db()
        row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id": row["id"],
            "findingId": row["finding_id"],
            "timestamp": row["timestamp"],
            "category": row["category"],
            "tool": row["tool"],
            "input": json.loads(row["input"]) if row["input"] else {},
            "status": row["status"],
            "beforeState": row["before_state"],
            "afterState": row["after_state"],
            "error": row["error"],
            "reasoning": row["reasoning"],
            "durationMs": row["duration_ms"],
            "rollbackAvailable": bool(row["rollback_available"]),
            "rollbackAction": json.loads(row["rollback_action"]) if row["rollback_action"] else None,
            "resources": json.loads(row["resources"]) if row["resources"] else [],
        }
    except Exception as e:
        logger.error("Failed to get action detail: %s", e)
        return None


# ── Scan Functions ─────────────────────────────────────────────────────────

def scan_crashlooping_pods() -> list[dict]:
    """Find pods in CrashLoopBackOff or high restart counts."""
    findings = []
    try:
        core = get_core_client()
        pods = safe(lambda: core.list_pod_for_all_namespaces())
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            # Skip system namespaces
            if ns.startswith("openshift-") or ns.startswith("kube-") or ns in ("default", "openshift"):
                continue
            for cs in pod.status.container_statuses or []:
                if cs.restart_count >= 5:
                    waiting = cs.state.waiting
                    reason = waiting.reason if waiting else "Unknown"
                    findings.append(_make_finding(
                        severity=SEVERITY_CRITICAL if cs.restart_count >= 10 else SEVERITY_WARNING,
                        category="crashloop",
                        title=f"Pod {name} restarting ({cs.restart_count}x)",
                        summary=f"Container '{cs.name}' has restarted {cs.restart_count} times. Reason: {reason}",
                        resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                        auto_fixable=True,
                        runbook_id="crashloop-restart",
                    ))
    except Exception as e:
        logger.error("Crash loop scan failed: %s", e)
    return findings


def scan_pending_pods() -> list[dict]:
    """Find pods stuck in Pending state."""
    findings = []
    try:
        core = get_core_client()
        pods = safe(lambda: core.list_pod_for_all_namespaces(field_selector="status.phase=Pending"))
        if isinstance(pods, ToolError):
            return findings
        for pod in pods.items:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            if ns.startswith("openshift-") or ns.startswith("kube-") or ns in ("default", "openshift"):
                continue
            # Check how long it's been pending
            created = pod.metadata.creation_timestamp
            if created:
                age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
                if age_minutes > 5:
                    reason = ""
                    for cond in pod.status.conditions or []:
                        if cond.type == "PodScheduled" and cond.status == "False":
                            reason = cond.message or cond.reason or "Unschedulable"
                            break
                    findings.append(_make_finding(
                        severity=SEVERITY_WARNING if age_minutes < 30 else SEVERITY_CRITICAL,
                        category="scheduling",
                        title=f"Pod {name} pending for {int(age_minutes)}m",
                        summary=f"Pod has been pending for {int(age_minutes)} minutes. {reason}",
                        resources=[{"kind": "Pod", "name": name, "namespace": ns}],
                    ))
    except Exception as e:
        logger.error("Pending pod scan failed: %s", e)
    return findings


def scan_failed_deployments() -> list[dict]:
    """Find deployments with unavailable replicas."""
    findings = []
    try:
        apps = get_apps_client()
        deploys = safe(lambda: apps.list_deployment_for_all_namespaces())
        if isinstance(deploys, ToolError):
            return findings
        for d in deploys.items:
            ns = d.metadata.namespace
            name = d.metadata.name
            if ns.startswith("openshift-") or ns.startswith("kube-") or ns in ("default", "openshift"):
                continue
            desired = d.spec.replicas or 0
            available = d.status.available_replicas or 0
            if desired > 0 and available < desired:
                findings.append(_make_finding(
                    severity=SEVERITY_WARNING if available > 0 else SEVERITY_CRITICAL,
                    category="workloads",
                    title=f"Deployment {name} degraded ({available}/{desired})",
                    summary=f"Only {available} of {desired} replicas available",
                    resources=[{"kind": "Deployment", "name": name, "namespace": ns}],
                    auto_fixable=True,
                    runbook_id="deployment-degraded",
                ))
    except Exception as e:
        logger.error("Deployment scan failed: %s", e)
    return findings


def scan_node_pressure() -> list[dict]:
    """Find nodes with pressure conditions (DiskPressure, MemoryPressure, PIDPressure)."""
    findings = []
    try:
        core = get_core_client()
        nodes = safe(lambda: core.list_node())
        if isinstance(nodes, ToolError):
            return findings
        for node in nodes.items:
            name = node.metadata.name
            for cond in node.status.conditions or []:
                if cond.type in ("DiskPressure", "MemoryPressure", "PIDPressure") and cond.status == "True":
                    findings.append(_make_finding(
                        severity=SEVERITY_CRITICAL,
                        category="nodes",
                        title=f"Node {name} has {cond.type}",
                        summary=f"{cond.type}: {cond.message or cond.reason or 'Condition active'}",
                        resources=[{"kind": "Node", "name": name}],
                    ))
                if cond.type == "Ready" and cond.status != "True":
                    findings.append(_make_finding(
                        severity=SEVERITY_CRITICAL,
                        category="nodes",
                        title=f"Node {name} NotReady",
                        summary=f"Node is not ready: {cond.message or cond.reason or 'Unknown'}",
                        resources=[{"kind": "Node", "name": name}],
                    ))
    except Exception as e:
        logger.error("Node pressure scan failed: %s", e)
    return findings


def scan_expiring_certs() -> list[dict]:
    """Find TLS secrets with certificates expiring within 30 days."""
    findings = []
    try:
        import base64
        from datetime import timedelta

        core = get_core_client()
        secrets = safe(lambda: core.list_secret_for_all_namespaces(field_selector="type=kubernetes.io/tls"))
        if isinstance(secrets, ToolError):
            return findings
        now = datetime.now(timezone.utc)
        warn_threshold = timedelta(days=30)

        for secret in secrets.items:
            ns = secret.metadata.namespace
            name = secret.metadata.name
            if ns.startswith("openshift-") or ns.startswith("kube-"):
                continue
            cert_data = (secret.data or {}).get("tls.crt")
            if not cert_data:
                continue
            try:
                import ssl
                import tempfile
                cert_bytes = base64.b64decode(cert_data)
                with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
                    f.write(cert_bytes)
                    f.flush()
                    cert_info = ssl._ssl._test_decode_cert(f.name)
                not_after_str = cert_info.get("notAfter", "")
                if not_after_str:
                    # Format: "Mon DD HH:MM:SS YYYY GMT"
                    not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    remaining = not_after - now
                    if remaining < timedelta(0):
                        findings.append(_make_finding(
                            severity=SEVERITY_CRITICAL,
                            category="cert_expiry",
                            title=f"Certificate {name} EXPIRED",
                            summary=f"TLS certificate expired {abs(remaining.days)} days ago",
                            resources=[{"kind": "Secret", "name": name, "namespace": ns}],
                        ))
                    elif remaining < warn_threshold:
                        findings.append(_make_finding(
                            severity=SEVERITY_WARNING,
                            category="cert_expiry",
                            title=f"Certificate {name} expiring in {remaining.days}d",
                            summary=f"TLS certificate expires on {not_after.isoformat()}",
                            resources=[{"kind": "Secret", "name": name, "namespace": ns}],
                        ))
            except Exception:
                pass  # Skip unparseable certs
    except Exception as e:
        logger.error("Certificate scan failed: %s", e)
    return findings


def scan_firing_alerts() -> list[dict]:
    """Check Prometheus for firing alerts."""
    findings = []
    try:
        core = get_core_client()
        result = core.connect_get_namespaced_service_proxy_with_path(
            "thanos-querier:web", "openshift-monitoring",
            path="api/v1/rules?type=alert",
            _preload_content=False,
        )
        data = json.loads(result.data)
        if data.get("status") != "success":
            return findings
        for group in data.get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("state") != "firing":
                    continue
                for alert in rule.get("alerts", []):
                    if alert.get("state") != "firing":
                        continue
                    labels = alert.get("labels", {})
                    severity = labels.get("severity", "warning")
                    ns = labels.get("namespace", "")
                    alertname = labels.get("alertname", rule.get("name", "Unknown"))
                    # Skip watchdog and info alerts
                    if alertname in ("Watchdog", "InfoInhibitor"):
                        continue
                    sev = SEVERITY_CRITICAL if severity == "critical" else SEVERITY_WARNING if severity == "warning" else SEVERITY_INFO
                    summary = alert.get("annotations", {}).get("summary", alert.get("annotations", {}).get("message", ""))
                    resources = []
                    if labels.get("pod"):
                        resources.append({"kind": "Pod", "name": labels["pod"], "namespace": ns})
                    elif labels.get("deployment"):
                        resources.append({"kind": "Deployment", "name": labels["deployment"], "namespace": ns})
                    elif labels.get("node"):
                        resources.append({"kind": "Node", "name": labels["node"]})
                    findings.append(_make_finding(
                        severity=sev,
                        category="alerts",
                        title=alertname,
                        summary=summary[:200] if summary else f"Alert {alertname} firing",
                        resources=resources,
                    ))
    except Exception as e:
        logger.debug("Alert scan failed (monitoring may not be available): %s", e)
    return findings


ALL_SCANNERS = [
    ("crashloop", scan_crashlooping_pods),
    ("pending", scan_pending_pods),
    ("workloads", scan_failed_deployments),
    ("nodes", scan_node_pressure),
    ("cert_expiry", scan_expiring_certs),
    ("alerts", scan_firing_alerts),
]


# ── Auto-fix functions ────────────────────────────────────────────────────


def _fix_crashloop(finding: dict) -> tuple[str, str, str]:
    """Delete crashlooping pod. Returns (tool, before_state, after_state) or raises."""
    resources = finding.get("resources", [])
    if not resources:
        raise ValueError("No resources to fix")
    r = resources[0]
    core = get_core_client()
    # Get current state
    pod = core.read_namespaced_pod(r["name"], r["namespace"])
    restart_count = 0
    if pod.status.container_statuses:
        restart_count = pod.status.container_statuses[0].restart_count
    before = f"Pod {r['name']} in {r['namespace']}: restarts={restart_count}"
    # Delete it — controller will recreate
    core.delete_namespaced_pod(r["name"], r["namespace"])
    return ("delete_pod", before, f"Pod {r['name']} deleted — controller will recreate")


def _fix_workloads(finding: dict) -> tuple[str, str, str]:
    """Restart a failed deployment. Returns (tool, before_state, after_state) or raises."""
    resources = finding.get("resources", [])
    if not resources:
        raise ValueError("No resources to fix")
    r = resources[0]
    apps = get_apps_client()
    # Get current state
    dep = apps.read_namespaced_deployment(r["name"], r["namespace"])
    desired = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0
    before = f"Deployment {r['name']} in {r['namespace']}: {available}/{desired} available"
    # Trigger rolling restart
    from datetime import datetime as _dt
    now = _dt.now(timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                }
            }
        }
    }
    apps.patch_namespaced_deployment(r["name"], r["namespace"], body=body)
    return ("restart_deployment", before, f"Deployment {r['name']} rolling restart triggered")


AUTO_FIX_HANDLERS: dict[str, callable] = {
    "crashloop": _fix_crashloop,
    "workloads": _fix_workloads,
}


def _estimate_auto_fix_confidence(finding: dict) -> float:
    """Estimate confidence for autonomous fixes for outcome calibration."""
    category = str(finding.get("category", ""))
    severity = str(finding.get("severity", "warning"))
    base_by_category = {
        "crashloop": 0.84,
        "workloads": 0.78,
    }
    base = base_by_category.get(category, 0.65)
    if severity == SEVERITY_CRITICAL:
        base -= 0.1
    elif severity == SEVERITY_INFO:
        base += 0.05
    return max(0.0, min(1.0, round(base, 2)))


# ── Monitor Loop ───────────────────────────────────────────────────────────

class MonitorSession:
    """Manages a single /ws/monitor connection with periodic scanning."""

    def __init__(self, websocket, trust_level: int = 1, auto_fix_categories: list[str] | None = None):
        self.websocket = websocket
        self.trust_level = trust_level
        self.auto_fix_categories = set(auto_fix_categories or [])
        self.running = True
        self.scan_interval = int(os.environ.get("PULSE_AGENT_SCAN_INTERVAL", "300"))  # 5 min default
        self._last_findings: dict[str, dict] = {}  # deduplicate by title+category
        self._recent_fixes: dict[str, float] = {}  # resource_key -> timestamp for cooldown

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
        fixes_this_cycle = 0
        MAX_FIXES_PER_CYCLE = 3

        for finding in findings:
            if fixes_this_cycle >= MAX_FIXES_PER_CYCLE:
                logger.info("Auto-fix rate limit reached (%d/%d), skipping remaining findings",
                            fixes_this_cycle, MAX_FIXES_PER_CYCLE)
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
                    logger.info("Auto-fix cooldown: %s was fixed %.0fs ago, skipping (%.0fs remaining)",
                                resource_key, time.time() - self._recent_fixes[resource_key], cooldown_remaining)
                    continue

            # Bare pod protection: don't delete pods that have no ownerReferences
            if category == "crashloop" and resources:
                r = resources[0]
                if r.get("kind") == "Pod":
                    try:
                        core = get_core_client()
                        pod = core.read_namespaced_pod(r["name"], r.get("namespace", "default"))
                        if not pod.metadata.owner_references:
                            logger.warning("Auto-fix skipped: Pod %s/%s has no ownerReferences (bare pod, won't be recreated)",
                                           r.get("namespace", "default"), r["name"])
                            continue
                    except Exception as e:
                        logger.warning("Auto-fix skipped: could not verify ownerReferences for %s: %s", r.get("name"), e)
                        continue

            logger.warning(
                "Auto-fix executing WITHOUT confirmation gate (trust_level=%d, category=%s, resource=%s). "
                "This is by design for autonomous operation.",
                self.trust_level, category, resource_key,
            )

            # Send executing report
            confidence = _estimate_auto_fix_confidence(finding)
            action_report = _make_action_report(
                finding_id=finding["id"],
                tool="",
                inp={"category": category, "resources": resources, "confidence": confidence},
                status="executing",
                reasoning=f"Auto-fix for {category}: {finding.get('title', '')} (confidence={confidence:.2f})",
            )
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

                # Record cooldown timestamp
                if resource_key:
                    self._recent_fixes[resource_key] = time.time()

                logger.info(
                    "Auto-fix completed: category=%s finding=%s tool=%s duration=%dms (%d/%d this cycle)",
                    category, finding["id"], tool, duration_ms, fixes_this_cycle, MAX_FIXES_PER_CYCLE,
                )
            except Exception as e:
                duration_ms = _ts() - start_ms
                action_report["status"] = "failed"
                action_report["error"] = str(e)
                action_report["durationMs"] = duration_ms

                logger.info(
                    "Auto-fix failed: category=%s finding=%s error=%s",
                    category, finding["id"], e,
                )

            # Send completed/failed report
            await self.send(action_report)

            # Persist to fix history
            save_action(
                action_report,
                category=category,
                resources=resources,
            )

    async def run_scan(self) -> None:
        """Run all scanners and push new findings."""
        logger.info("Running cluster scan...")
        scan_start = time.time()
        all_findings: list[dict] = []

        for category, scanner in ALL_SCANNERS:
            try:
                findings = await asyncio.to_thread(scanner)
                all_findings.extend(findings)
            except Exception as e:
                logger.error("Scanner %s failed: %s", category, e)

        # Deduplicate: only send new/changed findings
        current_keys = set()
        new_findings = []
        for f in all_findings:
            key = f"{f['category']}:{f['title']}"
            current_keys.add(key)
            if key not in self._last_findings:
                new_findings.append(f)
                self._last_findings[key] = f

        # Remove stale findings (resolved since last scan)
        stale_keys = set(self._last_findings.keys()) - current_keys
        for key in stale_keys:
            del self._last_findings[key]

        # Push new findings
        for f in new_findings:
            if not await self.send(f):
                return

        # Send snapshot of all active finding IDs so UI can remove stale ones
        await self.send({
            "type": "findings_snapshot",
            "activeIds": [f["id"] for f in all_findings],
            "timestamp": _ts(),
        })

        # Push monitor status
        scan_duration = time.time() - scan_start
        await self.send({
            "type": "monitor_status",
            "activeWatches": [cat for cat, _ in ALL_SCANNERS],
            "lastScan": _ts(),
            "findingsCount": len(self._last_findings),
            "nextScan": _ts() + self.scan_interval * 1000,
        })

        logger.info(
            "Scan complete: %d total findings (%d new) in %.1fs",
            len(self._last_findings), len(new_findings), scan_duration,
        )

        # Auto-fix at trust level 3+
        if self.trust_level >= 3:
            await self.auto_fix(all_findings)

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
