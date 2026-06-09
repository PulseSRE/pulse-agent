# Agentic Applied AI for OpenShift — Enhanced Proposal

## Executive Summary

OpenShift must evolve from a reactive infrastructure platform to an anticipatory, agentic ecosystem that detects problems before they surface, prepares evidence-backed remediation plans, and executes approved changes with full audit trails and safety guarantees.

This proposal defines a four-phase roadmap (2026–2028) built on three foundational principles proven in production prototyping:

1. **Task-first, not alert-first** — Raw signals are never exposed directly to operators. A deterministic correlation pipeline filters, deduplicates, and groups signals into issues. Only issues with sufficient evidence and a coherent remediation plan become actionable tasks. This eliminates alert fatigue and ensures operators focus on work that matters.

2. **Deterministic policy before AI reasoning** — Rules, correlation, and policy gates process signals before any LLM call. AI is the last resort for complex reasoning, not the first step for every alert. This reduces token costs by 60-80%, improves latency, and makes behavior predictable.

3. **Earned autonomy, not assumed automation** — Automation levels are earned through verified successful outcomes, not granted by category. A remediation that succeeds and is verified can be auto-approved next time. One that fails requires human review. Trust is built through evidence, not configuration.

---

## Architecture: The Signal-to-Task Pipeline

Every agentic capability in this proposal flows through a single, unified pipeline:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  Signals                    Deterministic Layer          AI Layer        │
│  ────────                   ──────────────────           ────────        │
│                                                                         │
│  Prometheus alerts ─┐                                                   │
│  OVN-K flows ───────┤       ┌──────────────┐     ┌─────────────────┐   │
│  Audit events ──────┤──────▶│  Correlation  │────▶│   The Brain     │   │
│  Scanner findings ──┤       │  & Policy     │     │   (investigate, │   │
│  etcd metrics ──────┤       │  Gate         │     │    plan,        │   │
│  Pipeline runs ─────┤       │              │     │    recommend)   │   │
│  GitOps drift ──────┘       │  Dedup       │     └────────┬────────┘   │
│                             │  Noise score │              │             │
│                             │  Suppress    │              ▼             │
│                             │  Auto-resolve│     ┌─────────────────┐   │
│                             └──────────────┘     │   Task Queue    │   │
│                                    │             │   (structured   │   │
│                                    │             │    work items   │   │
│                                    ▼             │    with plans)  │   │
│                             ┌──────────────┐     └────────┬────────┘   │
│                             │    Watch     │              │             │
│                             │  (what the   │              ▼             │
│                             │   system is  │     ┌─────────────────┐   │
│                             │   analyzing) │     │   Execute       │   │
│                             └──────────────┘     │   (approve →    │   │
│                                                  │    run →        │   │
│                                                  │    verify →     │   │
│                                                  │    learn)       │   │
│                                                  └─────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**What this means concretely:** When a Prometheus alert fires for high etcd latency, it doesn't immediately become a task or trigger an LLM call. The correlation layer checks: is this transient? (noise score). Has this been seen before? (dedup). Is there an auto-resolution rule? (policy gate). Only if the signal survives these filters AND the Brain can attach evidence and a recommended action does it become an operator task. This approach reduces LLM usage by 60-80% compared to sending every alert to an AI agent.

---

## Operator Surfaces

Operators interact with the system through four distinct surfaces:

| Surface | Purpose | What It Shows |
|---------|---------|--------------|
| **Tasks** | The primary workspace — only human-actionable work with coherent plans | Ready, In Progress, Blocked, Needs Approval items with evidence, recommended actions, confidence scores |
| **Watch** | What the system is doing NOW — builds trust through transparency | Signals under analysis, grouped issues, auto-remediations in progress, suppressions |
| **History** | Full operational audit and narrative | Completed tasks, approvals, executions, verifications, rollbacks, postmortems |
| **Readiness** | Cluster health baseline and best-practice compliance | Per-cluster readiness scores, domain audits, cluster profiles, compliance mapping |

