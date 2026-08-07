"""Component Registry — single source of truth for all UI component kinds.

Replaces scattered definitions in quality_engine.py (VALID_KINDS),
harness.py (COMPONENT_SCHEMAS), and the frontend switch statement.

Each entry defines: description, category, schema (required/optional fields),
validation rules, supported mutations, and example spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComponentKind:
    """Definition of a UI component kind."""

    name: str
    description: str
    category: str  # metrics, data, visualization, layout, detail
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    supports_mutations: list[str] = field(default_factory=list)
    example: dict = field(default_factory=dict)
    prompt_hint: str = ""  # One-line hint for the LLM
    title_required: bool = True
    is_container: bool = False  # True for tabs, grid, section


COMPONENT_REGISTRY: dict[str, ComponentKind] = {}


def register_component(kind: ComponentKind) -> ComponentKind:
    """Register a component kind in the registry."""
    COMPONENT_REGISTRY[kind.name] = kind
    return kind


def get_valid_kinds() -> frozenset[str]:
    """Return all valid component kind names (replaces VALID_KINDS)."""
    return frozenset(COMPONENT_REGISTRY.keys())


def get_component(name: str) -> ComponentKind | None:
    """Get a component kind by name."""
    return COMPONENT_REGISTRY.get(name)


def get_components_by_category(category: str) -> list[ComponentKind]:
    """Get all components in a category."""
    return [c for c in COMPONENT_REGISTRY.values() if c.category == category]


def get_prompt_hints(kinds: list[str] | None = None) -> str:
    """Generate component hint text from registry (replaces hardcoded COMPONENT_SCHEMAS).

    Produces output in the same format as COMPONENT_SCHEMAS in harness.py:
    each component kind gets a block with its name, description, example JSON,
    and field guidance derived from the registry's prompt_hint and example.

    Args:
        kinds: Optional list of component kind names to include.
               If None, includes all registered kinds.

    Returns:
        Formatted string suitable for injection into the system prompt.
    """
    import json as _json

    blocks: list[str] = []
    for name, comp in COMPONENT_REGISTRY.items():
        if kinds is not None and name not in kinds:
            continue

        # Build a block similar to COMPONENT_SCHEMAS entries
        lines: list[str] = []
        lines.append(f"{comp.name} -- {comp.description}")

        # Include the example as a compact JSON line
        if comp.example:
            lines.append(_json.dumps(comp.example, separators=(",", ": ")))

        # Append the prompt hint (contains field guidance)
        if comp.prompt_hint:
            # Strip the "name — " prefix since we already have the header
            hint = comp.prompt_hint
            prefix = f"{comp.name} — "
            if hint.startswith(prefix):
                hint = hint[len(prefix) :]
            # Also try with --
            prefix2 = f"{comp.name} -- "
            if hint.startswith(prefix2):
                hint = hint[len(prefix2) :]
            lines.append(hint)

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Built-in component kinds
# ---------------------------------------------------------------------------

# Metrics
register_component(
    ComponentKind(
        name="metric_card",
        description="Single KPI with value, optional sparkline, and status color",
        category="metrics",
        required_fields=["title", "value"],
        optional_fields=[
            "unit",
            "query",
            "status",
            "color",
            "trend",
            "trendValue",
            "description",
            "link",
            "thresholds",
        ],
        supports_mutations=["change_kind"],
        example={
            "kind": "metric_card",
            "title": "CPU Usage",
            "value": "72%",
            "status": "warning",
            "query": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100",
            "color": "#3b82f6",
            "link": "/compute",
        },
        prompt_hint="metric_card — Single KPI with optional sparkline. Include `query` for live sparklines. Status: healthy, warning, error. Add `link` for clickable navigation.",
    )
)

register_component(
    ComponentKind(
        name="info_card_grid",
        description="Grid of label/value summary cards",
        category="metrics",
        required_fields=["title", "cards"],
        optional_fields=["description"],
        example={
            "kind": "info_card_grid",
            "title": "Cluster Health",
            "cards": [{"label": "Nodes Ready", "value": "5/5", "sub": "all healthy"}],
        },
        prompt_hint="info_card_grid — Summary metric cards in a row. Each card has label, value, sub.",
    )
)

register_component(
    ComponentKind(
        name="stat_card",
        description="Single big number with trend arrow",
        category="metrics",
        required_fields=["title", "value"],
        optional_fields=["unit", "trend", "trendValue", "trendGood", "description", "status"],
        supports_mutations=["change_kind"],
        example={
            "kind": "stat_card",
            "title": "Error Rate",
            "value": "2.3",
            "unit": "%",
            "trend": "down",
            "trendValue": "12%",
            "trendGood": "down",
        },
        prompt_hint="stat_card — Big number with trend arrow. Use for prominent KPIs.",
    )
)

register_component(
    ComponentKind(
        name="resource_counts",
        description="Clickable resource summary cards with counts and icons",
        category="metrics",
        required_fields=["items"],
        optional_fields=["title", "namespace"],
        example={
            "kind": "resource_counts",
            "title": "production Resources",
            "namespace": "production",
            "items": [{"resource": "pods", "count": 42, "gvr": "v1~pods", "status": "healthy"}],
        },
        prompt_hint="resource_counts — Clickable resource cards. Each item has resource, count, gvr, status. Returned by namespace_summary().",
    )
)

# Data
register_component(
    ComponentKind(
        name="data_table",
        description="Sortable, filterable, paginated table",
        category="data",
        required_fields=["columns"],
        optional_fields=["title", "description", "resourceType", "gvr", "rows", "datasources", "deduplicateBy"],
        supports_mutations=["update_columns", "sort_by", "filter_by", "change_kind"],
        example={
            "kind": "data_table",
            "title": "Pods",
            "columns": [{"id": "name", "header": "Name"}],
            "rows": [{"name": "nginx-abc", "_gvr": "v1~pods"}],
        },
        prompt_hint="data_table — Sortable table. Include _gvr for clickable names. Column types: resource_name, status, age, cpu, memory, replicas, sparkline. Use datasources for live multi-source tables with K8s, PromQL, or log data.",
    )
)

register_component(
    ComponentKind(
        name="key_value",
        description="Key-value pair display",
        category="data",
        required_fields=["pairs"],
        optional_fields=["title", "description"],
        example={"kind": "key_value", "title": "Pod Details", "pairs": [{"key": "Status", "value": "Running"}]},
        prompt_hint="key_value — Key-value pairs for resource details.",
    )
)

register_component(
    ComponentKind(
        name="bar_list",
        description="Horizontal ranked bars with labels and values",
        category="data",
        required_fields=["items"],
        optional_fields=["title"],
        supports_mutations=["change_kind"],
        title_required=False,
        example={"kind": "bar_list", "title": "Top Pods", "items": [{"label": "nginx", "value": 42}]},
        prompt_hint='bar_list — Horizontal ranked bars. Use for "top N" views.',
    )
)

register_component(
    ComponentKind(
        name="progress_list",
        description="Utilization/capacity bars with auto color thresholds",
        category="data",
        required_fields=["items"],
        optional_fields=["title"],
        supports_mutations=["change_kind"],
        title_required=False,
        example={
            "kind": "progress_list",
            "title": "Node CPU",
            "items": [{"label": "node-1", "value": 70, "max": 100, "unit": "%"}],
        },
        prompt_hint="progress_list — Utilization bars with green/yellow/red auto-color.",
    )
)

# Visualization
register_component(
    ComponentKind(
        name="chart",
        description="Interactive time-series chart (line, bar, area, donut, etc.)",
        category="visualization",
        required_fields=[],
        optional_fields=["title", "description", "chartType", "series", "query", "time_range"],
        supports_mutations=["change_chart_type", "update_query", "change_kind"],
        example={
            "kind": "chart",
            "chartType": "line",
            "title": "CPU Usage",
            "query": "rate(container_cpu_usage_seconds_total[5m])",
        },
        prompt_hint="chart — Time-series chart. Use get_prometheus_query() to create. Types: line, area, bar, donut, stacked_area.",
    )
)

register_component(
    ComponentKind(
        name="node_map",
        description="Visual cluster node topology",
        category="visualization",
        required_fields=["nodes"],
        optional_fields=["title", "description"],
        example={
            "kind": "node_map",
            "title": "Cluster Nodes",
            "nodes": [{"name": "worker-1", "status": "ready", "cpuPct": 45.2}],
        },
        prompt_hint="node_map — Cluster node topology. Use visualize_nodes() for pre-built maps.",
    )
)

register_component(
    ComponentKind(
        name="timeline",
        description="Event timeline with swim lanes",
        category="visualization",
        required_fields=["lanes"],
        optional_fields=["title", "description"],
        title_required=False,
        example={
            "kind": "timeline",
            "lanes": [{"label": "Alerts", "category": "alert", "events": [{"timestamp": 0, "label": "CPU high"}]}],
        },
        prompt_hint="timeline — Swim lane timeline for events, alerts, rollouts.",
    )
)

# Status
register_component(
    ComponentKind(
        name="status_list",
        description="Items with health status indicators",
        category="status",
        required_fields=["items"],
        optional_fields=["title", "description"],
        supports_mutations=["change_kind"],
        example={
            "kind": "status_list",
            "title": "Alerts",
            "items": [{"name": "CPUThrottling", "status": "warning", "detail": "pod/api"}],
        },
        prompt_hint="status_list — Status items with health indicators. Returned by get_firing_alerts().",
    )
)

register_component(
    ComponentKind(
        name="badge_list",
        description="Colored badge/tag list",
        category="status",
        required_fields=["badges"],
        optional_fields=["title", "description"],
        example={"kind": "badge_list", "title": "Labels", "badges": [{"text": "app=nginx", "variant": "info"}]},
        prompt_hint="badge_list — Colored badges for labels, tags, categories.",
    )
)

# Detail
register_component(
    ComponentKind(
        name="log_viewer",
        description="Searchable log viewer with severity levels",
        category="detail",
        required_fields=["lines"],
        optional_fields=["title", "description", "source"],
        example={
            "kind": "log_viewer",
            "title": "Logs",
            "lines": [{"timestamp": "10:00", "level": "error", "message": "OOM"}],
        },
        prompt_hint="log_viewer — Searchable logs with severity filtering.",
    )
)

register_component(
    ComponentKind(
        name="yaml_viewer",
        description="Formatted YAML/JSON display",
        category="detail",
        required_fields=["content"],
        optional_fields=["title", "description", "language"],
        example={"kind": "yaml_viewer", "title": "Config", "content": "apiVersion: v1\nkind: Pod"},
        prompt_hint="yaml_viewer — Formatted YAML/JSON with syntax highlighting.",
    )
)

register_component(
    ComponentKind(
        name="relationship_tree",
        description="Resource ownership hierarchy tree",
        category="detail",
        required_fields=["nodes"],
        optional_fields=["title", "description"],
        example={
            "kind": "relationship_tree",
            "title": "Owners",
            "nodes": [{"id": "1", "label": "Deployment/nginx", "kind": "Deployment", "name": "nginx"}],
        },
        prompt_hint="relationship_tree — Resource ownership tree. Use get_resource_relationships().",
    )
)

# Layout containers
register_component(
    ComponentKind(
        name="tabs",
        description="Tabbed container for grouping components",
        category="layout",
        required_fields=["tabs"],
        optional_fields=["title"],
        title_required=False,
        is_container=True,
        example={"kind": "tabs", "tabs": [{"label": "Overview", "components": []}]},
        prompt_hint="tabs — Tabbed layout for organizing many widgets.",
    )
)

register_component(
    ComponentKind(
        name="grid",
        description="Multi-column layout for side-by-side components",
        category="layout",
        required_fields=["items"],
        optional_fields=["title", "description", "columns"],
        title_required=False,
        is_container=True,
        example={"kind": "grid", "title": "Overview", "columns": 4, "items": []},
        prompt_hint="grid — Multi-column layout. Use for metric card rows.",
    )
)

register_component(
    ComponentKind(
        name="section",
        description="Collapsible titled section",
        category="layout",
        required_fields=["components"],
        optional_fields=["title", "description", "defaultOpen"],
        title_required=False,
        is_container=True,
        example={"kind": "section", "title": "Details", "components": [], "defaultOpen": True},
        prompt_hint="section — Collapsible section for grouping.",
    )
)

register_component(
    ComponentKind(
        name="topology",
        description="Interactive resource dependency graph with health status and perspective filtering",
        category="visualization",
        required_fields=["nodes", "edges"],
        optional_fields=["title", "description", "layout_hint", "include_metrics", "group_by"],
        title_required=False,
        example={
            "kind": "topology",
            "title": "Physical Topology — production",
            "layout_hint": "grouped",
            "include_metrics": True,
            "group_by": "node",
            "nodes": [
                {
                    "id": "1",
                    "kind": "Node",
                    "name": "worker-1",
                    "namespace": "",
                    "status": "healthy",
                    "group": "worker-1",
                }
            ],
            "edges": [{"source": "1", "target": "2", "relationship": "schedules"}],
        },
        prompt_hint=(
            "topology — Interactive dependency graph. Use get_topology_graph() to create.\n"
            "Perspective patterns:\n"
            "  Physical: kinds='Node,Pod' layout_hint='grouped' include_metrics=true group_by='node'\n"
            "  Logical: kinds='Deployment,ReplicaSet,Pod,ConfigMap,Secret,PVC,ServiceAccount' layout_hint='top-down'\n"
            "  Network: kinds='Route,Ingress,Service,Pod,NetworkPolicy' layout_hint='left-to-right'\n"
            "  Multi-Tenant: kinds='Namespace,Pod,Node' layout_hint='grouped' include_metrics=true group_by='namespace'\n"
            "  Helm: kinds='HelmRelease,Deployment,StatefulSet,Service,ConfigMap,Secret' layout_hint='grouped'"
        ),
    )
)

# Phase 2: Investigation view components

register_component(
    ComponentKind(
        name="confidence_badge",
        description="Inline confidence score badge with color coding",
        category="status",
        required_fields=["score"],
        optional_fields=["label"],
        title_required=False,
        example={
            "kind": "confidence_badge",
            "score": 0.85,
            "label": "Root cause confidence",
        },
        prompt_hint="confidence_badge — Inline confidence score (0.0-1.0). Green >0.8, amber 0.5-0.8, red <0.5. Embed in card headers.",
    )
)

register_component(
    ComponentKind(
        name="resolution_tracker",
        description="Vertical checklist tracking resolution steps with status icons",
        category="status",
        required_fields=["steps"],
        optional_fields=["title"],
        title_required=False,
        example={
            "kind": "resolution_tracker",
            "title": "Recovery Steps",
            "steps": [
                {
                    "title": "Identify root cause",
                    "status": "done",
                    "detail": "OOM kill detected",
                    "timestamp": "2024-01-15T10:30:00Z",
                },
                {"title": "Scale up replicas", "status": "running", "detail": "2 → 4 replicas"},
                {"title": "Verify health", "status": "pending", "detail": "Waiting for rollout"},
            ],
        },
        prompt_hint="resolution_tracker — Vertical step checklist. Each step: title, status (done|running|pending), detail, optional output (monospace), optional timestamp.",
    )
)

register_component(
    ComponentKind(
        name="blast_radius",
        description="Downstream dependency impact list with status indicators",
        category="status",
        required_fields=["items"],
        optional_fields=["title", "perspective"],
        example={
            "kind": "blast_radius",
            "title": "Blast Radius — payment-api",
            "items": [
                {
                    "kind_abbrev": "Svc",
                    "name": "payment-api",
                    "relationship": "Service → payment-api (selector match)",
                    "status": "degraded",
                    "status_detail": "0 endpoints",
                },
                {
                    "kind_abbrev": "Ing",
                    "name": "payment-ingress",
                    "relationship": "Ingress → payment-api (backend)",
                    "status": "healthy",
                    "status_detail": "3 active backends",
                },
            ],
        },
        prompt_hint=(
            "blast_radius — Downstream dependency impact list. Each item: kind_abbrev, name, relationship, "
            "status (degraded|healthy|retrying|paused), status_detail. "
            "Optional perspective: physical|logical|network|multi_tenant|helm to filter dependencies."
        ),
    )
)

register_component(
    ComponentKind(
        name="status_pipeline",
        description="Horizontal status step indicator showing investigation/plan progress",
        category="status",
        required_fields=["steps", "current"],
        optional_fields=[],
        title_required=False,
        example={
            "kind": "status_pipeline",
            "steps": ["Detected", "Investigated", "Action Taken", "Verifying", "Resolved"],
            "current": 1,
        },
        prompt_hint="status_pipeline — Horizontal step indicator for view lifecycle progress. steps: list of stage names, current: 0-based index of active step.",
    )
)

register_component(
    ComponentKind(
        name="action_button",
        description="Executable action button that triggers a tool with confirmation for write operations",
        category="action",
        required_fields=["label", "action", "action_input"],
        optional_fields=["style", "confirm_text"],
        title_required=False,
        example={
            "kind": "action_button",
            "label": "Restart Deployment",
            "action": "restart_deployment",
            "action_input": {"name": "payment-api", "namespace": "production"},
            "style": "danger",
            "confirm_text": "This will restart all pods in the deployment.",
        },
        prompt_hint=(
            "action_button — Executable button. action must be a registered tool name. "
            "style: primary|danger|ghost. Write tools show confirmation dialog. "
            "confirm_text: optional tooltip for dangerous actions."
        ),
    )
)
