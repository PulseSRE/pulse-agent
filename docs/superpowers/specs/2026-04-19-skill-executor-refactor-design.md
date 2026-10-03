# SkillExecutor Refactor — Parallel Multi-Skill Execution v2

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


## Goal

Replace the bolted-on callback approach in `run_parallel_skills` with a `SkillExecutor` class that encapsulates the full WebSocket event pipeline. Both single-skill and parallel-skill execution use the same executor, eliminating the divergent code paths that cause missing features (no streaming, no usage recording, no components).

## Architecture

### Current Problem

`run_parallel_skills` calls `run_agent_streaming` directly, bypassing all WebSocket infrastructure in `_run_agent_ws`:

```
Single-skill path:  ws_endpoints → _run_agent_ws → run_agent_streaming
                                    ↳ callbacks: text, thinking, tool_use, component, confirm, usage, tool_result
                                    ↳ post-processing: view signals, memory, tool chain hints

Parallel path:      ws_endpoints → run_parallel_skills → run_agent_streaming (bare)
                                    ↳ bolted-on: on_tool_use only (no recording, no components, no confirm)
```

Missing from parallel path: text/thinking streaming (intentional), tool usage recording, component events, confirmation flow, memory augmentation, token tracking, view signal processing.

### Proposed Architecture

```
Single-skill:  ws_endpoints → SkillExecutor.run(skill_tag="") → run_agent_streaming
                               ↳ full streaming (text, thinking, tool_use, component, confirm)

Parallel:      ws_endpoints → asyncio.gather(
                   SkillExecutor.run(skill_tag="sre"),
                   SkillExecutor.run(skill_tag="security"),
               ) → synthesize → send merged response
                               ↳ tool_use → skill_progress events (live)
                               ↳ text/thinking → suppressed (synthesis replaces)
                               ↳ components → buffered (forwarded after synthesis)
                               ↳ confirm → primary only (secondary rejects)
                               ↳ tool_result → recorded to DB per-skill
                               ↳ usage → tracked per-skill
```

## Components

### 1. `SkillExecutor` (new class in `agent_ws.py`)

```python
@dataclass
class SkillOutput:
    text: str
    tools_called: list[str]
    components: list[dict]
    token_usage: dict[str, int]

class SkillExecutor:
    def __init__(self, websocket: WebSocket, session_id: str, loop: asyncio.AbstractEventLoop):
        self.websocket = websocket
        self.session_id = session_id
        self.loop = loop

    async def run(
        self,
        config: dict,
        messages: list[dict],
        client,
        write_tools: set[str],
        mode: str,
        *,
        skill_tag: str = "",
        current_user: str = "anonymous",
    ) -> SkillOutput:
```

**Responsibilities:**
- Creates thread-safe callbacks (Lock-protected mutable state)
- Augments prompt with memory (per-skill, not shared)
- Runs `run_agent_streaming` in a thread
- Returns structured `SkillOutput` with text, tools, components, tokens

**Behavior when `skill_tag` is set (parallel mode):**
- `on_text` / `on_thinking` → suppressed (no-op)
- `on_tool_use` → sends `{"type": "skill_progress", "skill": tag, "status": "tool_use", "tool": name}`
- `on_component` → buffers in Lock-protected list (not sent to WebSocket)
- `on_confirm` → rejects with `False` + logs warning (secondary must be read-only; primary uses confirm normally)
- `on_tool_result` → records to `tool_usage` table with `mode=skill_tag`
- `on_usage` → accumulates in per-instance `token_usage` dict

**Behavior when `skill_tag` is empty (single-skill, existing behavior):**
- All callbacks forward to WebSocket as-is
- Zero behavior change from current `_run_agent_ws`

**Thread safety:**
- `tools_called: list[str]` — protected by `threading.Lock`
- `components: list[dict]` — protected by same Lock
- `token_usage: dict[str, int]` — protected by same Lock
- `loop.call_soon_threadsafe` for all WebSocket sends (existing pattern)

### 2. Refactored `_run_agent_ws` (in `agent_ws.py`)

Shrinks from ~180 lines to ~60. Becomes:

1. Create client
2. Create `SkillExecutor`, call `executor.run()`
3. Post-process: extract view signals from `output.components`, record memory, emit done event

View signal extraction changes: reads from `SkillOutput.components` instead of scanning `messages` list. This fixes the parallel case where tool results aren't in `messages`.

