# Feature Proposal: OpenShift Console Readiness & Best Practices Platform

## Executive Summary

OpenShift customers consistently struggle with one question: **"Is my cluster production-ready?"**

The OCP console shows cluster health — pods running, operators available, nodes ready — but it doesn't tell customers whether their cluster follows best practices, whether it's configured correctly for their workload profile, or what they should do next. More critically, most customers use a fraction of the platform they're paying for and have no way to discover what they're missing.

This proposal adds a **Readiness & Best Practices Platform** to the OCP console: profile-aware readiness scoring across 7 domains (56 checks), organization-defined custom checklists, and OpenShift Lightspeed integration that analyzes cluster usage to recommend platform capabilities customers should adopt.

---

## Why This Matters

**1. "Day 2 Cliff"** — Clusters go live without readiness validation. No network policies, default SCCs, missing monitoring, no encryption at rest. These become support tickets or security incidents.

**2. No unified compliance view** — Checking best practices requires navigating dozens of console pages. No single view says "your security posture is 72% — here's what's missing."

**3. One-size-fits-all guidance** — A production cluster needs HA and encryption. A dev cluster doesn't. Edge clusters are single-node. The same checklist can't serve all of them.

**4. No value discovery** — The console shows what IS, not what SHOULD BE. Customers running ArgoCD don't get prompted to adopt Tekton. Customers with service mesh don't get told to enable mTLS. The platform has capabilities customers are paying for but never find.

**5. No way to codify org standards** — Every organization has internal requirements (labels, operators, node configs) that can't be automated as console checks today.

---

## Value Proposition

| | Customer Impact | Business Impact |
|---|---|---|
| **Risk reduction** | Catch misconfigurations before they become incidents or compliance violations | Fewer P1 escalations, lower support cost |
| **Faster Day 2** | Guided checklists replace weeks of tribal knowledge with hours of automation | Faster time-to-production = faster expansion |
| **Platform discovery** | Every check and AI recommendation teaches customers about capabilities they're paying for but not using | Drives adoption of ACM, ACS, RHOAI, Service Mesh, Tekton, OADP — each adopted capability deepens investment |
| **Org knowledge capture** | Custom checklists codify tribal knowledge; new team members inherit standards automatically | Increases switching costs — organizations encode their processes into the platform |
| **Support deflection** | "Why it matters" on every check educates in-context; Lightspeed handles remediation questions | Reduces repeat contacts and ticket volume |

### Competitive Position

| Platform | What They Have | What They Lack |
|----------|---------------|----------------|
| **Rancher/SUSE** | Production checklist docs, CIS benchmark scanning, NeuVector | No unified scoring dashboard, no profiles, no AI, no custom checks |
| **Tanzu/Broadcom** | MachineHealthCheck, OSS Health Assessment, Tanzu Labs (professional services) | Fragmented tools, no integrated dashboard, no profiles, no custom checks |
| **AWS EKS** | Cluster Insights (upgrade readiness), hardeneks CLI | Console only shows upgrade readiness, no domain scoring, no custom checks |
| **Azure AKS** | Azure Advisor (generic recommendations), community AKS Checklist | Generic Azure tool (not K8s-native), no profiles, no custom CRDs |

**Our differentiator:** No platform combines profile-aware scoring + domain audit panels + custom check CRDs + AI-powered usage recommendations in a single integrated console experience.

### Tier & Insights Opportunity
- Basic readiness checks in all tiers; AI recommendations and custom checklists as premium features
- Anonymized fleet-wide readiness data reveals which checks fail most often — informs docs, defaults, and product investment

---

## Feature Overview

An enhancement to the existing OCP console (not a standalone plugin):

- **Readiness Dashboard** — Overall score + per-domain scores with drill-down
- **7 Domain Overview Pages** — Security, Networking, Storage, Workloads, Compute, Observability, Identity — each with metric cards and audit checklists
- **7 Cluster Profiles** — Production, Development, Edge/SNO, AI/ML, Multi-Tenant, Disconnected, HPC — each tailors which checks are required, recommended, or N/A
- **Custom Checklists** — `ReadinessCheck` CRD for organization-defined checks, distributed via ACM policies
- **Lightspeed Advisor** — AI-powered recommendations based on cluster usage patterns

