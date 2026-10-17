"""One coherent, deterministic cluster for rendering what the view designer builds.

A production namespace with a crash-looping cache, a CPU-throttled API, a node
under pressure and two firing alerts. Everything the API and the metrics
endpoints return is derived from these tables, so a dashboard built against
it shows consistent numbers across widgets.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

_STARTED = datetime.now(UTC)


def ago(minutes: float) -> str:
    """An RFC 3339 timestamp ``minutes`` before the mock started."""
    return (_STARTED - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Objects default to a cluster that has been up for about two weeks.
CREATED = ago(15 * 24 * 60)

# Pods whose age is part of the story: the cache started crash looping three
# hours ago, and the staging pod has been unschedulable for 25 minutes.
POD_AGE_MINUTES = {"cache-7f9d8b-h3jxw": 180, "checkout-api-8b2c1-t6vns": 25}

NODES = [
    # name, roles, cpu cores, memory GiB, cpu use %, memory use %, ready, pressure
    ("master-0", ["control-plane", "master"], 8, 32, 38, 61, True, ""),
    ("worker-0", ["worker"], 8, 32, 82, 81, True, "MemoryPressure"),
    ("worker-1", ["worker"], 8, 32, 57, 56, True, ""),
    ("worker-2", ["worker"], 8, 32, 31, 35, True, ""),
]

NAMESPACES = ["production", "staging", "openshift-monitoring", "kube-system", "openshift-pulse"]

# deployment, namespace, replicas desired, image, cpu limit cores, memory limit MiB
DEPLOYMENTS = [
    ("checkout-api", "production", 2, "quay.io/shop/checkout:v2.4.1", 2.0, 2048),
    ("payments", "production", 2, "quay.io/shop/payments:v1.9.0", 1.0, 1536),
    ("cache", "production", 1, "docker.io/library/redis:7.2", 0.5, 512),
    ("frontend", "production", 2, "quay.io/shop/frontend:v3.0.2", 0.5, 768),
    ("search-indexer", "production", 1, "quay.io/shop/indexer:v0.8.3", 1.0, 1536),
    ("checkout-api", "staging", 1, "quay.io/shop/checkout:v2.5.0-rc1", 1.0, 1024),
]

# pod, deployment, namespace, node, phase, reason, restarts, cpu cores used, memory MiB used
PODS = [
    ("checkout-api-5d8f9c-x2k7q", "checkout-api", "production", "worker-0", "Running", "", 0, 1.62, 1900),
    ("checkout-api-5d8f9c-p9q4m", "checkout-api", "production", "worker-1", "Running", "", 1, 1.48, 1820),
    ("payments-6c4b7d-zz1kq", "payments", "production", "worker-1", "Running", "", 0, 0.81, 1200),
    ("payments-6c4b7d-ab3nd", "payments", "production", "worker-2", "Running", "", 0, 0.74, 1150),
    ("cache-7f9d8b-h3jxw", "cache", "production", "worker-0", "Running", "CrashLoopBackOff", 14, 0.02, 505),
    ("frontend-77a9c-k1mzt", "frontend", "production", "worker-2", "Running", "", 0, 0.47, 640),
    ("frontend-77a9c-q8wpl", "frontend", "production", "worker-0", "Running", "", 0, 0.44, 610),
    ("search-indexer-0", "search-indexer", "production", "worker-1", "Running", "", 3, 0.91, 1024),
    ("checkout-api-8b2c1-t6vns", "checkout-api", "staging", "worker-2", "Pending", "Unschedulable", 0, 0.0, 0),
    ("prometheus-k8s-0", "prometheus-k8s", "openshift-monitoring", "worker-1", "Running", "", 0, 0.65, 2400),
]

EVENTS = [
    # namespace, involved kind/name, type, reason, message, count, minutes ago
    ("production", "Pod/cache-7f9d8b-h3jxw", "Warning", "BackOff", "Back-off restarting failed container redis", 41, 2),
    (
        "production",
        "Pod/cache-7f9d8b-h3jxw",
        "Warning",
        "OOMKilled",
        "Container redis exceeded memory limit 512Mi",
        14,
        3,
    ),
    ("production", "Pod/checkout-api-5d8f9c-x2k7q", "Warning", "Unhealthy", "Readiness probe failed: timeout", 6, 9),
    (
        "staging",
        "Pod/checkout-api-8b2c1-t6vns",
        "Warning",
        "FailedScheduling",
        "0/4 nodes available: insufficient cpu",
        12,
        5,
    ),
    ("production", "Deployment/checkout-api", "Normal", "ScalingReplicaSet", "Scaled up replica set to 2", 1, 47),
]

ALERTS = [
    ("KubePodCrashLooping", "critical", "production", "cache-7f9d8b-h3jxw", "Pod production/cache is crash looping."),
    (
        "CPUThrottlingHigh",
        "warning",
        "production",
        "checkout-api-5d8f9c-x2k7q",
        "38% throttling of CPU in checkout-api.",
    ),
    ("KubeNodePressure", "warning", "", "worker-0", "worker-0 has MemoryPressure."),
]

SERVICES = [
    ("checkout-api", "production", 8080),
    ("payments", "production", 8080),
    ("cache", "production", 6379),
    ("frontend", "production", 80),
]

LOG_LINES = {
    "cache": [
        "1:M 16 Oct 2026 14:02:11.004 * Ready to accept connections tcp",
        "1:M 16 Oct 2026 14:05:40.118 # WARNING maxmemory reached (512mb), evicting keys",
        "1:M 16 Oct 2026 14:06:02.771 # OOM command not allowed when used memory > 'maxmemory'",
    ],
    "checkout-api": [
        "2026-10-16T14:02:11Z INFO  listening on :8080",
        "2026-10-16T14:05:40Z WARN  upstream payments slow (1.8s)",
        "2026-10-16T14:06:02Z ERROR payment call timed out after 2s",
    ],
}


def stable_unit(*parts: object) -> float:
    """A deterministic number in [0, 1) for a key, so reruns render identically."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def wave(base: float, amplitude: float, t: float, *key: object) -> float:
    """A smooth series around ``base``: one slow oscillation plus a small per-key offset."""
    phase = stable_unit(*key) * 2 * math.pi
    return max(0.0, base + amplitude * math.sin(t / 900 + phase))
