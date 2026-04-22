"""Advanced tools — apply YAML, network policies, exec, connectivity tests, resource recommendations."""

from __future__ import annotations

import atexit
import json
import re
from concurrent.futures import ThreadPoolExecutor

from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from .. import k8s_client as _kc
from ..decorators import beta_tool
from ..errors import ToolError
from .validators import _validate_k8s_name, _validate_k8s_namespace

# Shared pool for parallel Prometheus queries in resource recommendations
_query_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prom")
atexit.register(_query_pool.shutdown, wait=False)

# Characters that indicate shell metacharacters (security risk)
_DANGEROUS_CHARS = set(";|&$><`")

MAX_EXEC_OUTPUT = 10 * 1024  # 10KB cap


@beta_tool
def apply_yaml(yaml_content: str, namespace: str = "", dry_run: bool = False):
    """Apply a YAML manifest to the cluster. REQUIRES USER CONFIRMATION before execution.

    Args:
        yaml_content: The YAML content to apply (single resource only).
        namespace: Override namespace (optional, uses the one in the YAML if not specified).
        dry_run: If True, only validate without applying. Default is False (apply for real).
    """
    import yaml as yaml_lib

    try:
        resource = yaml_lib.safe_load(yaml_content)
    except Exception as e:
        return f"Error parsing YAML: {e}"

    if not isinstance(resource, dict) or "apiVersion" not in resource or "kind" not in resource:
        return "Error: YAML must contain a single Kubernetes resource with apiVersion and kind."

    api_version = resource.get("apiVersion", "")
    kind = resource.get("kind", "")
    metadata = resource.get("metadata", {})
    name = metadata.get("name", "")
    ns = namespace or metadata.get("namespace", "default")

    if not name:
        return "Error: Resource must have metadata.name."

    if err := _validate_k8s_name(name):
        return err
    if err := _validate_k8s_namespace(ns):
        return err

    # Allowlist — only these resource types can be created/modified via apply_yaml.
    # Everything else is blocked to prevent privilege escalation.
    _ALLOWED_KINDS = {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "Job",
        "CronJob",
        "Service",
        "ConfigMap",
        "Ingress",
        "NetworkPolicy",
        "HorizontalPodAutoscaler",
        "LimitRange",
        "ResourceQuota",
        "PersistentVolumeClaim",
    }
    if kind not in _ALLOWED_KINDS:
        return (
            f"Error: Creating/modifying {kind} resources is not allowed via apply_yaml. "
            f"Allowed kinds: {', '.join(sorted(_ALLOWED_KINDS))}"
        )

    # Check ArgoCD auto-sync — warn if changes will be reverted
    from ..gitops_tools import check_argo_auto_sync

    argo_warning = check_argo_auto_sync(ns, kind, name)
    if argo_warning and not dry_run:
        return argo_warning

    # Build API path
    if "/" in api_version:
        _group, _version = api_version.split("/", 1)
        base = f"/apis/{api_version}"
    else:
        base = f"/api/{api_version}"

    # Simple kind->plural (covers common cases)
    plural_map = {
        "Deployment": "deployments",
        "Service": "services",
        "ConfigMap": "configmaps",
        "Secret": "secrets",
        "Namespace": "namespaces",
        "Pod": "pods",
        "ServiceAccount": "serviceaccounts",
        "Role": "roles",
        "RoleBinding": "rolebindings",
        "ClusterRole": "clusterroles",
        "ClusterRoleBinding": "clusterrolebindings",
        "NetworkPolicy": "networkpolicies",
        "Ingress": "ingresses",
        "Job": "jobs",
        "CronJob": "cronjobs",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
        "PersistentVolumeClaim": "persistentvolumeclaims",
        "HorizontalPodAutoscaler": "horizontalpodautoscalers",
        "LimitRange": "limitranges",
        "ResourceQuota": "resourcequotas",
    }
    plural = plural_map.get(kind, kind.lower() + "s")

    # Use server-side apply
    from kubernetes import client as k8s_client

    api = k8s_client.ApiClient()

    try:
        # Try server-side apply (PATCH with application/apply-patch+yaml)
        path = f"{base}/namespaces/{ns}/{plural}/{name}" if ns and kind != "Namespace" else f"{base}/{plural}/{name}"
        resp = api.call_api(
            path,
            "PATCH",
            body=json.dumps(resource),
            header_params={
                "Content-Type": "application/apply-patch+yaml",
                "Accept": "application/json",
            },
            query_params=[("fieldManager", "pulse-agent")] + ([("dryRun", "All")] if dry_run else []),
            _preload_content=False,
        )
        json.loads(resp[0].data)  # validate response is valid JSON
        action = "Dry-run validated" if dry_run else "Applied"
        return f"{action} {kind}/{name} in namespace {ns} successfully."
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}\n{e.body}"
    except Exception as e:
        return f"Error applying YAML: {type(e).__name__}: {e}"


