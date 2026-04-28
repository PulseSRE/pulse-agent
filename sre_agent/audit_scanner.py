"""Audit log scanners — detect config changes, RBAC mutations, and suspicious events.

These scanners correlate K8s events with resource state to identify
changes that precede failures or represent security risks.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from .errors import ToolError
from .k8s_client import get_apps_client, get_core_client, get_custom_client, get_rbac_client, safe
from .monitor import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_WARNING, _make_finding, _skip_namespace

logger = logging.getLogger("pulse_agent")


def scan_config_changes() -> list[dict]:
    """Detect ConfigMap/Secret changes that may correlate with recent pod failures."""
    findings: list[dict] = []
    window_minutes = int(os.environ.get("PULSE_AGENT_AUDIT_CONFIG_WINDOW", "30"))
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)

    try:
        core = get_core_client()

        # Find recently modified ConfigMaps (using managedFields timestamps)
        configmaps = safe(lambda: core.list_config_map_for_all_namespaces())
        if isinstance(configmaps, ToolError):
            return findings

        recent_cms: list[dict] = []
        for cm in configmaps.items:
            ns = cm.metadata.namespace
            if _skip_namespace(ns):
                continue
            # Check managedFields for recent updates
            for mf in cm.metadata.managed_fields or []:
                if mf.time and mf.time > cutoff and mf.operation == "Update":
                    recent_cms.append(
                        {
                            "name": cm.metadata.name,
                            "namespace": ns,
                            "updated_at": mf.time,
                            "manager": mf.manager or "unknown",
                        }
                    )
                    break

        # Batch-fetch warning events once to avoid N+1 per-namespace API calls
        failure_events_by_ns: dict[str, list] = {}
        for reason in ("CrashLoopBackOff", "BackOff"):
            all_events = safe(
                lambda r=reason: core.list_event_for_all_namespaces(
                    field_selector=f"reason={r},type=Warning",
                    limit=200,
                )
            )
            if not isinstance(all_events, ToolError):
                for ev in all_events.items:
                    ev_ns = ev.metadata.namespace
                    failure_events_by_ns.setdefault(ev_ns, []).append(ev)

        # For each recently modified ConfigMap, check for pod failures in the same namespace
        for cm_info in recent_cms[:20]:
            ns = cm_info["namespace"]
            ns_events = failure_events_by_ns.get(ns, [])
            if not ns_events:
                continue

            # Check if any failure events happened after the config change
            for event in ns_events:
                event_time = event.last_timestamp or event.metadata.creation_timestamp
                if event_time and event_time > cm_info["updated_at"]:
                    pod_name = event.involved_object.name if event.involved_object else "unknown"
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING,
                            category="audit_config",
                            title=f"ConfigMap '{cm_info['name']}' change preceded pod failure",
                            summary=(
                                f"ConfigMap '{cm_info['name']}' was updated by {cm_info['manager']} "
                                f"in namespace {ns}, followed by pod '{pod_name}' entering {event.reason}. "
                                f"The config change may have caused the failure."
                            ),
                            resources=[
                                {"kind": "ConfigMap", "name": cm_info["name"], "namespace": ns},
                                {"kind": "Pod", "name": pod_name, "namespace": ns},
                            ],
                            confidence=0.72,
                        )
                    )
                    break  # One finding per ConfigMap

    except Exception as e:
        logger.error("Config change scan failed: %s", e)
    return findings


def scan_rbac_changes() -> list[dict]:
    """Detect new or modified RBAC bindings that may represent privilege escalation."""
    findings: list[dict] = []
    lookback_hours = int(os.environ.get("PULSE_AGENT_AUDIT_RBAC_LOOKBACK", "24"))
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)

    try:
        rbac = get_rbac_client()

        # Check ClusterRoleBindings for recent cluster-admin grants
        crbs = safe(lambda: rbac.list_cluster_role_binding())
        if isinstance(crbs, ToolError):
            return findings

        for crb in crbs.items:
            if crb.metadata.creation_timestamp and crb.metadata.creation_timestamp < cutoff:
                continue

            role_name = crb.role_ref.name if crb.role_ref else ""
            if role_name != "cluster-admin":
                continue

            # Only flag bindings created by humans (kubectl, oc, helm), not system controllers
            managers = {mf.manager or "" for mf in (crb.metadata.managed_fields or [])}
            user_managers = {"kubectl", "oc", "helm", "kubectl-edit", "kubectl-create"}
            if not managers & user_managers:
                continue  # Created by a controller/operator — expected on managed clusters

            subjects = crb.subjects or []
            subject_names = [f"{s.kind}/{s.name}" for s in subjects[:5]]

            findings.append(
                _make_finding(
                    severity=SEVERITY_CRITICAL,
                    category="audit_rbac",
                    title=f"New cluster-admin binding: {crb.metadata.name}",
                    summary=(
                        f"ClusterRoleBinding '{crb.metadata.name}' grants cluster-admin to "
                        f"{', '.join(subject_names)}. Created {crb.metadata.creation_timestamp.strftime('%Y-%m-%d %H:%M UTC')}."
                    ),
                    resources=[{"kind": "ClusterRoleBinding", "name": crb.metadata.name, "namespace": ""}],
                    confidence=0.95,
                )
            )

        # Check RoleBindings for wildcard permissions
        rbs = safe(lambda: rbac.list_role_binding_for_all_namespaces())
        if isinstance(rbs, ToolError):
            return findings

        for rb in rbs.items:
            if rb.metadata.creation_timestamp and rb.metadata.creation_timestamp < cutoff:
                continue
            ns = rb.metadata.namespace
            if _skip_namespace(ns):
                continue
            # Skip managed cluster system bindings
            rb_name = rb.metadata.name
            if (
                rb_name.startswith("dedicated-admin")
                or rb_name.startswith("system:")
                or rb_name.startswith("openshift-")
            ):
                continue

            role_name = rb.role_ref.name if rb.role_ref else ""
            # Check for bindings to cluster-admin or admin roles
            if role_name in ("cluster-admin", "admin", "edit") and rb.role_ref.kind == "ClusterRole":
                subjects = rb.subjects or []
                subject_names = [f"{s.kind}/{s.name}" for s in subjects[:5]]

                findings.append(
                    _make_finding(
                        severity=SEVERITY_WARNING,
                        category="audit_rbac",
                        title=f"New '{role_name}' role binding in {ns}",
                        summary=(
                            f"RoleBinding '{rb.metadata.name}' grants '{role_name}' ClusterRole to "
                            f"{', '.join(subject_names)} in namespace {ns}."
                        ),
                        resources=[{"kind": "RoleBinding", "name": rb.metadata.name, "namespace": ns}],
                        confidence=0.80,
                    )
                )

    except Exception as e:
        logger.error("RBAC change scan failed: %s", e)
    return findings


def scan_recent_deployments() -> list[dict]:
    """Detect deployment rollouts that may have caused issues."""
    findings: list[dict] = []
    window_minutes = int(os.environ.get("PULSE_AGENT_AUDIT_DEPLOY_WINDOW", "60"))
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)

    try:
        apps = get_apps_client()
        core = get_core_client()

        deploys = safe(lambda: apps.list_deployment_for_all_namespaces())
        if isinstance(deploys, ToolError):
            return findings

        # Batch-fetch warning events once to avoid N+1 per-deployment API calls
        all_warning_events = safe(
            lambda: core.list_event_for_all_namespaces(
                field_selector="type=Warning",
                limit=500,
            )
        )
        warning_events_by_obj: dict[str, list] = {}
        if not isinstance(all_warning_events, ToolError):
            for ev in all_warning_events.items:
                if ev.involved_object and ev.involved_object.name:
                    obj_key = f"{ev.metadata.namespace}/{ev.involved_object.name}"
                    warning_events_by_obj.setdefault(obj_key, []).append(ev)

        for dep in deploys.items:
            ns = dep.metadata.namespace
            if _skip_namespace(ns):
                continue

            revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "")

            # Check conditions for progressing
            conditions = dep.status.conditions or []
            for cond in conditions:
                if cond.type == "Progressing" and cond.status == "True":
                    if cond.last_transition_time and cond.last_transition_time > cutoff:
                        # Deployment is actively rolling out
                        desired = dep.spec.replicas or 0
                        available = dep.status.available_replicas or 0
                        unavailable = dep.status.unavailable_replicas or 0

                        if unavailable > 0 and available == desired:
                            # Only report when all desired replicas are available but
                            # extra pods are unavailable during rollout (surge).
                            # Skip when available < desired — the workloads scanner
                            # already catches that as "Deployment degraded".
                            # Look up related warning events from batch-fetched data
                            obj_key = f"{ns}/{dep.metadata.name}"
                            dep_events = warning_events_by_obj.get(obj_key, [])
                            event_reasons = list({e.reason for e in dep_events[:5]})

                            finding = _make_finding(
                                severity=SEVERITY_WARNING if available > 0 else SEVERITY_CRITICAL,
                                category="audit_deployment",
                                title=f"Deployment '{dep.metadata.name}' rollout with issues",
                                summary=(
                                    f"Deployment '{dep.metadata.name}' in {ns} is rolling out "
                                    f"(revision {revision}): {available}/{desired} available, "
                                    f"{unavailable} unavailable."
                                    + (f" Events: {', '.join(event_reasons)}" if event_reasons else "")
                                ),
                                resources=[{"kind": "Deployment", "name": dep.metadata.name, "namespace": ns}],
                                confidence=0.85,
                            )

                            # Score change risk
                            try:
                                from .change_risk import score_deployment_change

                                new_image = ""
                                for c in dep.spec.template.spec.containers or []:
                                    new_image = c.image or ""
                                    break
                                risk = score_deployment_change(
                                    deployment_name=dep.metadata.name,
                                    namespace=ns,
                                    new_image=new_image,
                                )
                                finding["riskScore"] = risk.score
                                finding["riskLevel"] = risk.level
                                finding["riskFactors"] = risk.factors
                            except Exception:
                                logger.debug(
                                    "Failed to compute change risk score for %s/%s",
                                    ns,
                                    dep.metadata.name,
                                    exc_info=True,
                                )

                            findings.append(finding)
                        break

    except Exception as e:
        logger.error("Deployment audit scan failed: %s", e)
    return findings


def scan_warning_events() -> list[dict]:
    """Surface high-frequency warning events that may indicate systemic issues."""
    findings: list[dict] = []
    threshold = int(os.environ.get("PULSE_AGENT_AUDIT_EVENT_THRESHOLD", "50"))

    try:
        core = get_core_client()
        events = safe(
            lambda: core.list_event_for_all_namespaces(
                field_selector="type=Warning",
            )
        )
        if isinstance(events, ToolError):
            return findings

        # Group events by reason + namespace
        event_groups: dict[str, list] = {}
        # Get the agent's own namespace to exclude self-generated noise
        agent_ns = os.environ.get("POD_NAMESPACE", "openshiftpulse")
        for event in events.items:
            ns = event.metadata.namespace
            if _skip_namespace(ns) or ns == agent_ns:
                continue
            key = f"{ns}:{event.reason}"
            if key not in event_groups:
                event_groups[key] = []
            event_groups[key].append(event)

        # Find groups exceeding threshold
        for key, group in event_groups.items():
            total_count = sum(e.count or 1 for e in group)
            if total_count < threshold:
                continue

            ns, reason = key.split(":", 1)
            sample = group[0]
            resource_kind = sample.involved_object.kind if sample.involved_object else "Unknown"
            resource_names = list({e.involved_object.name for e in group[:5] if e.involved_object})

            findings.append(
                _make_finding(
                    severity=SEVERITY_WARNING if total_count < 50 else SEVERITY_CRITICAL,
                    category="audit_events",
                    title=f"High-frequency '{reason}' events in {ns} ({total_count}x)",
                    summary=(
                        f"{total_count} '{reason}' warning events in namespace {ns} "
                        f"affecting {len(group)} {resource_kind}(s): {', '.join(resource_names[:3])}"
                        + (f" (+{len(resource_names) - 3} more)" if len(resource_names) > 3 else "")
                    ),
                    resources=[{"kind": resource_kind, "name": name, "namespace": ns} for name in resource_names[:5]],
                    confidence=0.78,
                )
            )

    except Exception as e:
        logger.error("Warning events scan failed: %s", e)
    return findings


def scan_auth_events() -> list[dict]:
    """Detect authentication anomalies — failed logins, kubeadmin usage, SA token creation."""
    findings: list[dict] = []

    try:
        core = get_core_client()

        # 1. Check for kubeadmin user (should be removed post-install)
        try:
            custom = get_custom_client()
            users = safe(lambda: custom.list_cluster_custom_object("user.openshift.io", "v1", "users"))
            if not isinstance(users, ToolError):
                for user in users.get("items", []):
                    if user.get("metadata", {}).get("name") == "kubeadmin":
                        findings.append(
                            _make_finding(
                                severity=SEVERITY_WARNING,
                                category="audit_auth",
                                title="kubeadmin account still exists",
                                summary=(
                                    "The emergency kubeadmin account has not been removed. "
                                    "This is a security risk — anyone with the kubeadmin password "
                                    "has full cluster access. Remove with: oc delete secret kubeadmin -n kube-system"
                                ),
                                resources=[{"kind": "User", "name": "kubeadmin", "namespace": ""}],
                                confidence=0.98,
                            )
                        )
                        break
        except Exception as e:
            logger.debug("user.openshift.io lookup skipped (non-OCP cluster): %s", e)

        # 2. Check for failed auth events in openshift-authentication namespace
        try:
            events = safe(
                lambda: core.list_namespaced_event(
                    "openshift-authentication",
                    field_selector="type=Warning",
                )
            )
            if not isinstance(events, ToolError):
                auth_failures = [
                    e
                    for e in events.items
                    if "fail" in (e.reason or "").lower() or "denied" in (e.message or "").lower()
                ]
                if len(auth_failures) >= 5:
                    findings.append(
                        _make_finding(
                            severity=SEVERITY_WARNING,
                            category="audit_auth",
                            title=f"Authentication failures detected ({len(auth_failures)} events)",
                            summary=(
                                f"{len(auth_failures)} authentication failure events in openshift-authentication. "
                                f"Recent: {auth_failures[0].message[:100] if auth_failures[0].message else 'unknown'}"
                            ),
                            resources=[{"kind": "Namespace", "name": "openshift-authentication", "namespace": ""}],
                            confidence=0.82,
                        )
                    )
        except Exception as e:
            logger.debug("openshift-authentication namespace not available (non-OCP cluster): %s", e)

        # 3. Check for recently created ServiceAccount tokens (potential token theft vector)
        secrets = safe(
            lambda: core.list_secret_for_all_namespaces(
                field_selector="type=kubernetes.io/service-account-token",
            )
        )
        if not isinstance(secrets, ToolError):
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            recent_tokens = []
            for secret in secrets.items:
                ns = secret.metadata.namespace
                if _skip_namespace(ns):
                    continue
                if secret.metadata.creation_timestamp and secret.metadata.creation_timestamp > cutoff:
                    recent_tokens.append({"name": secret.metadata.name, "namespace": ns})

            if len(recent_tokens) > 5:
                findings.append(
                    _make_finding(
                        severity=SEVERITY_INFO,
                        category="audit_auth",
                        title=f"{len(recent_tokens)} new service account tokens in 24h",
                        summary=(
                            f"{len(recent_tokens)} new ServiceAccount token secrets created in the last 24 hours. "
                            f"Review for unexpected token creation. "
                            f"Namespaces: {', '.join(list({t['namespace'] for t in recent_tokens[:5]}))}."
                        ),
                        resources=[
                            {
                                "kind": "Secret",
                                "name": recent_tokens[0]["name"],
                                "namespace": recent_tokens[0]["namespace"],
                            }
                        ],
                        confidence=0.65,
                    )
                )

        # 4. Check for new OAuthClient registrations (OpenShift)
        try:
            custom = get_custom_client()
            oauth_clients = safe(lambda: custom.list_cluster_custom_object("oauth.openshift.io", "v1", "oauthclients"))
            if not isinstance(oauth_clients, ToolError):
                cutoff = datetime.now(UTC) - timedelta(hours=24)
                for oc in oauth_clients.get("items", []):
                    created = oc.get("metadata", {}).get("creationTimestamp", "")
                    if created:
                        try:
                            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if created_dt > cutoff:
                                name = oc.get("metadata", {}).get("name", "unknown")
                                findings.append(
                                    _make_finding(
                                        severity=SEVERITY_WARNING,
                                        category="audit_auth",
                                        title=f"New OAuth client registered: {name}",
                                        summary=(
                                            f"OAuthClient '{name}' was created in the last 24 hours. "
                                            f"Verify this is an authorized application registration."
                                        ),
                                        resources=[{"kind": "OAuthClient", "name": name, "namespace": ""}],
                                        confidence=0.88,
                                    )
                                )
                        except (ValueError, TypeError):
                            logger.debug("Failed to parse OAuthClient creation timestamp", exc_info=True)
        except Exception:
            logger.debug("oauth.openshift.io may not exist on this cluster", exc_info=True)

    except Exception as e:
        logger.error("Auth events scan failed: %s", e)
    return findings
