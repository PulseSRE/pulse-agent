---
name: run-evals
description: |
  Run Pulse evaluation suites quickly. Use when the user says "run evals", "check evals",
  "run the gate", "check routing", "run release suite", or wants to verify scores.
  Supports all 11 suites + selector + baseline comparison.
---

# Pulse evaluation workflow

Read [TESTING.md](../../../TESTING.md) and [eval README](../../../sre_agent/evals/README.md). Report the execution mode as well as the score.

```bash
make evals       # offline harness and scenario fixture reports
make eval-gate   # real agent/model with recorded tools and live judge; provider cost
python -m sre_agent.evals.cli --audit-prompt --mode sre
```

The live gate currently uses model `claude-sonnet-5`, concurrency 4, minimum judge score 60, three judge samples, and the checked-in replay baseline. Match the current Makefile/workflow if settings change.

`python -m sre_agent.evals.cli --suite <suite>` scores scenario fixture data. `--fail-on-gate` enforces that fixture rubric but does not turn it into a live-agent run. `replay_cli --dry-run` uses expectation-derived mocks and establishes harness plumbing only. Live replay uses recorded tool responses, so no live Kubernetes cluster is needed; provider credentials/API spend are required. A saved baseline is a comparison artifact, not an independent quality measurement.

Capture exact commands, pass/fail, mode, artifacts, and limitations. Do not silently replace a provider-backed failure with a dry-run success or overwrite a baseline merely to suppress a regression.
