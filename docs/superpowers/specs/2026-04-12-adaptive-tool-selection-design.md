# Adaptive Tool Selection Engine

**Date:** 2026-04-12
**Status:** Approved
**Goal:** Improve harness tool selection accuracy from 8% to 50%+

## Problem

The harness currently dumps all tools from all skill categories (~80 tools) regardless of query content. Claude uses 3-5 per turn. 92% of offered tools are wasted context.

## Architecture

Tiered prediction system: TF-IDF token scoring (hot path) + LLM picker (cold-start fallback) + chain-based mid-turn expansion.

```
Query arrives
    |
    v
TF-IDF token scoring --> confidence score
    |
    v
confidence >= threshold? --yes--> use TF-IDF tool set (0ms, $0)
    | no
    v
Haiku tool picker (~100ms, ~$0.001) --> tool set
    |
    v
write selections back to TF-IDF dictionary (hot learning)
    |
    v
As tools get called --> chain bigrams expand the set mid-turn
```

The LLM path is self-eliminating -- it trains the TF-IDF dictionary until TF-IDF handles everything.

## Data Model

### tool_predictions

Stores learned token-to-tool affinities from real usage.

```sql
CREATE TABLE tool_predictions (
    token       TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    score       FLOAT NOT NULL DEFAULT 1.0,
    hit_count   INT NOT NULL DEFAULT 1,
    miss_count  INT NOT NULL DEFAULT 0,
    last_seen   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (token, tool_name)
);
CREATE INDEX idx_tool_predictions_token ON tool_predictions(token);
```

Effective score: `score - (miss_count * 0.3)`

### tool_cooccurrence

Tools called together in the same turn.

```sql
CREATE TABLE tool_cooccurrence (
    tool_a      TEXT NOT NULL,
    tool_b      TEXT NOT NULL,
    frequency   INT NOT NULL DEFAULT 1,
    PRIMARY KEY (tool_a, tool_b)
);
```

## Token Extraction

- Lowercase, split on whitespace + punctuation
- Drop stopwords: the, a, in, my, me, can, you, please, what, is, are, do, how, this, that, it, for, to, of, and, or
- Keep K8s terms intact: crashloopbackoff, imagepullbackoff, clusterip, etc.
- Generate bigrams for multi-word concepts: "node pressure", "pod logs"

## Query-Time Flow

1. **Tokenize** query into unigrams + bigrams
2. **Lookup** each token in `tool_predictions`, sum scores per tool
3. **Confidence check**: if max hit_count among matched tokens > 10 -> HIGH confidence (use TF-IDF). Otherwise LOW -> LLM fallback.
4. **Top-K selection** (K=10): rank by effective_score, take top 10
5. **Co-occurrence expansion** (+3-5): for each predicted tool, add co-occurring tools above 60% frequency
6. **ALWAYS_INCLUDE** (5 tools): list_pods, get_events, namespace_summary, record_audit_entry, list_my_skills
7. **Final set**: ~12-15 tools sent to Claude

## LLM Fallback (Low Confidence)

- Send query + tool names only (~200 tokens) to Haiku
- Prompt: "Pick the 10 most relevant tools for this query"
- Write selections back to tool_predictions (bootstraps TF-IDF)
- Same co-occurrence + ALWAYS_INCLUDE applied after

## Mid-Turn Chain Expansion

After Claude calls a tool:
1. Lookup chain bigrams (tool_chains.py) for next-tool predictions
2. Lookup co-occurrence for parallel-tool predictions
3. If predicted tools not in current set, add them to tool_defs
4. Next Claude iteration sees the expanded set

## Real-Time Learning

On every completed turn (`tool_usage.record_turn`):

```python
tokens = extract_tokens(query)
for token in tokens:
    for tool in tools_called:
        UPSERT tool_predictions(token, tool) SET score += 1.0, hit_count += 1
    for tool in (tools_offered - tools_called):
        UPSERT tool_predictions(token, tool) SET miss_count += 1

for tool_a, tool_b in combinations(tools_called, 2):
    UPSERT tool_cooccurrence(tool_a, tool_b) SET frequency += 1
```

## Cold Start Strategy

| Data Volume | Primary Path | Fallback |
|-------------|-------------|----------|
| 0-10 turns  | Category matching (current) | None |
| 10-50 turns | LLM picker (Haiku) | Category matching |
| 50+ turns   | TF-IDF predictions | LLM picker |

Turn count queried from tool_turns table at startup.

## Safety Rails

1. **Minimum set size: 8 tools.** Pad from category matching if predictor returns fewer.
2. **Write tools gated.** Confirmation gate unchanged regardless of prediction path.
3. **Staleness decay.** Daily: scores *= 0.95. Prune tokens not seen in 30 days.
4. **Accuracy monitoring.** If harness effectiveness drops below 30% for 24h, widen K from 10 to 15.
5. **Eval gate.** New scenario: predict against historical turns, verify 80%+ recall.

## ALWAYS_INCLUDE (trimmed)

Reduced from 12 to 5:
- list_pods
- get_events
- namespace_summary
- record_audit_entry
- list_my_skills

## Integration Points

### New file
- `sre_agent/tool_predictor.py` -- prediction engine, tokenizer, scoring, LLM fallback, DB ops

### Modified files
- `skill_loader.py:build_config_from_skill()` -- replace category dump with `predict_tools(query)`
- `agent.py:run_agent_streaming()` -- chain expansion hook after each tool call
- `tool_usage.py:record_turn()` -- call `tool_predictor.learn()` after recording
- `db.py` -- migration 007 for new tables

### Unchanged
- Skill routing (SRE/Security/View Designer)
- Tool chain bigrams (reused for expansion)
- Component hints (derived from selected set)
- System prompt construction
- Confirmation gate
- Intelligence analytics
