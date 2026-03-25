"""Shared Kubernetes client initialization and helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("pulse_agent")

_initialized = False


def _load_k8s() -> None:
    """Load kubeconfig or in-cluster config (idempotent)."""
    global _initialized
    if _initialized:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _initialized = True


def get_core_client() -> client.CoreV1Api:
    _load_k8s()
    return client.CoreV1Api()


def get_apps_client() -> client.AppsV1Api:
    _load_k8s()
    return client.AppsV1Api()


def get_custom_client() -> client.CustomObjectsApi:
    _load_k8s()
    return client.CustomObjectsApi()


def get_version_client() -> client.VersionApi:
    _load_k8s()
    return client.VersionApi()


def get_rbac_client() -> client.RbacAuthorizationV1Api:
    _load_k8s()
    return client.RbacAuthorizationV1Api()


def get_networking_client() -> client.NetworkingV1Api:
    _load_k8s()
    return client.NetworkingV1Api()


def get_batch_client() -> client.BatchV1Api:
    _load_k8s()
    return client.BatchV1Api()


def get_autoscaling_client() -> client.AutoscalingV2Api:
    _load_k8s()
    return client.AutoscalingV2Api()


def safe(fn) -> Any:
    """Wrap a k8s call so API errors return a structured error string."""
    try:
        return fn()
    except ApiException as e:
        return f"Error ({e.status}): {e.reason}"


def age(ts: Optional[datetime]) -> str:
    """Format a timestamp as a human-readable age string."""
    if ts is None:
        return "unknown"
    delta = datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"