**Why this matters:** The original proposal focused on agent capabilities (what agents DO) without specifying the operator experience (how operators SEE and CONTROL). Without structured surfaces, agent output is scattered, invisible, and untrusted.

---

## Task Lifecycle

Every agent-generated plan follows a structured lifecycle:

```
ready → accepted → in_progress → [blocked | waiting_for_approval] → done → history
```

**Key properties:**
- Tasks include `why_now` (evidence), `recommended_next_step`, confidence score, risk level, blast-radius assessment
- Approval is over an exact, immutable change-set digest — if the cluster state changes between approval and execution, the approval is invalidated and must be re-obtained
- Completed tasks move to History; recurrent issues reopen or link to previous tasks
- If an operator's cluster access expires, their assigned tasks return to the team queue

---

## Safety Model

### Execution Identity

Every action has a clear answer to "whose credentials executed this?"

| Action Type | Identity Used | Why |
|-------------|--------------|-----|
| Background observation (scans, metrics) | Observer service account (read-only) | Continuous, automated — doesn't need user context |
| Sensitive reads (logs, secrets, exec) | User's own cluster credentials | Audit trail shows who saw what |
| Mutations (scale, restart, apply) | User's own cluster credentials + approval | RBAC enforcement + accountability |

### Approval Model

- Approvals are tied to an exact change-set digest (not generic "approve category X")
- If issue, resources, or change-set drift before execution → approval invalidated, must re-obtain
- Very high-risk actions require fresh reauthentication regardless of session state
- Every approval is logged with approver identity, timestamp, and change-set hash

### Autonomy Tiers (Per-Domain)

| Level | Name | Behavior | Default |
|-------|------|----------|---------|
| L0 | Monitor Only | Observe and report; no remediation | — |
| L1 | Suggest | Propose plans; human must initiate all actions | All domains at launch |
| L2 | Ask First | Propose + queue for approval; execute on approval | Earned per domain |
| L3 | Auto-Fix Safe | Execute low-risk remediations automatically; log + verify | Earned through verified outcomes |
| L4 | Full Auto | Execute all remediations; human can kill/rollback | Explicit opt-in only |

Tiers are configurable **per domain** (upgrade, security, pipeline, networking, virtualization), not just globally. A customer can run L3 for troubleshooting while keeping upgrades at L1.

### Constitutional Rules (Never-Rules)

- Never auto-apply red-team findings
- Never execute without audit trail
- Never access customer data for model training
- Never bypass RBAC — always use the operator's own credentials for mutations
- Never approve a stale plan — re-validate before execution
- Kill switch available at every level: per-task, per-domain, per-cluster, per-fleet

---

## Phase 1 (2026): Foundation & Trust — Technology Preview

Ship with full audit, kill switches, and human-in-the-loop for all actions.

### Core Infrastructure

| Component | Description |
|-----------|-------------|
| Signal-to-Task Pipeline | Scanners → correlation → dedup → noise scoring → policy gate → investigation → task creation |
| Watch Surface | Live view of what the system is analyzing, grouping, suppressing, and investigating |
| Task Queue | Structured work items with evidence, plans, confidence scores, approval state |
| Readiness Dashboard | Production readiness scoring with cluster profiles (production, dev, edge, VM, AI/ML, multi-tenant, disconnected) |
| Approval Engine | Change-set-digest-based approvals with drift invalidation |
| Workflow Runtime | Durable execution engine — investigations, approvals, and verifications survive crashes |
| Eval Framework | Per-use-case eval suites with scenario-based scoring, release gating, and baseline comparison |

### Use Cases

