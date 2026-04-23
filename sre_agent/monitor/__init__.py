"""Autonomous cluster monitor — scans the cluster on configurable intervals,
pushes findings/predictions/action reports to connected /ws/monitor clients.

Protocol v2 addition. See API_CONTRACT.md for the full specification.
"""

from __future__ import annotations

# Re-export everything for backward compatibility.
# All existing imports like `from sre_agent.monitor import X` continue to work.
from .actions import (
    execute_rollback,
    get_action_detail,
    get_briefing,
    get_fix_history,
    get_investigation_stats,
    save_action,
    save_investigation,
    update_action_verification,
)
from .autofix import (
    AUTO_FIX_HANDLERS,
    _fix_crashloop,
    _fix_image_pull,
    _fix_workloads,
    is_autofix_paused,
    set_autofix_paused,
)
from .cluster_monitor import (
    ClusterMonitor,
    get_cluster_monitor,
    get_cluster_monitor_sync,
    reset_cluster_monitor,
)
from .confidence import (
    _estimate_auto_fix_confidence,
    _estimate_finding_confidence,
    _extract_json_object,
    _finding_content_hash,
    _finding_key,
    _sanitize_for_prompt,
)
from .findings import (
    _ensure_tables,
    _make_action_report,
    _make_finding,
    _make_prediction,
    _make_rollback_info,
    _skip_namespace,
    _ts,
)
from .investigations import (
    _build_investigation_prompt,
    _run_proactive_investigation,
    _run_security_followup,
    simulate_action,
)
from .registry import (
    SCANNER_REGISTRY,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
)
from .scanners import (
    ALL_SCANNERS,
    _get_all_scanners,
    scan_crashlooping_pods,
    scan_daemonset_gaps,
    scan_degraded_operators,
    scan_expiring_certs,
    scan_failed_deployments,
    scan_firing_alerts,
    scan_hpa_saturation,
    scan_image_pull_errors,
    scan_node_pressure,
    scan_oom_killed_pods,
    scan_pending_pods,
)
from .session import MonitorClient, MonitorSession
from .webhook import _send_webhook

# The `findings` submodule is imported above (via _ensure_tables, etc.)
# and is accessible as `sre_agent.monitor.findings` for test fixtures
# that need to reset `_tables_ensured`.


__all__ = [
    "ALL_SCANNERS",
    "AUTO_FIX_HANDLERS",
    "SCANNER_REGISTRY",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "ClusterMonitor",
    "MonitorClient",
    "MonitorSession",
    "_build_investigation_prompt",
    "_ensure_tables",
    "_estimate_auto_fix_confidence",
    "_estimate_finding_confidence",
    "_extract_json_object",
    "_finding_content_hash",
    "_finding_key",
    "_fix_crashloop",
    "_fix_image_pull",
    "_fix_workloads",
    "_get_all_scanners",
    "_make_action_report",
    "_make_finding",
    "_make_prediction",
    "_make_rollback_info",
    "_run_proactive_investigation",
    "_run_security_followup",
    "_sanitize_for_prompt",
    "_send_webhook",
    "_skip_namespace",
    "_ts",
    "execute_rollback",
    "get_action_detail",
    "get_briefing",
    "get_cluster_monitor",
    "get_cluster_monitor_sync",
    "get_fix_history",
    "get_investigation_stats",
    "is_autofix_paused",
    "reset_cluster_monitor",
    "save_action",
    "save_investigation",
    "scan_crashlooping_pods",
    "scan_daemonset_gaps",
    "scan_degraded_operators",
    "scan_expiring_certs",
    "scan_failed_deployments",
    "scan_firing_alerts",
    "scan_hpa_saturation",
    "scan_image_pull_errors",
    "scan_node_pressure",
    "scan_oom_killed_pods",
    "scan_pending_pods",
    "set_autofix_paused",
    "simulate_action",
    "update_action_verification",
]
