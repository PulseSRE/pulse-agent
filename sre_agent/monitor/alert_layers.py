"""What a firing alert is *about*, so it can take part in causation.

Every alert used to arrive as ``category="alerts"``, which the layer model
reads as the signal layer — derived observation, never a cause. That is true of
some alerts and badly wrong for others. ``HighOverallControlPlaneMemory`` is a
fact about the machines; ``CsvAbnormalFailedOver2Min`` is a fact about the
operator lifecycle everything else installs through. Filing both alongside
``TargetDown`` threw away the only thing that made them different.

Measured on the reference cluster: 15 of 15 standing findings were alerts, so
no episode could form at all — while a single investigation of one of those
alerts correctly tied four of them into one story. The deterministic layer knew
less than the model did, about data it already had.

The table is deliberately incomplete. An alert nobody has classified stays at
the signal layer, where it can be a symptom but never a cause, because the
alternative — guessing that an unknown alert may explain other things — is how
one noisy alert swallows a cluster. Promotion is opt-in and reviewable.
"""

from __future__ import annotations

from .layers import L_INFRA, L_PLATFORM, L_SIGNAL, L_WORKLOAD

# Alerts that describe the machines and the control plane itself. These can
# explain almost anything above them.
_INFRA_ALERTS = frozenset(
    {
        "KubeAPIDown",
        "KubeAPIErrorBudgetBurn",
        "KubeAPITerminatedRequests",
        "KubeClientErrors",
        "KubeletDown",
        "KubeNodeNotReady",
        "KubeNodeUnreachable",
        "KubeNodeReadinessFlapping",
        "NodeClockNotSynchronising",
        "NodeFilesystemAlmostOutOfSpace",
        "NodeFilesystemSpaceFillingUp",
        "NodeMemoryHighUtilization",
        "NodeNetworkInterfaceFlapping",
        "NodeWithoutOVNKubeNodePodRunning",
        "ControlPlaneNodeMemoryHigh",
        "ControlPlaneNodeMemoryCritical",
        "HighOverallControlPlaneMemory",
        "SystemMemoryExceedsReservation",
        "ExtremelyHighIndividualControlPlaneMemory",
        "etcdMembersDown",
        "etcdNoLeader",
        "etcdHighFsyncDurations",
        "etcdHighCommitDurations",
        "etcdMemberCommunicationSlow",
        "etcdInsufficientMembers",
        "etcdHighNumberOfFailedProposals",
        "etcdGRPCRequestsSlow",
    }
)

# Operators and controllers that other things depend on to run at all.
_PLATFORM_ALERTS = frozenset(
    {
        "ClusterOperatorDown",
        "ClusterOperatorDegraded",
        "ClusterOperatorFlapping",
        "CsvAbnormalFailedOver2Min",
        "CsvAbnormalOver30Min",
        "InstallPlanStepAppliedWithWarnings",
        "OperatorHubSourceError",
        "KubeControllerManagerDown",
        "KubeSchedulerDown",
        "KubeStateMetricsListErrors",
        "MachineConfigControllerPausedPoolKubeletCA",
        "MCDPivotError",
        "MCDRebootError",
        "SDNPodNotReady",
        "OVNKubernetesControllerDisconnectedSouthboundDatabase",
        "IngressControllerDegraded",
        "IngressControllerUnavailable",
    }
)

# Ordinary workloads. Explained by the two layers above, explaining nothing but
# the signal layer.
_WORKLOAD_ALERTS = frozenset(
    {
        "KubePodCrashLooping",
        "KubePodNotReady",
        "KubeDeploymentReplicasMismatch",
        "KubeDeploymentGenerationMismatch",
        "KubeStatefulSetReplicasMismatch",
        "KubeDaemonSetRolloutStuck",
        "KubeDaemonSetNotScheduled",
        "KubeJobFailed",
        "KubeJobNotCompleted",
        "KubeContainerWaiting",
        "KubeHpaMaxedOut",
        "KubeHpaReplicasMismatch",
        "KubePersistentVolumeFillingUp",
        "KubePersistentVolumeErrors",
        "ArgoCDSyncAlert",
        "SearchPVCNotPresent",
    }
)

# Standing configuration and advice, not events. An alert here is neither a
# cause nor a symptom — the same treatment `security` posture findings already
# get, and for the same reason. `AlertmanagerReceiversNotConfigured` had been
# firing for fifty hours on the reference cluster: nobody had configured a
# receiver, which no outage caused and which caused no outage.
POSTURE_ALERTS = frozenset(
    {
        "AlertmanagerReceiversNotConfigured",
        "InsightsRecommendationActive",
        "InsightsRecommendationCritical",
        "ClusterNotUpgradeable",
        "MultipleContainersOOMKilled",
        "PodDisruptionBudgetAtLimit",
        "PodDisruptionBudgetLimit",
        "TelemeterClientFailures",
    }
)


def alert_layer(alertname: str) -> int:
    """The causal layer this alert belongs to.

    Unknown alerts stay at the signal layer: able to be explained, never able
    to explain. Being wrong in that direction costs a missed correlation; being
    wrong the other way costs a wrong one, which is worse — a wrong episode
    tells an operator a confident story about a cause that is not the cause.
    """
    if alertname in _INFRA_ALERTS:
        return L_INFRA
    if alertname in _PLATFORM_ALERTS:
        return L_PLATFORM
    if alertname in _WORKLOAD_ALERTS:
        return L_WORKLOAD
    return L_SIGNAL


def is_posture_alert(alertname: str) -> bool:
    """Whether this alert describes a standing configuration rather than an event."""
    return alertname in POSTURE_ALERTS
