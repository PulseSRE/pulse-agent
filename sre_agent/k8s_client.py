"""Shared Kubernetes client initialization and helpers."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("pulse_agent")

_initialized = False
_clients: dict[str, Any] = {}

# CRI-O socket paths (OpenShift uses CRI-O, not Docker)
CRIO_SOCKET_PATHS = [
    "/var/run/crio/crio.sock",
    "/run/crio/crio.sock",
]
CONTAINER_RUNTIME_SOCKET = None


def _detect_container_runtime() -> str | None:
    """Detect the container runtime socket (CRI-O preferred for OpenShift)."""
    # Check explicit override first
    explicit = os.environ.get("CONTAINER_RUNTIME_ENDPOINT", "")
    if explicit:
        return explicit
    # Check CRI-O sockets
    for path in CRIO_SOCKET_PATHS:
        if os.path.exists(path):
            return f"unix://{path}"
    # Check containerd
    if os.path.exists("/run/containerd/containerd.sock"):
        return "unix:///run/containerd/containerd.sock"
    # Check Docker (legacy)
    if os.path.exists("/var/run/docker.sock"):
        return "unix:///var/run/docker.sock"
    return None


def _load_k8s() -> None:
    """Load kubeconfig or in-cluster config (idempotent)."""
    global _initialized, CONTAINER_RUNTIME_SOCKET
    if _initialized:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    CONTAINER_RUNTIME_SOCKET = _detect_container_runtime()
    if CONTAINER_RUNTIME_SOCKET:
        logger.info("Detected container runtime: %s", CONTAINER_RUNTIME_SOCKET)
    _initialized = True

    # Increase connection pool for parallel tool execution
    configuration = client.Configuration.get_default_copy()
    configuration.connection_pool_maxsize = 20
    client.Configuration.set_default(configuration)


def get_core_client() -> client.CoreV1Api:
    _load_k8s()
    if "core" not in _clients:
        _clients["core"] = client.CoreV1Api()
    return _clients["core"]


def get_apps_client() -> client.AppsV1Api:
    _load_k8s()
    if "apps" not in _clients:
        _clients["apps"] = client.AppsV1Api()
    return _clients["apps"]


def get_custom_client() -> client.CustomObjectsApi:
    _load_k8s()
    if "custom" not in _clients:
        _clients["custom"] = client.CustomObjectsApi()
    return _clients["custom"]


def get_version_client() -> client.VersionApi:
    _load_k8s()
    if "version" not in _clients:
        _clients["version"] = client.VersionApi()
    return _clients["version"]


def get_rbac_client() -> client.RbacAuthorizationV1Api:
    _load_k8s()
    if "rbac" not in _clients:
        _clients["rbac"] = client.RbacAuthorizationV1Api()
    return _clients["rbac"]


def get_networking_client() -> client.NetworkingV1Api:
    _load_k8s()
    if "networking" not in _clients:
        _clients["networking"] = client.NetworkingV1Api()
    return _clients["networking"]


def get_batch_client() -> client.BatchV1Api:
    _load_k8s()
    if "batch" not in _clients:
        _clients["batch"] = client.BatchV1Api()
    return _clients["batch"]


def get_autoscaling_client() -> client.AutoscalingV2Api:
    _load_k8s()
    if "autoscaling" not in _clients:
        _clients["autoscaling"] = client.AutoscalingV2Api()
    return _clients["autoscaling"]


def safe(fn) -> Any:
    """Wrap a k8s call so API errors return a structured error string."""
    try:
        return fn()
    except ApiException as e:
        from .errors import classify_api_error
        return classify_api_error(e)


def age(ts: Optional[datetime]) -> str:
    """Format a timestamp as a human-readable age string."""
    if ts is None:
        return "unknown"
    # Use astimezone to handle both naive and aware datetimes safely
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"