### 3. Refactored `run_parallel_skills` (in `plan_runtime.py`)

Simplified to:

1. Create client (lazy)
2. Build configs for both skills
3. Strip write tools from secondary
4. Create `SkillExecutor`
5. Run both skills with **progressive result streaming** (see below)
6. Return `ParallelSkillResult` with outputs, tokens, confidence from ORCA contextvar

The `on_progress` callback parameter is **removed**. All event forwarding happens inside the executor.

New parameters required: `websocket` and `session_id` (passed from `ws_endpoints.py`).

**Progressive result streaming:**

Replace `asyncio.gather` with `asyncio.wait(return_when=FIRST_COMPLETED)`:

```python
tasks = {
    asyncio.create_task(executor.run(..., skill_tag="sre")): "sre",
    asyncio.create_task(executor.run(..., skill_tag="security")): "security",
}
done, pending = await asyncio.wait(tasks, return_when=FIRST_COMPLETED, timeout=120)
```

When the first skill completes:
1. Send its output immediately as `text_delta` with a skill attribution header (`## SRE Analysis`)
2. Send `skill_progress` with `status: "complete"` for that skill
3. If the second skill is still running, send a note: `*Security analysis still running...*`
4. Wait for the second skill (remaining timeout)

When both skills complete:
- If both produced output, run synthesis to merge and detect conflicts
- Send the synthesized response (replaces the early partial output via a new `text_delta`)
- If only one produced output, use it directly (empty output guard)

This transforms the experience from "90 seconds of nothing" to "SRE results in 40s, full picture in 90s."

### 4. Tool completion events

The executor's `on_tool_result` callback (already wired for recording) also emits a `skill_progress` event with `status: "tool_complete"` and `duration_ms`:

```json
{"type": "skill_progress", "skill": "sre", "status": "tool_complete", "tool": "get_pod_logs", "duration_ms": 2300}
```

This lets the frontend transition tool pills from amber (active) to green (complete) with timing info. The `on_tool_result` callback from `_build_tool_result_handler` already has duration data — just forward it.

### 5. Synthesis streaming

Change `synthesize_parallel_outputs` in `synthesis.py` from batch to streaming:

```python
# Before: single API call, returns complete text
response = await asyncio.to_thread(client.messages.create, ...)

# After: streaming API call, yields deltas
stream = await asyncio.to_thread(client.messages.stream, ...)
```

The caller in `ws_endpoints.py` forwards `text_delta` events during synthesis so the merged response writes progressively instead of appearing all at once. A `skill_progress` event with `skill: "synthesis"` and `status: "running"` marks the transition.

### 6. Updated `ws_endpoints.py` caller

```python
parallel_result = await run_parallel_skills(
    primary=skill,
    secondary=secondary_skill,
    query=content,
    messages=messages,
    client=None,
    websocket=websocket,      # NEW
    session_id=session_id,    # NEW
)
```

After parallel execution:
- Check empty output guard (unchanged)
- Forward buffered components from both skills
- Run synthesis with streaming
- Extract view signals from merged components
- Emit done event with multi_skill metadata, combined token usage

### 7. Frontend updates

**`agentStore.ts`:**
- `skill_progress` handler:
  - `status === "tool_use"` → append `"skill:tool"` to `activeTools`
  - `status === "tool_complete"` → move tool from active to completed (amber → green)
  - `status === "complete"` → mark skill as done in a new `skillStatuses` map
  - `skill === "synthesis"` → update phase to "generating"

**`ThinkingIndicator.tsx`:**
- Tool pills transition: amber (active) → green (complete) with duration badge
- Per-skill completion checkmarks on skill badges (sre ✓, security ...)
- "Synthesizing..." sub-phase after both skills complete
- Early result text shown below the indicator while second skill runs

**`MessageBubble.tsx`:**
- Conflict cards: add `role="alert"` and `aria-label` (already done)
- No other changes needed

## Data Flow

