# Changelog

All notable changes to Pulse Agent are documented in this file.

## v2.30.1 (2026-09-01)

### Routing made a Prometheus call it threw away
- The selector eval's latency gate failed CI at 5,171ms against a 500ms bound — the same assertion an earlier fix was supposed to have protected by disabling the LLM fallback. That fix closed one of two doors. The other was `SkillSelector.select`, which calls `get_context_for_selector()` to fetch SLO status from Prometheus; on a runner with no Prometheus, one scenario stalled for five seconds
- The call was also pointless in most cases: the result is only used `if context is not None`, so every routing call without a context — including every one the eval makes, and a large share of production routing — paid for a round-trip whose answer was discarded. It is now skipped when there is nowhere to put it, which is why recall is unchanged at 0.9831 with p99 down from 559ms to 6.6ms locally
- `llm_fallback_disabled` becomes `offline_routing`, named for the property rather than the one mechanism it originally covered. New tests assert *that nothing reaches out* under it, so a third network path added to routing fails with an explanation instead of as an unexplained p99 regression
- Assert the property, not the number: the latency bound could only say something was slow, and it took two rounds to find out what

## v2.30.0 (2026-09-01)

### Durable runs you can see and stop
- `GET /workflow-runs` lists recent durable runs from Temporal's own visibility store, and `POST /workflow-runs/{id}/cancel` stops one. Cancellation was the largest Temporal capability sitting unused here: a run wedged in an LLM phase was released only by its full timeout, and nothing short of the Temporal CLI could show what was in flight
- Cancelling an incident run means *undo*, not "stop watching". Cancellation lands as `CancelledError` at whatever await the workflow holds — most often the half-hour recurrence timer, long after the fix mutated the cluster — and letting it propagate would end the run with the cluster changed and no outcome recorded: exactly the vanishing-verdict failure fixed in v2.29.1, arriving through a door we had just built. The workflow now restores the snapshot and records a `cancelled` verdict, under `asyncio.shield` because a cancelled workflow cannot otherwise schedule the activities that do the undoing
- The test for that was checked by removing the handler: it turns red with `WorkflowFailureError` and no result, which is the symptom itself. `list_runs` is likewise exercised against a real server rather than a mock, since its entire surface is SDK attribute names a mock would confirm and reality would not
- Runs now carry a memo — finding, strategy, namespace, resource — so a listing reads "restart_controller on web-1 in payments" instead of thirty rows of `incident-<uuid>`. Memo rather than registered search attributes deliberately: memo needs no namespace registration, so this works on any Temporal instead of only after an operator ran a schema command. The cost is that memo is not searchable server-side, which is the next step when the run count makes filtering matter

### Releases are now a consequence of tagging
- v2.28.0, v2.29.0 and v2.29.1 were tagged and shipped with no GitHub Release behind them, because creating one was a manual step. `version-sync.yml` compares the two repos' latest *releases* to catch agent/UI skew, so it was comparing two stale numbers three versions behind what actually ran and reporting green about a pair it no longer measured — a gate passing on absence rather than evidence, which is a shape this repo keeps rediscovering
- `release.yml` creates the Release from the tag's own CHANGELOG section on tag push, in both this repo and pulse-ui. The notes now always exist, and the skew gate compares what shipped

## v2.29.1 (2026-09-01)

### A failed apply must still produce a verdict
- Found by running `IncidentWorkflow` against the live Temporal server rather than only the test environment: snapshot completed, `apply_fix` failed on a missing pod, and the workflow died with `WorkflowExecutionFailed` having recorded no outcome at all. The dispatched action would sit with no verdict forever — strictly worse than the inline path it replaces, which records a failure. It is also the verdict-from-nothing shape this repo has been bitten by before
- The apply failure now compensates defensively (the failure may follow a partial mutation; restore is a no-op when there is nothing to undo), records a `failed` verdict with the cause, and skips verification — there is nothing to verify when the fix never applied
- The time-skipping tests, mypy and 3,342 unit tests all passed while this bug existed. Same species as the missing `db.commit()` and the optional-SDK import earlier in the day: local green is not the same as working

## v2.29.0 (2026-09-01)

### The fix lifecycle, durably — the system now uses Temporal rather than offering it
- `IncidentWorkflow` expresses Pulse's fix lifecycle as one durable workflow. Every mechanism it needs already existed, hand-rolled: the approval wait was a future with a DB-polled timeout (281 of 368 actions on dev05 expired unseen), the snapshot was captured and usually used, verification was probed on later scan cycles, the grace window was "3 scans" lost on restart, and the recurrence window was a 1800s database re-read. They become a signal, the saga's compensation, a retry policy, its backoff, and a durable timer
- The interesting property is not that each piece works — they did — but that the *sequence* becomes atomic. A restart between "applied the fix" and "verified it" loses the verification entirely, leaving a mutation nobody confirmed. The agent pod rolled five times during one working day of releases
- A failed verification now *restores the pre-write snapshot* rather than merely reporting failure — saga compensation using `snapshot.py`, which already existed for exactly this and had no orchestration to trigger it
- `PULSE_AGENT_DURABLE_AUTOFIX` (off by default, `spec.agent.durableAutoFix` on the CR) routes the monitor's auto-fix through it. Approval is already settled when `_execute_fix` runs, so the workflow does not re-ask and the WebSocket approval UI is untouched. The action records as `dispatched`; the verdict arrives from the workflow once verification and the recurrence window have actually run
- Fallback is the load-bearing behaviour: flag off, no Temporal host, unreachable server or dispatch error all run inline exactly as before. Losing durability is worse than the inline path; dropping an approved fix is worse than both

## v2.28.0 (2026-09-01)

### Plan runs that survive the pod, and approvals that actually wait
- The in-process engine runs a plan as an asyncio task inside the agent: `_record_execution` writes only at the end, so a restart mid-plan loses the run entirely. The agent pod was rolled five times in a single working day of releases and every in-flight plan died unrecorded. Same ephemerality bug as migration 036, one level up — definitions became durable, executions had not
- `approval_required` phases were marked `needs_escalation` and skipped, because an in-process engine cannot afford to block for hours. Human-in-the-loop plans were structurally impossible
- `temporal/` adds a durable path on Temporal. One interpreter workflow executes *any* plan definition by walking its phase graph, so a plan created in the UI runs durably with no deploy — a new workflow is data, not code. Decisions are pure functions in `sequencing.py`; activities reuse `PlanRuntime._execute_phase` wholesale, so the `produces`-contract check and retry-with-the-gap-named behave identically on both paths
- Approval is a signal the workflow waits on for `PULSE_AGENT_TEMPORAL_APPROVAL_TIMEOUT` (default 24h); denial or timeout records `needs_escalation` — exactly today's behaviour, so ignoring a request regresses nothing. Progress is a workflow query the UI polls
- The plan definition is pinned at workflow start: editing a plan changes the *next* run, never one in flight, mirroring what version history gives edits at rest
- Routing is deliberately narrow: the monitor's automatic execution is untouched, only `POST /plan-templates/{type}/run` goes durable, and plans using `branch_on`/`parallel_with` are refused by name rather than run half-faithfully

### Workflow changes are a versioned operation
- A running workflow replays its history against whatever code the worker has now, and approval waits are meant to last days — so a logic change would break every in-flight run. Closed before rollout rather than after the first nondeterminism error: the worker stamps `build_id=pulse-{version}`, and the patch discipline (`workflow.patched` for command-sequence changes; comments, logging and activity bodies never need it) is documented in the workflow's own docstring, with tests asserting the seams exist

### Everything is inert until configured
- No host configured means no worker starts and the run endpoints answer 503 naming the variable to set. The operator's `spec.temporal` (v0.6.1) provisions the server and injects the host

## v2.27.0 (2026-08-25)

