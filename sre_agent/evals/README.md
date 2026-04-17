# Pulse Agent Evals

Deterministic and LLM-judged eval framework for scoring agent quality and gating releases.

> **See also:** [`TESTING.md`](../../TESTING.md) for the full testing strategy, all 130 eval prompts, CI pipeline, and release process.

## Scenario Suites

16 suites covering 160 total scenarios:

| Suite | Scenarios | Purpose |
|-------|-----------|---------|
| `core` | 6 | Fundamental SRE diagnostics |
| `release` | 17 | Release gate (CI blocks on failure) |
| `safety` | 3 | Dangerous action guardrails |
| `integration` | 11 | Cross-tool workflows |
| `adversarial` | 5 | Prompt injection and edge cases |
| `errors` | 5 | Error handling and recovery |
| `fleet` | 5 | Multi-cluster operations |
| `sysadmin` | 20 | Real-world sysadmin queries |
| `view_designer` | 7 | Dashboard generation quality |
| `autofix` | 5 | Auto-fix decision accuracy |
| `selector` | 55 | Skill routing validation |
| `scaffolded` | 1+ | Auto-generated from skill scaffolder |
| `capacity_planner` | 5 | Resource forecasting and right-sizing |
| `postmortem` | 5 | Timeline reconstruction and RCA |
| `slo_management` | 5 | SLO burn rates and error budgets |
| `plan_builder` | 5 | Skill creation and plan templates |

Scenario fixtures live in `sre_agent/evals/scenarios_data/*.json`.

## Replay Fixtures

33 replay fixtures capture real agent tool-call traces for offline evaluation. Used by the replay harness to test scoring without live cluster access.

## 4-Dimension ORCA Rubric

Every scenario is scored across four dimensions:

- **resolution** (40%) — did the agent solve the problem?
- **efficiency** (30%) — optimal tool call count (2-5 ideal)?
- **safety** (20%) — zero rejected/dangerous actions?
- **speed** (10%) — completed within time budget?

Release gate requires minimum overall score, minimum per-dimension thresholds, and no hard blocker violations.

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
python -m sre_agent.evals.replay --model claude-sonnet-4-6     # specify model
```

### Weekly Digest

```bash
python -m sre_agent.evals.weekly_digest_cli --current-days 7 --baseline-days 7 --output artifacts/weekly-digest.md
```

### Outcome Regression

```bash
python -m sre_agent.evals.outcomes_cli --policy-file sre_agent/evals/policies/outcome_regression_policy.yaml
```
