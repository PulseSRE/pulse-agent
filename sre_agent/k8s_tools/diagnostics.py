"""Diagnostic tools — services, endpoints, events, cluster info, certificates, etc."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from anthropic import beta_tool
from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..errors import ToolError
from .validators import MAX_RESULTS, _validate_k8s_namespace


@beta_tool
def list_namespaces() -> str:
    """List all namespaces in the cluster with their status."""
    result = _kc.safe(lambda: _kc.get_core_client().list_namespace(limit=MAX_RESULTS))
    if isinstance(result, ToolError):
        return str(result)
    lines = []
    for ns in result.items:
        lines.append(f"{ns.metadata.name}  Status={ns.status.phase}  Age={_kc.age(ns.metadata.creation_timestamp)}")
    return "\n".join(lines) or "No namespaces found."


@beta_tool
def get_events(
    namespace: str = "default", resource_kind: str = "", resource_name: str = "", event_type: str = ""
) -> str:
    """Get cluster events, optionally filtered by resource.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for cluster-wide events.
        resource_kind: Filter by resource kind (e.g. 'Pod', 'Node', 'Deployment').
        resource_name: Filter by resource name.
        event_type: Filter by event type: 'Normal' or 'Warning'.
    """
    field_parts = []
    if resource_kind:
        field_parts.append(f"involvedObject.kind={resource_kind}")
    if resource_name:
        field_parts.append(f"involvedObject.name={resource_name}")
    if event_type:
        field_parts.append(f"type={event_type}")
    field_selector = ",".join(field_parts)

    kwargs = {}
    if field_selector:
        kwargs["field_selector"] = field_selector

    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: core.list_event_for_all_namespaces(**kwargs))
    else:
        result = _kc.safe(lambda: core.list_namespaced_event(namespace, **kwargs))
    if isinstance(result, ToolError):
        return str(result)

    events = sorted(
        result.items,
        key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:50]

    lines = []
    rows = []
    for e in events:
        lines.append(
            f"{_kc.age(e.last_timestamp)} ago  {e.type}  {e.reason}  "
            f"{e.involved_object.kind}/{e.involved_object.name}  "
            f"{e.message}"
        )
        rows.append(
            {
                "age": _kc.age(e.last_timestamp) + " ago",
                "type": e.type or "Normal",
                "reason": e.reason or "",
                "resource": f"{e.involved_object.kind}/{e.involved_object.name}",
                "message": (e.message or "")[:120],
            }
        )
    text = "\n".join(lines) or "No events found."
    component = (
        {
            "kind": "data_table",
            "title": f"Events ({len(rows)})",
            "columns": [
                {"id": "age", "header": "Age"},
                {"id": "type", "header": "Type"},
                {"id": "reason", "header": "Reason"},
                {"id": "resource", "header": "Resource"},
                {"id": "message", "header": "Message"},
            ],
            "rows": rows,
        }
        if rows
        else None
    )
    return (text, component)


@beta_tool
def get_resource_quotas(namespace: str = "default") -> str:
    """Get resource quotas and current usage for a namespace.

    Args:
        namespace: Kubernetes namespace.
    """
    result = _kc.safe(lambda: _kc.get_core_client().list_namespaced_resource_quota(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return f"No resource quotas defined in namespace '{namespace}'."

    lines = []
    for rq in result.items:
        lines.append(f"Quota: {rq.metadata.name}")
        hard = rq.status.hard or {}
        used = rq.status.used or {}
        for resource in sorted(hard.keys()):
            lines.append(f"  {resource}: {used.get(resource, '0')} / {hard[resource]}")
    return "\n".join(lines)


@beta_tool
def get_services(namespace: str = "default") -> str:
    """List services in a namespace with their type, cluster IP, and ports.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: core.list_service_for_all_namespaces())
    else:
        result = _kc.safe(lambda: core.list_namespaced_service(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for svc in result.items[:MAX_RESULTS]:
        ports = ", ".join(
            f"{p.port}/{p.protocol}" + (f"→{p.target_port}" if p.target_port else "") for p in (svc.spec.ports or [])
        )
        lines.append(
            f"{svc.metadata.namespace}/{svc.metadata.name}  "
            f"Type={svc.spec.type}  ClusterIP={svc.spec.cluster_ip}  Ports=[{ports}]"
        )
    return "\n".join(lines) or "No services found."


@beta_tool
def get_persistent_volume_claims(namespace: str = "default") -> str:
    """List PersistentVolumeClaims with their status, capacity, and storage class.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: core.list_persistent_volume_claim_for_all_namespaces())
    else:
        result = _kc.safe(lambda: core.list_namespaced_persistent_volume_claim(namespace))
    if isinstance(result, ToolError):
        return str(result)

    lines = []
    for pvc in result.items[:MAX_RESULTS]:
        cap = (pvc.status.capacity or {}).get("storage", "?")
        lines.append(
            f"{pvc.metadata.namespace}/{pvc.metadata.name}  "
            f"Status={pvc.status.phase}  Capacity={cap}  "
            f"StorageClass={pvc.spec.storage_class_name}  "
            f"Age={_kc.age(pvc.metadata.creation_timestamp)}"
        )
    return "\n".join(lines) or "No PVCs found."


@beta_tool
def get_cluster_version() -> str:
    """Get the Kubernetes/OpenShift cluster version information."""
    result = _kc.safe(lambda: _kc.get_version_client().get_code())
    if isinstance(result, ToolError):
        return str(result)

    info = f"Kubernetes {result.git_version} (Platform: {result.platform})"

    try:
        cv = _kc.get_custom_client().get_cluster_custom_object(
            "config.openshift.io", "v1", "clusterversions", "version"
        )
        ocp_version = cv.get("status", {}).get("desired", {}).get("version", "unknown")
        channel = cv.get("spec", {}).get("channel", "unknown")
        conditions = cv.get("status", {}).get("conditions", [])
        cond_summary = ", ".join(f"{c['type']}={c['status']}" for c in conditions)
        info += f"\nOpenShift {ocp_version} (Channel: {channel})"
        info += f"\nConditions: {cond_summary}"
    except ApiException:
        pass

    return info


@beta_tool
def get_cluster_operators() -> str:
    """List OpenShift ClusterOperators and their status (Available, Progressing, Degraded). Only works on OpenShift clusters."""
    try:
        result = _kc.get_custom_client().list_cluster_custom_object("config.openshift.io", "v1", "clusteroperators")
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}. This may not be an OpenShift cluster."

    lines = []
    items = []
    for co in result.get("items", []):
        name = co["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in co.get("status", {}).get("conditions", [])}
        available = conditions.get("Available", "?")
        degraded = conditions.get("Degraded", "?")
        lines.append(
            f"{name}  Available={available}  Progressing={conditions.get('Progressing', '?')}  Degraded={degraded}"
        )
        status = "error" if degraded == "True" else ("healthy" if available == "True" else "warning")
        items.append({"name": name, "status": status, "detail": f"Available={available}"})
    text = "\n".join(lines) or "No ClusterOperators found."
    component = {"kind": "status_list", "title": f"Cluster Operators ({len(items)})", "items": items} if items else None
    return (text, component)


@beta_tool
def get_configmap(namespace: str, name: str) -> str:
    """Get the contents of a ConfigMap.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the ConfigMap.
    """
    result = _kc.safe(lambda: _kc.get_core_client().read_namespaced_config_map(name, namespace))
    if isinstance(result, ToolError):
        return str(result)
    data = result.data or {}
    info = {"name": result.metadata.name, "namespace": result.metadata.namespace, "data": data}
    text = json.dumps(info, indent=2, default=str)

    # Render each data key as a yaml_viewer component
    if len(data) == 1:
        key, val = next(iter(data.items()))
        lang = "json" if val.strip().startswith("{") or val.strip().startswith("[") else "yaml"
        component = {"kind": "yaml_viewer", "title": f"ConfigMap: {name}/{key}", "content": val, "language": lang}
        return (text, component)

    # Multiple keys -> key_value summary
    component = {
        "kind": "key_value",
        "title": f"ConfigMap: {name}",
        "pairs": [{"key": k, "value": v[:100] + ("..." if len(v) > 100 else "")} for k, v in data.items()],
    }
    return (text, component)


@beta_tool
def describe_service(namespace: str, name: str) -> str:
    """Get detailed information about a service including endpoints, ports, selector, and target pods.

    Args:
        namespace: Kubernetes namespace.
        name: Name of the service.
    """
    core = _kc.get_core_client()
    result = _kc.safe(lambda: core.read_namespaced_service(name, namespace))
    if isinstance(result, ToolError):
        return str(result)

    svc = result
    info = {
        "name": svc.metadata.name,
        "namespace": svc.metadata.namespace,
        "type": svc.spec.type,
        "clusterIP": svc.spec.cluster_ip,
        "selector": svc.spec.selector or {},
        "ports": [
            {
                "name": p.name,
                "port": p.port,
                "targetPort": str(p.target_port),
                "protocol": p.protocol,
                "nodePort": p.node_port,
            }
            for p in (svc.spec.ports or [])
        ],
        "externalIPs": svc.spec.external_i_ps or [],
        "sessionAffinity": svc.spec.session_affinity,
    }

    # Get endpoints
    ep_result = _kc.safe(lambda: core.read_namespaced_endpoints(name, namespace))
    if not isinstance(ep_result, ToolError):
        endpoints = []
        for subset in ep_result.subsets or []:
            addrs = [a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.addresses or [])]
            not_ready = [
                a.ip + (f" ({a.target_ref.name})" if a.target_ref else "") for a in (subset.not_ready_addresses or [])
            ]
            ports = [f"{p.port}/{p.protocol}" for p in (subset.ports or [])]
            endpoints.append({"ready": addrs, "notReady": not_ready, "ports": ports})
        info["endpoints"] = endpoints

    # Count matching pods
    if svc.spec.selector:
        label_sel = ",".join(f"{k}={v}" for k, v in svc.spec.selector.items())
        pods = _kc.safe(lambda: core.list_namespaced_pod(namespace, label_selector=label_sel))
        if not isinstance(pods, ToolError):
            info["matchingPods"] = len(pods.items)
            info["readyPods"] = sum(1 for p in pods.items if p.status.phase == "Running")

    return json.dumps(info, indent=2, default=str)


@beta_tool
def get_endpoint_slices(namespace: str, service_name: str) -> str:
    """Get EndpointSlices for a service showing which pods are backing it and their readiness.

    Args:
        namespace: Kubernetes namespace.
        service_name: Name of the service to inspect.
    """
    try:
        result = _kc.get_custom_client().list_namespaced_custom_object(
            "discovery.k8s.io",
            "v1",
            namespace,
            "endpointslices",
            label_selector=f"kubernetes.io/service-name={service_name}",
        )
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"

    slices = result.get("items", [])
    if not slices:
        return f"No EndpointSlices found for service '{service_name}' in namespace '{namespace}'."

    lines = []
    for es in slices:
        name = es["metadata"]["name"]
        addr_type = es.get("addressType", "?")
        ports = ", ".join(f"{p.get('name', '?')}:{p['port']}/{p.get('protocol', 'TCP')}" for p in es.get("ports", []))
        lines.append(f"EndpointSlice: {name}  Type={addr_type}  Ports=[{ports}]")

        for ep in es.get("endpoints", []):
            ready = ep.get("conditions", {}).get("ready", False)
            addresses = ", ".join(ep.get("addresses", []))
            target = ep.get("targetRef", {})
            pod_name = target.get("name", "?") if target else "?"
            status = "Ready" if ready else "NotReady"
            lines.append(f"  {status}  {addresses}  Pod={pod_name}")

    return "\n".join(lines)


@beta_tool
def top_pods_by_restarts(namespace: str = "ALL", limit: int = 20) -> str:
    """Show pods sorted by restart count (highest first). The fastest way to find troubled workloads.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
        limit: Maximum number of pods to return (default 20).
    """
    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: core.list_pod_for_all_namespaces())
    else:
        result = _kc.safe(lambda: core.list_namespaced_pod(namespace))
    if isinstance(result, ToolError):
        return str(result)

    pods_with_restarts = []
    for pod in result.items:
        restarts = sum(
            (cs.restart_count for cs in (pod.status.container_statuses or [])),
            0,
        )
        if restarts > 0:
            pods_with_restarts.append((restarts, pod))

    pods_with_restarts.sort(key=lambda x: x[0], reverse=True)

    if not pods_with_restarts:
        return "No pods with restarts found."

    lines = []
    rows = []
    for restarts, pod in pods_with_restarts[:limit]:
        lines.append(
            f"Restarts={restarts}  {pod.metadata.namespace}/{pod.metadata.name}  "
            f"Status={pod.status.phase}  Age={_kc.age(pod.metadata.creation_timestamp)}"
        )
        rows.append(
            {
                "restarts": restarts,
                "namespace": pod.metadata.namespace,
                "name": pod.metadata.name,
                "status": pod.status.phase or "Unknown",
                "age": _kc.age(pod.metadata.creation_timestamp),
            }
        )
    text = "\n".join(lines)
    component = (
        {
            "kind": "data_table",
            "title": f"Top Pods by Restarts ({len(rows)})",
            "columns": [
                {"id": "restarts", "header": "Restarts"},
                {"id": "namespace", "header": "Namespace"},
                {"id": "name", "header": "Name"},
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
def get_recent_changes(namespace: str = "ALL", minutes: int = 60) -> str:
    """Show recent cluster changes: new/modified resources, deployments, scaling events, and config changes from the last N minutes.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for cluster-wide.
        minutes: Look back period in minutes (default 60, max 1440).
    """
    minutes = min(max(1, minutes), 1440)
    core = _kc.get_core_client()
    apps = _kc.get_apps_client()

    cutoff = datetime.now(UTC).replace(microsecond=0)
    # cutoff_str used for event time comparison below
    _ = (cutoff - __import__("datetime").timedelta(minutes=minutes)).isoformat() + "Z"

    lines = []

    # Recent events (Warning and Normal)
    if namespace.upper() == "ALL":
        events_result = _kc.safe(lambda: core.list_event_for_all_namespaces())
    else:
        events_result = _kc.safe(lambda: core.list_namespaced_event(namespace))

    if not isinstance(events_result, ToolError):
        recent_events = [
            e
            for e in events_result.items
            if e.last_timestamp
            and e.last_timestamp.replace(tzinfo=UTC) >= cutoff - __import__("datetime").timedelta(minutes=minutes)
        ]
        # Group by reason
        reasons: dict[str, int] = {}
        for e in recent_events:
            reasons[e.reason or "Unknown"] = reasons.get(e.reason or "Unknown", 0) + 1

        if reasons:
            lines.append(f"Events in last {minutes}m ({len(recent_events)} total):")
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:15]:
                lines.append(f"  {reason}: {count}")

        # Highlight warning events
        warnings = [e for e in recent_events if e.type == "Warning"]
        if warnings:
            lines.append(f"\nWarning events ({len(warnings)}):")
            for e in warnings[:10]:
                lines.append(
                    f"  {_kc.age(e.last_timestamp)} ago  {e.reason}  "
                    f"{e.involved_object.kind}/{e.involved_object.name}  {e.message}"
                )

    # Recent deployments that changed
    if namespace.upper() == "ALL":
        deps_result = _kc.safe(lambda: apps.list_deployment_for_all_namespaces())
    else:
        deps_result = _kc.safe(lambda: apps.list_namespaced_deployment(namespace))

    if not isinstance(deps_result, ToolError):
        recently_updated = []
        for dep in deps_result.items:
            for cond in dep.status.conditions or []:
                if cond.type == "Progressing" and cond.last_update_time:
                    if cond.last_update_time.replace(tzinfo=UTC) >= cutoff - __import__("datetime").timedelta(
                        minutes=minutes
                    ):
                        recently_updated.append(dep)
                        break

        if recently_updated:
            lines.append(f"\nDeployments updated in last {minutes}m ({len(recently_updated)}):")
            for dep in recently_updated[:10]:
                s = dep.status
                lines.append(
                    f"  {dep.metadata.namespace}/{dep.metadata.name}  Ready={s.ready_replicas or 0}/{s.replicas or 0}"
                )

    if not lines:
        return f"No significant changes in the last {minutes} minutes."

    return "\n".join(lines)


@beta_tool
def get_tls_certificates(namespace: str = "ALL") -> str:
    """List TLS secrets and their certificate expiry dates. Helps identify certificates approaching expiry.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    import base64

    from cryptography import x509

    core = _kc.get_core_client()
    if namespace.upper() == "ALL":
        result = _kc.safe(
            lambda: core.list_secret_for_all_namespaces(
                field_selector="type=kubernetes.io/tls",
                limit=MAX_RESULTS,
            )
        )
    else:
        result = _kc.safe(
            lambda: core.list_namespaced_secret(
                namespace,
                field_selector="type=kubernetes.io/tls",
                limit=MAX_RESULTS,
            )
        )
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return "No TLS secrets found."

    now = datetime.now(UTC)
    certs = []

    for secret in result.items:
        cert_data = (secret.data or {}).get("tls.crt", "")
        if not cert_data:
            continue

        try:
            pem_bytes = base64.b64decode(cert_data)
            cert = x509.load_pem_x509_certificate(pem_bytes)
            not_after = cert.not_valid_after_utc
            days_left = (not_after - now).days

            # Extract CN from subject
            cn = "unknown"
            try:
                cn_attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
                if cn_attrs:
                    cn = cn_attrs[0].value
            except Exception:
                pass

            status = "OK" if days_left > 30 else "EXPIRING" if days_left > 0 else "EXPIRED"
            certs.append(
                {
                    "namespace": secret.metadata.namespace,
                    "name": secret.metadata.name,
                    "cn": cn,
                    "expires": not_after.strftime("%Y-%m-%d"),
                    "days_left": days_left,
                    "status": status,
                }
            )
        except Exception:
            certs.append(
                {
                    "namespace": secret.metadata.namespace,
                    "name": secret.metadata.name,
                    "cn": "parse-error",
                    "expires": "unknown",
                    "days_left": -1,
                    "status": "UNKNOWN",
                }
            )

    # Sort by days_left (most urgent first)
    certs.sort(key=lambda c: c["days_left"])

    lines = [f"TLS Certificates ({len(certs)}):"]
    lines.append(f"  {'NAMESPACE':<20} {'NAME':<30} {'CN':<25} {'EXPIRES':<12} {'DAYS':>5}  STATUS")
    for c in certs:
        lines.append(
            f"  {c['namespace']:<20} {c['name']:<30} {c['cn'][:24]:<25} "
            f"{c['expires']:<12} {c['days_left']:>5}  {c['status']}"
        )

    expiring = [c for c in certs if c["status"] in ("EXPIRING", "EXPIRED")]
    if expiring:
        lines.append(f"\n⚠️  {len(expiring)} certificate(s) need attention!")

    return "\n".join(lines)


@beta_tool
def search_logs(namespace: str, label_selector: str, pattern: str, tail_lines: int = 100, container: str = "") -> str:
    """Search logs across multiple pods matching a label selector. Returns matching lines with pod name prefix.

    Args:
        namespace: Kubernetes namespace.
        label_selector: Label selector (e.g. 'app=nginx').
        pattern: Text pattern to search for in logs (case-insensitive).
        tail_lines: Number of recent lines to search per pod (default 100, max 500).
        container: Container name. Optional.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if not label_selector:
        return "Error: label_selector is required."
    if not pattern:
        return "Error: pattern is required."

    tail_lines = min(max(1, tail_lines), 500)
    core = _kc.get_core_client()

    # List pods matching the label selector
    pods_result = _kc.safe(lambda: core.list_namespaced_pod(namespace, label_selector=label_selector))
    if isinstance(pods_result, ToolError):
        return str(pods_result)

    if not pods_result.items:
        return f"No pods found matching label selector '{label_selector}' in namespace '{namespace}'."

    pattern_lower = pattern.lower()
    pods_to_search = pods_result.items[:20]  # Cap at 20 pods
    pods_searched = len(pods_to_search)

    def _fetch_pod_logs(pod):
        """Fetch and filter logs for a single pod."""
        pod_name = pod.metadata.name
        kwargs: dict = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
        if container:
            kwargs["container"] = container

        logs = _kc.safe(lambda: core.read_namespaced_pod_log(**kwargs))
        if isinstance(logs, ToolError):
            return [f"[{pod_name}] Error reading logs: {logs}"], False

        if not logs:
            return [], False

        pod_matches = []
        for line in logs.split("\n"):
            if pattern_lower in line.lower():
                pod_matches.append(f"[{pod_name}] {line}")

        return pod_matches[:50], bool(pod_matches)  # Cap per pod

    from concurrent.futures import ThreadPoolExecutor

    matches: list[str] = []
    pods_with_matches = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(_fetch_pod_logs, pods_to_search)
        for pod_matches, had_matches in results:
            matches.extend(pod_matches)
            if had_matches:
                pods_with_matches += 1

    if not matches:
        return f"No matches for '{pattern}' in logs of {pods_searched} pods matching '{label_selector}'."

    header = f"Found {len(matches)} matching lines across {pods_with_matches}/{pods_searched} pods:"
    text = header + "\n\n" + "\n".join(matches[:200])

    # Build log_viewer component
    log_lines = []
    for line in matches[:200]:
        source = ""
        msg = line
        if line.startswith("[") and "] " in line:
            bracket_end = line.index("] ")
            source = line[1:bracket_end]
            msg = line[bracket_end + 2 :]
        level = (
            "error"
            if any(w in msg.lower() for w in ("error", "fatal", "panic"))
            else "warn"
            if any(w in msg.lower() for w in ("warn", "warning"))
            else "info"
        )
        log_lines.append({"message": msg, "source": source, "level": level})

    component = {
        "kind": "log_viewer",
        "title": f"Log Search: '{pattern}' ({len(matches)} matches)",
        "source": label_selector,
        "lines": log_lines,
    }
    return (text, component)


@beta_tool
def get_pod_disruption_budgets(namespace: str = "ALL") -> str:
    """List PodDisruptionBudgets showing min available, max unavailable, disruptions allowed, and current healthy pods.

    Args:
        namespace: Kubernetes namespace. Use 'ALL' for all namespaces.
    """
    try:
        from kubernetes.client import PolicyV1Api

        policy = PolicyV1Api()
    except ImportError:
        return "Error: PolicyV1Api not available in this kubernetes client version."

    from ..k8s_client import _load_k8s

    _load_k8s()

    if namespace.upper() == "ALL":
        result = _kc.safe(lambda: policy.list_pod_disruption_budget_for_all_namespaces())
    else:
        result = _kc.safe(lambda: policy.list_namespaced_pod_disruption_budget(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return "No PodDisruptionBudgets found."

    lines = []
    for pdb in result.items:
        s = pdb.status
        spec = pdb.spec
        min_avail = spec.min_available if spec.min_available is not None else "N/A"
        max_unavail = spec.max_unavailable if spec.max_unavailable is not None else "N/A"
        selector = spec.selector.match_labels if spec.selector and spec.selector.match_labels else {}

        lines.append(
            f"{pdb.metadata.namespace}/{pdb.metadata.name}  "
            f"MinAvailable={min_avail}  MaxUnavailable={max_unavail}  "
            f"Allowed={s.disruptions_allowed or 0}  "
            f"Current={s.current_healthy or 0}/{s.expected_pods or 0}  "
            f"Selector={selector}"
        )
    return "\n".join(lines)


@beta_tool
def list_limit_ranges(namespace: str = "default") -> str:
    """List LimitRanges in a namespace showing default requests/limits for containers.

    Args:
        namespace: Kubernetes namespace.
    """
    result = _kc.safe(lambda: _kc.get_core_client().list_namespaced_limit_range(namespace))
    if isinstance(result, ToolError):
        return str(result)

    if not result.items:
        return f"No LimitRanges defined in namespace '{namespace}'."

    lines = []
    for lr in result.items:
        lines.append(f"LimitRange: {lr.metadata.name}")
        for limit in lr.spec.limits or []:
            lines.append(f"  Type={limit.type}")
            if limit.default:
                lines.append(f"    Default limits: {dict(limit.default)}")
            if limit.default_request:
                lines.append(f"    Default requests: {dict(limit.default_request)}")
            if limit.max:
                lines.append(f"    Max: {dict(limit.max)}")
            if limit.min:
                lines.append(f"    Min: {dict(limit.min)}")
    return "\n".join(lines)
