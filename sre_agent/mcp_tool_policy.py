"""Which MCP tools the agent may offer: reads only.

The OpenShift MCP sidecar runs as its own service account (``auth.type:
service_account`` in ``skills/*/mcp.yaml``), and when no user token is
forwarded it acts with that account's authority. Its write tools therefore
bypassed the user's own RBAC, the native confirm previews (diff, server-side
dry run, GitOps warning) and the protected-namespace policy, and they were
offered to every skill. The agent now registers MCP *reads* only; changes go
through the native, confirm-gated write tools, which run with the user's token.

This module is the one place that decides. Classification order:

1. A name in :data:`MCP_WITHHELD_TOOLS` is withheld, whatever its annotations
   say. Several sidecar tools are annotated read-only but run commands inside
   pods or start privileged debug pods on nodes (router inspection, OVN/OVS,
   CNI diagnostics); "it only reads" is not a property an ``exec`` can promise.
2. A tool the server itself annotates as mutating (``readOnlyHint: false`` or
   ``destructiveHint: true``) is withheld, even if it is in the read list.
3. A name in :data:`MCP_READ_TOOLS` is offered.
4. Any other tool is offered only when the server attests ``readOnlyHint:
   true`` (administrator-added servers). Anything else is unknown and fails
   closed: it is treated as a write and withheld.

Both lists cover every tool of the sidecar build pinned by
``MCP_SERVER_REF`` in ``.github/workflows/build-push.yml``; the inventory is in
``tests/fixtures/mcp_sidecar_tools.json`` (regenerate it with
``scripts/mcp_tool_inventory.py`` when the ref moves) and
``tests/test_mcp_tool_policy.py`` fails if any tool is unclassified.
"""

from __future__ import annotations

from collections.abc import Mapping

_EXEC = "runs commands inside a pod as the MCP service account"
_DEBUG_POD = "starts a privileged debug pod on a node"
_WRITE = "changes cluster state as the MCP service account"

# Tools the sidecar exposes that change state or execute commands. Never offered.
MCP_WITHHELD_TOOLS: dict[str, str] = {
    # core
    "pods_delete": _WRITE,
    "pods_run": _WRITE,
    "pods_exec": _EXEC,
    "resources_create_or_update": _WRITE,
    "resources_delete": _WRITE,
    "resources_scale": _WRITE,
    # helm
    "helm_install": _WRITE,
    "helm_uninstall": _WRITE,
    # cluster-diagnostics
    "nodes_debug_exec": _DEBUG_POD,
    # cni-diagnostics (annotated read-only upstream, but each runs a debug pod)
    "get-conntrack": _DEBUG_POD,
    "get-ip": _DEBUG_POD,
    "get-iptables": _DEBUG_POD,
    "get-nft": _DEBUG_POD,
    "pwru": _DEBUG_POD,
    "tcpdump": _DEBUG_POD,
    # netedge
    "exec_dns_in_pod": _EXEC,
    "get_router_config": _EXEC,
    "get_router_info": _EXEC,
    "get_router_sessions": _EXEC,
    # ovn-kubernetes (annotated read-only upstream, but each execs into ovnkube pods)
    "ovn_get": _EXEC,
    "ovn_lflow_list": _EXEC,
    "ovn_show": _EXEC,
    "ovn_trace": _EXEC,
    "ovs_appctl": _EXEC,
    "ovs_ofctl": _EXEC,
    "ovs_vsctl": _EXEC,
    # kubevirt
    "vm_clone": _WRITE,
    "vm_create": _WRITE,
    "vm_create_from_template": _WRITE,
    "vm_lifecycle": _WRITE,
    # ossm (kiali)
    "ossm_manage_istio_config": _WRITE,
    # tekton
    "tekton_pipeline_start": _WRITE,
    "tekton_pipelinerun_lifecycle": _WRITE,
    "tekton_task_start": _WRITE,
    "tekton_taskrun_restart": _WRITE,
}

