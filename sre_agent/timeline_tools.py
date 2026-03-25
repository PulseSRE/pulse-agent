"""Incident correlation timeline — merges alerts, events, deployments, and admin actions.

Pillar 2: The "Time Machine" — correlates disparate data streams into
a unified timeline to answer "why did this happen?"
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .k8s_client import get_apps_client, get_core_client, get_custom_client


@beta_tool
def correlate_incident(
    namespace: str = "default",
    minutes_back: int = 30,
    resource_name: str = "",
) -> str:
    """Build a unified incident timeline by correlating Prometheus alerts, Kubernetes events, deployment rollouts, and config changes within a time window.

    Automatically highlights the probable cause of issues by finding changes that preceded symptoms.

    Args:
        namespace: Namespace to investigate. Use 'ALL' for cluster-wide.
        minutes_back: How many minutes of history to include (1-120).
        resource_name: Optional resource name to focus on.
    """
    minutes_back = min(max(1, minutes_back), 120)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    timeline: list[dict] = []

    core = get_core_client()
    apps = get_apps_client()

    # 1. Kubernetes Events (symptoms + reactions)
    kwargs = {}
    if resource_name:
        kwargs["field_selector"] = f"involvedObject.name={resource_name}"
    if namespace.upper() == "ALL":
        events_result = safe(lambda: core.list_event_for_all_namespaces(**kwargs))
    else:
        events_result = safe(lambda: core.list_namespaced_event(namespace, **kwargs))

    if not isinstance(events_result, str):
        for e in events_result.items:
            ts = e.last_timestamp or e.event_time
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            timeline.append({
                "time": ts.isoformat(),
                "source": "k8s-event",
                "severity": "warning" if e.type == "Warning" else "info",
                "summary": f"[{e.type}] {e.involved_object.kind}/{e.involved_object.name}: {e.reason} — {e.message}",
                "namespace": e.involved_object.namespace or "",
            })

    # 2. Deployment rollouts (probable cause)
    if namespace.upper() == "ALL":
        deploys = safe(lambda: apps.list_deployment_for_all_namespaces())
    else:
        deploys = safe(lambda: apps.list_namespaced_deployment(namespace))

    if not isinstance(deploys, str):
        for dep in deploys.items:
            for cond in dep.status.conditions or []:
                if cond.last_transition_time is None:
                    continue
                ts = cond.last_transition_time
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                if cond.type in ("Progressing", "Available"):
                    timeline.append({
                        "time": ts.isoformat(),
                        "source": "deployment",
                        "severity": "change",
                        "summary": f"Deployment {dep.metadata.namespace}/{dep.metadata.name}: "
                                   f"{cond.type}={cond.status} — {cond.message}",
                        "namespace": dep.metadata.namespace,
                    })

    # 3. ReplicaSet creation (tracks image changes)
    if namespace.upper() == "ALL":
        rs_result = safe(lambda: apps.list_replica_set_for_all_namespaces())
    else:
        rs_result = safe(lambda: apps.list_namespaced_replica_set(namespace))

    if not isinstance(rs_result, str):
        for rs in rs_result.items:
            ts = rs.metadata.creation_timestamp
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            images = [c.image for c in (rs.spec.template.spec.containers or [])]
            owner = ""
            for ref in rs.metadata.owner_references or []:
                if ref.kind == "Deployment":
                    owner = ref.name
            if owner:
                timeline.append({
                    "time": ts.isoformat(),
                    "source": "rollout",
                    "severity": "change",
                    "summary": f"New ReplicaSet for {rs.metadata.namespace}/{owner}: "
                               f"images={', '.join(images)}",
                    "namespace": rs.metadata.namespace,
                })

    # 4. Prometheus alerts (symptoms — via Alertmanager service proxy)
    try:
        alert_result = core.connect_get_namespaced_service_proxy_with_path(
            "alertmanager-main:web",
            "openshift-monitoring",
            path="api/v2/alerts",
            _preload_content=False,
        )
        alerts = json.loads(alert_result.data)
        for a in alerts:
            if a.get("status", {}).get("state") != "active":
                continue
            starts = a.get("startsAt", "")
            if starts:
                try:
                    ts = datetime.fromisoformat(starts.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        labels = a.get("labels", {})
                        annotations = a.get("annotations", {})
                        timeline.append({
                            "time": ts.isoformat(),
                            "source": "alert",
                            "severity": labels.get("severity", "warning"),
                            "summary": f"[ALERT] {labels.get('alertname', '?')}: "
                                       f"{annotations.get('summary', annotations.get('message', ''))}",
                            "namespace": labels.get("namespace", "cluster"),
                        })
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass  # Alertmanager not reachable

    # 5. ArgoCD sync events (if available)
    try:
        if namespace.upper() == "ALL":
            argo_apps = get_custom_client().list_cluster_custom_object(
                "argoproj.io", "v1alpha1", "applications"
            )
        else:
            argo_apps = get_custom_client().list_namespaced_custom_object(
                "argoproj.io", "v1alpha1", namespace, "applications"
            )
        for app in argo_apps.get("items", []):
            op = app.get("status", {}).get("operationState", {})
            started = op.get("startedAt", "")
            if started:
                try:
                    ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        timeline.append({
                            "time": ts.isoformat(),
                            "source": "argocd",
                            "severity": "change",
                            "summary": f"ArgoCD sync: {app['metadata']['name']} — "
                                       f"phase={op.get('phase', '?')} {op.get('message', '')}",
                            "namespace": app["metadata"].get("namespace", ""),
                        })
                except (ValueError, TypeError):
                    pass
    except (ApiException, Exception):
        pass  # ArgoCD not installed

    if not timeline:
        return f"No events found in the last {minutes_back} minutes."

    # Sort by time
    timeline.sort(key=lambda e: e["time"])

    # Auto-correlate: find changes that preceded warnings/alerts
    correlation_notes = []
    alerts_and_warnings = [e for e in timeline if e["severity"] in ("warning", "critical")]
    changes = [e for e in timeline if e["severity"] == "change"]

    for alert in alerts_and_warnings[:5]:
        alert_time = datetime.fromisoformat(alert["time"])
        preceding_changes = [
            c for c in changes
            if 0 < (alert_time - datetime.fromisoformat(c["time"])).total_seconds() <= 600
        ]
        if preceding_changes:
            cause = preceding_changes[-1]  # Most recent change before the alert
            correlation_notes.append(
                f"PROBABLE CAUSE: {alert['summary'][:80]}\n"
                f"  preceded by: {cause['summary'][:80]} "
                f"({int((alert_time - datetime.fromisoformat(cause['time'])).total_seconds())}s before)"
            )

    # Format output
    lines = [f"Timeline ({len(timeline)} events, last {minutes_back} minutes):\n"]
    for e in timeline:
        ts_short = e["time"][11:19]
        icon = {"alert": "!!", "warning": "! ", "critical": "!!", "change": ">>", "info": "  "}.get(e["severity"], "  ")
        src = e["source"].ljust(12)
        lines.append(f"  {ts_short}  {icon}  [{src}] {e['summary']}")

    if correlation_notes:
        lines.append(f"\n--- Correlations ---")
        for note in correlation_notes:
            lines.append(note)

    return "\n".join(lines)


TIMELINE_TOOLS = [correlate_incident]