```
┌─────────────────────────────────────────────────┐
│  OCP Console (Administrator View)               │
│  ┌───────────────────────────────────────────┐  │
│  │  Readiness Dashboard                      │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐  │  │
│  │  │ Overall  │ │ Cluster  │ │ Custom   │  │  │
│  │  │ Score    │ │ Profile  │ │ Checks   │  │  │
│  │  │  78%     │ │Production│ │  3 orgs  │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘  │  │
│  │                                           │  │
│  │  Domain Scores                            │  │
│  │  ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐   │  │
│  │  │Sec  ││Net  ││Store││Work ││Obs  │   │  │
│  │  │ 85% ││ 70% ││ 90% ││ 65% ││ 80% │   │  │
│  │  └─────┘└─────┘└─────┘└─────┘└─────┘   │  │
│  │                                           │  │
│  │  ┌─ Lightspeed Advisor ────────────────┐  │  │
│  │  │ "Based on your GitOps usage, consider│  │  │
│  │  │  adopting Tekton + GitOps Promoter   │  │  │
│  │  │  for automated promotion pipelines." │  │  │
│  │  └─────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

---

## Proposed UI Mockups

PatternFly 6 (Compass theme), integrated into OCP console with sidebar navigation:

### Readiness Dashboard
![Readiness Dashboard](../../mockups/readiness-dashboard.png)

### Cluster Profile Wizard
![Profile Wizard](../../mockups/profile-wizard.png)

### Security Overview + Audit
![Security Audit](../../mockups/security-audit.png)

### Networking Overview + Audit
![Networking Audit](../../mockups/networking-audit.png)

### Storage Overview + Audit
![Storage Audit](../../mockups/storage-audit.png)

### Workloads Overview + Audit
![Workloads Audit](../../mockups/workloads-audit.png)

### Compute Overview + Audit
![Compute Audit](../../mockups/compute-audit.png)

### Observability Overview + Audit
![Observability Audit](../../mockups/observability-audit.png)

### Identity & Access Overview + Audit
![Identity Audit](../../mockups/identity-audit.png)

---

## Cluster Profiles

Administrators select (or auto-detect) a profile that tailors every check's severity:

| Profile | Use Case | Required | Recommended | Key Differentiators |
|---------|----------|----------|-------------|-------------------|
| **Production** | Full hardening | 48 | 14 | HA, encryption, TLS, PDBs, backups, quotas |
| **Development** | Fast iteration | 12 | 20 | Identity + quotas required; HA, encryption N/A |
| **Edge / SNO** | Resource-constrained | 15 | 10 | Workload partitioning, local storage; HA N/A |
| **AI/ML** | GPU workloads | 22 | 16 | GPU operator, RHOAI, node feature discovery |
| **Multi-Tenant** | Strong isolation | 30 | 12 | Network policies + quotas + RBAC per tenant |
| **Disconnected** | Air-gapped | 18 | 8 | Mirror registry, internal catalogs, CA trust |
| **HPC** | Low-latency batch | 16 | 10 | CPU pinning, NUMA, hugepages, SR-IOV |

**Auto-detection** uses: node topology (SNO → Edge, 3+ masters → Production), installed operators (GPU Operator → AI/ML, RHACM → Multi-cluster), hardware capabilities (`nvidia.com/gpu` → AI/ML), infrastructure provider, namespace patterns, and workload CRDs. Administrators override at any time.

---

## Custom Checklists

Organizations define checks as `ReadinessCheck` custom resources:

```yaml
apiVersion: console.openshift.io/v1alpha1
kind: ReadinessCheck
metadata:
  name: require-cost-center-label
  namespace: openshift-config
spec:
  displayName: "Cost Center Label Required"
  description: "All deployments must have a 'cost-center' label for chargeback"
  severity: required
  check:
    type: resource-label
    resource: { apiVersion: apps/v1, kind: Deployment }
    label: cost-center
  remediation:
    description: "Add metadata.labels.cost-center to the deployment"