@beta_tool
def create_network_policy(
    namespace: str,
    name: str = "default-deny-ingress",
    policy_type: str = "deny-all-ingress",
) -> str:
    """Create a network policy in a namespace. REQUIRES USER CONFIRMATION.

    Args:
        namespace: Target namespace for the network policy.
        name: Name of the NetworkPolicy resource.
        policy_type: Policy template: 'deny-all-ingress' (default), 'deny-all-egress', or 'deny-all'.
    """
    if policy_type == "deny-all-ingress":
        spec = {"podSelector": {}, "policyTypes": ["Ingress"]}
    elif policy_type == "deny-all-egress":
        spec = {"podSelector": {}, "policyTypes": ["Egress"]}
    elif policy_type == "deny-all":
        spec = {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    else:
        return f"Unknown policy type: {policy_type}. Use 'deny-all-ingress', 'deny-all-egress', or 'deny-all'."

    body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }

    # Dry-run first to validate
    dry_result = _kc.safe(
        lambda: _kc.get_networking_client().create_namespaced_network_policy(namespace, body, dry_run="All")
    )
    if isinstance(dry_result, ToolError):
        return f"Dry-run failed: {dry_result}"

    # Apply for real
    result = _kc.safe(lambda: _kc.get_networking_client().create_namespaced_network_policy(namespace, body))
    if isinstance(result, ToolError):
        return str(result)
    return f"NetworkPolicy '{name}' created in namespace '{namespace}' (type={policy_type})."


@beta_tool
def exec_command(namespace: str, pod_name: str, command: str, container: str = ""):
    """Execute a command inside a running pod container. Use this for debugging, checking environment variables, testing connectivity, or inspecting files.

    Args:
        namespace: Kubernetes namespace.
        pod_name: Name of the pod.
        command: Command to run (e.g. 'env', 'cat /etc/config/app.yaml', 'whoami'). Shell metacharacters are not allowed.
        container: Container name. Optional if pod has only one container.
    """
    if err := _validate_k8s_namespace(namespace):
        return err
    if err := _validate_k8s_name(pod_name, "pod_name"):
        return err
    if not command or not command.strip():
        return "Error: command is required."

    # Reject shell metacharacters
    if any(c in command for c in _DANGEROUS_CHARS):
        return "Error: Shell metacharacters (;|&$><`) are not allowed in commands for security reasons."

    cmd_parts = command.split()
    core = _kc.get_core_client()

    kwargs: dict = {
        "name": pod_name,
        "namespace": namespace,
        "command": cmd_parts,
        "stderr": True,
        "stdout": True,
        "stdin": False,
        "tty": False,
    }
    if container:
        kwargs["container"] = container

    try:
        output = k8s_stream(core.connect_get_namespaced_pod_exec, **kwargs)
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"
    except Exception as e:
        return f"Error executing command: {type(e).__name__}: {e}"

    if not output:
        return "(no output)"

    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n\n... (truncated, {len(output)} total bytes)"

    return output