```
User query
  ↓
classify_query_multi → (primary, secondary)
  ↓
multi_skill_start event → UI shows parallel phase
  ↓
SkillExecutor.run(primary, skill_tag="sre")     ─┐
SkillExecutor.run(secondary, skill_tag="security")─┤ asyncio.wait(FIRST_COMPLETED)
  │                                                 │
  │ skill_progress: tool_use ──────→ UI tool pills (amber)
  │ skill_progress: tool_complete ──────→ UI tool pills (green)
  │ tool_result recording ──────→ tool_usage DB
  │ token accumulation ──────→ SkillOutput.token_usage
  ↓                                                 │
First skill completes                               │
  ↓                                                 │
text_delta: "## SRE Analysis\n..." ──────→ UI shows early result
skill_progress: complete ──────→ UI: sre ✓          │
  ↓                                                 ↓
Second skill completes
  ↓
skill_progress: complete ──────→ UI: security ✓
  ↓
Empty output guard → skip synthesis if one/both empty
  ↓
skill_progress: synthesis running ──────→ UI: "Synthesizing..."
  ↓
synthesize_parallel_outputs (streaming) → text_delta events
  ↓
Forward buffered components from both skills
  ↓
done event (multi_skill metadata, per-skill tokens, conflicts)
```

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Text/thinking during parallel | Suppressed | Synthesis replaces both; showing them confuses users |
| Early result streaming | First skill streams immediately | Transforms 90s wait into 40s partial + 90s complete |
| Confirmation flow | Primary only, with WebSocket confirm | Secondary is read-only; primary can take action with user approval |
| Memory augmentation | Per-skill | Different skills retrieve different relevant context |
| Thread safety | Lock-protected lists | Simpler than Queue, sufficient for append-only pattern |
| Abstraction | SkillExecutor class | Encapsulates callbacks + execution + results; safer than bare dict |
| View signals | From SkillOutput.components | Can't extract from messages (parallel results not appended) |
| Token reporting | Per-skill in SkillOutput | Caller decides aggregation strategy |
| Tool completion events | Via on_tool_result callback | Duration already available; forward as skill_progress |
| Synthesis | Streaming via messages.stream | Progressive display instead of all-at-once |
| Timeout | 120s overall via asyncio.wait | Prevents indefinite blocking |

## Files Changed

| File | Change |
|------|--------|
| `sre_agent/api/agent_ws.py` | Add `SkillExecutor` class + `SkillOutput` dataclass. Refactor `_run_agent_ws` to use executor. |
| `sre_agent/plan_runtime.py` | Simplify `run_parallel_skills` to use `SkillExecutor`. Replace `asyncio.gather` with `asyncio.wait(FIRST_COMPLETED)`. Add `websocket` + `session_id` params. Remove `on_progress`. |
| `sre_agent/api/ws_endpoints.py` | Pass `websocket` + `session_id` to `run_parallel_skills`. Handle early result streaming. Forward buffered components after synthesis. Remove `_on_skill_progress` callback. |
| `sre_agent/synthesis.py` | Switch `synthesize_parallel_outputs` from `client.messages.create` to `client.messages.stream` for progressive output. Add `on_text` callback parameter. |
| `tests/test_multi_skill.py` | Update `run_parallel_skills` call signatures. Add `SkillExecutor` unit tests. |
| `tests/test_api_websocket.py` | Update integration tests for new call signature. Test early result streaming. |
| `OpenshiftPulse: agentStore.ts` | Handle `skill_progress` with `tool_complete` and `complete` statuses. Add `skillStatuses` tracking. |
| `OpenshiftPulse: ThinkingIndicator.tsx` | Tool pills amber → green transition. Per-skill completion checkmarks. Early result text display. |
| `API_CONTRACT.md` | Document `tool_complete` status and `duration_ms` field on `skill_progress`. |

## What This Does NOT Change

- `run_agent_streaming` in `agent.py` — unchanged, still the core agent loop
- `skill_selector.py` / `skill_loader.py` — unchanged, routing logic stays
- `context_bus.py` — unchanged, buffering still works
- Single-skill execution path — functionally identical after refactor

## Testing

- Existing 1780 tests must pass (refactor, not new feature)
- New unit tests for `SkillExecutor`:
  - `test_executor_single_skill_streams_all_events`
  - `test_executor_parallel_suppresses_text`
  - `test_executor_parallel_buffers_components`
  - `test_executor_confirm_rejected_for_secondary`
  - `test_executor_thread_safe_tool_list`
  - `test_executor_per_skill_memory_augmentation`
  - `test_executor_tool_complete_events`
- Integration tests:
  - `test_early_result_streaming` — first skill output appears before second completes
  - `test_synthesis_streams_progressively` — merged response arrives as text_delta events
  - Update existing multi-skill tests for new call signature
- Eval prompts: existing 12 multi-skill prompts cover routing; executor is transparent to evals