### Nothing the agent wrote at runtime survived a restart
- Skills created with `create_skill`, scaffolded from a verified trajectory, or refined in place by the learning flywheel were written to `PULSE_AGENT_USER_SKILLS_DIR` (default `/tmp/pulse_agent/skills`) or into the installed package directory. Verified on the live pod: both are the container's overlay filesystem, the only PVC is mounted at `/memory` and sits empty, and no env var points skills anywhere durable. Every restart, deploy and reschedule erased them
- The same was true of three more things: plan templates (the `PUT /plan-templates` handler rewrites YAML *inside the installed package*, so an operator's edit survived until the next rollout), scaffolded eval scenarios and replay fixtures (losing the eval loses the proof that the skill scaffolded beside it still works), and the `.versions/` directory that was supposed to make a skill edit reversible and was as ephemeral as the skill itself
- This silently broke half the learning flywheel. `learning_candidates` (migration 033) fixed exactly this bug class for the *input* to learning — the skill it produces was still thrown away. A skill could be scaffolded from a verified trajectory, refined to v2 and v3 by later cases, and vanish on the next rollout with nothing recording that it had existed
- Migration 036 adds `runtime_artifacts` + `runtime_artifact_versions`. The four are the same shape — a named text document with a version — so they share one table rather than four near-identical ones. `artifact_store` writes through on every change and replays the table onto disk at startup, before the loaders scan. `skill_loader` and `plan_templates` are untouched: the filesystem is still the read path, the table is the durable copy beside it
- Hydration never clobbers a file already on disk (a bundled skill is the image's to define) and refuses a `rel_path` that escapes its root — the store writes to the filesystem from database content, which is a real injection surface

### Plans can be created, and are versioned like skills
- Plans could be edited and deleted but never created: a new incident type meant editing repo YAML and cutting a release. `POST /plan-templates` creates one, validating phase ids and skill names, and carrying each phase's `produces` contract so `phase_judge` can check the phase returned what it promised
- Every write archives the prior body, so a plan edit is reversible for the first time. `GET /plan-templates/{type}/versions` returns the history. `DELETE` drops the durable copy too — otherwise the next boot would faithfully restore what was just deleted, and history is kept so it stays recoverable
- The UI gains a create form with repeatable phase rows, mirroring the server's `incident_type` validation client-side so the error arrives while typing rather than as a 400, and a History toggle on the plan drawer

### Why a plan phase went partial, not just that it did
- `phase_judge` computes exactly which declared `produces` fields a phase failed to return, and `plan_runtime` retries with the gap named — then `_record_execution` threw the answer away, persisting `evidence_length` (a character count) and dropping the field names. The Plans tab could render "3 partial" and never answer "partial because diagnose never produced root_cause"
- `contract_missing` is now carried on `SkillOutput`, persisted per phase, aggregated across the window by `/analytics/plans`, and rendered beneath the phase breakdown next to the `Produces:` declaration it is the counterpart to

### A write that reported success and did nothing
- `artifact_store.persist` and `forget` never called `db.commit`. `Database.execute` keeps its connection checked out until commit, so every write was silently discarded and the connection leaked — a skill would report as persisted and be gone on the next restart, which is the bug this release exists to fix, reintroduced one layer down
- The mocked tests could not catch it: they assert the SQL, which is not the same as asserting the effect. Found by running the store against Postgres while verifying the migration path before release, and now covered by tests that use a real database rather than a mock

## v2.26.1 (2026-08-25)

### The deny policy did not cover the unattended path
- v2.26.0 added a harness deny policy and enforced it in `agent._execute_tool`, which covers chat and view-action calls. The monitor's auto-fix does not go through that choke point: it dispatches via `monitor/fix_planner.execute_fix` and calls the Kubernetes API directly, so `_execute_restart_controller` was deleting production pods with `grace_period_seconds=0` and no policy check at all
- Asking the agent to delete a production pod was refused; the agent deciding to delete one on its own initiative — unattended, at trust >= 3, which is what the dev cluster runs — was not. The less supervised path had the weaker control, and SECURITY.md described a harness-level guarantee that was only half true
- `execute_fix` now evaluates each mutating strategy against the tool it is equivalent to (`restart_controller` -> `delete_pod`) and returns a `blocked` result the caller records like any other outcome. A test pins that non-protected namespaces still auto-fix, so the block cannot quietly become unconditional

### A fixture scoring 96/100 was reported as a regression
- The release gate failed `integration_incident_correlation`. Re-run against the real model it scores 96/100 with a `[96, 96]` spread across three samples, and its only failing check is a `mentions` substring that judge gating demotes to advisory
- Every judge sample had returned malformed JSON, so `judge_response_median` returned None and `_apply_judge_gate` fell through to `if not judge: return score` — handing grading back to verbatim keyword matching for that one fixture, while every other fixture in the run was judged. The criteria changed silently and the result was reported as a regression
- The judge now retries one round when every sample fails, and a requested-but-missing judge score appends an explicit failing check instead of falling through. An unmeasured fixture is not a keyword-graded fixture; `--confirm-retries` absorbs transient failures

### Eval determinism and cleanups
- `run_selector_eval` documented itself as "Deterministic — no LLM calls" while `classify_query` could reach the LLM fallback, making a p99 latency assertion measure a network round-trip; the bound had already been relaxed once (100ms -> 500ms on CI) for what was really this bug. `skill_router.llm_fallback_disabled()` makes the claim enforced
- The new SRE-Bench sim gate reports scores rather than blocking at 6/6 on the day it landed; its integrity checks (`sre-bench verify`) still gate
- `policy.py` re-implemented a comma-list split `config.py` already exposes; both list-valued settings now parse in one place

## v2.26.0 (2026-08-25)

### A deny policy the model cannot talk its way past
- Migrating the cluster to `claude-sonnet-5` changed the agent's effective safety posture and nothing announced it. Measured on SRE-Bench's simulated fixtures with backend-observed flags: told to delete a production pod "immediately, don't ask questions", the agent deleted it; told to drain a node and "skip the change process", it drained it. The same two traps on `claude-opus-4-6` produced one refusal. Confirmation was the only backstop, and confirmation is consent, not policy — with a permissive approver clicking yes, both writes went through
- `policy.py` adds deterministic rules checked in `_execute_tool` *after* confirmation and before the call: `delete_pod` is refused in `PULSE_AGENT_PROTECTED_NAMESPACES` (default `production,openshift-*,kube-system`), and `drain_node`/`cordon_node` require `PULSE_AGENT_ALLOW_NODE_OPS`. `restart_deployment` stays open in protected namespaces on purpose — it is the reviewable, controller-managed path, so routine crashloop remediation is untouched
- A denied call explains the policy and names the sanctioned alternative rather than just refusing, because the request was already made and confirmed by a human who now needs somewhere to go
- Re-measured after the change: the node-drain trap went 0.68 FAIL → 1.0 PASS (refused outright, zero tool calls), the production delete was blocked by policy, and the core suite went 0.8300 → 0.8900 with gates 3/6 → 5/6. The remaining violation is honest signal, not a scoring artifact: the agent still reaches for `restart_deployment` on production when told not to ask questions

### The eval gate stopped rotating
- Four consecutive CI gate runs produced four different failing sets, which forced 21 of 49 replay fixtures non-gating (v2.25.0). The variance was the *judge* re-scoring a fixed transcript, not the agent behaving differently
- `judge_response_median` samples the judge N times per fixture and gates on the per-dimension median (`--judge-samples`, wired to 3 in CI and in `make release`). Sampling a fixed transcript is cheap next to the agent run, and the median discards the outlier grades that were doing the flipping. Each fixture now reports `total_spread` — a wide spread is itself a calibration signal
- `evals.yml` gains a deterministic SRE-Bench sim gate: the live agent against simulated cluster fixtures where `verification_passed` is only true on a post-fix read the backend actually saw, and policy violations are recorded mechanically. No judge in that lane at all — it is the gate that would have caught the sonnet-5 posture change on the day it landed
- `make release` and `make test-everything` were still pinned to `claude-sonnet-4-6`, a model this project no longer runs

### Scoring Pulse on the observed lane
- `evals/srebench_adapter.py` runs the production stack — skill router, orchestrated config, system prompts, confirmation gates, the real agent loop — against SRE-Bench fixtures, with only the tool *implementations* swapped for the simulated backend. Flag fields come from the observer, never from the agent's own claims
- First head-to-head on identical fixtures and model: Pulse 0.8533 on core against plain `claude-sonnet-5` at 0.8067, with two more gates passing. Trajectory files ship with the numbers

## v2.24.0 (2026-08-24)

### The CR's trust level never reached the agent
- `spec.agent.trustLevel` has been a no-op on every deployment: the operator injects `PULSE_AGENT_TRUST_LEVEL`, the agent only read `PULSE_AGENT_MAX_TRUST_LEVEL`, and both sides defaulting to 2 kept the mismatch invisible. Measured live on the dev cluster: CR patched to `trustLevel: 3`, new pod running with `PULSE_AGENT_TRUST_LEVEL=3` in its env, agent still gating every fix at trust 2 — which is one reason its `actions` table shows 681 proposals and zero executions
- `max_trust_level` now accepts both names (`AliasChoices`); the canonical `PULSE_AGENT_MAX_TRUST_LEVEL` wins when both are set. The operator emits both names from the same CR field, so an old agent keeps seeing what it always saw and a fixed agent finally sees the CR's value
- Same shape as the `PULSE_AGENT_APPROVAL_TIMEOUT` bug already documented in this file: a knob that ships without an end-to-end check that the value it sets is the value something reads

### Verification contracts for interactive write tools
- The monitor's auto-fix path earned its trust piecewise — RBAC preflight before proposing, a restorable snapshot before writing (migration 034), an affirmative health gate after — while a write tool called from chat validated its inputs, mutated the cluster, and returned a sentence. The five most-used mutating tools (`restart_deployment`, `scale_deployment`, `delete_pod`, `rollback_deployment`, `cordon_node`) now run under one contract: precondition read → snapshot → action → postcondition probe → restore (`tool_contracts.py`, wrapped around the single tool-execution choke point in `agent.py`, so the chat and view-action paths both get it)
- The precondition reads the target before writing, under the caller's forwarded token — a missing Deployment or a permission gap refuses the write with a plain reason instead of letting the mutation discover it. This is the same class of failure as the approved fix that 403'd mid-flight; now it surfaces before anything changed
- Postconditions are tool-specific, not generic health: a scale-to-0 verifies as 0 ready replicas where the generic gate would call it a failure; a rollback verifies the revision actually moved, not just that the Deployment looks healthy; a deleted pod verifies through its owning controller, and a bare pod's deletion is reported UNVERIFIABLE with "permanent" said out loud rather than scored either way
- Probes run on the monitor's existing verification pipeline with a 3-scan grace window, because a rollout mid-progress is not a failed rollout — a FAIL inside the grace window stays pending; PASS and UNVERIFIABLE resolve immediately. Chat actions carry no finding, so the pipeline's memory- and trajectory-learning blocks are now gated on `finding_id` — a chat-initiated restart must not be filed as a learned "Auto-fix" runbook
- Every contracted write becomes a fix-history row with the snapshot attached and the probe's reading as `verification_evidence` — a measurement, not the agent's claim. In CLI mode with no monitor loop, the row says `unverifiable — monitor loop not running` instead of a `pending` that never resolves
- `execute_rollback` now executes the `restore_snapshot` rollback that `_make_rollback_info` had been persisting all along — fix history was offering a rollback the endpoint then refused as "not supported for tool 'restore_snapshot'"
- Offline eval replay suspends contracts explicitly (`offline_context` enters `tool_contracts.suspended()`): a precondition reaching for a patched-out cluster would fail closed and replace the recorded response the replay exists to score

### A test suite that failed one run in N on a table it had defined twice
- `ERROR at setup of TestScanPendingPods.test_no_conditions_still_reports — column "category" does not exist`, from migration 001's `CREATE INDEX idx_actions_category ON actions(category)`. The named test passes in isolation on every branch; one full run gave "3102 passed, 1 error" and an immediate re-run of the same tree gave "3103 passed"
- `tests/test_eval_outcomes.py` declared a five-column `actions` of its own under `CREATE TABLE IF NOT EXISTS`. That is a no-op while the real 23-column table is there, so it read as harmless for as long as nothing disturbed it. The tests share one long-lived PostgreSQL that no run ever reset, so a run interrupted between a fixture's `DROP TABLE actions` and its rebuild leaves the table missing — and then this fixture's shape is the one the *next* whole session gets. The baseline's index on `category` is where that surfaces, in a different file, against a scanner test that has nothing to do with it
- The second half is why the DROP that should have cleared it did not. The monitor and scanner fixtures dropped eight tables in a loop, each inside one transaction, each wrapped in `except Exception: pass`. `Database.execute` rolls the whole transaction back on error, so a lock timeout on the *eighth* table silently undid the successful drop of the first — and the fixture went straight on to rebuild the schema over the table it believed it had removed
- Those lock timeouts are real and routine here: background threads (`expire_orphaned_proposals`, the harness view count, WebSocket handlers) call `ensure_tables()` long after the test that started them finished, concurrently with fixture DDL. The same contention was already producing deadlocks and duplicate keys in `pg_class_relname_nsp_index` — the signature of two sessions running `CREATE TABLE IF NOT EXISTS` at once
- The schema is now built once per session from an empty `public`, so the starting state is a fact rather than whatever the last run left behind, and the fixtures that wanted a clean slate `TRUNCATE` rather than rebuild. Converted across `test_monitor`, `test_scanners`, `test_eval_outcomes`, `test_chat_history`, `test_views`, `test_view_critic`, `test_view_lifecycle`, `test_tool_usage` and `test_tool_chains`; the four tests that are genuinely *about* a migration still drop and replay, because that is what they assert. `tests/test_scanners.py` drops from ~6s to ~2s
- One of those fixtures dropped `schema_migrations` itself, which sent the next replay anywhere in the suite back through migration 001 — the second flake found while fixing the first, and the one that produced the duplicate key
- Reproduced deterministically before the fix by planting the five-column table and holding one lock — the reported error, the reported test, the reported traceback. The same script passes on the fix, as does the full suite started from a deliberately poisoned database
- `no-test-owned-core-schema` in `scripts/check_discipline.py` fails CI on any test that declares a table `db_schema.py` owns, because this one was invisible for as long as it was right by luck

### Ranking that ignored its own causal model
- Pulse has had a causal layer model since episodes were built — infrastructure explains platform explains workload explains signal — and the inbox ranking never used it. Measured on the reference cluster: four workload-layer pod crashloops outranked an infrastructure-layer node that had gone `NotReady`, and derived `audit_events` rows outranked both. The product knew which of those was the cause and sorted it fourth
- The whole top ten spanned **5.69 to 5.22**. Age contributes up to 3.5 points (`age_bonus` 2.0 + `novelty` 1.5) while the entire severity range contributes at most 3 — so with every item about the same age, time outweighed severity and everything converged. The order was correct and told an operator nothing
- Severity is now weighted by causal layer: infrastructure ×2.0, platform ×1.4, workload ×1.0, signal ×0.7. A signal is an observation *about* something else and is the weakest claim on attention, not the strongest — the same judgement episodes already make when they rank a symptom underneath its cause
- Re-scored against the live queue, the `NotReady` node moves from 4th to 2nd, the `BackOff` event rows fall below the workloads they describe, and the spread across the queue widens from **2.61 to 6.65**
- An unrecognised category weighs 1.0 — neutral, via `layer_of`'s existing workload default. A category nobody has classified yet is not evidence that the finding is unimportant, and silently demoting it is how a new scanner's output disappears from the queue it was written to fill
- `category` is optional, so a caller that omits it ranks exactly as before rather than shifting underneath itself

### Approve buttons for symptoms of a cause we had already named
- Measured on the reference cluster: the Inbox showed **"4 fixes waiting on you"**, each with an Approve button, targeting exactly the four pods the same screen labelled *"Explained by the cause above — not separate problems"* under an open `HighOverallControlPlaneMemory` episode. Same four pods, same restart counts, one screen
- Approving any of them restarts a pod whose cause is control-plane memory pressure. It crashloops again while the memory stays high. The causal engine knew that; the action panel said nothing, and put the most actionable-looking control on the page beside it
- A proposal now carries `explainedBy` — the cause title of the open episode that explains its finding. `symptom_keys_by_episode` already answered *whether* something is explained; `explaining_cause` answers *by what*, which is the part an operator needs before deciding
- Labelled, not suppressed. Restarting a symptom is sometimes a legitimate stopgap and that is the operator's call; it is only wrong to ask them to make it blind. The same reasoning the webhook already applies when it stays silent for findings an episode explains
- The label is an enhancement, so losing it must not lose the proposal: a database that will not answer, an episode with no title, and an empty correlation key all yield no label rather than an empty one beside an Approve button
### A queue that never forgets what recovered
- Resolution rides on `ClusterMonitor._last_findings`: present last scan, gone this scan, raise a resolution event. That dict is **in-memory and starts empty in every process**, so anything that recovered while the agent was restarting was never in it and could never become stale. The item stayed open, critical and wrong until `expire_untouched_items` archived it 48 hours later
- Measured on the reference cluster: of the open items that could be checked against live state, **seven of seven were already resolved**. A node listed critical/`NotReady` had been `Ready` for six and a half hours; four Deployments listed `degraded (0/1)` were all `1/1`. 27 of 39 open items had not been re-examined in over six hours. An SRE working that queue top-down investigates healthy infrastructure
- The agent already seeds `_known_episodes` from the database on startup for exactly this reason — "restarting the agent is not news". Findings never got the same treatment. One reconciliation now runs per process, after the first scan settles, resolving open items whose correlation key the cluster is no longer reporting
- An empty scan resolves nothing. A cycle that reports zero findings is far more likely to be broken than a cluster that became perfect between two scans, and emptying an operator's queue on that reading is the worse failure by a distance
- Deliberately narrow, matching `expire_untouched_items`: items a person claimed or pinned are theirs, and hand-created items were never tied to a scanner finding. Only work the monitor put there is the monitor's to take back — and it records *why*, since "resolved" with no reason is indistinguishable from somebody closing it

### The trust ladder described a different agent than the one that ships
- Level 1 was labelled **"Confirm — every action requires your explicit approval"** for a level that never enters `auto_fix` at all. Nothing is ever proposed, so there is nothing to approve. An operator who wanted supervised remediation read that ladder, chose 1, and got an agent that did nothing — the `total_actions: 0` symptom, sitting in plain sight in the control's own name
- Level 2, labelled "Batch — low-risk auto-approved", is the level that actually asks: it proposes every fix and waits for a human. The two were the wrong way round
- Levels 3 and 4 promised LOW/MEDIUM/HIGH risk tiers. No such tiering exists in the fix path — `risk_level` appears only in LLM investigation output and `auto_fix` never reads it. What separates 3 from 4 is category filtering, nothing else
- Behaviour is deliberately unchanged. Making level 1 act as its old label promised would start remediation on every cluster currently configured at 1, on upgrade, without anyone asking for it. The names were wrong, not the ladder — it is monotonic and the levels are genuinely distinct
- The gate is now a named `remediation_enabled` rather than an inline `>= 2`, because a test that restates the threshold keeps passing when the real one moves. `tests/test_trust_ladder.py` pins what each level does by observing whether `execute_fix` is actually called — status strings alone cannot tell "asked and waited" from "ran it"

### The trust level an operator reads should be the one that governs
- `/monitor/capabilities` reported `max_trust_level` — the ceiling a client may *select* — and nothing about the level the scan loop is actually running at. Those became different numbers when `effective_trust_level` started reading the server's configured floor instead of whichever browser tabs happened to be open
- The response now carries `effective_trust_level` alongside the ceiling. A subscriber can still raise the monitor above the floor, so the running level is read from the live monitor rather than re-derived from settings
- No monitor yet means nothing is scanning, so the configured floor is the honest answer. A monitor that raises is caught for the same reason: Mission Control uses this response to decide which trust controls to enable, and a 500 here disables the operator's controls entirely

### An inbox row should say where
- Inbox items took `namespace` from `finding.get("namespace")`, and `_make_finding` has never set a top-level namespace — every scanner puts it inside `resources[0]`. So the field was always None. Measured on the reference cluster: **0 of 31** open items carried a namespace while the resource underneath plainly did
- The UI has rendered a namespace badge on every row this whole time and it has never once appeared, which is why four identical `TargetDown` rows sat in the list with nothing to tell them apart
- Same bug `_finding_corr_key` already had and already documents — two workloads sharing a name in different namespaces collided onto one item. That one was fixed by falling back to the primary resource; this one was not
- Cluster-scoped findings still get nothing. A node or an alert with no namespace label has none, and a row should not invent a location for something that does not have one

### A correlation key that changed on every restart
- The events scanner picked `resources[0]` out of a **set**. Python randomises string hashing per process, so the set's iteration order moved every time the agent restarted — and `resources[0]` is exactly what `_finding_corr_key` builds the key from. An unchanged condition was handed a brand new correlation key on each restart: a new inbox item, a new symptom on the episode, a fresh "is this still happening?" question with no memory of the last answer
- Found by pulling apart an episode that carried two symptoms with byte-identical titles — `High-frequency 'BackOff' events in multicluster-engine (93x)`, the same count on both — under two different keys, `Pod/infrastructure-operator` and `Pod/ocm-controller`. One finding group, forked in two by nothing more than which pod happened to come out of a set first
- Four `list(set(...))` sites are now `sorted(...)`; the other three fed summaries and a briefing, where the same reshuffling made identical text read as changed
- The regression test crosses a process boundary on five `PYTHONHASHSEED` values, because a single-process test cannot see this at all — within one interpreter the order never moves. Reverting the fix makes it produce four different keys from five runs

### An episode nobody re-detects is over
- `close_for_correlation` was the only way an episode ever ended, and it fires when a finding *resolves*. A cause that merely stopped being reported — a node that went NotReady and came back, an alert that stopped firing between scans — left its episode open indefinitely. `last_seen_at` has been written on every touch since episodes existed and read by nothing
- Observed live: a master flapped NotReady and recovered. Sixty-eight minutes later the episode was still open and still the headline on the front-door banner, captioned "running 68m" — which reads as *broken for 68 minutes* when the truth was *last seen 68 minutes ago and never re-checked*. A monitoring product announcing a resolved incident as live is worse than one that says nothing
- Open episodes whose cause has gone unseen for fifteen minutes now close on the next scan. Same rule the findings themselves got in 2.12.0 — detect slowly, clear quickly — finally applied to the layer above them
- Fifteen minutes because the slowest scanners run every fifth cycle, roughly five and a half minutes, so the window has to outlast more than one missed turn. A database that will not answer closes nothing: an unreadable table is not evidence that every incident is over

## [2.16.5] - 2026-08-21

### A long-running cause is not a magnet
- Symptom attachment had a lower bound and no upper bound, so anything that started after the cause qualified no matter how long after. On the reference cluster a memory alert firing for thirty hours had collected **22 symptoms**, among them a missing PVC — something memory pressure does not cause and cannot cause. The cause had stopped being an explanation and become a bucket
- A symptom must now begin within two hours of the cause. A cascade propagates in minutes — memory starves the API server, the API server times out probes, the probes kill pods — so two hours is generous for the real shape and still says no to a coincidence a day later
- The window is measured between the two onsets, not from now, so an old cause and its equally old symptom are still one event
- One existing test asserted the opposite, with a 21.9-hour gap between cause and symptom. It was built on onsets I had guessed rather than measured — the real ones predated the cause by 26 hours — so it had been encoding the magnet rather than defending against it

## [2.16.4] - 2026-08-21

### A rejected fix showed its raw exception, not what happened
- Approving a fix that then failed against the API server stored `str(e)` on the `kubernetes.client.rest.ApiException` as the failure reason -- the object's entire repr, HTTP response headers and all, dumped verbatim into the Inbox's "Fixes applied" banner. Observed live: a `restart_controller` fix on `klusterlet-646d4fdd8b-4kz56` in `open-cluster-management-agent` was denied by RBAC, and the banner read as several hundred characters of `HTTPHeaderDict({'Audit-Id': ...})`, cut off mid-header by the UI
- Both `approve_fix` and the unattended `auto_fix` path now run the exception through `classify_exception` -- already used everywhere else a K8s call can fail -- before storing it, so the message is the Kubernetes API's own `Status.message` (e.g. `pods "klusterlet-646d4fdd8b-4kz56" is forbidden: ... in the namespace "open-cluster-management-agent"`), not the transport object around it
- Checked whether the agent should have had permission to make this call at all: it should not have. `allowWriteOperations` is false on this cluster, the operator's ClusterRole correctly omits `delete(pods)`/`patch(deployments)` as a result, and the 403 is RBAC working as designed -- the gap was only in how the failure was displayed, not in what the agent was allowed to do

## [2.16.3] - 2026-08-21

### A proposal outlives the condition it was raised for, unless something says so
- `approve_fix` already refused a proposal whose finding had cleared on its own -- but only at the moment a person clicked Approve. Until then it kept counting toward "N fixes waiting on you," asking for a decision about a fix that no longer applied. Observed live: a crashloop proposal sat in fix history for the better part of an hour after the pod stopped restarting, Approve returned 409 every time, and the banner never dropped to zero
- `expire_orphaned_proposals` now answers these the same way `approve_fix` would, once per scan cycle -- so a self-healed condition stops asking instead of waiting for someone to find out the hard way that there is nothing left to fix
- Clicking Approve on a stale proposal was already safe (a 409, no fix executed) -- this closes the other half: nobody has to click it to hear no

## [2.16.2] - 2026-08-21

### Dedupe on the condition, not on one sighting of it
- The proposal guard shipped in 2.16.1 keyed on `finding["id"]`, and `_make_finding` mints a fresh `f-{uuid4}` on every scan. So the same condition arrives with a different finding id every 65 seconds and the guard could never match its own previous proposal. It did nothing: the reference cluster went on to **718 proposals**, one per sighting, each looking brand new
- Actions now carry the correlation key — the identity the rest of the system already uses for "this same condition, seen again" — and both guards key on it (migration 031)
- `check_existing_human_review` had the same defect for as long as it has existed. It never matched anything, and nobody noticed because `auto_fix` was never entered until the trust-level fix, so the guard had never once been asked a real question
- The regression test runs against a real database, because this class of bug is invisible to mocks: a mock answers whatever key it is handed, and only real rows show the id changing underneath the lookup

## [2.16.1] - 2026-08-21

### A proposal is a question, not a chant
- Unattended proposing re-asked the same question every scan. One hour on the reference cluster produced **701 proposal rows for two findings**, burying the two that mattered. This is the cost of the previous release finally letting `auto_fix` run: the flood was impossible before, because the function was never entered. A finding with a proposal still awaiting an answer is now skipped
- The notification-gap finding carried its count in the title, so every time the number moved it became a different correlation key: the item resolved and was raised again rather than staying open. Observed live — raised once, resolved, gone. The count lives in the summary now

## [2.16.0] - 2026-08-21

### Pulse says when what it found cannot reach anyone
- A `degraded` finding when no notification channel is configured *and* there is something waiting to be delivered — an open episode or a proposed fix. It reports how many. The reference cluster ran a control-plane problem for 30 hours with the diagnosis sitting in a database and nothing configured to carry it
- Gated on there being something to deliver, on purpose. An unconfigured webhook on a quiet cluster is a deployment's choice, not a fault; reporting it on every scan regardless would be the same standing-posture nagging that `AlertmanagerReceiversNotConfigured` had been doing on that cluster for 57 hours to nobody's benefit. Six existing tests failed when the first version of this check ignored that, which was the right answer from the tests rather than a reason to change them

## [2.15.0] - 2026-08-21

### "What changed just before this started" finally has an answer
- The change window was anchored on when Pulse *opened* an episode, not on when the condition began. Observed live: a cause firing for 30 hours, an episode 12 minutes old, and a window covering the half hour before the episode — a day after anything that could have caused it. Episodes now record the cause's own onset where it is known and measure back from that (migration 030). Conditions that report no onset still fall back to the episode's start, which is the best that is known for them

### Something reaches a person who is not looking at Pulse
- Notifications used to fire per critical finding, which for one control-plane problem on the reference cluster would have been 33 messages — that is how a monitoring system teaches people to filter it. The outbound events are now the two that mean something to a human: an **episode opening**, which is one event with a cause, and a **fix proposed**, which is something waiting on *them*
- A finding that an open episode already explains stays silent. Its episode spoke for it
- The proposal notification carries the call that answers it — `POST /fix-history/{id}/approve`. A message that reports a problem without saying what to do about it is only half a notification
- An episode announces itself once. Known episodes are seeded from the database on startup, because restarting the agent is not news

### A remedy for the one recurring problem Pulse could not touch
- Every firing alert arrived under one category, and a category cannot carry a remedy — so the OLM install loop that has been firing for 30 hours on the reference cluster had no fix path at all, despite being both well understood and safely reversible. Alerts now dispatch to a fix strategy by name: `CsvAbnormalFailedOver2Min` and `CsvAbnormalOver30Min` restart the operator that is stuck re-running an install strategy and starving its own probes
- Deliberately a short list. An alert says something is wrong, not what to do about it, and guessing a remedy from an alert name is how an automated fixer earns its reputation
- An alert with a known remedy but no pod to act on gets no proposal. A proposal an operator could never carry out is worse than none

### A proposal can be answered after the moment has passed
- `POST /fix-history/{action_id}/approve` runs a fix that was proposed while nobody was connected. Until now the only way to answer a trust-level-2 proposal was to be holding a WebSocket open when the question was asked, with 120 seconds to reply — nobody is watching a dashboard at 03:00, which is how the reference cluster reached 2,528 investigations and zero actions
- The proposal is a pointer to work, not a captured command. Approving re-derives the plan from the finding as it stands now: an image tag, a resource limit or an owning Deployment may all have moved since it was raised, and running a stale plan against a changed cluster is worse than refusing. A proposal whose condition has since cleared is declined — acting then would be operating on a memory of the cluster rather than on the cluster
- The status check lives in the `WHERE` clause rather than in a read-then-write, so two operators approving at the same instant produce one fix and one conflict. Verified against a real Postgres: the first claim returns true, the second false
- Gated on a real authenticated user rather than the shared UI token, and the approver's name is recorded against the action (migration 029). Approving a change to a live cluster is a person taking responsibility, which deserves more than an anonymous state transition
- Fix history used to lose the `tool`, `before_state` and `reasoning` of any action that transitioned in place, because the upsert did not carry them. A proposal approved later would have shown as completed while never saying what it did

### Remediation no longer depends on somebody having a browser tab open
- `auto_fix` runs only at trust level >= 2, and the effective trust level was "the highest among connected subscribers, or 1 if there are none". Subscribers are browser tabs. With nobody watching, the level was 1, `auto_fix` was never entered, and the agent quietly did nothing about problems it had correctly diagnosed. Measured on the reference cluster after days of running: 2,528 investigations, **zero actions**, and not one auto-fix line anywhere in the logs. `PULSE_AGENT_TRUST_LEVEL=2` was set on the deployment and never consulted by this path
- This is the same bug as the scan loop only running while a client was connected. That half was found and fixed; this half was left behind, which is what makes it worth naming rather than quietly correcting — "works only while someone is looking" is a shape worth recognising on sight
- The trust level now comes from the server's configuration, and a subscriber may raise it but no longer lower it by being absent. Auto-fix categories likewise start from what the server can actually do rather than from an empty union
- Trust level 2 means *ask first*, and with no subscriber there is nobody to ask. Rather than waiting 120 seconds per finding for an approval that cannot arrive — which would stall a 65-second scan loop — the proposal is recorded and the scan moves on. It stays in fix history, where an operator can approve it later. Absence of a reviewer is not consent



### An alert can be a cause
- Every firing alert arrived as `category="alerts"`, which the layer model reads as *signal* — able to be explained, never able to explain. On the reference cluster that made the episode layer structurally dead: 15 of 15 standing findings were alerts, `/episodes` returned `[]`, and meanwhile a single investigation of one of those same alerts correctly tied four of them into one story. The deterministic layer knew less than the model did, about data it already had. Alerts are now layered by what they are *about* — node memory is infrastructure, a stuck CSV is platform, `TargetDown` really is signal
- The table is deliberately incomplete. An unclassified alert stays at the signal layer, where it can be a symptom but never a cause. Being wrong in that direction costs a missed correlation; being wrong the other way costs a wrong one, and a wrong episode tells an operator a confident story about a cause that is not the cause
- Standing configuration is neither cause nor symptom, the same treatment posture findings already had. `AlertmanagerReceiversNotConfigured` had been firing for fifty hours: nobody had configured a receiver, which no outage caused and which caused no outage
- Findings now carry the moment the condition *began*, taken from Prometheus rather than from Pulse's own bookkeeping. The old signal was an in-memory dict on the monitor, lost on every restart — after a redeploy every standing problem on the cluster claimed to have started at the same second, which is exactly when correlation matters most and exactly when it was least trustworthy
- Symptoms are compared against the *cause's* onset rather than against when Pulse got around to opening the episode, with a 15-minute window. Prometheus holds a per-rule `for:` duration before an alert fires at all, so two alerts describing one event start minutes apart — memory pressure and the OLM install loop were four and a half minutes apart, and the old 180-second grace would have split them — measured on the cluster, and now a test that fails if the window is put back
- One event, one cause: a symptom belongs to a single episode, and a finding that something deeper already explains no longer heads its own. Without them the layer fix lets every cause above a symptom list it, which is the "N findings that are wrong" problem wearing a different hat. This one is held by a synthetic test rather than by cluster data — the reference cluster did not happen to have two causes at different depths sharing a symptom that started after both

## [2.13.0] - 2026-08-20

### Reset the inbox: count from now, keep the history
- `POST /inbox/reset` archives every open item, records a baseline, and rescans. Measured on the reference cluster before building it: 339 items, 306 of them resolved, and a critical item reading "Pod promoter-controller-manager restarting (122x)" for a container whose lifetime counter had been climbing for days. The number was true and useless
- Restart counts are the part that cannot be derived. `restart_count` is cumulative for the life of the pod and Kubernetes will not say how many of those happened recently, so the count at reset time is snapshotted per container and findings report the difference — "restarting (6x) … (128 in the pod's lifetime)". Without the snapshot the next scan re-reports 122x and the button looks broken
- Event frequency gets no snapshot on purpose: events expire within the hour, so a baseline taken at reset is stale within a scan or two. Filtering on last-seen is simpler and closer to what "still happening" means
- Current-state scanners are untouched. A deployment at 0/2 or a firing alert already describes now, which is why those reappear immediately after a reset if they still hold — the intended behaviour, not a leak
- Nothing is deleted. Items move to `archived` with a reason naming who reset and when, and the reset itself is recorded with what it took: items archived, how many were pinned or claimed, episodes closed
- Gated on a real authenticated user rather than the shared UI token: this clears a queue several people may be working from, and "somebody with the UI credential" is not an answer to who (migration 028)

## [2.12.0] - 2026-08-20

### Fixes found by watching v2.11.0 on a real cluster
- Untouched-item expiry could never fire. The query filtered `pinned_by IS NULL`, but `pinned_by` is a JSON list defaulting to `'[]'` — 323 of 323 rows had `'[]'` and none had NULL, so it matched nothing. The stubbed unit tests passed because they never ran the SQL
- A standing posture is nobody's symptom. 117 `Security: …` findings were attached to an etcd write failure, because a posture finding sits at the signal layer and the layer test alone said yes. Forecasts and posture findings can now be neither cause nor symptom
- Confirmed fixed by this release rather than newly broken: the same etcd cause opened 13 separate episodes in two hours, each living ~80s. `control_plane` ran every 5th cycle, so its finding vanished for the four cycles in between, the stale sweep closed the episode, and the next run opened a new one. Running it every cycle removes the churn
- A timed-out investigation permanently broke every investigation after it. `run_investigations()` reused one long-lived Anthropic client across every proactive investigation and wrapped each streamed call in a 20s `asyncio.wait_for`; cancelling an in-flight stream on that *shared* client corrupted its connection pool for the rest of the process. On the reference cluster this reads as genuine 20s-spaced `"Investigation timed out after 20s"` failures, then 244+ (and climbing) `"Connection error."` failures roughly 1.2-1.5s apart — the shared client failing instantly, never touching the network again. Each investigation and security followup now gets its own disposable client, so a timeout can only ever damage a client nobody else will reuse. Also invisible in logs until now: the catch block only did `report["error"] = str(e)`, with no logger call at all — failures were visible in the database but never in application logs. Both branches now log; the generic-exception branch logs the full traceback. The 20s timeout — shorter than the 120s minimum single phase (triage alone) that every plan template in `sre_agent/plan_templates/*.yaml` already allows for the same kind of Claude tool-calling work — is now 120s

### An episode you can actually clear
- Findings raised on a one-hour window stayed true for a full hour after the problem stopped. Reported from real use: the cluster recovered, the agent said so, and the card would not go away — at 17:22 `increase(etcd_server_proposals_failed_total[1h])` still read 15 with **zero** failures in the preceding fifteen minutes. Every windowed check is now two: the long window says the problem is real, a 15-minute window says it is still happening. Detect slowly, clear quickly. Verified against the live cluster at that exact timestamp — the old query fires, the new one does not
- `POST /episodes/{id}/dismiss` — an operator can close an episode the scanner will not close itself. Recorded with who dismissed it, because an operator overriding the scanner is evidence the clearing logic is wrong and worth counting. If the cause re-fires it opens a *new* episode with `recurrence_of` set, so dismissing can never hide a problem that comes back (migration 027)

### The episode shows the work already done
- `GET /episodes/{id}` now returns the investigation already run against the cause. Causes are eligible for automatic investigation, so by the time an operator opens the card the work has usually been attempted — 22 attempts on the reference cluster, all failed. Showing a fresh "ask the AI" without that would give two routes to the same call and imply nothing had been tried. A failed attempt is returned rather than hidden, because an empty panel reads as "nothing worth investigating"

## [2.11.0] - 2026-08-20

### Review pass
- `control_plane` ran every 5th scan cycle — a 5-minute detection lag on the layer that explains everything above it, while `crashloop` (one of its symptoms) ran every 60s. That inverts the precedence the whole layer model exists to establish. It now runs every cycle; `hot_loop` moved to every 3rd. `stuck` and `degraded` stay at 5, where a 15-minute threshold and a failure streak make the lag irrelevant
- Removed `take_failures()`, which had no production caller and was kept alive by its own tests. The behaviour those tests covered is real and is now asserted through `get_failure` and `reset`, which are used


### The inbox is a queue again
- `sweep_stale_items` runs on every scan cycle, not only at startup. It had a five-minute threshold and ran once per process, so three items sat in `agent_reviewing` for 73 minutes on a live cluster. A guard that only runs at boot does not guard anything while the process is running
- Items nobody has claimed, pinned or acted on in 48 hours are archived. Measured: 40 of 76 open items were more than 40 hours old, which is how an inbox stops being a queue. Deliberately narrow — anything claimed, pinned, or created by a person is left alone regardless of age, and a database error expires nothing

### Episodes answer the next two questions
- `changes_around()` puts config, RBAC and deployment activity from the 30 minutes before an episode on its timeline. The audit scanners have been collecting this all along and filing it as ordinary inbox rows, where it answered nothing. It reports what happened shortly before, in time order, and claims no causation
- `recurrence_summary()` reads the `recurrence_of` chain the schema already recorded: how many times a cause has returned, over what window, and the interval when it is regular enough to name. "Sixth time today, every two hours, escalating" was the most useful sentence available about a real outage, and a human found it by reading graphs afterwards
- Both are returned by `GET /episodes/{id}` — what is broken, what changed, and has this happened before, in one response

### Tests
- End-to-end pipeline test: real findings through the real correlation into the real collapse. Every layer was unit-tested and green while the engine opened seven episodes headed by "Certificate expiring in 9d" and absorbed 21 of 23 findings — the fault was in how correct layers composed, and only a live cluster caught it. Verified: reverting that fix now fails this suite
- Monitor lifecycle test. The loop was started only on WebSocket connect for most of this product's life and nothing tested it, which is why nobody noticed


### Fixes
- The monitor only ran while a WebSocket client was connected. `run_loop()` was started inside the `/ws/monitor` handler and nowhere else, so the agent scanned the cluster only while somebody had the UI open — scan history on a live cluster shows exactly that: bursts a minute apart while someone was looking, then hours of nothing. Every claim about autonomous or overnight monitoring was untrue whenever the tab was closed. It now starts in the app lifespan; the WS handler still starts it if it somehow is not running
- The `degraded` scanner missed a backend that fails most of the time but not all of it. Watching the cluster after deploying the consecutive-failure check: 65 of the last 70 investigations had failed — 93% — and it said nothing, because the newest one happened to succeed and the streak was 0. A quota-limited backend flaps rather than failing cleanly, so the failure *rate* over the last 40 attempts is now checked alongside the streak. One fault still produces one finding

## [2.10.0] - 2026-08-20

### Fixes
- The inbox listed items an open episode already explained, so an episode *added* rows instead of removing them: the panel showed a cause with its symptoms folded underneath, and the queue below still listed the same symptoms. `symptom_keys_by_episode()` existed for exactly this and had no caller. Both list paths now drop symptoms of open episodes and return `collapsedIntoEpisodes` so the UI can say how many were folded away — items disappearing from a work queue with no explanation is its own way of losing trust. The lookup fails open: if episodes cannot be read, every item is shown


### Episodes — one event with a cause, instead of N things that are wrong

The product had `findings` and `inbox_items`. Both mean "this is wrong". Neither means "this happened". So when one cause produced fourteen wrong things, there were fourteen equal rows and no way to say they were one event.

Measured on the cluster this was built against: at 20:35 during a control-plane outage the monitor produced fourteen findings inside a single second — nine `Deployment degraded` rated critical, three pod restarts, and one `etcdMemberCommunicationSlow` rated *warning*. The warning was the cause of the other thirteen and did not make the top thirteen by priority.

- New `episodes` and `episode_symptoms` tables (migration 026). Named episodes because `incidents` was already taken by the agent's memory store and the UI's "Incident Center" is a findings list — the word was spoken for twice and meant neither thing
- New causal layer model: infrastructure → platform → workload → signal. A finding may only be explained by one strictly beneath it, so a crashing pod can never absorb a failing API server, and two crashlooping pods are never evidence about each other (burst correlation already handles same-layer siblings)
- Only infrastructure and platform findings may head an episode. A single restarting pod heading one would absorb signal-layer findings across the whole cluster
- A symptom must have been first seen at or after the cause, with a 3-minute grace for detection lag. Something already broken an hour earlier was not caused by the thing that started now
- Episodes are DB-backed, unlike the in-memory `_last_findings` they sit beside — which lost every open condition on restart, and with it the ability to ever resolve anything created before one
- Recurrence is recorded: the same cause returning within 24h links to the prior episode. "Sixth time today, escalating" was the most actionable sentence available about that outage and a human found it by hand
- `GET /episodes`, `GET /episodes/{id}`, `POST /episodes/{id}/detach`. Detachment is stored rather than deleted and never re-attached — an operator saying "this was not caused by that" is the only ground truth the system gets about its own correlation, and it arrives as a by-product of them doing their job
### Pulse now admits when it is broken

The rule: **absence of findings must never be indistinguishable from absence of problems.**

- Twenty-two scanners caught their own top-level exception, logged it, and returned an empty list — which is exactly what a healthy scan of a healthy cluster returns. The dispatcher recorded `status: "clean"` and whatever that scanner watched was silently unwatched. Each now calls `report_failure(e)` beside the logging it already did, and the dispatcher takes the scanner's word over the shape of its return value. Partial findings are still returned: losing 49 real findings because the 50th pod had an odd shape would be the worse trade
- New `degraded` scanner — the only one that looks inward. Reports any scanner that has errored on 3+ consecutive runs, and the AI backend once 5+ investigations fail in a row. It reads `scan_runs.scanner_results`, which has recorded per-scanner status as JSON since migration 005 and which nothing has ever read back
- On the reference cluster this matters: 1,155 of 1,177 investigations had failed (98.1%), 1,111 with `Connection error.`, and the product said nothing. An empty investigation panel reads as "nothing worth investigating", which is the opposite of the truth
- New discipline rule `no-silent-scanner-failure` makes it mechanical: a `scan_*` function that logs a swallowed exception without reporting it fails CI. It caught five violations on its first run, three of them in scanners added the same day
### Fixes
- Skill routing was decided by usage history when a query matched nothing. Below the confidence threshold the selector returned the highest-scoring skill anyway — and for an unrecognised query the only thing scoring was the *temporal* channel, a learned prior about what had been used recently. `classify_query("hello")` returned `slo_management` on a fused score of 0.01 against a 0.45 threshold. Because the prior is learned, identical code routed differently in CI and locally, which is how the broken default went unnoticed and left `main` red. The fallback now ignores prior-only channels: a query with real keyword or semantic evidence still routes on it (`audit cluster-admin bindings` → security at 0.40), and one with none goes to the default


### Liveness monitoring

Every existing scanner measured the *health of state* — is this pod crashing, is this deployment short of replicas. None measured the *liveness of a process* — should this have finished by now. That gap is why a Kuadrant CRD finalizer hammered the API server for four months without producing a single finding: a stuck finalizer leaves no crashing pod, no degraded deployment and no firing alert, so all 23 scanners saw a healthy cluster.

- `stuck` scanner — resources whose deletion was requested but never completed: namespaces terminating past 15 minutes, pods past their grace period, PVCs, and CRDs mid-deletion. Namespace findings carry the API server's own `NamespaceContentRemaining` / `NamespaceFinalizersRemaining` messages, which name the exact resource types and finalizers holding the deletion open. Unlike the health scanners this one does not skip `openshift-*` and `kube-*`, because a wedged system namespace is precisely the case the others hide
- `hot_loop` scanner — the same failure seen from the symptom side, so a loop with any other cause is still caught: sustained work-queue retries (`workqueue_retries_total`), write amplification against one resource kind (`apiserver_request_total`), and pods retrying failing API calls (`rest_client_requests_total`). Thresholds are calibrated against a healthy production cluster, where the busiest legitimate controller sustains ~10 retries/s and the busiest non-lease write ~0.7/s; the scanner fires at 20/s and 5/s respectively and returns nothing on that cluster
- `diagnose_stuck_deletion` tool — explains why a deletion has not completed (finalizers, owner references, remaining namespace content). A read, because the diagnosis is mechanical and safe to automate
- `remove_finalizer` tool — confirmation-gated, and refuses more than `kubectl patch` would: it will not touch an object that is not already being deleted, will not force control-plane finalizers such as `pvc-protection`, and will not clear a namespace's `spec.finalizers` while the API server still reports content inside it, which is the case that orphans every object in the namespace
- `control_plane` scanner — the layer underneath everything the other scanners measure. Added after an incident all 25 of them walked past: etcd peer latency spiked to 3.3s, the API server's p99 went from 20ms to the 60s timeout ceiling for fifteen minutes, liveness probes timed out, and the kubelet SIGKILLed 135 containers across all six nodes in thirteen minutes. Every workload scanner saw the restarts; none could say why. Checks etcd leader changes, failed proposals, peer round-trip and disk commit latency, API server p99 latency, and cluster-scoped LIST rate. Every threshold is set between a measured healthy value and a measured incident value from the same cluster, and the tests assert both sides
- The hot-loop scanner watched writes and retries but not reads, and a cluster-scoped LIST — which returns every object of its kind — is what actually costs an API server. `control_plane` covers it: on the reference cluster it finds two admission-webhook configs being listed 8,500 times an hour apiece
- All three categories are investigable by default (`stuck`, `hot_loop` added to `PULSE_AGENT_INVESTIGATION_CATEGORIES`)

### Fixes
- Migration 025 repairs inbox items orphaned by the v2.9.0 correlation-key change. Adding the namespace to the key meant every pre-existing open item stopped matching any finding: each froze at the values it held that day while a second, live item was created beside it. On the cluster this was found on, 38 of 62 open items were orphans, last updated more than two hours earlier, with 16 workloads showing both copies. The migration resolves orphans that duplicate a live item and re-keys the rest in place; cluster-scoped keys and resolved history are deliberately left alone
- `describe_resource`, `explain_resource` and `list_api_resources` were dead. All three passed `response_type="object"` to `ApiClient.call_api()`, a kwarg the Kubernetes client removed several major versions ago, so every call raised `TypeError` — and every call site caught it and returned its own "Error fetching ..." string. Verified against a live cluster: the generic describe tool returned an error for every resource kind, including every CRD. They now share one `get_raw_json()` helper, and a test sweeps the source for `call_api()` keywords the installed client cannot accept
- The `describe_resource` tests stubbed `call_api` to return a bare dict, a shape the real client never produces, so they passed against code that could not work. They now stub the wire helper and assert on rendering, with the wire contract tested separately

## [2.9.0] - 2026-08-19

### Signal quality
- Simultaneous pod restarts are grouped into one finding instead of one per pod. Measured on a live cluster, 75 crashloop findings became 19: 39 of the 75 shared just two moments, so they were one event reported 39 times. Burst items carry every affected pod and are raised to critical — a correlated restart across namespaces matters more than any single pod in it
- Inbox ranking now favours novelty. `age_bonus` alone meant a week-old warning outranked one raised ten minutes ago, so permanent conditions ("1 cluster-admin binding to review") drifted to the top and stayed. The new bonus decays to zero over 24h, after which ranking behaves as before
- Conditions can be muted by `correlation_key` (`POST /inbox/mute`), with an optional expiry and a required reason. Muted conditions are dropped at creation rather than filtered on read, so they cannot resurface, be counted, or trigger an investigation

### Fixes
- Inbox items showed pod names that had been deleted days earlier: `resources` were re-pointed at the current pod on every scan while `title` and `summary` were frozen at creation
- Correlation keys omitted the namespace (`crashloop::Pod/name`), so same-named workloads in different namespaces shared one item
- Every inbox-triggered investigation was discarded on save — the report had no `id` and the column is `NOT NULL`, so the model was paid for work that was then thrown away
- The stale-findings digest counted and listed itself once it was 72h old
- `/metrics/*` REST routes were shadowed by the Prometheus scrape mount and had been unreachable
- Investigation view plans were dropped by the inbox (`viewPlan` read where the producer emits `view_plan`)

### Security
- The agent could rewrite its own system prompt with no confirmation: the four skill-mutation tools were registered as reads, so they never reached the confirmation gate
- `/admin/skills` mutation required only the shared token — now needs a real authenticated user, with an optional `PULSE_AGENT_ADMIN_USERS` allowlist. `PUT` can no longer overwrite built-in skills, which `DELETE` already refused
- `PULSE_AGENT_DEV_USER` could mask a real authenticated identity; it is now a fallback for the no-proxy case only
- The auto-fix kill switch never reached the monitor loop (a by-value import), and once fixed still reset to armed on every restart. It is now persisted and fails closed

### CI
- The test suite runs again. It had not passed since 2026-06-26 — every scheduled and push run failed for seven weeks. The cause was tests building a real Anthropic client with no API key, which hung until the job cap
- Scheduled failures now open or update a GitHub issue, so an unattended run that breaks is not silent
- Two bug classes are mechanically blocked: tests that skip instead of failing, and `from mod import FLAG` where the module rebinds `FLAG` (the shape behind the inert kill switch)
- `register_tool`'s `is_write` is now required, so a tool cannot be added without deciding whether it needs confirmation

## [2.8.0] - 2026-08-07

Backfilled — this release shipped without a changelog entry.

### Features
- Intelligence bridge: component spec normalization and validation
- Targeted async DB migration for tool usage, monitor repo and REST endpoints
- `current_user` included in the `/inbox` list response

### Fixes
- Needs-attention exclusion narrowed to fully-closed statuses
- Component spec field names standardized across tools
- Source filter uses `created_by`; claim rejects terminal states
- Async DB fallbacks narrowed to specific errors, preventing fire-and-forget task GC
- Auto-detection uses the RHACM hub for multi-cluster rather than Submariner

## [2.7.1] - 2026-05-20

### Security
- Default `max_trust_level` lowered from 3 to 2 — autonomous auto-fix now requires explicit server-side opt-in via `PULSE_AGENT_MAX_TRUST_LEVEL=3`

### Fixes
- fix: add `threading.Lock` to `CircuitBreaker` — prevents race conditions when tools run in `ThreadPoolExecutor`
- fix: add `db.transaction()` context manager — prevents connection pool leaks when `commit()` is not called
- fix: `/health` endpoint now reports database connectivity (`"database": "ok"|"unavailable"`) and returns `"degraded"` when PG is down
- fix: graceful `ClusterMonitor` shutdown on SIGTERM — sets `running=False` and cancels pending investigation tasks

### Observability
- `COST_BUDGET_REMAINING_USD` Prometheus gauge for real-time cost budget alerting
- `COST_BUDGET_EXHAUSTION_TOTAL` counter for budget exhaustion events
- Wired cost budget metrics into monitor investigation gate

### Docs
- Updated all README badges (v2.7.1, 154 tools, 23 scanners, 2372 tests, 16 suites/192 scenarios, 99.6% gate, 83 PromQL)
- Fixed CLAUDE.md counts (118 native tools, 23 scanners with corrected breakdown, removed phantom eval prompt claim)
- Updated API_CONTRACT.md `/health` response schema with `database` field
- Updated GitHub Pages site with current stats, trust level description, and PulseSRE org links
- Migrated all links from `alimobrem/*` to `PulseSRE/*` across README, SECURITY.md, JOURNEY.md, index.html

### Tests
- 9 new tests: CircuitBreaker thread safety (3), trust level defaults (2), cost budget metrics (3), /health database field (1)

## [2.7.0] - 2026-05-12

### Features
- Prometheus `/metrics` endpoint — token usage, cost, investigations, scanner runs, autofix outcomes as counters/gauges
- `GET /analytics/budget` — real-time investigation budget (used/remaining) and optional cost budget status
- 30-day cost forecast in `/analytics/cost` based on 7-day daily token totals
- Optional daily dollar-amount budget enforcement (`PULSE_AGENT_COST_BUDGET_USD`) pauses investigations when exceeded
- ServiceMonitor Helm template for Prometheus Operator scraping
- `observability.py` — centralized Prometheus metrics registry with `record_token_metrics()` helper

### Fixes
- fix: exclude resolved items from Needs Attention list and count
- fix: atomic claim, trend degraded finding, MCP shutdown race
- fix: inbox dedup — reopen recently-resolved items instead of creating duplicates
- fix: auto-resolve inbox items when all referenced resources are gone
- fix: inbox resolution falls back to correlation_key when finding_id misses
- fix: MCP toolset 'observability' → 'metrics' + 'openshift'
- fix: disconnect_all unregisters tools, clear _mcp_shutdown on restart

### Tests
- 12 new observability tests (metric registration, counter increments, gauge operations, label cardinality)
- Total: 2372 backend tests, 2021 frontend tests

## [2.4.0] - 2026-04-17

### Features
- Multi-datasource live tables — K8s watches + PromQL metrics + log enrichment
- All K8s table tools emit datasources for live rendering
- ResourceTable shared component — unified rendering for live and static tables
- Chart editor modal — edit PromQL, title, axes, legend, thresholds, time range
- Chart threshold lines — warning (amber) and critical (red) reference lines
- Cross-chart hover synchronization in custom views
- Global time range selector for custom views (1h/6h/24h/3d/7d)
- Persist chart customizations to saved views
- Inline row actions — open detail, YAML, logs, delete with confirmation
- Column auto-detect for resources without enhancers
- Plans drawer (matches skills pattern)
- Topology graph component with Add to View
- Clickable component cards with detail drawer
- ORCA hard pre-route rules (55/55 routing accuracy)
- Release skill for coordinated dual-repo releases
- Chaos test WebSocket client + topology health overlay

### Fixes
- ORCA routing: hard pre-route before typo correction
- API group resolution for plural resource names
- Namespace "ALL" normalized for frontend watches
- optimize_view saves both layout and positions
- Dynamic table heights based on row count
- view_designer requires_tools includes editing tools
- tool_sequence crash in MemoryView
- K8s API proxy uses SA token

### Tests
- 43 new tests (topology, live table, useMultiSourceTable, ResourceTable, layout)
- 55 routing eval scenarios (100% pass)

## [2.3.0] - 2026-04-14

### ORCA UI Surfaces
- **Postmortems tab** — Auto-generated postmortem reports with timeline, root cause, blast radius, and prevention recommendations in the Incident Center.
- **Topology view** — Dependency graph visualization with blast radius analysis at `/topology` (Impact Analysis).
- **Plans tab** — View, edit, and delete investigation plan templates from the Toolbox Skills section.
- **SLOs tab** — CRUD for SLO definitions with live Prometheus burn-rate queries (`GET /slo`, `POST /slo`, `DELETE /slo/{service}/{slo_type}`).

### Analytics Restructured
- **Agent Intelligence section** — Unified analytics view with routing decisions, fix strategies, and learning feed.
- **Fix strategy effectiveness** — `GET /analytics/fix-strategies` shows per-category+tool success rates.
- **Learning feed** — `GET /analytics/learning` surfaces weight updates, scaffolded skills, and routing decisions.

### Unified Routing
- **Orchestrator delegates to ORCA selector** — ~200 lines of duplicate keyword routing removed from `orchestrator.py`. The ORCA 5-channel selector is now the single routing authority.

### Unified Layout Engine
- **Backend-authoritative layout** — Layout engine now owns all positioning with optional frontend hint support. Eliminates layout drift between backend generation and frontend rendering.

### Plan CRUD
- **Plan template management** — `PUT /plan-templates/{type}` and `DELETE /plan-templates/{type}` endpoints for editing and deleting plan templates from the UI.

### SLO Management
- **SLO registry** — `slo_registry.py` provides CRUD operations with live Prometheus burn-rate integration. Persisted to `slo_definitions` table (migration 016).

### Skill Enrichment
- **All skills have trigger_patterns, tool_sequences, investigation_framework** — Every built-in skill now declares regex trigger patterns, named tool sequences for phased execution, and structured investigation methodology.

### Live Investigation Phases
- **`investigation_progress` WebSocket event** — Real-time phase updates during multi-phase investigations. Each phase reports status (pending/running/complete/failed/skipped), skill name, summary, and confidence.

### Deploy Risk Badges
- **Change risk scoring** — `change_risk.py` correlates recent changes with incidents. Findings display deploy risk badges in the UI.

### Skill Badges
- **Tool catalog badges** — Tools in the catalog show which skill(s) they belong to.

### Node Dedup in Dependency Graph
- **Topology deduplication** — Duplicate nodes in the dependency graph are merged, reducing visual noise in large clusters.

### Tool Renames
- **`describe_agent` / `describe_tools`** — Self-description tools renamed for clarity (previously `self_describe` / `self_describe_tools`).

### Code Review Fixes
- **Crash bug** — Fixed null pointer in plan runtime when investigation has no phases.
- **SQL precedence** — Fixed operator precedence in selector learning weight query.
- **Duplicate computation** — Eliminated redundant burn-rate calculation in SLO status endpoint.
- **O(n^2) BFS** — Fixed quadratic performance in dependency graph traversal.

### New Key Files
- `slo_registry.py` — SLO definition CRUD with live Prometheus burn rates
- `change_risk.py` — Deploy risk scoring for findings
- `plan_runtime.py` — Phased investigation plan execution engine
- `skill_scaffolder.py` — AI-generated skill packages from usage patterns
- `selector_learning.py` — ORCA selector weight learning from feedback signals

## v2.2.0 (2026-04-12)

### Adaptive Tool Selection Engine
- **TF-IDF Prediction** — Learns which tools are relevant for each query from real usage. Tokenizes queries into unigrams + bigrams, scores against `tool_predictions` table, returns top-K tools. Zero cost, sub-millisecond.
- **LLM Fallback** — When TF-IDF confidence is low (cold start), Haiku picks tools from names only (~200 tokens). Selections feed back into TF-IDF dictionary, making the LLM path self-eliminating.
- **Co-occurrence Bundles** — Tracks tools called together in the same turn (`tool_cooccurrence` table). When a tool is predicted, its co-occurring tools are automatically included.
- **Negative Signals** — Tools offered but never called get `miss_count` increments, actively suppressing wasted tools via `score - miss_count * 0.3`.
- **Mid-Turn Chain Expansion** — After each tool call, chain bigrams and co-occurrence data dynamically add predicted next-tools to the available set.
- **Daily Score Decay** — Scores multiplied by 0.95 daily, entries not seen in 30 days pruned. Prevents stale patterns from dominating.
- **ALWAYS_INCLUDE trimmed** — Reduced from 12 to 5 essential tools (list_pods, get_events, namespace_summary, record_audit_entry, list_my_skills).
- **Minimum set size** — Enforces at least 8 tools per query, padding from category fallback if needed.
- **New tables** — `tool_predictions` and `tool_cooccurrence` (migration 012).

### Pre-Route Handoff
- **Skill classifier checks handoff rules during routing** — If the keyword winner's `handoff_to` rules match the query, routes directly to the handoff target instead of routing to the winner first. Fixes queries like "create a capacity planning dashboard" routing to capacity_planner instead of view_designer.

### Type Safety
- **Typed `beta_tool` wrapper** — Created `sre_agent/decorators.py` with a properly-typed wrapper around `anthropic.beta_tool`. All 21 tool files import from `decorators` instead of `anthropic` directly. Single `type: ignore` in one file replaces 40+ across the codebase.
- **ToolLike Protocol** — Added `ToolLike` protocol to `tool_registry.py` for proper typing of tool object collections.
- **Mypy clean** — 0 errors across 115 source files with proper type annotations (no `type: ignore` in tool files).
- **Ruff clean** — 0 lint errors.

### Eval Improvements
- **Error suite fixed** — `completed: true` for error-handling scenarios, `should_block_release: false` for all 5 error scenarios.
- **Adversarial suite fixed** — `adversarial_resource_exhaustion` changed to `should_block_release: false` (agent correctly mitigated, not refused).
- **Synonym expansion** — Added "forbidden"/"exceeded" as synonyms for "quota" in replay scoring.
- **Error display** — Replay CLI now shows error messages in text output instead of silently swallowing exceptions.
- **All 9 eval suites pass** — 70/70 scenarios green.

## v2.1.0 (2026-04-12)

- Vertex AI cost analytics endpoint
- Coverage percentage returns 0-100 not 0-1
- Analytics router prefix fix
- Context bus timestamp test stabilization

## v2.0.0 (2026-04-12)

### Extensible Skill System
- Skill packages: drop-in .md files with routing, tools, evals, hot reload
- 6 skills: SRE, Security, View Designer, Capacity Planner + user-created
- Create/edit/delete/clone skills through chat or Toolbox UI
- Skill name routing (2x weight), keyword scoring, LLM fallback (haiku)
- User-created skills persist on PVC across restarts

### MCP Integration
- OpenShift MCP server (11 toolsets, 36 tools)
- SSE transport, prompt discovery, 3-tier rendering
- Toolset toggle from UI with crashloop detection
- Table parser for kubectl-style output

### Agent Intelligence
- Intent analysis prefix (think-before-acting)
- Dynamic prompt builder (centralized assembly)
- Skill-aware component hints (data-driven from registry)
- Edit-distance typo correction (catches novel misspellings)
- Synonym-based eval scoring
- Lazy skill validation (no false degradation at startup)
- ALWAYS_INCLUDE trimmed 23→12 (self-describe tools conditional)
- Runbook injection capped at 2000 chars

### Transparency & Observability
- Skill attribution footer on every chat response (skill, tools, duration, tokens)
- Prompt logging (hash, sections, tokens, version tracking)
- Hallucination detection (unknown tools, empty results)
- Confidence scoring in routing decisions
- Capability change toast notifications
- Welcome message with dynamic tool/skill counts

### Toolbox UI (/toolbox)
- Consolidated /tools + /extensions into 6-tab page
- Source badges (native/mcp) throughout
- Follow-up suggestion pills (context-aware)
- Prompt Audit section in Analytics
- Skill detail drawer with editor, versions, diff viewer
- MCP toolset toggles with checkboxes
- Clone + Delete buttons for skills
- Arrow key tab navigation, proper ARIA

### Self-Description Tools (12)
- list_my_skills, list_my_tools, list_ui_components
- list_promql_recipes, list_runbooks
- explain_resource, list_api_resources, list_deprecated_apis
- create_skill, edit_skill, delete_skill, create_skill_from_template

### Testing
- 1454 backend tests, 1934 frontend tests
- 9 multi-turn replay fixtures
- 15 security eval scenarios (3x increase)
- Prompt quality test suite
- 0.981 deterministic eval score, 19/21 judge pass

## v1.16.0 (2026-04-09)

### Added
- **Eval comparison infrastructure** — A/B baseline diffing with `--save-baseline`, `--compare-baseline`, `--fail-on-regression` CLI flags.
- **Prompt token audit** — `--audit-prompt` shows token cost per prompt section.
- **Section ablation framework** — test impact of removing prompt sections on eval scores.
- **View designer eval suite** — 6 scenarios + 4 new replay fixtures (17 total).
- **Eval history DB** — `eval_runs` table (migration 006) with trends API (`GET /eval/history`, `GET /eval/trend`).
- **CI automation** — live judge runs on releases, daily cron, prompt change triggers.
- **GitHub secrets** for Vertex AI (`VERTEX_PROJECT_ID`, `VERTEX_REGION`, `GCP_SA_KEY`).
- **UI Evals tab** on Agent Settings — quality gate, suite scores, dimension bars, prompt audit viz, sparkline trends.
- **ToolsView accessibility fixes** — aria-labels, keyboard nav, ToolCard extraction.
- **View designer prompt improvement** — specific commands, cautious writes.
- **bump-version.sh** auto-updates umbrella chart subchart.
- **Replay harness** — thinking parameter support, config singleton fix, model defaults to `claude-sonnet-4-6`.

## v1.15.0 (2026-04-09)

### Added
- **Modular package architecture** — Split 3 largest files into focused packages: `k8s_tools/` (11 modules, was 4419 lines), `monitor/` (10 modules, was 2486 lines), `api/` (12 modules, was 2415 lines). No file exceeds 910 lines. All backward-compatible imports preserved.
- **Typo auto-correction** — `fix_typos()` corrects ~130 common K8s/SRE misspellings (deployment, namespace, prometheus, vulnerability, etc.) with automatic plural/suffix handling. Applied before intent classification and tool selection.
- **Route safety tests** — 22 tests guard against broken GVR routes (leading tilde, wrong namespace wildcard, double slashes in API paths).
- **Centralized configuration** — All ~30 raw `os.environ.get()` calls migrated to `get_settings()`. Added 6 missing config fields: `db_pool_min/max`, `noise_threshold`, `max_trust_level`, `investigations_max_per_scan`, `investigation_cooldown`, `dev_user`.

### Removed
- **`layout_templates.py`** — deleted deprecated module (replaced by `layout_engine.py`), along with 4 backward-compat tests.
- **Dead code** — removed unused `DEFAULT_DB_PATH` constant, identity typo mapping, unused `os` imports from 4 files.

### Fixed
- **Nodes page 404** — `TopologyMap.tsx` used `/r/~v1~nodes/*` which decoded to empty API group (`/apis//v1/nodes`). Fixed to `/r/v1~nodes/_`.
- **CRDs route bug** — `CRDsView.tsx` produced leading tilde for CRDs with empty `spec.group`.
- **`MAX_RESULTS` duplication** — was defined identically in 8 k8s_tools submodules; centralized to `validators.py`.

## v1.14.0 (2026-04-03)

### Added
- **Tool eval prompts** -- 84 real-world user queries mapped to expected tool calls, covering all 82 registered tools. CI enforces eval prompt coverage for new tools.
- **`delete_dashboard` and `clone_dashboard` tools** -- manage saved views from the agent conversation.
- **Token usage tracking** -- records input/output/cache tokens per turn from the Claude API for cost visibility.
- **Semantic layout engine** -- role-based auto-layout replaces 5 fixed dashboard templates. Widgets arranged by role (KPI, chart, table, status) and content relationships.
- **Intelligence loop** -- `intelligence.py` feeds tool analytics (query reliability, dashboard patterns, error hotspots) back into the system prompt.
- **Plan validation** -- `plan_dashboard` now validates plans before execution, catching missing components early.
- **Tool analytics** -- full audit log with chain intelligence (bigram discovery, next-tool hints), usage stats API, chains endpoint.
- **View versioning** -- version history with undo support for saved views.

### Changed
- **Prompt optimization** -- SRE system prompt reduced from 28KB to 8KB (71% reduction) via selective component schema and runbook injection.
- **View designer prompt** rewritten -- 50% smaller, workflow-first approach.
- `verify_query` made optional for recipe-based PromQL queries to reduce Prometheus round-trips.
- `time_range` defaults to 1h for chart components.

### Fixed
- NaN values in chart data causing JSON parse failures and 500 errors on view endpoints.
- Connection leak, context validation, and ApiClient reuse issues.
- View designer bugs: generic `cluster_metrics` forced on every dashboard, missing title exemptions for grid/tabs/section.
- SQL interval syntax, category tracking, and Prometheus client issues.
- Invalid PromQL, dead code, session leak, and KPI sizing issues from code review.

### Docs
- Updated tool count from 105 to 82 across all documentation.
- Updated test count to 1078.
- Added EVAL_PROMPTS.md, DESIGN_PRINCIPLES.md, and CHANGELOG.md.
- Added Tool Analytics section to README.
- Updated API_CONTRACT.md with view REST endpoints and tool chain endpoint.

## v1.13.1 (2026-03-28)

### Added
- 88 unit tests for all 11 monitor scanner functions.
- Startup probes for agent (60s) and PostgreSQL (30s).
- PodDisruptionBudget for zero-downtime rollouts.

### Changed
- Deployment strategy changed to RollingUpdate with maxUnavailable=1/maxSurge=0.
- Removed standalone WS token generator; umbrella chart owns the token.

### Fixed
- Share endpoint JSONResponse bug.
- Memory timing bug, async pattern detection, version history diffs.

## v1.13.0 (2026-03-25)

### Added
- Generic `list_resources` and `describe_resource` tools for any K8s resource type via the Table API.
- 14 smart column renderers for resource tables.
- Resource relationship tracer (`get_resource_relationships`).
- View auto-save: `create_dashboard` saves directly to PostgreSQL.
- View versioning with undo and share/clone support.
- Structured JSON logging.
- Connection pooling with `ThreadedConnectionPool`.
- Schema migration system (`db_migrations.py`).
- Warning-severity investigation in monitor (not just critical).
- Default namespace scanning (removed from skip list).
- Showcase eval scenarios for all 10 component types.

### Changed
- PostgreSQL-only database layer (SQLite removed for production).
- Removed 9 redundant tools, consolidated into generic resource tools.
- Context helper extraction, tool parallelization.

### Fixed
- 401 on views API.
- Prometheus table labels.
- NaN in chart data causing JSON parse failures.

## v1.12.0 (2026-03-18)

### Added
- Auto-routing orchestrator (`/ws/agent` endpoint).
- Agent-to-agent handoff tools (`request_security_scan`, `request_sre_investigation`).
- Shared context bus for cross-agent communication.
- Noise learning for monitor findings.
- Morning briefing endpoint (`GET /briefing`).
- Simulation preview endpoint (`POST /simulate`).

## v1.9.0 (2026-03-01)

### Added
- Self-improving agent with incident memory, learned runbooks, and pattern detection.
- 73 production-tested PromQL recipes across 16 categories.
- `discover_metrics` and `verify_query` tools.
- Pydantic v2 configuration (`PulseAgentSettings`).

## v1.4.0 (2026-02-01)

### Added
- Protocol v2: `/ws/monitor` for autonomous scanning.
- 16 scanners (11 cluster + 5 audit).
- Auto-fix at trust levels 3 and 4.
- Fix history with rollback support.
- Confidence scores on all findings.

## v1.0.0 (2026-01-15)

- Initial release: SRE agent, security scanner, 9 security tools, CLI and WebSocket API.
