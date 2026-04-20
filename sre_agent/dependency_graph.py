"""Live Kubernetes resource dependency graph.

Builds an in-memory graph from K8s API data:
- Nodes: Pods, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs,
  Services, Ingresses, Routes, HPAs, NetworkPolicies, ServiceAccounts,
  PVCs, ConfigMaps, Secrets, Nodes, HelmReleases
- Edges: ownerReferences, service selectors, volume mounts, ingress backends,
  route backends, HPA scale targets, network policy selectors, service accounts,
  helm instance labels, node scheduling
- Refreshed every scan cycle, stored as adjacency dict

Used by: skill selector (topology-aware routing), fix planner (blast radius),
plan runtime (parallel branch isolation), investigation prompts.
"""

from __future__ import annotations

import logging
import time
import types
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("pulse_agent.dependency_graph")


@dataclass
class ResourceNode:
    """A node in the dependency graph."""

    kind: str  # Pod, Deployment, Service, etc.
    name: str
    namespace: str
    labels: dict = field(default_factory=dict)


@dataclass
class ResourceEdge:
    """An edge in the dependency graph."""

    source: str  # "kind/namespace/name"
    target: str  # "kind/namespace/name"
    relationship: str  # owns, selects, mounts, references, uses, routes_to, applies_to, scales, manages, schedules


def _resource_key(kind: str, namespace: str, name: str) -> str:
    return f"{kind}/{namespace}/{name}"


