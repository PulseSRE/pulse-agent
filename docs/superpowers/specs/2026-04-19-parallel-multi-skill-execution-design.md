# Parallel Multi-Skill Execution Design

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Date:** 2026-04-19
**Status:** Approved
**Author:** Ali + Claude

## Problem

ORCA selects a single skill per turn. When a query spans multiple domains (e.g., "check why pods are crashing and scan for CVEs"), ORCA picks the strongest match and drops the other. The plan runtime supports multi-phase execution via `_race_parallel()` but can't be triggered from the per-turn WebSocket flow. Additionally, the temporal channel uses keyword-only scoring — `change_risk.py` and `get_recent_changes()` exist but aren't wired into the selector.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Trigger model | Both: ORCA auto-detect + plan-declared parallel phases | Score gap catches ambiguous queries; intent splitting catches compound queries |
| Conflict resolution | Merge with human arbitration | Design principle #5: human-in-the-loop for anything that matters |
| Result presentation | Unified synthesis | Design principle #8: single pane of glass, minimal cognitive load |
| Multi-skill trigger | Score gap heuristic + intent splitting | Score gap for ambiguous queries, intent splitting for explicit compound requests |
| Concurrency limit | Max 2 parallel skills | Covers 95% of cases, keeps synthesis manageable, easy to bump later |
| Synthesis model | claude-sonnet-4-6 | Subtle conflict detection requires semantic understanding; Haiku would miss non-obvious contradictions |
| Temporal channel | Wire change_risk.py into ORCA selector | Low effort, high payoff; infrastructure already exists |

## Architecture

### Section 1: ORCA Top-K Selection

**Current state:** `SkillSelector.select()` returns a single `SelectionResult` with `skill_name` and `fused_scores` dict. `classify_query()` returns a single `Skill`.

**Changes:**

- `SelectionResult` gets `secondary_skill: str | None` — populated when the gap between top-1 and top-2 fused scores is <= `MULTI_SKILL_THRESHOLD` (default 0.15, configurable via `PulseAgentSettings`).

- New function `classify_query_multi(query: str, *, context: dict | None = None) -> tuple[Skill, Skill | None]`. Returns primary + optional secondary skill. Existing `classify_query()` is unchanged (returns `Skill`) — no caller breakage. `classify_query_multi()` calls `classify_query()` internally, then checks score gap and intent splitting for the secondary. Only `ws_endpoints.py` calls the new function.

- Intent splitter: `split_compound_intent(query: str) -> list[str]` in `orchestrator.py`. Lightweight regex for conjunctions ("and", "also", "plus", "then") splitting around independent clauses. Each sub-query runs through ORCA independently. Different skills -> multi-skill activation. Same skill -> single-skill (the full original query is passed to that single skill, not split). If splitting produces more than 2 sub-queries routing to different skills, only the top-2 by ORCA score are activated.

- Conflict guard: Bidirectional check — if `primary.conflicts_with` includes the secondary OR `secondary.conflicts_with` includes the primary, suppress multi-skill and fall back to top-1.

- Budget: Each skill gets 25 total tools (including ALWAYS_INCLUDE). With ~5 ALWAYS_INCLUDE tools, each skill gets ~20 discretionary tools. ALWAYS_INCLUDE tools are shared (not doubled in the combined set).

### Section 2: Temporal Channel Wiring

**Current state:** Temporal channel in `skill_selector.py` scores are keyword-only. `change_risk.py` has `score_deploy_risk()` and `get_recent_changes()` but neither feeds into the selector.

**Changes:**

- `TemporalSignal` dataclass in `skill_selector.py`:
  - `recent_deploys: list[dict]` — from `get_recent_changes()`, last 30 min
  - `time_of_day: str` — `"business_hours"` | `"off_hours"` | `"weekend"`
  - `active_incidents: int` — from context_bus open findings count

