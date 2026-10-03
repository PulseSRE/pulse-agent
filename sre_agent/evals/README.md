# Pulse Agent Evals

Evaluation framework with fixture reports, replay harness checks, and live model/judge runs. Only executing the real agent establishes model-behavior evidence.

> **See also:** [`TESTING.md`](../../TESTING.md) for commands, evidence limits, CI conditions, and the release process.

## Scenario Suites

Scenario-suite inventory below is illustrative; JSON/YAML fixtures are authoritative. Suite scores of hand-authored data do not prove agent quality:

| Suite | Scenarios | Purpose |
|-------|-----------|---------|
| `core` | 6 | Fundamental SRE diagnostics |
| `release` | 19 | Fixture report; actual release gate is live judged replay |
| `safety` | 5 | Dangerous action guardrails |
| `integration` | 23 | Cross-tool workflows |
| `adversarial` | 5 | Prompt injection and edge cases |
| `errors` | 5 | Error handling and recovery |
| `fleet` | 11 | Multi-cluster operations |
| `sysadmin` | 20 | Real-world sysadmin queries |
| `view_designer` | 11 | Dashboard generation quality |
| `autofix` | 7 | Auto-fix decision accuracy |
| `selector` | 59 | Skill routing validation |
| `scaffolded` | 1+ | Auto-generated from skill scaffolder |
| `capacity_planner` | 5 | Resource forecasting and right-sizing |
| `postmortem` | 5 | Timeline reconstruction and RCA |
| `slo_management` | 5 | SLO burn rates and error budgets |
| `plan_builder` | 5 | Skill creation and plan templates |

Scenario fixtures live in `sre_agent/evals/scenarios_data/*.json`.

## Replay Fixtures

Replay fixtures provide recorded cluster/tool responses. `--dry-run` builds mock responses from fixture expectations and checks plumbing, not model quality. A live replay invokes the real model and optional judge against those recordings without a live cluster.

### Replay runs the real agent

The replay harness (`replay.py` + `replay_config.py`) configures each turn the way `/ws/agent` does: the prompt is routed to a skill, `build_orchestrated_config()` selects that skill's tools, and `prompt_builder.assemble_prompt()` assembles the real system prompt (skill prompt, intent prefix, component catalog, runbooks). The judge therefore scores Pulse, not a bare model with a one-line prompt.

Two guarantees keep it offline:

- **Every** tool in the map is rebuilt as a recorded stub by `shadow_tool_map()` before the loop starts — a recorded response when the fixture has one, otherwise a "no recorded response" sentinel that tells the agent the tool is unavailable. No executable tool object reaches the agent loop.
- `offline_context()` patches out cluster-context injection (`harness.get_cluster_context` and the alias in `agent`) and the tool selector's live LLM fallback for the duration of the run. A required patch that cannot be applied raises rather than silently running without isolation.

```bash
python -m sre_agent.evals.replay_cli --fixture crashloop_diagnosis --judge   # real Pulse config (default)
python -m sre_agent.evals.replay_cli --fixture crashloop_diagnosis --judge --stub-config  # old stub config
python -m sre_agent.evals.replay_cli --all --dry-run --mode sre              # force a skill
```

Text output reports the routed skill, how many tools were offered, tools the agent called with no recording, and recorded tools the real config did not offer (a tool-selection miss worth investigating).

## 4-Dimension ORCA Rubric

Every scenario is scored across four dimensions:

- **resolution** (40%) — did the agent solve the problem?
- **efficiency** (30%) — optimal tool call count (2-5 ideal)?
- **safety** (20%) — zero rejected/dangerous actions?
- **speed** (10%) — completed within time budget?

The ORCA rubric scores scenario data. The current live replay gate instead uses deterministic replay checks, judge scores, and baseline comparison; see `make eval-gate` and the workflow for actual thresholds.

## LLM Judge

An LLM judge scores replay traces on four axes: correctness, completeness, actionability, and safety. Used for richer evaluation beyond deterministic checks.

## A/B Comparison (`compare.py`)

Compare eval results against a saved baseline to detect regressions:

```bash
python -m sre_agent.evals.cli --suite release --save-baseline      # save current as baseline
python -m sre_agent.evals.cli --suite release --compare-baseline   # diff against baseline
python -m sre_agent.evals.cli --suite release --fail-on-regression # CI gate: fail if scores drop
```

## Ablation Framework (`ablation.py`)

Test the impact of removing prompt sections on eval scores. Uses `PULSE_PROMPT_EXCLUDE_SECTIONS` to selectively disable prompt sections and measure score deltas.

```bash
python -m sre_agent.evals.ablation --suite release --mode sre
```

## Eval History DB (`history.py`)

Eval runs are persisted to the `eval_runs` table (migration 006). The REST API exposes trend data:

- `GET /eval/history` — paginated run history (filter by suite, days, limit)
- `GET /eval/trend` — score trend summary with sparkline data

## Outcome Regression Tracking (`outcomes.py`)

Tracks outcome regressions across eval runs. Thresholds are versioned in:

```
sre_agent/evals/policies/outcome_regression_policy.yaml
```

## CLI Commands

### Eval CLI

```bash
python -m sre_agent.evals.cli --suite release              # run a suite
python -m sre_agent.evals.cli --suite core --format json    # JSON output
python -m sre_agent.evals.cli --suite release --fail-on-gate       # fail if gate not met
python -m sre_agent.evals.cli --suite core --save-baseline         # save baseline
python -m sre_agent.evals.cli --suite core --compare-baseline      # compare vs baseline
python -m sre_agent.evals.cli --suite release --fail-on-regression # fail if scores regress
python -m sre_agent.evals.cli --audit-prompt --mode sre            # prompt token cost breakdown
```

### Replay CLI

```bash
python -m sre_agent.evals.replay --fixture pod_crashloop   # replay single fixture
python -m sre_agent.evals.replay --all                     # replay all fixtures
python -m sre_agent.evals.replay --all --judge             # replay + LLM judge scoring
python -m sre_agent.evals.replay --fixture node_pressure --dry-run  # preview without scoring
python -m sre_agent.evals.replay --model claude-sonnet-5     # specify model
```

### Weekly Digest

```bash
python -m sre_agent.evals.weekly_digest_cli --current-days 7 --baseline-days 7 --output artifacts/weekly-digest.md
```

### Outcome Regression

```bash
python -m sre_agent.evals.outcomes_cli --policy-file sre_agent/evals/policies/outcome_regression_policy.yaml
```
