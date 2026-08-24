"""Can the agent actually perform this fix? Ask the API server, don't hope.

The agent's ClusterRole is read-only unless the operator's
``spec.agent.allowWriteOperations`` is enabled — yet the fix planner happily
proposed pod deletions, an operator clicked Approve, and execution died on a
403 the agent could have predicted. A proposal the proposer provably cannot
execute is not a proposal; it is a dead-end button.

This module preflights a fix strategy's writes with SelfSubjectAccessReview.
The gate fires only on an affirmative denial from the API server: if the
check itself errors, the fix proceeds and execution reports the real outcome
— a failed permission *check* must never be treated as a permission denial.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("pulse_agent.monitor")

# The write each strategy performs: (apiGroup, resource, verb).
# Strategies absent here (noops, require_human_review) write nothing.
_STRATEGY_WRITES: dict[str, list[tuple[str, str, str]]] = {
    "restart_controller": [("", "pods", "delete")],
    # patch_image patches the owning Deployment, with a pod-delete fallback
    "patch_image": [("apps", "deployments", "patch"), ("", "pods", "delete")],
    "patch_resources": [("apps", "deployments", "patch")],
}

# (strategy, namespace) -> (allowed, reason, checked_at). RBAC changes rarely;
# a short TTL keeps one scan cycle from issuing an SSAR per finding.
_cache: dict[tuple[str, str], tuple[bool, str, float]] = {}
_CACHE_TTL = 300.0


def remediation_text(verb: str, resource: str, namespace: str) -> str:
    return (
        f"The agent's service account cannot {verb} {resource} in namespace '{namespace}' — "
        "its RBAC is read-only. Enable write operations on the OpenShiftPulse CR "
        "(spec.agent.allowWriteOperations: true) to let approved fixes execute, "
        "or apply this fix manually."
    )


def can_execute(strategy: str, namespace: str) -> tuple[bool, str]:
    """Whether the agent's own credentials can perform this strategy's writes.

    Returns (True, "") when allowed, when the strategy writes nothing, or when
    the check itself could not be completed. Returns (False, remediation) only
    on an affirmative denial.
    """
    writes = _STRATEGY_WRITES.get(strategy)
    if not writes:
        return True, ""

    cached = _cache.get((strategy, namespace))
    if cached and time.time() - cached[2] < _CACHE_TTL:
        return cached[0], cached[1]

    allowed, reason = True, ""
    try:
        from kubernetes import client

        from ..k8s_client import get_authorization_client

        auth = get_authorization_client()
        for group, resource, verb in writes:
            review = client.V1SelfSubjectAccessReview(
                spec=client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=client.V1ResourceAttributes(
                        group=group,
                        resource=resource,
                        verb=verb,
                        namespace=namespace,
                    )
                )
            )
            resp = auth.create_self_subject_access_review(review)
            if resp.status is not None and resp.status.allowed is False:
                allowed = False
                reason = remediation_text(verb, resource, namespace)
                break
    except Exception:
        # An unverifiable check is not a denial — let execution find out.
        logger.debug("RBAC preflight unavailable for %s in %s; not blocking", strategy, namespace, exc_info=True)
        return True, ""

    _cache[(strategy, namespace)] = (allowed, reason, time.time())
    if not allowed:
        logger.info("RBAC preflight: %s denied in %s", strategy, namespace)
    return allowed, reason


def clear_cache() -> None:
    """For tests, and for callers that know RBAC just changed."""
    _cache.clear()
