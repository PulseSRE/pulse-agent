"""Self-description tools — let the agent tell users what it can do.

Tools for agent self-awareness: skills, tools, UI components,
PromQL recipes, runbooks, and Kubernetes API introspection.
"""

from __future__ import annotations

from anthropic import beta_tool

from .k8s_client import safe


@beta_tool
def list_my_skills() -> str:
    """List all available agent skills with descriptions. Call when the user asks what you can do or what skills you have."""
    from .skill_loader import list_skills

    skills = list_skills()
    lines = [f"I have {len(skills)} skills:\n"]
    for skill in sorted(skills, key=lambda s: -s.priority):
        status = " (degraded)" if skill.degraded else ""
        lines.append(f"**{skill.name}** v{skill.version}{status} — {skill.description}")
        if skill.categories:
            lines.append(f"  Categories: {', '.join(skill.categories)}")
        if skill.handoff_to:
            targets = ", ".join(skill.handoff_to.keys())
            lines.append(f"  Can hand off to: {targets}")

    return "\n".join(lines)


@beta_tool
def list_my_tools() -> str:
    """List all tools available to the agent grouped by source. Call when the user asks what tools you have."""
    from .mcp_client import list_mcp_tools
    from .tool_registry import TOOL_REGISTRY

    try:
        mcp_names = {t["name"] for t in list_mcp_tools()}
    except Exception:
        mcp_names = set()

    native = []
    mcp = []
    for name in sorted(TOOL_REGISTRY):
        tool = TOOL_REGISTRY[name]
        desc = getattr(tool, "description", "")[:80]
        if name in mcp_names:
            mcp.append(f"- `{name}` — {desc}")
        else:
            native.append(f"- `{name}` — {desc}")

    lines = [f"I have {len(TOOL_REGISTRY)} tools:\n"]
    lines.append(f"**Native ({len(native)}):**")
    lines.extend(native[:30])  # cap to avoid massive output
    if len(native) > 30:
        lines.append(f"  ... and {len(native) - 30} more")
    if mcp:
        lines.append(f"\n**MCP ({len(mcp)}):**")
        lines.extend(mcp)

    return "\n".join(lines)


@beta_tool
def list_ui_components() -> str:
    """List all UI component types the agent can render in dashboards and chat responses.

    Call when the user asks what visualizations, components, or widget types are available.
    """
    from .component_registry import COMPONENT_REGISTRY

    lines = [f"I can render {len(COMPONENT_REGISTRY)} UI component types:\n"]

    by_category: dict[str, list[tuple[str, str]]] = {}
    for name, comp in COMPONENT_REGISTRY.items():
        cat = comp.category
        if cat not in by_category:
            by_category[cat] = []
        mutations = f" (editable: {', '.join(comp.supports_mutations)})" if comp.supports_mutations else ""
        by_category[cat].append((name, f"{comp.description}{mutations}"))

    for cat in sorted(by_category):
        lines.append(f"\n**{cat.title()}:**")
        for name, desc in sorted(by_category[cat]):
            lines.append(f"- `{name}` — {desc}")

    return "\n".join(lines)


@beta_tool
def list_promql_recipes(category: str = "") -> str:
    """List available PromQL query recipes by category.

    Call when the user asks what metrics, queries, or PromQL recipes are available.
    If category is empty, shows all categories with counts. If specified, shows
    recipes in that category with their queries.

    Categories: cpu, memory, network, storage, pods, alerts, cluster_health,
    control_plane, ingress, monitoring, node_use, operators, overcommit,
    scheduler, storage_state, workload_state
    """
    from .promql_recipes import RECIPES

    if not category:
        total = sum(len(v) for v in RECIPES.values())
        lines = [f"I have {total} PromQL recipes across {len(RECIPES)} categories:\n"]
        for cat in sorted(RECIPES):
            lines.append(f"- **{cat}** ({len(RECIPES[cat])} recipes)")
        lines.append("\nAsk for a specific category to see the queries (e.g., 'show me cpu recipes').")
        return "\n".join(lines)

    recipes = RECIPES.get(category, [])
    if not recipes:
        return f"No recipes for category '{category}'. Available: {', '.join(sorted(RECIPES.keys()))}"

    lines = [f"**{category}** — {len(recipes)} recipes:\n"]
    for r in recipes:
        lines.append(f"**{r.name}** ({r.scope})")
        lines.append(f"  `{r.query}`")
        lines.append(f"  {r.description} — renders as {r.chart_type}")
        lines.append("")

    return "\n".join(lines)