| Use Case | Scope | Signal Source | Plan Output |
|----------|-------|--------------|-------------|
| **Autonomous troubleshooting** | Fleet-correlated diagnose → evidence plan → approved remediate | Prometheus, events, logs, scanner findings | Root-cause analysis with evidence, blast-radius, recommended fix, rollback path |
| **Noise reduction** | Learn normal patterns, auto-group related alerts, propose suppression rules | Alert history, transient signal tracking | Suppression rules, SLO-based alert policies, routing changes |
| **Agentic upgrade** | Generate/edit/execute upgrade plans with canary waves and rollback per site | OCP version, operator compatibility, cluster health | Wave plan with per-site rollback, kill switch per wave, OLM operator sequencing |
| **Pipeline failure analysis** | TaskRun/log correlation across fleet; root-cause with citations | Tekton PipelineRun, TaskRun logs, GitOps state, quota events | Root-cause hypothesis with evidence links, suggested pipeline/task edits |
| **Pipeline authoring** | AI-assisted Task/Pipeline YAML with promotion flows | Developer Hub context, existing pipeline patterns | Generated YAML with PR-first apply, human review gate |
| **Networking (ServiceMesh)** | Mesh telemetry analysis, policy drift detection | OSSM metrics, mTLS status, traffic flows | Traffic-shift, mTLS, retry/circuit-breaker change plans with blast-radius analysis |

### Confidence Scoring (Mandatory)

Every agent output includes:
- **Confidence score** (0-100%) on the diagnosis and recommended action
- **Evidence chain** — what data supported the conclusion
- **Alternatives considered** — what other hypotheses were evaluated and rejected
- **Risk level** — what could go wrong if the recommended action is taken

### Noise Learning (From Day 1)

The noise reduction system tracks:
- **Transient signal counts** — how often a signal appears and disappears within N scan cycles
- **Noise scores** — per-finding scores that increase with transience, decrease with persistence
- **Suppression thresholds** — configurable per-domain; signals above threshold are auto-suppressed
- **Learning feedback** — if a suppressed signal later causes an incident, the threshold adjusts

---

## Phase 2A (2026–1H 2027): Fleet Intelligence & GA

Promote core solutions to GA. Invest in fleet-level intelligence and advanced domains.

### Fleet Intelligence

| Capability | Description |
|-----------|-------------|
| Cross-cluster correlation | Same issue across multiple clusters → single fleet-level task with per-cluster details |
| Fleet-wide plans | Upgrade, security, and GitOps plans that operate across the fleet with site-level rollback |
| Sovereign hubs | Regional inference, audit, and memory — no mandatory cross-border egress |
| Federated read mesh | Global ops visibility across hubs; writes stay local to sovereign zone |

### Expanded Domains

| Domain | Key Capabilities |
|--------|-----------------|
| **Security (Blue Team)** | ACS + ACM policy violation analysis → ranked remediation plans with SoD approval |
| **Security (Red Team)** | Adversarial agent probes agents and skills → hardening proposals (never auto-applied) |
| **Networking (OVN-K)** | Connectivity diagnosis, NetworkPolicy gap detection, DNS troubleshooting → evidence-backed policy updates |
| **OpenShift Virtualization** | VM scheduling failures, migration readiness, storage/CNV capacity → wave plans for VM fleets |
| **GitOps drift & promotion** | Desired vs live state scanning, ApplicationSet skew → corrective PRs with blast-radius control |
| **Control-plane health** | etcd latency/size/compaction monitoring → defrag/cleanup plans gated by admin approval |
| **OADP resilience** | Backup job monitoring, RPO/RTO tracking → restore drill planning, disaster recovery runbooks |

### Governance Enhancements

| Enhancement | Description |
|-------------|-------------|
| **Per-domain autonomy tiers** | L0-L4 per domain via policy overlays, not global opt-in |
| **Plan conflict detection** | Two plans touching the same cluster/namespace → merge, lock, or precedence rules |
| **Maintenance window integration** | Respect freeze periods and ACM placement policies before execute |
| **Dry-run mode** | Shadow execution mandatory for risk >70; available for all plans |
| **Provenance** | Sign/attest generated YAML with SLSA-style lineage before PR-first apply |