- `_score_temporal()` rework:
  1. Calls `get_recent_changes(minutes=30)` (cached, 60s TTL)
  2. Recent deploys -> boost `sre` by 0.3, boost `postmortem` by 0.15
  3. Off-hours/weekend -> boost `slo-management` by 0.1
  4. Active incidents > 0 in context_bus -> boost `sre` by 0.2
  5. Boost magnitudes configurable in skill frontmatter via `temporal_boost: float`

- Caching: `_temporal_signal` cached at module level with `time.monotonic()` TTL check. In-memory dict with expiry, no new dependency.

- Fallback: `_score_temporal()` wraps `get_recent_changes()` in try/except. If K8s API or DB is unreachable, returns empty dict (neutral scores). Logged at debug level, no error propagation into routing.

### Section 3: Parallel Execution Engine

**Current state:** `_race_parallel()` in `plan_runtime.py` runs phases concurrently with first-high-confidence-wins cancellation. Plan-only, not usable from WebSocket per-turn flow.

**Changes:**

- New function `run_parallel_skills()` in `plan_runtime.py`:
  ```python
  async def run_parallel_skills(
      primary: Skill,
      secondary: Skill,
      query: str,
      messages: list[dict],
      client,
      on_text=None,
      on_tool_use=None,
  ) -> ParallelSkillResult
  ```

- `ParallelSkillResult` dataclass:
  - `primary_output: str` — full response from primary skill
  - `secondary_output: str` — full response from secondary skill
  - `primary_skill: str`
  - `secondary_skill: str`
  - `primary_confidence: float`
  - `secondary_confidence: float`
  - `duration_ms: int`

- Execution flow:
  1. Build two configs via `build_config_from_skill()` — one per skill
  2. Split tool budget: 25 tools each, ALWAYS_INCLUDE shared
  3. Launch two `run_agent_streaming()` calls as `asyncio.Task`s
  4. Both run to completion (no early cancellation — both needed for synthesis)
  5. Collect into `ParallelSkillResult`

- Timeout: Each skill gets standard per-turn timeout. If one times out, the other's result is used alone (graceful degradation to single-skill).

- Context isolation: During parallel execution, context bus entries are buffered per-skill with a `parallel_task_id` tag. Published to the bus only after synthesis completes, preventing interleaved findings from confusing future turns.

- Integration point: `ws_endpoints.py` calls `run_parallel_skills()` instead of `run_agent_streaming()` when `classify_query_multi()` returns a secondary skill.

- Distinct from `_race_parallel()`: This function does NOT reuse `_race_parallel()` because that function cancels siblings on high-confidence wins. `run_parallel_skills()` always runs both to completion since synthesis needs both outputs.

### Section 4: Synthesis Layer

**Current state:** No synthesis — each turn's response is streamed directly from one skill.

**Changes:**

- New module `synthesis.py` with `synthesize_parallel_outputs()`:
  ```python
  async def synthesize_parallel_outputs(
      result: ParallelSkillResult,
      query: str,
      client,
  ) -> SynthesisResult
  ```

- `SynthesisResult` dataclass:
  - `unified_response: str` — merged coherent response
  - `conflicts: list[Conflict]` — detected contradictions
  - `sources: dict[str, str]` — skill_name -> attribution

- `Conflict` dataclass:
  - `topic: str` — e.g., "scaling recommendation"
  - `skill_a: str`
  - `position_a: str`
  - `skill_b: str`
  - `position_b: str`

- Synthesis prompt: Focused Claude Sonnet call (~500 token system prompt). Instructions:
  1. Merge non-conflicting findings into one coherent narrative
  2. Identify contradictions and emit as structured `Conflict` blocks
  3. Don't resolve conflicts — present both positions for user arbitration
  4. Attribute findings to source skill where relevant

- Conflict presentation in response:
  ```
  Conflicting recommendations on [topic]:
  - SRE: [position_a]
  - Capacity Planner: [position_b]
  -> Which approach would you like to proceed with?
  ```

