# Durable plan execution on Temporal

## Why

The in-process engine (`plan_runtime.py`) runs a plan as an asyncio task inside
the agent pod. The original implementation had two structural limitations:

- **Executions die with the pod.** `_record_execution` writes only at the end,
  so a restart mid-plan loses the run entirely — no record, no resume. This is the same ephemerality problem as
  runtime artifacts (migration 036), one level up: definitions are durable now,
  executions were not.
- **Approval cannot wait.** `approval_required` phases are marked
  `needs_escalation` and skipped, because an in-process engine cannot afford to
  block for hours. Human-in-the-loop plans are structurally impossible.

## Shape

One **interpreter workflow** (`PulsePlanWorkflow`) executes *any* plan
definition by walking its phase graph — including plans created in the UI at
runtime. A new plan is data, not a deploy; that is the extensibility story.

```
POST /plan-templates/{type}/run ──► start_workflow(PlanRunInput)
                                         │
                    ┌────────────────────┴───────────────────┐
                    │ PulsePlanWorkflow (deterministic)       │
                    │  load_plan ─ pins the definition        │
                    │  loop: ready_phases → run_plan_phase    │
                    │  approval_required → wait for signal    │
                    │  record_plan_execution                  │
                    └────────────────────┬───────────────────┘
                                activities (all IO)
                        run_plan_phase → PlanRuntime._execute_phase
                        (contract check + retry-with-gap, unchanged)
```

- **Decisions** are pure functions in `temporal/sequencing.py` — testable
  without Temporal, incapable of IO.
- **Activities** reuse the engine wholesale: `run_plan_phase` calls
  `PlanRuntime._execute_phase`, so the produces-contract check and the
  retry-with-the-gap-named behaviour are identical on both paths.
- **The plan is pinned at start**: `load_plan` runs once and the definition
  rides in workflow state, so editing a plan changes the *next* run, never one
  in flight — matching what version history gives edits at rest.
- **Approval is a signal** (`approve_phase`), delivered by
  `POST /workflow-runs/{id}/approve`. The workflow waits up to
  `PULSE_AGENT_TEMPORAL_APPROVAL_TIMEOUT` (default 24h); on timeout or denial
  the phase records `needs_escalation` — exactly what the in-process engine
  records immediately, so ignoring a request degrades to today's behaviour.
- **Progress is a query**: the UI polls `GET /workflow-runs/{id}`, which asks
  the workflow itself what ran and what is waiting.

## What routes where

| Path | Engine |
|---|---|
| Monitor's automatic plan execution | in-process (unchanged) |
| `POST /plan-templates/{type}/run` (UI "Run durably") | Temporal |
| Branching, ready-wave parallel phases, and subplans | supported by the interpreter; decisions are replay-safe and subplans execute as child workflows |

Durable execution is disabled until
`PULSE_AGENT_TEMPORAL_HOST` is set; without it the run endpoints answer 503
with the exact variable to configure.

## Worker

Runs inside the agent pod as a lifespan task when a host is configured — no new
Deployment for v1, and it shares the agent's credentials, tools and database
exactly as the in-process engine does. Splitting it out later is an operator
change, not a code change: `temporal/worker.py` is already the entrypoint.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PULSE_AGENT_TEMPORAL_HOST` | `""` (disabled) | Temporal frontend, e.g. `temporal-frontend.temporal.svc:7233` |
| `PULSE_AGENT_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `PULSE_AGENT_TEMPORAL_TASK_QUEUE` | `pulse-plans` | Task queue the worker polls |
| `PULSE_AGENT_TEMPORAL_APPROVAL_TIMEOUT` | `86400` | Seconds an approval phase waits for a human |

## Infrastructure and limitations

The operator now owns `spec.temporal` and can provision the server and inject agent configuration. Follow the operator's current CRD/README for persistent storage, PostgreSQL access, image, and service names. A disposable Temporal dev server is useful for testing but is not a production durability guarantee. External service/TLS/auth support must be checked against the actual client configuration; a host string alone is not a verified Temporal Cloud integration.

`PULSE_AGENT_DURABLE_AUTOFIX` (default false) selects the durable incident-remediation path after approval. The incident workflow and ordinary plan workflow are distinct; inspect `temporal/incident_workflow.py` and related activities for snapshot, verification, cancellation, and fallback behavior. Temporal orchestration does not itself supply Kubernetes authorization. Workers share service credentials. Durable start, approve, and cancel endpoints require `require_admin`. A write-enabled phase must declare `approval_required`, receive an affirmative signal, and run under server trust >=2; other phases cannot authorize writes. Caller tokens are not written into workflow history. The explicit approval activity argument is guarded by a Temporal patch marker; old-history argument shapes remain compatible and omitted approval defaults to denied. Validate these controls with actual cluster RBAC before enabling writes.

## Testing

`tests/test_temporal_plans.py` runs the real workflow on Temporal's
time-skipping test environment with stub activities registered under the real
names: dependency ordering, approval, denial, and the 24-hour timeout (instant
under time-skipping) are all executed, not mocked. Sequencing decisions are
additionally covered as pure functions. The test server binary is downloaded
and cached by `temporalio` on first use.
