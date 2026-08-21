# Node pressure and eviction

Load this when pods are being evicted, when a node reports MemoryPressure or
DiskPressure, or when workloads are Pending with no obvious quota problem.

## Read the condition, not the utilisation

A node at 92% memory with no `MemoryPressure` condition is not evicting anything.
Kubelet evicts on *its* thresholds, not on what the metrics dashboard shows.

- `MemoryPressure=True` — kubelet is below `memory.available` and is evicting
- `DiskPressure=True` — below `nodefs.available` or `imagefs.available`
- `PIDPressure=True` — rare, usually a process leak in one workload

`describe_node` gives the conditions with reason and message; `get_node_metrics`
gives utilisation. You need both: the condition tells you kubelet is acting, the
utilisation tells you how far from recovery it is.

## Distinguish the three causes

1. **One workload regressed.** A single pod's usage climbed. `list_pods` on the
   node and compare against what that workload normally uses. This is the common
   case and the fix is that workload's limits, not the node.
2. **The node is genuinely undersized.** Aggregate requests approach allocatable
   with no single outlier. Adding capacity is the real fix; evicting just moves it.
3. **Disk, not memory.** Image cache growth and log volume are the usual causes,
   and they present as eviction of pods that are not using much memory at all.

## Mitigation order

Least disruptive first, and stop as soon as the condition clears:

1. Fix the outlier workload's requests/limits if there is one.
2. Cordon the node to stop new scheduling — this is reversible and buys time.
3. Drain only if the node must be taken out of service. Draining a node under
   pressure moves the pressure to its neighbours; if the cluster is uniformly tight,
   draining starts a cascade.
4. Add capacity.

## What not to do

- **Do not drain multiple nodes concurrently.** If aggregate capacity is the
  problem, this converts a degraded cluster into an outage.
- **Do not delete evicted pods to "clean up"** before reading them. Evicted pod
  records carry the reason and the resource that triggered eviction.
- **Do not raise limits to stop eviction** without checking whether the workload's
  usage is legitimate. It converts an eviction into an OOMKill later.

## What to report

The node, which condition is true and why, whether it is one workload or aggregate
pressure, the specific workloads affected, and a mitigation with its blast radius
stated. If capacity is the cause, say that plainly rather than recommending a drain.