@beta_tool
def test_connectivity(source_namespace: str, source_pod: str, target_host: str, target_port: int):
    """Test network connectivity from a pod to a target host and port. Useful for debugging service discovery, network policies, and DNS issues.

    Args:
        source_namespace: Namespace of the source pod.
        source_pod: Name of the source pod.
        target_host: Target hostname or IP (e.g. 'my-service.default.svc', '10.0.0.1').
        target_port: Target port number.
    """
    if err := _validate_k8s_namespace(source_namespace):
        return err
    if err := _validate_k8s_name(source_pod, "source_pod"):
        return err
    if not target_host:
        return "Error: target_host is required."
    # Sanitize target_host — only allow alphanumeric, dots, dashes, colons (IPv6)
    if not re.match(r"^[a-zA-Z0-9.\-:]+$", target_host):
        return "Error: target_host contains invalid characters."
    if not (1 <= target_port <= 65535):
        return f"Error: target_port must be between 1 and 65535, got {target_port}."

    core = _kc.get_core_client()

    # Try multiple connectivity check methods (not all containers have all tools)
    methods = [
        # nc (netcat) — most common
        ["nc", "-zv", "-w", "5", target_host, str(target_port)],
        # bash built-in /dev/tcp (works on most containers with bash)
        ["timeout", "5", "bash", "-c", f"echo > /dev/tcp/{target_host}/{target_port}"],
        # wget — available in many alpine-based images
        ["wget", "--spider", "--timeout=5", f"http://{target_host}:{target_port}/", "-O", "/dev/null"],
    ]

    import time as _time

    for cmd in methods:
        start = _time.monotonic()
        try:
            output = k8s_stream(
                core.connect_get_namespaced_pod_exec,
                name=source_pod,
                namespace=source_namespace,
                command=cmd,
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False,
            )
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            # If we got here without exception, connection likely succeeded
            return (
                f"Connection to {target_host}:{target_port} succeeded.\n"
                f"Latency: {elapsed_ms}ms\n"
                f"Method: {cmd[0]}\n"
                f"Output: {(output or '').strip()[:500]}"
            )
        except ApiException as e:
            if e.status == 404:
                return f"Error: Pod '{source_pod}' not found in namespace '{source_namespace}'."
            # Command failed — try next method
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            if cmd == methods[-1]:
                # Last method — report failure
                return (
                    f"Connection to {target_host}:{target_port} FAILED.\n"
                    f"Latency: {elapsed_ms}ms\n"
                    f"All connectivity methods failed. The target may be unreachable, "
                    f"blocked by a NetworkPolicy, or the container lacks network tools.\n"
                    f"Last error: {e.reason}"
                )
            continue
        except Exception as e:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            if cmd == methods[-1]:
                return (
                    f"Connection to {target_host}:{target_port} FAILED.\n"
                    f"Latency: {elapsed_ms}ms\n"
                    f"Error: {type(e).__name__}: {e}"
                )
            continue

    return f"Connection to {target_host}:{target_port} FAILED. No connectivity tools available in the container."


