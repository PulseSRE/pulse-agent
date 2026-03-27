# Pulse Agent Evals

Deterministic eval suites used to score quality and gate releases.

## Run

```bash
python -m sre_agent.evals.cli --suite core
python -m sre_agent.evals.cli --suite core --format json
python -m sre_agent.evals.cli --suite core --fail-on-gate
python -m sre_agent.evals.weekly_digest_cli --output artifacts/weekly-digest.md
```

## Suite Data

Scenario fixtures live in `sre_agent/evals/scenarios_data/*.json`.

Each scenario includes:
- tool usage trace
- safety flags
- completion/outcome metadata
- optional expected blockers

## Rubric

Dimensions:
- `task_success`
- `safety`
- `tool_efficiency`
- `operational_quality`
- `reliability`

Release gate requires:
- minimum overall score threshold
- minimum per-dimension thresholds
- no hard blocker violations

## Weekly Digest

Generate a Markdown summary that combines release/safety/integration gate status with outcome trend regressions:

```bash
python -m sre_agent.evals.weekly_digest_cli --current-days 7 --baseline-days 7 --output artifacts/weekly-digest.md
```

## Regression Budget Policy

Outcome regression thresholds are versioned in:
- `sre_agent/evals/policies/outcome_regression_policy.yaml`

Override from CLI when needed:

```bash
python -m sre_agent.evals.outcomes_cli --policy-file sre_agent/evals/policies/outcome_regression_policy.yaml
```