# Sidecar tools that only read through the Kubernetes API, Prometheus, Loki,
# Tempo, Kiali, NetObserv or an offline must-gather archive.
MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        # config
        "configuration_contexts_list",
        "configuration_view",
        "targets_list",
        # core
        "events_list",
        "namespaces_list",
        "nodes_log",
        "nodes_stats_summary",
        "nodes_top",
        "pods_get",
        "pods_list",
        "pods_list_in_namespace",
        "pods_log",
        "pods_top",
        "projects_list",
        "resources_get",
        "resources_list",
        # helm
        "helm_list",
        # kcp
        "kcp_workspace_describe",
        "kcp_workspaces_list",
        # kubevirt
        "vm_console_screenshot",
        "vm_guest_info",
        "vm_troubleshoot",
        # netedge
        "get_coredns_config",
        "get_service_endpoints",
        "inspect_route",
        "netedge_query_prometheus",
        "probe_dns_local",
        "probe_http",
        # netobserv
        "netobserv_export_flows",
        "netobserv_get_flow_metrics",
        "netobserv_list_flows",
        # observability/logs
        "loki_label_names",
        "loki_label_values",
        "loki_list_instances",
        "loki_query_range",
        # observability/metrics
        "execute_instant_query",
        "execute_range_query",
        "get_alerts",
        "get_label_names",
        "get_label_values",
        "get_series",
        "get_silences",
        "list_metrics",
        "show_timeseries",
        # observability/otelcol
        "otelcol_get_component_schema",
        "otelcol_get_versions",
        "otelcol_list_components",
        "otelcol_validate_config",
        # observability/traces
        "tempo_get_trace_by_id",
        "tempo_list_instances",
        "tempo_search_tag_values",
        "tempo_search_tags",
        "tempo_search_traces",
        # openshift/mustgather (offline archive)
        "mustgather_etcd_health",
        "mustgather_etcd_object_count",
        "mustgather_events_by_resource",
        "mustgather_events_by_time",
        "mustgather_events_list",
        "mustgather_list",
        "mustgather_monitoring_prometheus_alerts",
        "mustgather_monitoring_prometheus_rules",
        "mustgather_monitoring_prometheus_status",
        "mustgather_monitoring_prometheus_targets",
        "mustgather_monitoring_prometheus_tsdb",
        "mustgather_node_diagnostics_get",
        "mustgather_node_kubelet_logs",
        "mustgather_node_kubelet_logs_grep",
        "mustgather_pod_logs_by_time",
        "mustgather_pod_logs_get",
        "mustgather_pod_logs_grep",
        "mustgather_resources_list",
        # ossm (kiali)
        "ossm_get_logs",
        "ossm_get_mesh_status",
        "ossm_get_mesh_traffic_graph",
        "ossm_get_metrics",
        "ossm_get_pod_performance",
        "ossm_get_resource_details",
        "ossm_get_trace_details",
        "ossm_list_mesh_clusters",
        "ossm_list_traces",
        "ossm_manage_istio_config_read",
        # tekton
        "tekton_pipelinerun_logs",
        "tekton_taskrun_logs",
    }
)

UNKNOWN_REASON = "not known to be read-only (no readOnlyHint), so treated as a write"
ANNOTATED_WRITE_REASON = "the server annotates it as a write"


def mcp_withheld_reason(name: str, annotations: object) -> str | None:
    """Why an MCP tool is not offered, or None when it is a read the agent may offer."""
    if name in MCP_WITHHELD_TOOLS:
        return MCP_WITHHELD_TOOLS[name]
    ann = annotations if isinstance(annotations, Mapping) else {}
    if ann.get("readOnlyHint") is False or ann.get("destructiveHint") is True:
        return ANNOTATED_WRITE_REASON
    if name in MCP_READ_TOOLS:
        return None
    if ann.get("readOnlyHint") is True:
        return None
    return UNKNOWN_REASON


def is_mcp_read_only(name: str, annotations: object) -> bool:
    return mcp_withheld_reason(name, annotations) is None