---

## Phase 2B (2027): Observability Enhancements

| Use Case | Description |
|----------|-------------|
| **Rightsizing** | Fleet health and capacity planner using long-horizon telemetry → scale/rebalancing plans per site |
| **AI/ML guardrails** | GPU-heavy workload monitoring → scale caps, priority classes, scheduled downscaling to prevent runaway spend |
| **Instrumentation coach** | Inspect services for missing/low-value metrics/traces/logs → generate OTel definitions |
| **Log-to-action pipeline** | Cluster recurring log patterns → "fix recipes" (config cleanups, quota adjustments) for approval |
| **Topology-aware trace fabric** | Live service map from OTel data → validate critical paths are traced → auto-suggest coverage updates |
| **Telemetry cost optimizer** | Adjust sampling, filtering, and routing via OTel Collector → keep debugging data, reduce noise and spend |
| **FinOps** | Token consumption reporting, cost attribution per tenant, chargeback, model downgrade on budget exhaust |

---

## Phase 3 (2H 2027+): Autonomy & Expansion

| Use Case | Description |
|----------|-------------|
| **Self-upgrading platform** | Meta-agent watches OpenShift health, proposes upgrades, runs canary rollouts, rolls back failures |
| **Self-healing platform** | Fully autonomous troubleshooting and remediation with audit trail and daily reporting |
| **Persona-based agents** | Per-role agent customization (SRE, security, developer, capacity planner) |
| **VM GA** | Full VM lifecycle management with migration, capacity planning, and DR |

---

## Multi-Tenancy & Sovereignty

Built into every phase, not a late add-on.

### Multi-Tenancy (MSP / Shared Hub)

- **FleetPartition** = tenant boundary (customer, business unit, environment)
- Isolated: agent enablement, token budgets, audit retention, memory, RBAC
- Per-tenant constitutional overlays (tighten only)
- No cross-partition plans, credentials, or memory bleed

### Sovereignty (Regulated / Regional)

- Regional sovereign hubs — inference, audit, memory remain in-region
- Federated read mesh for global visibility; writes stay local
- Landing-zone templates: sovereign-strict (propose-only, on-cluster LLM, no memory export)
- Air-gapped enclave profile mirrors sovereign controls without federation

---

## Agentic Lightspeed Enhancements

### AL1: Failure Mode Handling
- LLM degradation: queue → fallback model → hard stop with human escalation
- Loop detection: cap replan retries, detect no-progress cycles, auto-escalate with preserved evidence

### AL2: Quota Management
- Token budgets as cluster-wide resources (like CPU/memory)
- Per-tenant quotas, chargeback, automatic model downgrade on budget exhaust

### AL3: Pre-Remediation Safety
- Snapshot/backup verification before destructive operations
- Dependency ordering (CRD → operator → workload)
- Change-set digest validation against approval

### AL4: Interpretability & Causal Debugging
- Every agent run emits structured trace: chain-of-thought, uncertainty scores, alternatives considered, memory entries that influenced decisions
- Traces stored, indexed, queryable: "show me every run where the agent considered deleting a resource but didn't"
- When verification fails, operators see WHY, not just "Failed"

### AL5: Data Flywheel (Cross-Fleet Memory)
- **Schema**: incidents, runbooks, patterns, feedback signals
- **Query**: similarity search, category matching, evidence fingerprinting
- **Decay**: TTL, relevance scoring, confidence degradation over time
- **Fleet**: cross-cluster knowledge sharing (within tenant boundaries)
- **Learning**: successful remediation patterns increase automation confidence for similar future issues