```

**8 check types:** `resource-label`, `resource-annotation`, `operator-installed`, `operator-version`, `resource-exists`, `resource-field`, `prometheus-query`, `script` (CEL expression).

**Sharing:** Cluster-wide via `openshift-config`, namespace-scoped for teams, fleet-wide via ACM policies.

---

## Lightspeed Advisor

Extends OpenShift Lightspeed's cluster interaction capabilities with readiness-specific intelligence:

1. **Usage pattern analysis** — Detects what customers are doing (ArgoCD, service mesh, GPU workloads) and recommends what they should do next
2. **"Ask Lightspeed" on every failing check** — One-click remediation guidance
3. **Proactive recommendation cards** on the Readiness Dashboard
4. **BYO Knowledge** — Organizations feed internal runbooks for org-specific recommendations

**Example recommendations:**

| Pattern Detected | Recommendation |
|-----------------|----------------|
| ArgoCD with 23 apps, no CI/CD | Adopt Tekton + GitOps Promoter for automated promotion |
| Service mesh installed, mTLS permissive | Enable strict mTLS for zero-trust networking |
| 50+ deployments, no PDBs | Add PodDisruptionBudgets for upgrade safety |
| GPU nodes at 30% utilization | GPU time-slicing or MIG partitioning |
| Multiple clusters, no ACM | RHACM for unified policy and lifecycle |
| PVs at 85% capacity | Volume expansion + alerting before full |

---

## Implementation Phases

| Phase | Release | Scope |
|-------|---------|-------|
| **Foundation** | OCP 5.1 | Dashboard, 56 checks, 7 profiles with auto-detection, domain overview pages |
| **Customization** | OCP 5.2 | ReadinessCheck CRD, org-level sharing, ACM policy distribution |
| **Intelligence** | OCP 5.3 | Lightspeed Advisor, usage analysis, proactive recommendations |
| **Fleet** | OCP 5.4 | ACM hub readiness aggregation, fleet dashboard, compliance exports (SOC2, FedRAMP) |

---

## Success Metrics

| Metric | Target | Source |
|--------|--------|--------|
| Readiness adoption | 60% of clusters scored within 30 days | Telemetry |
| Support deflection | 25% fewer Day 2 config tickets | Support data |
| Capability adoption | 15% increase in operator installs from recommendations | Telemetry |
| Custom check usage | 30% of enterprise customers define ≥1 check in 90 days | Telemetry |
| Lightspeed engagement | 40% click-through on "Ask Lightspeed" | Analytics |

---

## Appendix: Domain Checklist Details

### Security (12 checks)

| Check | Description | Prod | Dev | Edge | AI/ML |
|-------|-------------|------|-----|------|-------|
| Identity Providers | OAuth configured (not kubeadmin-only) | Req | Req | Req | Req |
| Kubeadmin Removed | kubeadmin secret deleted | Req | Rec | Rec | Rec |
| TLS Security Profile | Intermediate or Modern (not Old) | Req | Rec | Req | Rec |
| Encryption at Rest | etcd encryption enabled | Req | N/A | Rec | Rec |
| Network Policies | Enforced in user namespaces | Req | Rec | N/A | Rec |
| External Secrets | External Secrets or Sealed Secrets operator | Req | Rec | Rec | Rec |
| SCC Audit | No unnecessary privileged SCCs | Req | Rec | Rec | Req |
| Pod Security Admission | Enforced (not privileged baseline) | Req | Rec | Rec | Req |
| Image Signature Verification | Container image signatures verified | Req | N/A | Req | Rec |
| ACS/StackRox Integration | Advanced Cluster Security deployed | Rec | N/A | N/A | Rec |
| Compliance Operator | OpenSCAP compliance scanning | Rec | N/A | Rec | N/A |
| Audit Log Forwarding | API audit logs sent to SIEM | Rec | N/A | Rec | Rec |

### Networking (8 checks)

| Check | Description | Prod | Dev | Edge | Multi-T |
|-------|-------------|------|-----|------|---------|
| Custom Ingress Certificate | Not using self-signed default | Req | Rec | Req | Req |
| TLS on All Routes | All routes have TLS termination | Req | Rec | Req | Req |
| Network Policy Coverage | % of namespaces with policies | Req | Rec | N/A | Req |
| Egress Restrictions | Default-deny egress in sensitive namespaces | Rec | N/A | N/A | Req |
| Service Mesh | Istio/OSSM installed for mTLS | Rec | N/A | N/A | Rec |
| NodePort Exposure | Minimize NodePort services | Req | Info | N/A | Req |
| DNS Configuration | Custom DNS resolvers | Req | Info | Req | Req |
| Ingress Controller Sharding | Multiple IngressControllers for isolation | Rec | N/A | N/A | Req |

### Storage (8 checks)

| Check | Description | Prod | Dev | Edge | AI/ML |
|-------|-------------|------|-----|------|-------|
| Default StorageClass | At least one default SC set | Req | Req | Req | Req |
| Reclaim Policy | Retain for production data | Req | N/A | Rec | Req |
| Volume Binding Mode | WaitForFirstConsumer | Req | Rec | Req | Req |
| CSI Drivers | At least one CSI driver installed | Req | Req | Req | Req |
| Volume Snapshots | VolumeSnapshot CRDs and controller | Rec | N/A | N/A | Rec |
| Storage Quotas | Per-namespace storage limits | Rec | Req | N/A | Req |
| Persistent Registry | Image registry not on emptyDir | Req | Rec | Req | Rec |
| Backup Solution | OADP or equivalent installed | Req | N/A | Rec | Req |

### Workloads (8 checks)

| Check | Description | Prod | Dev | Edge | AI/ML |
|-------|-------------|------|-----|------|-------|
| Resource Limits | All deployments have CPU/memory limits | Req | Rec | Req | Req |
| Health Probes | Liveness + readiness probes configured | Req | Rec | Req | Rec |
| Pod Disruption Budgets | PDBs on critical workloads | Req | N/A | N/A | Rec |
| Rolling Update Strategy | Not using Recreate for production | Req | N/A | N/A | Rec |
| High Restart Detection | Pods with >5 restarts | Info | Info | Info | Info |
| Replica Count | Critical deployments have 2+ replicas | Req | N/A | N/A | Rec |
| Anti-Affinity Rules | Spread across nodes/zones | Req | N/A | N/A | Rec |
| Resource Quota Enforcement | Quotas in all user namespaces | Req | Req | N/A | Req |

### Observability (6 checks)

| Check | Description | Prod | Dev | Edge | AI/ML |
|-------|-------------|------|-----|------|-------|
| Monitoring Stack | Prometheus + Alertmanager healthy | Req | Req | Req | Req |
| Log Forwarding | ClusterLogForwarder to external sink | Req | Rec | Rec | Req |
| Audit Logging | API server audit policy (not None) | Req | N/A | Rec | Req |
| Custom Alerts | At least one PrometheusRule defined | Rec | N/A | Rec | Rec |
| Dashboard Templates | Grafana dashboards for key metrics | Rec | N/A | N/A | Rec |
| Cluster Observability Operator | COO installed | Rec | N/A | N/A | Rec |

### Reliability (8 checks)

| Check | Description | Prod | Dev | Edge | AI/ML |
|-------|-------------|------|-----|------|-------|
| HA Control Plane | 3+ master nodes | Req | N/A | N/A | Req |
| Worker Availability | Minimum worker count per profile | Req | Req | N/A | Req |
| Cluster Autoscaling | ClusterAutoscaler configured | Rec | N/A | N/A | Req |
| Machine Health Checks | Auto-remediation for failed nodes | Req | N/A | N/A | Req |
| Update Channel | Stable (not candidate/fast) | Req | Rec | Req | Req |
| Cluster Up to Date | No pending updates >30 days | Req | Rec | Req | Req |
| Etcd Backup | Automated backup configured | Req | N/A | Rec | Req |
| GitOps Enabled | ArgoCD/Flux for declarative config | Rec | Rec | Rec | Rec |

### Identity & Access (6 checks)

| Check | Description | Prod | Dev | Edge | Multi-T |
|-------|-------------|------|-----|------|---------|
| RBAC Least Privilege | No unnecessary cluster-admin bindings | Req | Rec | Rec | Req |
| Service Account Audit | No SA with cluster-admin | Req | Rec | Rec | Req |
| Group-Based Access | Users assigned via groups | Rec | N/A | N/A | Req |
| Stale Binding Detection | Bindings for deleted users/SAs | Info | Info | Info | Info |
| Namespace Isolation | Per-team namespace boundaries | Rec | Rec | N/A | Req |
| Wildcard RBAC Detection | No `*` verbs or resources in roles | Req | Rec | Rec | Req |

---

## References

- [OpenShift Console Dynamic Plugin Architecture](https://github.com/openshift/enhancements/blob/master/enhancements/console/dynamic-plugins.md)
- [OpenShift Lightspeed](https://www.redhat.com/en/technologies/cloud-computing/openshift/lightspeed)
- [Lightspeed Console Plugin](https://github.com/openshift/lightspeed-console)
- [Red Hat AI Platform (RHAE)](https://www.redhat.com/en/about/press-releases/red-hat-delivers-accessible-open-source-generative-ai-innovation-red-hat-enterprise-linux-ai)
- [InstructLab / Granite Models](https://www.redhat.com/en/topics/ai/what-are-granite-models)