- No-conflict fast path: If outputs are complementary, skip conflict block and emit merged response directly.

- Fallback on synthesis failure: If the Sonnet synthesis call fails (API error, timeout, circuit breaker), fall back to concatenation — emit primary output followed by secondary output separated by a skill header. Never drop a completed skill's output due to synthesis failure.

### Section 5: WebSocket Integration & Protocol

**Current state:** `websocket_auto_agent()` calls `classify_query()` -> `build_orchestrated_config()` -> `run_agent_streaming()` per turn. Single skill, single response stream.

**Changes:**

- Routing branch in `websocket_auto_agent()`:
  ```python
  skill, secondary = classify_query_multi(content)
  if secondary:
      await ws.send_json({"type": "multi_skill_start", "skills": [skill.name, secondary.name]})
      result = await run_parallel_skills(skill, secondary, ...)
      await ws.send_json({"type": "skill_progress", "skill": skill.name, "status": "complete"})
      await ws.send_json({"type": "skill_progress", "skill": secondary.name, "status": "complete"})
      await ws.send_json({"type": "skill_progress", "skill": "synthesis", "status": "running"})
      synthesis = await synthesize_parallel_outputs(result, content, client)
      # stream synthesis.unified_response as text_delta events (same as single-skill)
      # emit done with extended metadata
  else:
      # existing single-skill path unchanged
  ```

- New WebSocket event types (Protocol v2 additions):
  - `{"type": "multi_skill_start", "skills": ["sre", "security"]}` — emitted immediately after routing
  - `{"type": "skill_progress", "skill": "sre", "status": "running"|"complete"|"timeout"}` — per-skill progress

- No `synthesis` event type. Synthesis output streams as standard `text_delta` events. The `done` event is extended with optional metadata:
  ```json
  {"type": "done", "full_response": "...", "multi_skill": {"skills": ["sre", "security"], "conflicts": [...]}}
  ```
  Old clients ignore the `multi_skill` field. New clients use it to render conflict UI.

- Backward compatibility: Single-skill turns emit no new events. Old clients that don't handle `multi_skill_start` or `skill_progress` silently drop them (unknown event types). The response itself arrives as standard `text_delta` + `done`.

- Sticky mode: Resets to `None` after multi-skill turn — prevents locking into one skill after a multi-domain turn.

- Context bus publication: Both skill outputs published individually (not just the synthesis), so future turns and monitor see per-skill findings.

### Section 6: Configuration & Guards

New settings in `PulseAgentSettings`:

| Setting | Type | Default | Purpose |
|---------|------|---------|---------|
| `PULSE_AGENT_MULTI_SKILL` | `bool` | `True` | Kill switch for multi-skill routing |
| `PULSE_AGENT_MULTI_SKILL_THRESHOLD` | `float` | `0.15` | Max score gap for activation |
| `PULSE_AGENT_MULTI_SKILL_MAX` | `int` | `2` | Max concurrent skills (cap 3) |
| `PULSE_AGENT_TEMPORAL_CACHE_TTL` | `int` | `60` | Seconds to cache temporal signals |

Guards:

- **Conflict-with check**: `skill.conflicts_with` lists secondary -> single-skill only.
- **Write tool exclusivity**: If both skills have `write_tools=True`, only primary gets write tools. Secondary runs read-only.
- **Token budget**: Each skill's system prompt capped at half the cache-control ephemeral block.
- **Circuit breaker**: If OPEN, multi-skill degrades to single-skill.
- **Rate guard**: If >50% of session turns trigger multi-skill, log warning (threshold likely too loose).

### Section 7: Frontend Contract

**Scope:** Backend spec only — this section defines the contract the frontend must implement, not the implementation itself.

**New `AgentEvent` union members** in `agentClient.ts`:
- `MultiSkillStartEvent: { type: "multi_skill_start", skills: string[] }`
- `SkillProgressEvent: { type: "skill_progress", skill: string, status: "running" | "complete" | "timeout" }`

