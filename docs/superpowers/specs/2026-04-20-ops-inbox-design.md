# Ops Inbox — Proactive SRE Task List

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give SREs a proactive daily inbox of things that need attention before they become incidents — distinct from the incidents page (reactive, broken now) and the briefing banner (summary, not actionable).

**Architecture:** Task generators run at the end of each monitor scan cycle, upsert into a `proactive_tasks` table, and auto-resolve when conditions clear. Frontend polls a REST endpoint. Agent and users can create tasks via the `create_ops_task` tool.

**Tech Stack:** Python backend (FastAPI endpoints, monitor integration), React/TypeScript frontend (new top-level route), PostgreSQL persistence.

---

## 1. Data Model

Each ops inbox item is a `ProactiveTask`:

```
id: string (pt-{uuid12})
category: cert_expiry | trend_prediction | degraded_operator | upgrade_available
          | slo_burn | capacity | stale_finding | privileged_workloads
          | rbac_drift | network_policy_gaps | route_cert_expiry
          | service_endpoint_gaps | readiness_regressions
          | agent_recommendation | manual
title: string
detail: string (actionable guidance)
urgency_hours: float (estimated hours until this becomes an incident)
severity: critical | warning | info
source_kind: string | null (K8s resource kind)
source_name: string | null
source_namespace: string | null
blast_radius: int (downstream dependency count, 0 if unknown)
status: new | acked | snoozed | resolved | dismissed
snoozed_until: timestamp | null
created_at: timestamp
acked_by: string | null
acked_at: timestamp | null
resolved_at: timestamp | null
generator: string (generator function name or "agent" or "user")
```

**Dedup key:** `category + source_kind + source_name + source_namespace`. Each scan cycle upserts — if the task already exists and is still active, update `urgency_hours`. If the condition clears, set `status = resolved`.

**Snoozed tasks:** excluded from results until `snoozed_until < NOW()`, then flip back to `new`.

**Cleanup:** resolved/dismissed tasks older than 30 days pruned each scan cycle.

---

## 2. Task Generators

13 built-in generators, each a pure function returning `list[ProactiveTask]`:

### Infrastructure (7)

| Generator | Source | Urgency Calculation |
|-----------|--------|-------------------|
| `cert_expiry` | TLS secrets (type=kubernetes.io/tls) | hours until expiry |
| `trend_prediction` | 4 trend scanners (predict_linear) | predicted hours to breach |
| `degraded_operator` | ClusterOperator conditions | 0 if degraded >1h |
| `upgrade_available` | ClusterVersion.status.availableUpdates | 168h default (1 week) |
| `slo_burn` | SLO registry burn rates via Prometheus | hours until error budget exhausted |
| `capacity_projection` | metrics-server node allocatable vs requests | hours until node >90% |
| `stale_finding` | findings with no action >72h | 0 minus hours stale (negative = overdue) |

### Security (3)

| Generator | Source | Urgency |
|-----------|--------|---------|
| `privileged_workloads` | Pods with privileged SCC or runAsRoot | 24h |
| `rbac_drift` | ClusterRoleBindings with cluster-admin not in last audit | 12h |
| `network_policy_gaps` | Namespaces with no NetworkPolicy | 48h |

### Networking (2)

| Generator | Source | Urgency |
|-----------|--------|---------|
| `route_cert_expiry` | Route TLS certs (separate from service certs) | hours until expiry |
| `service_endpoint_gaps` | Services with 0 ready endpoints | 1h |

### Production Readiness (1)

| Generator | Source | Urgency |
|-----------|--------|---------|
| `readiness_regressions` | Readiness gate API — workloads that newly failed a gate after a redeploy | 24h |

### Registration

Generators register in a `TASK_GENERATORS` list (same pattern as `SCANNER_REGISTRY`). Adding a new generator: write a function returning `list[ProactiveTask]`, append to the list.

---

## 3. Scan Integration

Generators run at the end of each monitor scan cycle (60s interval) in `MonitorSession._run_scan_locked()`:

1. Call each generator → collect all `ProactiveTask` items
2. For each task, compute dedup key (`category:source_kind:source_name:source_namespace`)
3. Upsert into `proactive_tasks` table:
   - New key → INSERT with `status = 'new'`
   - Existing key with status `new|acked` → UPDATE `urgency_hours` (recalculate)
   - Existing key with status `snoozed` → skip (respect snooze)
   - Existing key with status `dismissed` → skip (respect dismiss)
4. For tasks in DB that were NOT generated this cycle and are `new|acked`:
   - Set `status = 'resolved'`, `resolved_at = NOW()`
5. Prune resolved/dismissed tasks older than 30 days

---

## 4. REST Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/ops-inbox` | token | List tasks. Query: `status` (new,acked,snoozed), `category`, sorted by urgency_hours ASC |
| `POST` | `/ops-inbox/{id}/ack` | token + owner | Acknowledge a task |
| `POST` | `/ops-inbox/{id}/snooze` | token + owner | Snooze. Body: `{hours: 24}` |
| `POST` | `/ops-inbox/{id}/dismiss` | token + owner | Dismiss permanently |