@beta_tool
def get_resource_recommendations(namespace: str, time_range: str = "24h"):
    """Analyze resource usage vs requests/limits and recommend right-sizing. Shows over-provisioned and under-provisioned workloads.

    Args:
        namespace: Kubernetes namespace.
        time_range: Time window for usage analysis (default '24h').
    """
    from ..prometheus import get_prometheus_client

    if err := _validate_k8s_namespace(namespace):
        return err

    prom = get_prometheus_client()

    # Build Prometheus queries for CPU and memory P95
    cpu_query = (
        f"quantile_over_time(0.95, rate(container_cpu_usage_seconds_total"
        f'{{namespace="{namespace}",container!="",container!="POD"}}[5m])[{time_range}:])'
    )
    mem_query = (
        f"quantile_over_time(0.95, container_memory_working_set_bytes"
        f'{{namespace="{namespace}",container!="",container!="POD"}}[{time_range}:])'
    )

    # Also get current requests/limits from kube_state_metrics
    cpu_req_query = f'kube_pod_container_resource_requests{{namespace="{namespace}",resource="cpu"}}'
    mem_req_query = f'kube_pod_container_resource_requests{{namespace="{namespace}",resource="memory"}}'

    def _instant_query(query: str) -> list[dict]:
        try:
            data = prom.query(query, timeout=15)
            if data.get("status") == "success":
                return data.get("data", {}).get("result", [])
        except Exception:
            pass
        return []

    cpu_usage_f = _query_pool.submit(_instant_query, cpu_query)
    mem_usage_f = _query_pool.submit(_instant_query, mem_query)
    cpu_requests_f = _query_pool.submit(_instant_query, cpu_req_query)
    mem_requests_f = _query_pool.submit(_instant_query, mem_req_query)

    cpu_usage = cpu_usage_f.result()
    mem_usage = mem_usage_f.result()
    cpu_requests = cpu_requests_f.result()
    mem_requests = mem_requests_f.result()

    if not cpu_usage and not mem_usage and not cpu_requests:
        return (
            f"No resource metrics available for namespace '{namespace}'. "
            "Ensure Prometheus/Thanos and kube-state-metrics are running."
        )

    # Index requests by pod+container
    def _key(r: dict) -> str:
        m = r.get("metric", {})
        return f"{m.get('pod', '')}:{m.get('container', '')}"

    cpu_req_map = {_key(r): float(r["value"][1]) for r in cpu_requests if r.get("value")}
    mem_req_map = {_key(r): float(r["value"][1]) for r in mem_requests if r.get("value")}
    cpu_use_map = {_key(r): float(r["value"][1]) for r in cpu_usage if r.get("value")}
    mem_use_map = {_key(r): float(r["value"][1]) for r in mem_usage if r.get("value")}

    # Merge all keys
    all_keys = set(cpu_req_map) | set(mem_req_map) | set(cpu_use_map) | set(mem_use_map)

    rows = []
    for key in sorted(all_keys):
        pod, container = key.split(":", 1) if ":" in key else (key, "")
        if not pod or not container:
            continue

        cpu_req = cpu_req_map.get(key, 0)
        cpu_p95 = cpu_use_map.get(key, 0)
        mem_req = mem_req_map.get(key, 0)
        mem_p95 = mem_use_map.get(key, 0)

        # Recommend: 20% headroom above P95, rounded to nearest 50m/50Mi
        cpu_rec = max(0.05, round((cpu_p95 * 1.2) * 20) / 20)  # Round to nearest 50m
        mem_rec = max(64 * 1024 * 1024, int((mem_p95 * 1.2) / (50 * 1024 * 1024)) * 50 * 1024 * 1024)  # Round 50Mi

        def _fmt_cpu(cores: float) -> str:
            if cores < 1:
                return f"{int(cores * 1000)}m"
            return f"{cores:.2f}"

        def _fmt_mem(b: float) -> str:
            mi = b / (1024 * 1024)
            if mi < 1024:
                return f"{int(mi)}Mi"
            return f"{mi / 1024:.1f}Gi"

        rows.append(
            {
                "pod": pod,
                "container": container,
                "cpu_request": _fmt_cpu(cpu_req),
                "cpu_p95": _fmt_cpu(cpu_p95),
                "cpu_recommendation": _fmt_cpu(cpu_rec),
                "mem_request": _fmt_mem(mem_req),
                "mem_p95": _fmt_mem(mem_p95),
                "mem_recommendation": _fmt_mem(mem_rec),
            }
        )

    if not rows:
        return f"No workload resource data found for namespace '{namespace}'."

    # Text summary
    lines = [f"Resource recommendations for namespace '{namespace}' (P95 over {time_range}):"]
    for r in rows[:30]:
        lines.append(
            f"  {r['pod']}/{r['container']}: "
            f"CPU {r['cpu_request']}→{r['cpu_recommendation']} (P95={r['cpu_p95']})  "
            f"Mem {r['mem_request']}→{r['mem_recommendation']} (P95={r['mem_p95']})"
        )
    text = "\n".join(lines)

    component = {
        "kind": "data_table",
        "title": f"Resource Recommendations — {namespace}",
        "description": f"Right-sizing based on P95 usage over {time_range} with 20% headroom",
        "columns": [
            {"id": "pod", "header": "Pod"},
            {"id": "container", "header": "Container"},
            {"id": "cpu_request", "header": "CPU Request"},
            {"id": "cpu_p95", "header": "CPU P95"},
            {"id": "cpu_recommendation", "header": "CPU Rec."},
            {"id": "mem_request", "header": "Mem Request"},
            {"id": "mem_p95", "header": "Mem P95"},
            {"id": "mem_recommendation", "header": "Mem Rec."},
        ],
        "rows": rows[:50],
    }
    return (text, component)
