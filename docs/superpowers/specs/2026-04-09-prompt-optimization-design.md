# Prompt Optimization — Design Spec

**Date:** 2026-04-09
**Goal:** Optimize the SRE system prompt for higher quality and lower token cost based on ablation experiments.

## Problem

The SRE system prompt grew organically with each feature addition. Nobody measured whether each section actually helped. The prompt token audit showed:

- component_schemas: 46% of tokens
- component_hint_ops: 20%
- runbooks: 14%
- base_prompt: 14%
- component_hint_core: 5%

Total ~2,500 tokens before any dynamic injection (intelligence, chain hints, cluster context).

## Methodology

Ran 12 experiments using the replay harness with Sonnet 4.6 and LLM-as-judge scoring (correctness/completeness/actionability/safety). Each experiment modified one variable and tested against 5 SRE fixtures. Baseline: 90.4/100 avg.

## Experiment Results

### Section Removal (Ablation)

| Section | Tokens | Score | Delta | Decision |
|---|---|---|---|---|
| component_schemas (46%) | ~1194 | 88.4 | -2.6 | **KEEP** — agent needs JSON format examples |
| component_hint_ops (20%) | ~512 | 91.0 | +0.6 | **REMOVE** — PromQL syntax, table guidelines redundant |
| runbooks (14%) | ~368 | 92.2 | +1.8 | **REMOVE** — Claude already knows K8s troubleshooting |
| component_hint_core (5%) | ~127 | 91.0 | +0.6 | **REMOVE** — "use list_resources" guidance adds nothing |
| all intelligence (dynamic) | ~0* | 90.8 | +0.4 | KEEP — has value when DB has real data |
| chain_hints (dynamic) | ~0* | 90.8 | +0.4 | KEEP — has value when DB has real data |
| Combined trim (39%) | ~1007 | 92.8 | +2.4 | **APPLY** — no compound negative effect |

*Intelligence and chain hints were 0 chars locally (no DB data). On deployed instance they have content.

### Prompt Engineering

| Experiment | Score | Delta | Decision |
|---|---|---|---|
| Security rules first | 93.6 | +3.2 | **APPLY** — safety context before instructions |
| Few-shot example | 93.2 | +2.8 | **APPLY** — one worked example > pages of rules |
| Shorter base prompt (20→8 lines) | 93.0 | +2.6 | **APPLY** — less noise, better focus |
| Single runbook (1 instead of 3) | 92.6 | +2.2 | **APPLY** — most relevant only |
| Chain of thought instruction | 91.2 | +0.8 | SKIP — minimal improvement |

### View Designer

| Experiment | Score | Delta | Decision |
|---|---|---|---|
| No chart type table | 88.2 | -0.8 | SKIP — within noise, keep for safety |
| Dashboard quality checklist | 88.2 | -0.8 | SKIP — no improvement |

## Optimal Prompt Design

Combine the winning experiments:

1. **Security rules FIRST** (was last) — +3.2 pts
2. **Compressed core rules** (20→8 lines) — +2.6 pts  
3. **One worked diagnostic example** (new) — +2.8 pts
4. **Remove component_hint_ops** — saves ~512 tokens
5. **Remove component_hint_core** — saves ~127 tokens
6. **Remove runbooks from default** — saves ~368 tokens
7. **Single runbook selection** (1 instead of 3) — +2.2 pts

**Expected result:** ~93-95/100 judge avg (up from 90.4 baseline), ~40% fewer prompt tokens.

## What NOT to change

- **component_schemas** — must stay, -2.6 pts without them
- **Chart type selection table** — keep in view designer, removal showed no benefit
- **Intelligence injection** — keep, provides cluster-specific guidance when DB has data
- **Chain hints** — keep, guides tool sequences from real usage patterns

## Risks

- Experiments tested 5 fixtures each. With ~5pt judge variance, deltas under 3 are borderline.
- Combined effect of all changes together hasn't been tested (only individual + triple trim).
- Shorter prompt may miss edge cases not covered by fixtures.
- Removing runbooks might hurt on rare diagnostic scenarios (DNS, PVC issues) not in test set.
