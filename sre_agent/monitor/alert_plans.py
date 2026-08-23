"""Which plan template a firing alert should engage.

Every alert arrives as ``category="alerts"`` — a category no plan template
matches, so the phased-investigation machinery never engaged on alert-borne
incidents at all. Measured on the reference cluster: the live queue was
crashloopx12, alertsx6, workloadsx4, control_planex2, nodesx1 — and the
dominant incident (control-plane memory pressure, 10 recurrences in 2 days)
arrived under ``alerts``/``control_plane``, both planless, while 9 of 11
templates had never fired.

Same design as ``alert_layers``: the table is deliberately incomplete. An
alert nobody has classified simply gets no plan and falls back to the
freeform investigation path — guessing a template for an unknown alert would
run the wrong playbook with confidence. Additions are opt-in and reviewable.
"""

from __future__ import annotations

# Alert name → plan template incident_type. Only alerts whose right playbook
# is unambiguous; the incident_type must exist in sre_agent/plan_templates/
# (the contract suite cross-checks this).
_ALERT_TEMPLATE: dict[str, str] = {
    # Node pressure / availability → node-pressure-v1
    "KubeNodeNotReady": "nodes",
    "KubeNodeUnreachable": "nodes",
    "KubeNodeReadinessFlapping": "nodes",
    "NodeMemoryHighUtilization": "nodes",
    "NodeFilesystemAlmostOutOfSpace": "nodes",
    "NodeFilesystemSpaceFillingUp": "nodes",
    "HighOverallControlPlaneMemory": "nodes",
    "ControlPlaneNodeMemoryHigh": "nodes",
    # Workload rollout health → deployment-failure-v1
    "KubeDeploymentReplicasMismatch": "workloads",
    "KubeStatefulSetReplicasMismatch": "workloads",
    "KubeDeploymentRolloutStuck": "workloads",
    # Crashloop → crashloop-resolution-v1
    "KubePodCrashLooping": "crashloop",
    # Autoscaling ceiling → hpa-saturation-v1
    "KubeHpaMaxedOut": "hpa",
    "KubeHpaReplicasMismatch": "hpa",
    # Operator health → operator-degraded-v1
    "ClusterOperatorDegraded": "operators",
    "ClusterOperatorDown": "operators",
    # Certificate expiry → cert-expiry-v1
    "KubeClientCertificateExpiration": "cert_expiry",
}

# Finding categories that map wholesale onto an existing template. The
# control-plane liveness scanner reports master-node pressure as its own
# category; until a dedicated control-plane template exists, node-pressure-v1
# is the right playbook — control-plane pressure IS node pressure on masters.
_CATEGORY_TEMPLATE: dict[str, str] = {
    "control_plane": "nodes",
}


def plan_category_for(finding: dict) -> str:
    """The category to match plan templates with, honouring alert identity.

    For alert findings the title carries the alert name (the scanner sets
    ``title=alertname``); for everything else the category stands as-is.
    """
    category = str(finding.get("category", ""))
    if category == "alerts":
        mapped = _ALERT_TEMPLATE.get(str(finding.get("title", "")))
        if mapped:
            return mapped
    return _CATEGORY_TEMPLATE.get(category, category)