@beta_tool
def list_runbooks() -> str:
    """List available SRE runbooks that guide incident diagnosis.

    Call when the user asks what runbooks are available, what incidents
    you can help with, or what diagnostic workflows you know.
    """
    from .runbooks import _RUNBOOK_KEYWORDS

    lines = [f"I have {len(_RUNBOOK_KEYWORDS)} diagnostic runbooks:\n"]
    descriptions = {
        "crashloop": "Pod stuck in CrashLoopBackOff — check logs, exit codes, resource limits",
        "imagepull": "ImagePullBackOff — check registry auth, image tag, network policies",
        "oomkilled": "OOMKilled pods — check memory limits, leak detection, VPA recommendations",
        "node_notready": "Node NotReady — check kubelet, disk pressure, memory pressure, network",
        "pvc_pending": "PVC stuck Pending — check StorageClass, provisioner, capacity",
        "dns": "DNS resolution failures — check CoreDNS pods, configmap, network policies",
        "high_restarts": "High container restart count — check liveness probes, resource limits",
        "deployment_stuck": "Deployment not progressing — check rollout status, pod scheduling, events",
        "operator_degraded": "ClusterOperator degraded — check operator pods, CRDs, dependencies",
        "quota": "Resource quota exceeded — check namespace quotas, limit ranges, usage",
    }

    for name, keywords in sorted(_RUNBOOK_KEYWORDS.items()):
        desc = descriptions.get(name, f"Triggers on: {', '.join(keywords[:3])}")
        lines.append(f"- **{name}** — {desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Kubernetes API introspection tools
# ---------------------------------------------------------------------------


@beta_tool
def explain_resource(resource: str, field: str = "") -> str:
    """Explain a Kubernetes resource type or field using the cluster's live API schema.

    Works like 'kubectl explain'. Shows fields, types, and descriptions from
    the actual cluster's OpenAPI spec — accurate for the cluster version.

    Args:
        resource: Resource type (e.g., 'pods', 'deployments', 'services', 'nodes', 'configmaps').
        field: Optional dotted field path (e.g., 'spec.containers', 'spec.template.spec').
    """
    from kubernetes import client

    from .k8s_client import _load_k8s

    _load_k8s()

    resource_lower = resource.lower().rstrip("s") if resource.lower() not in ("ingress", "status") else resource.lower()

    # Map common names to API group + kind
    _RESOURCE_MAP = {
        "pod": ("v1", "Pod", "io.k8s.api.core.v1.Pod"),
        "deployment": ("apps/v1", "Deployment", "io.k8s.api.apps.v1.Deployment"),
        "service": ("v1", "Service", "io.k8s.api.core.v1.Service"),
        "node": ("v1", "Node", "io.k8s.api.core.v1.Node"),
        "configmap": ("v1", "ConfigMap", "io.k8s.api.core.v1.ConfigMap"),
        "secret": ("v1", "Secret", "io.k8s.api.core.v1.Secret"),
        "namespace": ("v1", "Namespace", "io.k8s.api.core.v1.Namespace"),
        "ingress": ("networking.k8s.io/v1", "Ingress", "io.k8s.api.networking.v1.Ingress"),
        "statefulset": ("apps/v1", "StatefulSet", "io.k8s.api.apps.v1.StatefulSet"),
        "daemonset": ("apps/v1", "DaemonSet", "io.k8s.api.apps.v1.DaemonSet"),
        "job": ("batch/v1", "Job", "io.k8s.api.batch.v1.Job"),
        "cronjob": ("batch/v1", "CronJob", "io.k8s.api.batch.v1.CronJob"),
        "pvc": ("v1", "PersistentVolumeClaim", "io.k8s.api.core.v1.PersistentVolumeClaim"),
        "persistentvolumeclaim": ("v1", "PersistentVolumeClaim", "io.k8s.api.core.v1.PersistentVolumeClaim"),
        "hpa": ("autoscaling/v2", "HorizontalPodAutoscaler", "io.k8s.api.autoscaling.v2.HorizontalPodAutoscaler"),
        "horizontalpodautoscaler": (
            "autoscaling/v2",
            "HorizontalPodAutoscaler",
            "io.k8s.api.autoscaling.v2.HorizontalPodAutoscaler",
        ),
        "networkpolicy": ("networking.k8s.io/v1", "NetworkPolicy", "io.k8s.api.networking.v1.NetworkPolicy"),
        "serviceaccount": ("v1", "ServiceAccount", "io.k8s.api.core.v1.ServiceAccount"),
        "role": ("rbac.authorization.k8s.io/v1", "Role", "io.k8s.api.rbac.v1.Role"),
        "clusterrole": ("rbac.authorization.k8s.io/v1", "ClusterRole", "io.k8s.api.rbac.v1.ClusterRole"),
        "rolebinding": ("rbac.authorization.k8s.io/v1", "RoleBinding", "io.k8s.api.rbac.v1.RoleBinding"),
        "clusterrolebinding": (
            "rbac.authorization.k8s.io/v1",
            "ClusterRoleBinding",
            "io.k8s.api.rbac.v1.ClusterRoleBinding",
        ),
        "route": ("route.openshift.io/v1", "Route", "com.github.openshift.api.route.v1.Route"),
    }

    entry = _RESOURCE_MAP.get(resource_lower)
    if not entry:
        available = ", ".join(sorted(_RESOURCE_MAP.keys()))
        return f"Unknown resource '{resource}'. Available: {available}"

    api_version, kind, schema_key = entry

    # Fetch OpenAPI schema
    try:
        api_client = client.ApiClient()
        openapi = api_client.call_api("/openapi/v2", "GET", response_type="object", _preload_content=False)
        import json

        schema_data = json.loads(openapi[0].read())
        definitions = schema_data.get("definitions", {})

        schema = definitions.get(schema_key)
        if not schema:
            return f"Schema for {kind} ({schema_key}) not found in cluster's OpenAPI spec."

        # Navigate to field if specified
        if field:
            for part in field.split("."):
                props = schema.get("properties", {})
                if part not in props:
                    available_fields = ", ".join(sorted(props.keys()))
                    return f"Field '{part}' not found in {kind}. Available fields: {available_fields}"
                schema = props[part]
                # Follow $ref if present
                ref = schema.get("$ref", "")
                if ref:
                    ref_key = ref.replace("#/definitions/", "")
                    schema = definitions.get(ref_key, schema)

        # Format output
        lines = [f"**{kind}** ({api_version})\n"]

        desc = schema.get("description", "")
        if desc:
            lines.append(f"{desc}\n")

        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if props:
            lines.append("**Fields:**")
            for fname in sorted(props):
                fdef = props[fname]
                ftype = fdef.get("type", "")
                ref = fdef.get("$ref", "")
                if ref:
                    ftype = ref.split(".")[-1]
                fdesc = fdef.get("description", "")[:100]
                req = " (required)" if fname in required else ""
                lines.append(f"- `{fname}` ({ftype}){req} — {fdesc}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error reading API schema: {e}"


@beta_tool
def list_api_resources(group: str = "") -> str:
    """List all Kubernetes API resources available on this cluster.

    Shows resource types, their API group, kind, and supported verbs.
    Optionally filter by API group name.

    Args:
        group: Optional API group filter (e.g., 'apps', 'batch', 'networking.k8s.io').
               Empty = show all groups with resource counts.
    """
    from kubernetes import client

    from .k8s_client import _load_k8s

    _load_k8s()

    if not group:
        # Overview: list all API groups with resource counts
        api = client.ApisApi()
        result = safe(lambda: api.get_api_versions())
        if isinstance(result, str):
            return result

        lines = [f"**{len(result.groups)} API groups** on this cluster:\n"]
        for g in sorted(result.groups, key=lambda x: x.name):
            preferred = g.preferred_version.group_version
            versions = [v.group_version for v in g.versions]
            ver_str = f" (also: {', '.join(v for v in versions if v != preferred)})" if len(versions) > 1 else ""
            lines.append(f"- **{g.name}** → {preferred}{ver_str}")

        lines.append("\n**Core API (v1):** pods, services, configmaps, secrets, nodes, namespaces, etc.")
        lines.append("\nUse `list_api_resources(group='apps')` to see resources in a specific group.")
        return "\n".join(lines)

    # Specific group: list resources
    try:
        api_client = client.ApiClient()
        if group == "v1" or group == "core":
            core = client.CoreV1Api()
            resources = safe(lambda: core.get_api_resources())
        else:
            # Try group/v1 first, then discover preferred version
            apis = client.ApisApi()
            groups = safe(lambda: apis.get_api_versions())
            if isinstance(groups, str):
                return groups

            target_group = None
            for g in groups.groups:
                if g.name == group:
                    target_group = g
                    break

            if not target_group:
                return f"API group '{group}' not found. Use list_api_resources() to see all groups."

            preferred = target_group.preferred_version.group_version
            path = f"/apis/{preferred}"
            resp = api_client.call_api(path, "GET", response_type="object", _preload_content=False)
            import json

            data = json.loads(resp[0].read())
            resources_list = data.get("resources", [])

            lines = [f"**{group}** ({preferred}) — {len(resources_list)} resources:\n"]
            for r in sorted(resources_list, key=lambda x: x.get("name", "")):
                name = r.get("name", "")
                if "/" in name:
                    continue  # skip subresources
                kind = r.get("kind", "")
                verbs = r.get("verbs", [])
                namespaced = "namespaced" if r.get("namespaced") else "cluster-scoped"
                lines.append(f"- `{name}` ({kind}) — {namespaced}, verbs: {', '.join(verbs)}")
            return "\n".join(lines)

        if isinstance(resources, str):
            return resources

        lines = [f"**{group}** — {len(resources.resources)} resources:\n"]
        for r in sorted(resources.resources, key=lambda x: x.name):
            if "/" in r.name:
                continue
            namespaced = "namespaced" if r.namespaced else "cluster-scoped"
            lines.append(f"- `{r.name}` ({r.kind}) — {namespaced}, verbs: {', '.join(r.verbs)}")
        return "\n".join(lines)

    except Exception as e:
        return f"Error listing API resources: {e}"


@beta_tool
def list_deprecated_apis() -> str:
    """Check for deprecated API versions on this cluster.

    Shows API groups where non-preferred (older) versions are still available,
    indicating resources that may need migration before the next cluster upgrade.
    """
    from kubernetes import client

    from .k8s_client import _load_k8s

    _load_k8s()

    api = client.ApisApi()
    result = safe(lambda: api.get_api_versions())
    if isinstance(result, str):
        return result

    deprecated = []
    for g in result.groups:
        if len(g.versions) > 1:
            preferred = g.preferred_version.group_version
            older = [v.group_version for v in g.versions if v.group_version != preferred]
            if older:
                deprecated.append(
                    {
                        "group": g.name,
                        "preferred": preferred,
                        "older": older,
                    }
                )

    if not deprecated:
        return "No deprecated API versions found — all API groups have a single version."

    lines = [f"**{len(deprecated)} API groups** have non-preferred (potentially deprecated) versions:\n"]
    for d in sorted(deprecated, key=lambda x: x["group"]):
        lines.append(f"**{d['group']}**")
        lines.append(f"  Preferred: `{d['preferred']}`")
        lines.append(f"  Older: {', '.join(f'`{v}`' for v in d['older'])}")
        lines.append("")

    lines.append("**Action:** Check if any workloads use older API versions with:")
    lines.append("`kubectl get <resource> -o jsonpath='{.apiVersion}'` or review manifests.")
    lines.append("Migrate to preferred versions before the next cluster upgrade.")

    return "\n".join(lines)