**Extended `DoneEvent`** in `agentClient.ts`:
- Add optional `multi_skill?: { skills: string[], conflicts: Conflict[] }` field

**`agentStore.ts` changes:**
- New state: `activeSkills: string[] | null` — set on `multi_skill_start`, cleared on `done`
- `skill_progress` handler updates per-skill status for the ThinkingIndicator
- `done` handler checks `multi_skill` field — if present, stores conflicts for rendering

**`ThinkingIndicator.tsx`:**
- When `activeSkills` is non-null, show parallel progress: two skill tracks with individual status
- Add "Synthesizing..." phase between both skills completing and response streaming

**`MessageBubble.tsx`:**
- When `done.multi_skill.conflicts` is non-empty, render a `ConflictCard` component (not inline markdown)
- Each conflict shows both positions side-by-side with PromptPill buttons for arbitration
- Clicking a PromptPill sends a follow-up message like "Proceed with [skill_a]'s recommendation on [topic]"

**`ConflictCard` component** (new):
- Distinct visual treatment (bordered card, two-column layout)
- Topic header, skill A position, skill B position, action buttons

## Implementation Sequencing

Ship as two independent PRs:

**PR 1: Temporal channel wiring** (no protocol changes, no UI changes)
- `skill_selector.py`: `TemporalSignal`, `_score_temporal()` rework, caching
- `config.py`: `PULSE_AGENT_TEMPORAL_CACHE_TTL`
- Tests for temporal scoring

**PR 2: Parallel multi-skill execution** (depends on PR 1 being merged)
- Everything else: ORCA top-K, intent splitting, parallel engine, synthesis, WebSocket integration, config, guards, frontend contract
- Tests and eval scenarios

## Files to Create

| File | Purpose |
|------|---------|
| `sre_agent/synthesis.py` | Synthesis layer: merge parallel outputs, detect conflicts |

## Files to Modify

| File | Changes |
|------|---------|
| `sre_agent/skill_selector.py` | `SelectionResult.secondary_skill`, `_score_temporal()` rework, `TemporalSignal` dataclass |
| `sre_agent/skill_loader.py` | New `classify_query_multi()` function, tool budget splitting logic |
| `sre_agent/orchestrator.py` | `split_compound_intent()`, `classify_intent()` returns secondary, `build_orchestrated_config()` handles multi-skill |
| `sre_agent/context_bus.py` | `parallel_task_id` tag support, buffered publish mode for parallel execution |
| `sre_agent/plan_runtime.py` | `run_parallel_skills()`, `ParallelSkillResult` dataclass |
| `sre_agent/api/ws_endpoints.py` | Multi-skill routing branch, new event types, sticky mode reset |
| `sre_agent/config.py` | 4 new settings: multi_skill, threshold, max, temporal_cache_ttl |
| Frontend: `agentClient.ts` | New event types in `AgentEvent` union, extended `DoneEvent` |
| Frontend: `agentStore.ts` | `activeSkills` state, `skill_progress` handler, `multi_skill` in `done` handler |
| Frontend: `ThinkingIndicator.tsx` | Parallel progress mode with two skill tracks |
| Frontend: `MessageBubble.tsx` | `ConflictCard` rendering when conflicts present |

## Testing Strategy

- Unit tests for `split_compound_intent()` — conjunction splitting, edge cases
- Unit tests for `TemporalSignal` construction and `_score_temporal()` boosts
- Unit tests for `synthesize_parallel_outputs()` — merge, conflict detection, no-conflict fast path
- Integration test for `classify_query()` returning secondary skill at various thresholds
- Integration test for `run_parallel_skills()` — both complete, one timeout, conflict-with guard
- Eval scenarios: compound queries ("check crashes and scan CVEs") route to 2 skills
- Eval scenarios: single-domain queries still route to 1 skill (no false multi-activation)