class DependencyGraph:
    """In-memory resource dependency graph."""

    def __init__(self):
        self._nodes: dict[str, ResourceNode] = {}
        self._edges: list[ResourceEdge] = []
        self._adjacency: dict[str, list[str]] = {}  # key -> [downstream keys]
        self._reverse: dict[str, list[str]] = {}  # key -> [upstream keys]
        self._last_refresh: float = 0

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def add_node(self, kind: str, namespace: str, name: str, labels: dict | None = None) -> str:
        key = _resource_key(kind, namespace, name)
        self._nodes[key] = ResourceNode(kind=kind, name=name, namespace=namespace, labels=labels or {})
        if key not in self._adjacency:
            self._adjacency[key] = []
        if key not in self._reverse:
            self._reverse[key] = []
        return key

    def add_edge(self, source_key: str, target_key: str, relationship: str) -> None:
        self._edges.append(ResourceEdge(source=source_key, target=target_key, relationship=relationship))
        if source_key not in self._adjacency:
            self._adjacency[source_key] = []
        self._adjacency[source_key].append(target_key)
        if target_key not in self._reverse:
            self._reverse[target_key] = []
        self._reverse[target_key].append(source_key)

    def upstream_dependencies(self, kind: str, namespace: str, name: str) -> list[str]:
        """Get resources that this resource depends on (upstream)."""
        key = _resource_key(kind, namespace, name)
        return list(self._reverse.get(key, []))

    def downstream_blast_radius(self, kind: str, namespace: str, name: str) -> list[str]:
        """Get resources that depend on this resource (downstream blast radius)."""
        key = _resource_key(kind, namespace, name)
        result: list[str] = []
        visited: set[str] = set()
        queue = deque(self._adjacency.get(key, []))
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            queue.extend(self._adjacency.get(current, []))
        return result

    def related_resources(self, kind: str, namespace: str, name: str) -> list[str]:
        """Get all related resources (upstream + downstream)."""
        up = set(self.upstream_dependencies(kind, namespace, name))
        down = set(self.downstream_blast_radius(kind, namespace, name))
        return sorted(up | down)

    def get_node(self, key: str) -> ResourceNode | None:
        return self._nodes.get(key)

    def get_nodes(self) -> types.MappingProxyType[str, ResourceNode]:
        """Return a read-only view of all nodes (key → ResourceNode)."""
        return types.MappingProxyType(self._nodes)

    def get_edges(self) -> tuple[ResourceEdge, ...]:
        """Return an immutable snapshot of all edges."""
        return tuple(self._edges)

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._adjacency.clear()
        self._reverse.clear()

    def refresh_from_cluster(self) -> None:
        """Refresh the graph from live K8s API data."""
        try:
            from .errors import ToolError
            from .k8s_client import get_apps_client, get_core_client, get_custom_client, safe

            self.clear()
            core = get_core_client()
            apps = get_apps_client()
            custom = get_custom_client()

            # Deployments
            deploys = safe(lambda: apps.list_deployment_for_all_namespaces())
            if not isinstance(deploys, ToolError):
                for d in deploys.items:
                    ns = d.metadata.namespace
                    name = d.metadata.name
                    labels = dict(d.metadata.labels or {})
                    self.add_node("Deployment", ns, name, labels)

            # StatefulSets
            statefulsets = safe(lambda: apps.list_stateful_set_for_all_namespaces())
            if not isinstance(statefulsets, ToolError):
                for ss in statefulsets.items:
                    self.add_node(
                        "StatefulSet", ss.metadata.namespace, ss.metadata.name, dict(ss.metadata.labels or {})
                    )

            # DaemonSets
            daemonsets = safe(lambda: apps.list_daemon_set_for_all_namespaces())
            if not isinstance(daemonsets, ToolError):
                for ds in daemonsets.items:
                    self.add_node("DaemonSet", ds.metadata.namespace, ds.metadata.name, dict(ds.metadata.labels or {}))

            # Jobs
            try:
                from kubernetes import client as k8s_client

                batch = k8s_client.BatchV1Api()
                jobs = safe(lambda: batch.list_job_for_all_namespaces())
                if not isinstance(jobs, ToolError):
                    for j in jobs.items:
                        self.add_node("Job", j.metadata.namespace, j.metadata.name, dict(j.metadata.labels or {}))

                # CronJobs
                cronjobs = safe(lambda: batch.list_cron_job_for_all_namespaces())
                if not isinstance(cronjobs, ToolError):
                    for cj in cronjobs.items:
                        self.add_node(
                            "CronJob", cj.metadata.namespace, cj.metadata.name, dict(cj.metadata.labels or {})
                        )
            except Exception:
                logger.debug("Batch API unavailable for topology", exc_info=True)

            # Pods with owner references
            pods = safe(lambda: core.list_pod_for_all_namespaces())
            if not isinstance(pods, ToolError):
                for p in pods.items:
                    ns = p.metadata.namespace
                    name = p.metadata.name
                    labels = dict(p.metadata.labels or {})
                    pod_key = self.add_node("Pod", ns, name, labels)

                    # Owner references (handles Deployment, ReplicaSet, StatefulSet, DaemonSet, Job, etc.)
                    for ref in p.metadata.owner_references or []:
                        owner_ns = "" if ref.kind == "Node" else ns
                        owner_key = _resource_key(ref.kind, owner_ns, ref.name)
                        if owner_key not in self._nodes:
                            self.add_node(ref.kind, owner_ns, ref.name)
                        self.add_edge(owner_key, pod_key, "owns")

                    # Volume mounts → PVC, ConfigMap, Secret
                    for vol in p.spec.volumes or []:
                        if vol.persistent_volume_claim:
                            pvc_key = self.add_node("PVC", ns, vol.persistent_volume_claim.claim_name)
                            self.add_edge(pod_key, pvc_key, "mounts")
                        if vol.config_map:
                            cm_key = self.add_node("ConfigMap", ns, vol.config_map.name)
                            self.add_edge(pod_key, cm_key, "references")
                        if vol.secret:
                            sec_key = self.add_node("Secret", ns, vol.secret.secret_name)
                            self.add_edge(pod_key, sec_key, "references")

                    # ServiceAccount
                    sa_name = getattr(p.spec, "service_account_name", None)
                    if sa_name:
                        sa_key = self.add_node("ServiceAccount", ns, sa_name)
                        self.add_edge(pod_key, sa_key, "uses")

            # Services → pod selectors
            services = safe(lambda: core.list_service_for_all_namespaces())
            if not isinstance(services, ToolError):
                for svc in services.items:
                    ns = svc.metadata.namespace
                    name = svc.metadata.name
                    svc_key = self.add_node("Service", ns, name)
                    selector = svc.spec.selector or {}
                    for pod_key, node in self._nodes.items():
                        if node.kind == "Pod" and node.namespace == ns:
                            if all(node.labels.get(k) == v for k, v in selector.items()):
                                self.add_edge(svc_key, pod_key, "selects")

            # Ingresses → Service backends
            try:
                from kubernetes import client as k8s_client

                networking = k8s_client.NetworkingV1Api()
                ingresses = safe(lambda: networking.list_ingress_for_all_namespaces())
                if not isinstance(ingresses, ToolError):
                    for ing in ingresses.items:
                        ns = ing.metadata.namespace
                        ing_key = self.add_node("Ingress", ns, ing.metadata.name)
                        for rule in ing.spec.rules or []:
                            if rule.http:
                                for path in rule.http.paths or []:
                                    backend = getattr(path, "backend", None)
                                    if backend and backend.service:
                                        svc_key = _resource_key("Service", ns, backend.service.name)
                                        if svc_key in self._nodes:
                                            self.add_edge(ing_key, svc_key, "routes_to")

                # NetworkPolicies → pod selectors
                netpols = safe(lambda: networking.list_network_policy_for_all_namespaces())
                if not isinstance(netpols, ToolError):
                    for np in netpols.items:
                        ns = np.metadata.namespace
                        np_key = self.add_node("NetworkPolicy", ns, np.metadata.name)
                        selector = (np.spec.pod_selector.match_labels or {}) if np.spec.pod_selector else {}
                        for pod_key, node in self._nodes.items():
                            if node.kind == "Pod" and node.namespace == ns:
                                if all(node.labels.get(k) == v for k, v in selector.items()):
                                    self.add_edge(np_key, pod_key, "applies_to")
            except Exception:
                logger.debug("Networking API unavailable for topology", exc_info=True)

            # OpenShift Routes → Service backends
            try:
                routes = safe(lambda: custom.list_cluster_custom_object("route.openshift.io", "v1", "routes"))
                if not isinstance(routes, ToolError):
                    for r in routes.get("items", []):
                        ns = r["metadata"]["namespace"]
                        route_key = self.add_node("Route", ns, r["metadata"]["name"])
                        svc_name = r.get("spec", {}).get("to", {}).get("name", "")
                        if svc_name:
                            svc_key = _resource_key("Service", ns, svc_name)
                            if svc_key in self._nodes:
                                self.add_edge(route_key, svc_key, "routes_to")
            except Exception:
                logger.debug("OpenShift Route API unavailable", exc_info=True)

            # HPAs → scale targets
            try:
                from kubernetes import client as k8s_client

                autoscaling = k8s_client.AutoscalingV2Api()
                hpas = safe(lambda: autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces())
                if not isinstance(hpas, ToolError):
                    for hpa in hpas.items:
                        ns = hpa.metadata.namespace
                        hpa_key = self.add_node("HPA", ns, hpa.metadata.name)
                        ref = hpa.spec.scale_target_ref
                        if ref:
                            target_key = _resource_key(ref.kind, ns, ref.name)
                            if target_key in self._nodes:
                                self.add_edge(hpa_key, target_key, "scales")
            except Exception:
                logger.debug("Autoscaling API unavailable for topology", exc_info=True)

            # Helm releases (stored as Secrets with owner=helm label)
            try:
                secrets = safe(lambda: core.list_secret_for_all_namespaces(label_selector="owner=helm"))
                if not isinstance(secrets, ToolError):
                    for s in secrets.items:
                        labels = dict(s.metadata.labels or {})
                        release_name = labels.get("name", "")
                        if release_name and labels.get("status") == "deployed":
                            ns = s.metadata.namespace
                            helm_key = self.add_node("HelmRelease", ns, release_name, labels)
                            for node_key, node in self._nodes.items():
                                if (
                                    node.namespace == ns
                                    and node.labels.get("app.kubernetes.io/instance") == release_name
                                ):
                                    self.add_edge(helm_key, node_key, "manages")
            except Exception:
                logger.debug("Helm release discovery unavailable", exc_info=True)

            # Nodes
            nodes = safe(lambda: core.list_node())
            if not isinstance(nodes, ToolError):
                for n in nodes.items:
                    self.add_node("Node", "", n.metadata.name, dict(n.metadata.labels or {}))

            # Node → Pod scheduling (pod.spec.nodeName)
            if not isinstance(pods, ToolError):
                for p in pods.items:
                    node_name = getattr(p.spec, "node_name", None)
                    if node_name:
                        node_key = _resource_key("Node", "", node_name)
                        pod_key = _resource_key("Pod", p.metadata.namespace, p.metadata.name)
                        if node_key in self._nodes and pod_key in self._nodes:
                            self.add_edge(node_key, pod_key, "schedules")

            self._last_refresh = time.time()
            logger.info("Dependency graph refreshed: %d nodes, %d edges", self.node_count, self.edge_count)

        except Exception:
            logger.debug("Failed to refresh dependency graph", exc_info=True)

    def summary(self) -> dict:
        """Return a summary of the graph for analytics."""
        kinds: dict[str, int] = {}
        for node in self._nodes.values():
            kinds[node.kind] = kinds.get(node.kind, 0) + 1
        return {
            "nodes": self.node_count,
            "edges": self.edge_count,
            "kinds": kinds,
            "last_refresh": self._last_refresh,
        }


# Singleton
_graph: DependencyGraph | None = None


def get_dependency_graph() -> DependencyGraph:
    global _graph
    if _graph is None:
        _graph = DependencyGraph()
    return _graph
