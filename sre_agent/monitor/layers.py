"""Causal layers — which findings can explain which other findings.

At 20:35 during a control-plane outage the monitor produced fourteen findings
in one second: nine "Deployment degraded" rated critical, three pod restarts,
and one `etcdMemberCommunicationSlow` rated *warning*. The warning was the
cause of the other thirteen. Ranked by priority it did not make the top
thirteen, because severity is decided by whichever scanner produced the
finding and no scanner can see the others.

The missing idea is precedence. A pod cannot explain a failing API server; a
failing API server explains almost every pod on the cluster. That relation is
static, known in advance, and belongs next to the scanner registry rather than
in a model prompt.

Layers are ordered: a finding may only be explained by one strictly *below* it.
Nothing here decides that two findings are related — that is timing, and it
lives in ``episodes``. This module only says which direction an explanation is
allowed to flow.
"""

from __future__ import annotations

# Lower number = closer to the metal = more able to explain things above it.
L_INFRA = 0  # etcd, the API server, the nodes themselves
L_PLATFORM = 1  # operators and controllers that everything else depends on
L_WORKLOAD = 2  # ordinary application and add-on workloads
L_SIGNAL = 3  # alerts, audit trails, posture — derived observations

LAYER_NAMES = {
    L_INFRA: "infrastructure",
    L_PLATFORM: "platform",
    L_WORKLOAD: "workload",
    L_SIGNAL: "signal",
}

# Keyed by the finding's `category`, which is what a finding actually carries —
# the scanner name is not on it by the time ranking happens.
CATEGORY_LAYER: dict[str, int] = {
    # ── infrastructure ────────────────────────────────────────────────────
    "control_plane": L_INFRA,
    "nodes": L_INFRA,
    "memory_pressure": L_INFRA,
    "disk_pressure": L_INFRA,
    # ── platform ──────────────────────────────────────────────────────────
    "operators": L_PLATFORM,
    "stuck": L_PLATFORM,
    "hot_loop": L_PLATFORM,
    "daemonsets": L_PLATFORM,
    # ── workload ──────────────────────────────────────────────────────────
    "crashloop": L_WORKLOAD,
    "workloads": L_WORKLOAD,
    "oom": L_WORKLOAD,
    "image_pull": L_WORKLOAD,
    "scheduling": L_WORKLOAD,
    "hpa": L_WORKLOAD,
    # ── signal ────────────────────────────────────────────────────────────
    "alerts": L_SIGNAL,
    "slo_burn": L_SIGNAL,
    "security": L_SIGNAL,
    "cert_expiry": L_SIGNAL,
    "errors": L_SIGNAL,
    "monitoring": L_SIGNAL,
    "audit_config": L_SIGNAL,
    "audit_rbac": L_SIGNAL,
    "audit_deployment": L_SIGNAL,
    "audit_events": L_SIGNAL,
    "audit_auth": L_SIGNAL,
}

# Findings about Pulse itself. They are never anyone's symptom and never
# anyone's cause — an outage does not make a scanner broken, and a broken
# scanner does not crash pods. Kept out of episodes entirely.
STANDALONE_CATEGORIES = frozenset({"degraded"})

# A finding that describes the future, or a standing posture, is never the
# cause of something happening now. Found the hard way: running the full
# scanner set against a live cluster produced seven episodes headed by
# "Certificate expiring in 9d", between them absorbing 21 of 23 findings. A
# certificate that expires next week did not crash a pod this morning.
#
# The test for heading an episode is not "is this important" but "is this
# happening now, and can it propagate downward".
NON_CAUSAL_CATEGORIES = frozenset(
    {
        "cert_expiry",  # expires in N days — has not happened yet
        "memory_pressure",  # predict_linear forecast
        "disk_pressure",  # predict_linear forecast
        "security",  # standing posture, not an event
        "monitoring",  # observations about the monitoring stack
    }
)


def layer_for_finding(finding: dict) -> int:
    """The causal layer of a specific finding, honouring an explicit override.

    A scanner that knows more about its own output than the category does may
    set ``layer`` on the finding. The firing-alert scanner is the case this
    exists for: every alert carries ``category="alerts"``, but an alert about
    node memory and an alert about a scrape target are not the same kind of
    fact, and only the scanner is holding the alert name that tells them apart.
    """
    explicit = finding.get("layer")
    if isinstance(explicit, int) and explicit in LAYER_NAMES:
        return explicit
    return layer_of(finding.get("category", ""))


def is_non_causal_finding(finding: dict) -> bool:
    """Whether this finding is a forecast or a standing posture.

    Category-level for most scanners; a per-finding ``posture`` flag for
    alerts, where one scanner emits both events and standing configuration.
    """
    if finding.get("posture") is True:
        return True
    return finding.get("category", "") in NON_CAUSAL_CATEGORIES


def can_head_episode_finding(finding: dict) -> bool:
    """Finding-aware form of :func:`can_head_episode`."""
    category = finding.get("category", "")
    if category in STANDALONE_CATEGORIES or is_non_causal_finding(finding):
        return False
    if finding.get("findingType", "current") != "current":
        return False
    return layer_for_finding(finding) <= L_PLATFORM


def can_explain_finding(cause: dict, symptom: dict) -> bool:
    """Finding-aware form of :func:`can_explain`."""
    if cause.get("category", "") in STANDALONE_CATEGORIES:
        return False
    if symptom.get("category", "") in STANDALONE_CATEGORIES:
        return False
    if is_non_causal_finding(symptom):
        return False
    return layer_for_finding(cause) < layer_for_finding(symptom)


def can_head_episode(category: str, finding_type: str = "current") -> bool:
    """Whether a finding of this kind may be the cause an episode is built around.

    Three ways to fail: it is about Pulse itself, it forecasts rather than
    observes, or it sits too far up the stack to explain anything beneath it.
    """
    if category in STANDALONE_CATEGORIES or category in NON_CAUSAL_CATEGORIES:
        return False
    if finding_type != "current":
        # Trend and prediction findings describe what has not happened yet.
        return False
    return layer_of(category) <= L_PLATFORM


# An unknown category sits at workload level: it can be explained by
# infrastructure, and it will not silently swallow anything beneath it.
DEFAULT_LAYER = L_WORKLOAD


def layer_of(category: str) -> int:
    """The causal layer a finding of this category belongs to."""
    return CATEGORY_LAYER.get(category, DEFAULT_LAYER)


def layer_name(category: str) -> str:
    return LAYER_NAMES[layer_of(category)]


def can_explain(cause_category: str, symptom_category: str) -> bool:
    """True if a finding of the first category may be the cause of the second.

    Strictly below, never equal: two crashlooping pods in different namespaces
    are not evidence about each other, and letting a layer absorb its own peers
    is how one noisy finding would swallow a whole cluster's worth of unrelated
    ones. Burst correlation already groups same-layer siblings and is the right
    tool for that job.
    """
    if cause_category in STANDALONE_CATEGORIES or symptom_category in STANDALONE_CATEGORIES:
        return False
    # A forecast or a standing posture is nobody's symptom either. Seen on a
    # live cluster: 117 "Security: Resource Limits" findings attached to an
    # etcd write failure, because a posture finding sits at the signal layer
    # and the layer test alone said yes. Having too many privileged containers
    # is a property of the cluster, not something etcd did to it.
    if symptom_category in NON_CAUSAL_CATEGORIES:
        return False
    return layer_of(cause_category) < layer_of(symptom_category)
