"""Workload-related Kubernetes tools (StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, Routes, HPAs, Operators)."""

from __future__ import annotations

from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from .validators import MAX_RESULTS


@beta_tool
def list_statefulsets(namespace: str = "default"):
    """List StatefulSets with their replica counts and status.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = _kc.get_apps_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: apps.list_stateful_set_for_all_namespaces())
    else:
        result = _kc.safe(lambda: apps.list_namespaced_stateful_set(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for sts in result.items[:MAX_RESULTS]:
        s = sts.status
        ready = s.ready_replicas or 0
        desired = s.replicas or 0
        lines.append(
            f"{sts.metadata.namespace}/{sts.metadata.name}  "
            f"Ready={ready}/{desired}  "
            f"Updated={s.updated_replicas or 0}  "
            f"Age={_kc.age(sts.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "apps~v1~statefulsets",
                "namespace": sts.metadata.namespace,
                "name": sts.metadata.name,
                "ready": f"{ready}/{desired}",
                "status": "Healthy"
                if ready == desired and desired > 0
                else ("Degraded" if ready > 0 else "Unavailable"),
                "updated": s.updated_replicas or 0,
                "age": _kc.age(sts.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No StatefulSets found."
    component = (
        {
            "kind": "data_table",
            "title": f"StatefulSets ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "ready", "header": "Ready"},
                {"id": "status", "header": "Status"},
                {"id": "updated", "header": "Updated"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_daemonsets(namespace: str = "default"):
    """List DaemonSets with their status and node counts.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    apps = _kc.get_apps_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: apps.list_daemon_set_for_all_namespaces())
    else:
        result = _kc.safe(lambda: apps.list_namespaced_daemon_set(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for ds in result.items[:MAX_RESULTS]:
        s = ds.status
        desired = s.desired_number_scheduled
        ready = s.number_ready or 0
        lines.append(
            f"{ds.metadata.namespace}/{ds.metadata.name}  "
            f"Desired={desired}  "
            f"Ready={ready}  "
            f"Available={s.number_available or 0}  "
            f"Misscheduled={s.number_misscheduled or 0}  "
            f"Age={_kc.age(ds.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "apps~v1~daemonsets",
                "namespace": ds.metadata.namespace,
                "name": ds.metadata.name,
                "desired": desired,
                "ready": ready,
                "available": s.number_available or 0,
                "status": "Healthy" if ready == desired else "Degraded",
                "age": _kc.age(ds.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No DaemonSets found."
    component = (
        {
            "kind": "data_table",
            "title": f"DaemonSets ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "desired", "header": "Desired"},
                {"id": "ready", "header": "Ready"},
                {"id": "available", "header": "Available"},
                {"id": "status", "header": "Status"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_jobs(namespace: str = "default", show_completed: bool = False):
    """List Jobs with their status, completions, and duration.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        show_completed: If False (default), only show active/failed jobs.
    """
    batch = _kc.get_batch_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: batch.list_job_for_all_namespaces())
    else:
        result = _kc.safe(lambda: batch.list_namespaced_job(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for job in result.items[:MAX_RESULTS]:
        s = job.status
        succeeded = s.succeeded or 0
        failed = s.failed or 0
        active = s.active or 0
        completions = job.spec.completions or 1

        if not show_completed and succeeded >= completions and failed == 0 and active == 0:
            continue

        duration = ""
        if s.start_time and s.completion_time:
            delta = s.completion_time - s.start_time
            duration = f"  Duration={int(delta.total_seconds())}s"

        status = "Running" if active > 0 else ("Complete" if succeeded >= completions else "Failed")
        lines.append(
            f"{job.metadata.namespace}/{job.metadata.name}  "
            f"Status={status}  "
            f"Completions={succeeded}/{completions}  "
            f"Failed={failed}  Active={active}"
            f"{duration}  Age={_kc.age(job.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No matching Jobs found."


@beta_tool
def list_cronjobs(namespace: str = "default"):
    """List CronJobs with their schedule, last run, and active jobs.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    batch = _kc.get_batch_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: batch.list_cron_job_for_all_namespaces())
    else:
        result = _kc.safe(lambda: batch.list_namespaced_cron_job(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for cj in result.items[:MAX_RESULTS]:
        last_schedule = _kc.age(cj.status.last_schedule_time) + " ago" if cj.status.last_schedule_time else "never"
        active = len(cj.status.active or [])
        suspended = "SUSPENDED" if cj.spec.suspend else "Active"
        lines.append(
            f"{cj.metadata.namespace}/{cj.metadata.name}  "
            f"Schedule={cj.spec.schedule}  {suspended}  "
            f"LastRun={last_schedule}  ActiveJobs={active}  "
            f"Age={_kc.age(cj.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No CronJobs found."


@beta_tool
def list_ingresses(namespace: str = "default"):
    """List Ingresses with their hosts, paths, and backends.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    net = _kc.get_networking_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: net.list_ingress_for_all_namespaces())
    else:
        result = _kc.safe(lambda: net.list_namespaced_ingress(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for ing in result.items[:MAX_RESULTS]:
        hosts = []
        for rule in ing.spec.rules or []:
            host = rule.host or "*"
            paths = []
            for p in rule.http.paths if rule.http else []:
                backend = (
                    f"{p.backend.service.name}:{p.backend.service.port.number or p.backend.service.port.name}"
                    if p.backend.service
                    else "?"
                )
                paths.append(f"{p.path or '/'}→{backend}")
            hosts.append(f"{host} [{', '.join(paths)}]")

        tls = "TLS" if ing.spec.tls else "HTTP"
        class_name = ing.spec.ingress_class_name or "default"
        lines.append(
            f"{ing.metadata.namespace}/{ing.metadata.name}  "
            f"Class={class_name}  {tls}  "
            f"Hosts: {'; '.join(hosts)}  "
            f"Age={_kc.age(ing.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No Ingresses found."


@beta_tool
def list_routes(namespace: str = "default"):
    """List OpenShift Routes with their hosts, paths, TLS, and target services. OpenShift only.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    try:
        if namespace.upper() == "ALL":
            result = _kc.get_custom_client().list_cluster_custom_object("route.openshift.io", "v1", "routes")
        else:
            result = _kc.get_custom_client().list_namespaced_custom_object(
                "route.openshift.io", "v1", namespace, "routes"
            )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. Is this an OpenShift cluster?"

    lines = []
    for route in result.get("items", [])[:MAX_RESULTS]:
        meta = route["metadata"]
        spec = route.get("spec", {})
        status = route.get("status", {})

        host = spec.get("host", "?")
        path = spec.get("path", "/")
        svc = spec.get("to", {}).get("name", "?")
        port = spec.get("port", {}).get("targetPort", "?")
        tls = "TLS" if spec.get("tls") else "HTTP"
        termination = spec.get("tls", {}).get("termination", "") if spec.get("tls") else ""

        admitted = "Unknown"
        for ingress in status.get("ingress", []):
            for cond in ingress.get("conditions", []):
                if cond.get("type") == "Admitted":
                    admitted = "Admitted" if cond.get("status") == "True" else "NotAdmitted"

        lines.append(
            f"{meta.get('namespace', '?')}/{meta['name']}  "
            f"{tls}{('/' + termination) if termination else ''}  "
            f"Host={host}{path}  Service={svc}:{port}  "
            f"Status={admitted}"
        )
    return "\n".join(lines) or "No Routes found."


@beta_tool
def list_hpas(namespace: str = "default"):
    """List Horizontal Pod Autoscalers with their current/target metrics and replica counts.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    auto = _kc.get_autoscaling_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: auto.list_horizontal_pod_autoscaler_for_all_namespaces())
    else:
        result = _kc.safe(lambda: auto.list_namespaced_horizontal_pod_autoscaler(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    rows = []
    for hpa in result.items[:MAX_RESULTS]:
        s = hpa.status
        ref = hpa.spec.scale_target_ref
        target = f"{ref.kind}/{ref.name}"

        metrics_str = []
        for mc in hpa.status.current_metrics or []:
            if mc.type == "Resource" and mc.resource:
                current = mc.resource.current.average_utilization
                metrics_str.append(f"{mc.resource.name}={current}%")

        replicas_str = f"{s.current_replicas or 0}/{hpa.spec.min_replicas or 1}-{hpa.spec.max_replicas}"
        metrics_display = ", ".join(metrics_str) or "none"
        lines.append(
            f"{hpa.metadata.namespace}/{hpa.metadata.name}  "
            f"Target={target}  "
            f"Replicas={replicas_str}  "
            f"Metrics=[{metrics_display}]  "
            f"Age={_kc.age(hpa.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "_gvr": "autoscaling~v2~horizontalpodautoscalers",
                "namespace": hpa.metadata.namespace,
                "name": hpa.metadata.name,
                "target": target,
                "replicas": replicas_str,
                "metrics": metrics_display,
                "age": _kc.age(hpa.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines) or "No HPAs found."
    component = (
        {
            "kind": "data_table",
            "title": f"HPAs ({len(rows)})",
            "columns": [
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
                {"id": "target", "header": "Target"},
                {"id": "replicas", "header": "Replicas (cur/min-max)"},
                {"id": "metrics", "header": "Metrics"},
                {"id": "age", "header": "Age"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def list_operator_subscriptions(namespace: str = "ALL"):
    """List OLM Operator Subscriptions showing installed operators, their channels, and install plans. OpenShift only.

    Args:
        namespace: Namespace to check. Use 'ALL' for all namespaces.
    """
    try:
        if namespace.upper() == "ALL":
            result = _kc.get_custom_client().list_cluster_custom_object(
                "operators.coreos.com", "v1alpha1", "subscriptions"
            )
        else:
            result = _kc.get_custom_client().list_namespaced_custom_object(
                "operators.coreos.com", "v1alpha1", namespace, "subscriptions"
            )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. OLM may not be installed."

    lines = []
    for sub in result.get("items", [])[:MAX_RESULTS]:
        meta = sub["metadata"]
        spec = sub.get("spec", {})
        status = sub.get("status", {})

        pkg = spec.get("name", "?")
        channel = spec.get("channel", "?")
        source = spec.get("source", "?")
        csv = status.get("installedCSV", "not installed")
        state = status.get("state", "Unknown")

        conditions = status.get("conditions", [])
        health = "OK"
        for c in conditions:
            if c.get("type") == "CatalogSourcesUnhealthy" and c.get("status") == "True":
                health = "CatalogUnhealthy"

        lines.append(
            f"{meta.get('namespace', '?')}/{meta['name']}  "
            f"Package={pkg}  Channel={channel}  Source={source}  "
            f"CSV={csv}  State={state}  Health={health}"
        )
    return "\n".join(lines) or "No Operator Subscriptions found."
