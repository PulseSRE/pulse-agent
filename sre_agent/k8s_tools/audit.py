"""Audit trail — record agent actions to cluster ConfigMap."""

from __future__ import annotations

from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client.rest import ApiException

from .. import k8s_client as _kc
from ..decorators import beta_tool


@beta_tool
def record_audit_entry(action: str, details: str, namespace: str = "pulse-agent") -> str:
    """Record an agent action to a ConfigMap in the cluster for team visibility.

    Args:
        action: Short action name (e.g. 'scale_deployment', 'security_scan').
        details: Description of what was done and the outcome.
        namespace: Namespace for the audit ConfigMap (default: pulse-agent).
    """
    now = datetime.now(UTC)
    entry_key = f"{now.strftime('%Y%m%d-%H%M%S-%f')}-{action}"
    # Truncate details to prevent exceeding ConfigMap 1MB limit
    truncated = details[:1000] if len(details) > 1000 else details
    entry_value = f"{now.isoformat()} | {action} | {truncated}"

    core = _kc.get_core_client()

    # Ensure namespace exists
    try:
        core.read_namespace(namespace)
    except ApiException as e:
        if e.status == 404:
            return f"Namespace '{namespace}' does not exist. Create it first."
        return f"Error checking namespace: {e.reason}"

    cm_name = "pulse-agent-audit"

    # Retry loop for 409 Conflict (optimistic concurrency)
    for attempt in range(3):
        try:
            cm = core.read_namespaced_config_map(cm_name, namespace)
            data = cm.data or {}
            # Keep last 100 entries
            if len(data) >= 100:
                oldest = sorted(data.keys())[0]
                del data[oldest]
            data[entry_key] = entry_value
            cm.data = data
            core.replace_namespaced_config_map(cm_name, namespace, cm)
            return f"Audit entry recorded: {entry_key}"
        except ApiException as e:
            if e.status == 404:
                # Create the ConfigMap
                body = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=cm_name, namespace=namespace),
                    data={entry_key: entry_value},
                )
                _kc.safe(lambda: core.create_namespaced_config_map(namespace, body))
                return f"Audit entry recorded: {entry_key}"
            elif e.status == 409 and attempt < 2:
                continue  # Retry on conflict
            else:
                return f"Error writing audit: {e.reason}"

    return f"Audit entry recorded: {entry_key}"