`GET /ops-inbox` response:
```json
{
  "tasks": [...],
  "counts": {"new": 3, "acked": 5, "snoozed": 2},
  "total": 10
}
```

---

## 5. Agent Tool

New `create_ops_task` tool registered in `sre_agent/ops_inbox.py`:

```python
@beta_tool
def create_ops_task(
    title: str,
    detail: str = "",
    urgency: str = "this_week",
    namespace: str = "",
    resource_name: str = "",
    resource_kind: str = "",
):
    """Add a proactive task to the ops inbox.

    Use when the user asks to track, remind, or follow up on something
    that isn't an active incident but needs future attention.

    Args:
        title: Short description (e.g. "Review HPA config before Black Friday")
        detail: Actionable guidance on what to do
        urgency: today (8h), this_week (168h), this_month (720h)
        namespace: Optional K8s namespace
        resource_name: Optional resource name
        resource_kind: Optional resource kind (Deployment, Node, etc.)
    """
```

Urgency mapping: `today=8, this_week=168, this_month=720`.

Category: `agent_recommendation` when called by agent during investigation, `manual` when triggered by user chat request.

---

## 6. Frontend

### Route & Navigation

- New route: `/ops-inbox`
- Nav tab: "Ops Inbox" with badge showing count of `new` tasks
- Position: after Pulse, before Compute (it's the #2 daily workflow after the overview)

### Layout

Single page, no sub-tabs:

```
┌──────────────────────────────────────────────────┐
│ Ops Inbox                         3 new · 8 total │
│ "5 items need attention today"                     │
├──────────────────────────────────────────────────┤
│ [All] [New] [Acked] [Snoozed]     [Category ▾]    │
├──────────────────────────────────────────────────┤
│ 🔴 TLS cert api-server expires in 2 days      NEW │
│    production · Blast: 4 deps                      │
│    Renew with cert-manager or rotate manually      │
│                         [Ack] [Snooze ▾] [Dismiss] │
├──────────────────────────────────────────────────┤
│ 🟡 Memory breach predicted in 18h            NEW  │
│    Node: worker-03 · Blast: 12 pods                │
│    Scale node pool or evict low-priority loads     │
│                         [Ack] [Snooze ▾] [Dismiss] │
├──────────────────────────────────────────────────┤
│                               [+ Add task]         │
└──────────────────────────────────────────────────┘
```

### Interactions

- **Sort:** urgency_hours ascending (most urgent first)
- **Click row:** expands inline with full detail + link to resource detail view
- **Ack:** marks as seen, moves to "acked", decrements badge
- **Snooze dropdown:** 4h, 24h, 3d, 1w. Hides until time expires, reappears as "new"
- **Dismiss:** permanently removes (confirm dialog for critical severity)
- **Add task:** inline form — title, detail, urgency dropdown (today/this week/this month), optional namespace
- **Badge:** nav tab shows `new` count only (not acked)
- **Polling:** `GET /ops-inbox` every 30 seconds

### Empty State

When no tasks: "All clear — nothing needs proactive attention" with prompt pills:
- "Scan for security gaps"
- "Check capacity projections"
- "Review cert expiry"

---

## 7. Differentiation from Incidents Page

| Aspect | Incidents Page | Ops Inbox |
|--------|---------------|-----------|
| **Trigger** | Something IS broken | Something WILL break |
| **Data** | Live scanner findings | Generator predictions + user tasks |
| **Urgency** | Now (minutes) | Hours to days |
| **Resolution** | Fix applied or self-healed | Condition clears or manual dismiss |
| **User model** | Triage & respond | Plan & prevent |

No cross-linking between the two — they serve different workflows. If an ops inbox item's condition worsens into an actual incident (e.g., cert expires), it auto-resolves in the inbox when the monitor creates a finding.

---

## 8. Database Migration (021)

```sql
CREATE TABLE IF NOT EXISTS proactive_tasks (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    urgency_hours REAL NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    source_kind TEXT,
    source_name TEXT,
    source_namespace TEXT,
    blast_radius INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    snoozed_until BIGINT,
    created_at BIGINT NOT NULL,
    acked_by TEXT,
    acked_at BIGINT,
    resolved_at BIGINT,
    generator TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proactive_tasks_status ON proactive_tasks (status);
CREATE INDEX IF NOT EXISTS idx_proactive_tasks_category ON proactive_tasks (category);
```

---

## 9. Acceptance Criteria

- [ ] 13 task generators run each scan cycle and populate proactive_tasks table
- [ ] Tasks auto-resolve when conditions clear
- [ ] Snooze respects duration and re-surfaces tasks
- [ ] Badge on nav tab shows unacked count
- [ ] Sorted by urgency_hours (most urgent first)
- [ ] Agent can create tasks via `create_ops_task` tool
- [ ] Users can create tasks via "Add task" button and chat ("remind me to...")
- [ ] No overlap with incidents page — different data, different workflow
- [ ] Manual browser verification of the full page