### AL6: Real-Time Agent Observability
- Live token streams, tool call graphs, cost attribution visible in one pane
- Per-investigation: current reasoning step, tools queued, tokens burned, cost accruing
- Pause, inspect, and resume any in-flight run
- Kill and rollback with one action
- **Brain Usage View** in the UI: cache hit rate, cost per investigation, most expensive categories, repeated-investigation avoidance rate

### AL7: Continuous Self-Improvement
- Propose → Execute → Verify → Learn → Improve next proposal
- Track which analysis patterns predicted success, which execution paths failed verification
- Auto-tune suppression thresholds, investigation strategies, and delegation graphs
- **Not model fine-tuning** — operational outcome data drives policy changes

### AL8: Sub-Second Cold Start
- Warm sandbox pools, pre-provisioned skill sidecars, cached LLM contexts
- Proposal-to-action latency under 1 second for read-only analysis
- Execution still uses full isolation

### AL9: Inference Economics & Model Routing
- Routing layer arbitrates between frontier cloud models, in-cluster GGUF, and task-specific small models
- Cost, latency, and capability tradeoffs are automatic
- Model tiers: Utility (fast/cheap), Interactive (conversational), Reasoning (complex diagnosis), Synthesis (narrative generation)

---

## Evaluation Framework

"95% accuracy" is not a target without a measurement system. Every solution must:

### Eval Architecture

| Component | Description |
|-----------|-------------|
| **Eval suites** | Per-use-case scenario collections (upgrade, troubleshoot, security, pipeline, networking, etc.) |
| **Scenarios** | Concrete input → expected output pairs with scoring dimensions |
| **Dimensions** | Resolution correctness, efficiency (token cost), safety (no harmful actions), speed (time-to-task) |
| **Release gating** | New versions must meet minimum score thresholds before shipping |
| **Baseline comparison** | Detect regressions by comparing current scores against saved baselines |
| **Ablation testing** | Remove components to measure their contribution to overall quality |

### Quality Metrics (Per Use Case)

| Metric | Description | Target |
|--------|-------------|--------|
| Plan correctness | Does the plan actually fix the problem? | ≥95% |
| False-positive rate | Issues that aren't real problems | ≤5% |
| Remediation success | Execution achieves desired state (verified) | ≥95% |
| Rollback success | Rollback restores previous state when needed | 100% |
| Noise suppression accuracy | Suppressed signals that would have been noise | ≥90% |
| Investigation reuse rate | Percentage of investigations served from cached artifacts | ≥50% |

---

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|-------------|
| Fleet MTTR | −40% from baseline | Compare MTTR before/after for same issue categories |
| Agentic upgrade adoption | ≥80% of eligible clusters | Percentage of clusters using UpgradePlan |
| Troubleshoot success with remediation | ≥95% verified success | Post-remediation verification pass rate |
| Pipeline plan adoption | ≥50% of pipeline incidents | Percentage of Tekton failures with agent-generated plans |
| Multi-domain adoption | ≥2 domains per customer | Domains with L2+ autonomy tier |
| Sovereign hub compliance | 100% in-region audit | Zero cross-border egress for sovereign customers |
| Unaudited production mutations | 0 | Audit coverage of all cluster-modifying actions |
| LLM cost per investigation | Decreasing QoQ | Token cost per investigation category |
| Investigation cache hit rate | ≥50% | Percentage of investigations served from cached artifacts |
| Task-to-resolution time | <30 min for P1 | Time from task creation to verified resolution |

---

## References

- [OpenShift Lightspeed](https://www.redhat.com/en/technologies/cloud-computing/openshift/lightspeed)
- [Lightspeed Console Plugin](https://github.com/openshift/lightspeed-console)
- [Red Hat AI Platform (RHAE)](https://www.redhat.com/en/about/press-releases/red-hat-delivers-accessible-open-source-generative-ai-innovation-red-hat-enterprise-linux-ai)
- [OpenShift Console Dynamic Plugin Architecture](https://github.com/openshift/enhancements/blob/master/enhancements/console/dynamic-plugins.md)
