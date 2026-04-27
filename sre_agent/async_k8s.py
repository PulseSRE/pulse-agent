"""Async Kubernetes client wrappers using kubernetes_asyncio.

Provides async counterparts to the sync K8s clients in ``k8s_client.py``.
Both coexist — the sync path (kubernetes) remains the default for tools
executed in the ThreadPoolExecutor.  Async clients are for modules that
run natively on the event loop (e.g., cluster_monitor scanners).

Usage::

    core = await get_async_core_client()
    pods = await core.list_pod_for_all_namespaces()
    for pod in pods.items:
        print(pod.metadata.name)

The async clients share the same kubeconfig/in-cluster configuration as
the sync clients.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("pulse_agent.async_k8s")

_async_initialized = False
_async_clients: dict[str, Any] = {}


async def _load_async_k8s() -> None:
    """Load kubeconfig or in-cluster config for kubernetes_asyncio."""
    global _async_initialized
    if _async_initialized:
        return

    from kubernetes_asyncio import config as async_config

    try:
        async_config.load_incluster_config()
        logger.info("Loaded in-cluster config (async)")
    except async_config.ConfigException:
        try:
            await async_config.load_kube_config()
            logger.info("Loaded kubeconfig (async)")
        except Exception as e:
            logger.warning("Failed to load async K8s config: %s", e)
            raise

    _async_initialized = True


async def get_async_core_client() -> Any:
    """Return an async CoreV1Api client."""
    if "core" not in _async_clients:
        await _load_async_k8s()
        from kubernetes_asyncio.client import CoreV1Api

        _async_clients["core"] = CoreV1Api()
    return _async_clients["core"]


async def get_async_apps_client() -> Any:
    """Return an async AppsV1Api client."""
    if "apps" not in _async_clients:
        await _load_async_k8s()
        from kubernetes_asyncio.client import AppsV1Api

        _async_clients["apps"] = AppsV1Api()
    return _async_clients["apps"]


async def get_async_custom_client() -> Any:
    """Return an async CustomObjectsApi client."""
    if "custom" not in _async_clients:
        await _load_async_k8s()
        from kubernetes_asyncio.client import CustomObjectsApi

        _async_clients["custom"] = CustomObjectsApi()
    return _async_clients["custom"]


async def get_async_autoscaling_client() -> Any:
    """Return an async AutoscalingV1Api client."""
    if "autoscaling" not in _async_clients:
        await _load_async_k8s()
        from kubernetes_asyncio.client import AutoscalingV1Api

        _async_clients["autoscaling"] = AutoscalingV1Api()
    return _async_clients["autoscaling"]


async def safe_async(coro: Any) -> Any:
    """Await a coroutine, returning a ToolError on API failures."""
    from .errors import classify_api_error

    try:
        return await coro
    except Exception as e:
        return classify_api_error(e)


async def close_async_clients() -> None:
    """Close all cached async API clients."""
    global _async_initialized
    for name, api_client in _async_clients.items():
        try:
            if hasattr(api_client, "api_client"):
                await api_client.api_client.close()
        except Exception:
            logger.debug("Failed to close async client %s", name, exc_info=True)
    _async_clients.clear()
    _async_initialized = False
