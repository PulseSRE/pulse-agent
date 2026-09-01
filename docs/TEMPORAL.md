# Durable plan execution on Temporal

## Why

The in-process engine (`plan_runtime.py`) runs a plan as an asyncio task inside
the agent pod. Two structural consequences, both observed on dev05:

- **Executions die with the pod.** `_record_execution` writes only at the end,
  so a restart mid-plan loses the run entirely — no record, no resume. The
  agent pod was rolled five times in a single working day of releases; every
  in-flight plan died silently each time. This is the same ephemerality bug as
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
| Plans using `branch_on` / `branches` / `parallel_with` | in-process only; the run endpoint refuses them by name |

Nothing existing changes behaviour. The whole package is inert until
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

## The open infrastructure decision

The code is complete and dark. Turning it on requires a Temporal server, and
that is a deployment decision, not a code one:

- **Self-hosted** (Temporal OSS via its Helm chart or operator, backed by the
  existing PostgreSQL or its own): no external dependency, runs air-gapped,
  but it is a real stateful service to operate — schema upgrades, visibility
  store, retention.
- **Temporal Cloud**: nothing to operate, per-action pricing, but an external
  dependency and egress from the cluster.

For dev05 the pragmatic first step is the single-binary dev server
(`temporalio/auto-setup` image or `temporal server start-dev`) in a
`temporal` namespace — sufficient to exercise everything here end to end,
explicitly not durable enough to *be* the durability story in production.
The operator should eventually own this surface as `spec.temporal` on the CR.

## Testing

`tests/test_temporal_plans.py` runs the real workflow on Temporal's
time-skipping test environment with stub activities registered under the real
names: dependency ordering, approval, denial, and the 24-hour timeout (instant
under time-skipping) are all executed, not mocked. Sequencing decisions are
additionally covered as pure functions. The test server binary is downloaded
and cached by `temporalio` on first use.
