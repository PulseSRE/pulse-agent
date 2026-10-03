"""Read-only installation observations; configuration is not connectivity proof."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime


def check(check_id: str, status: str, message: str, remediation: str, source: str) -> dict:
    return dict(id=check_id, status=status, message=message, remediation=remediation, source=source)


def provider_checks(environ=None) -> list[dict]:
    env = os.environ if environ is None else environ
    project, region = env.get("ANTHROPIC_VERTEX_PROJECT_ID", ""), env.get("CLOUD_ML_REGION", "")
    if project and region:
        configured = check(
            "provider_configuration",
            "healthy",
            "Vertex project and region configured; credentials not validated.",
            "",
            "environment",
        )
    elif project or region:
        configured = check(
            "provider_configuration",
            "unhealthy",
            "Incomplete Vertex configuration; runtime falls back to direct Anthropic.",
            "Set both ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION, or remove both and configure ANTHROPIC_API_KEY.",
            "environment",
        )
    elif env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        configured = check(
            "provider_configuration",
            "healthy",
            "Direct Anthropic credential configured; validity not validated.",
            "",
            "environment",
        )
    else:
        configured = check(
            "provider_configuration",
            "unhealthy",
            "No direct Anthropic credential or complete Vertex configuration.",
            "Configure ANTHROPIC_API_KEY, or Vertex project/region with application default credentials.",
            "environment",
        )
    return [
        configured,
        check(
            "provider_connectivity",
            "unknown",
            "No nonbillable provider/model inference probe is available.",
            "Run the documented provider-backed outcome gate with the deployed model; configuration alone does not prove connectivity or model access.",
            "not_probed",
        ),
    ]


def database_probe() -> bool:
    from .db import get_database

    return get_database().health_check()


def kubernetes_probe(resource: str) -> bool:
    # Explicitly inspect installation credentials, never forwarded caller credentials.
    from kubernetes import client

    from .k8s_client import _load_k8s

    _load_k8s()
    with client.ApiClient() as api:
        kwargs = dict(limit=1, _request_timeout=(3, 5))
        if resource == "deployments":
            client.AppsV1Api(api).list_deployment_for_all_namespaces(**kwargs)
        elif resource == "pods":
            client.CoreV1Api(api).list_pod_for_all_namespaces(**kwargs)
        elif resource == "nodes":
            client.CoreV1Api(api).list_node(**kwargs)
        elif resource == "events":
            client.CoreV1Api(api).list_event_for_all_namespaces(**kwargs)
        else:
            review = client.V1SelfSubjectAccessReview(
                spec=client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=client.V1ResourceAttributes(
                        group="", resource="pods", subresource="log", verb="get"
                    )
                )
            )
            result = client.AuthorizationV1Api(api).create_self_subject_access_review(review, _request_timeout=(3, 5))
            status = result.status
            if status is None or status.evaluation_error or status.allowed is None:
                raise RuntimeError("Permission review inconclusive")
            return status.allowed is True
    return True


def monitor_check() -> dict:
    from .monitor.cluster_monitor import _cluster_monitor

    if _cluster_monitor is None or not _cluster_monitor.running:
        return check(
            "monitor",
            "unhealthy",
            "Background monitor is not running.",
            "Inspect agent startup logs and restart after fixing dependencies.",
            "monitor_state",
        )
    return check(
        "monitor",
        "healthy",
        "Background monitor loop is running; scanner results not validated by this check.",
        "",
        "monitor_state",
    )


async def observed(check_id: str, probe: Callable, message: str, remediation: str, source: str) -> dict:
    try:
        result = await asyncio.wait_for(asyncio.to_thread(probe), timeout=6)
        return check(
            check_id,
            "healthy" if result is True else "unhealthy",
            message if result is True else "Probe failed or permission was denied.",
            "" if result is True else remediation,
            source,
        )
    except Exception as exc:
        code = getattr(exc, "status", None)
        # Never expose URLs, API response bodies, bearer tokens or DB exceptions.
        denied = code in (401, 403)
        return check(
            check_id,
            "unhealthy" if denied else "unknown",
            "Credentials rejected or access denied."
            if denied
            else "Probe unavailable or timed out; readiness cannot be established.",
            remediation,
            source,
        )


async def collect_readiness() -> dict:
    probes = [
        observed(
            "database",
            database_probe,
            "Existing database SELECT 1 health check passed; schema/migrations not validated.",
            "Check database connection configuration, PostgreSQL availability and agent logs.",
            "database_health_check",
        )
    ]
    for resource in ("pods", "deployments", "nodes", "events", "logs"):
        probes.append(
            observed(
                f"kubernetes_{resource}",
                lambda r=resource: kubernetes_probe(r),
                "Installation identity can access this resource."
                if resource != "logs"
                else "RBAC review allows get pods/log; actual log retrieval not probed.",
                f"Check cluster connectivity and installation service-account RBAC for {resource}; caller permissions are separate.",
                "self_subject_access_review" if resource == "logs" else "kubernetes_list_limit_1",
            )
        )
    checks = provider_checks() + await asyncio.gather(*probes) + [monitor_check()]
    statuses = {item["status"] for item in checks}
    return dict(
        status="degraded" if "unhealthy" in statuses else "unknown" if "unknown" in statuses else "healthy",
        checked_at=datetime.now(UTC).isoformat(),
        scope="agent installation credentials",
        checks=checks,
        limitations=[
            "Provider inference/connectivity is not probed; overall readiness remains unknown until separately demonstrated.",
            "These observations cover the three-incident prerequisites, not every scanner, namespace, caller permission, mutation permission or sustained incident recovery.",
            "Read-only lists retrieve at most one item; logs use an authorization review without retrieving user log data.",
        ],
    )
