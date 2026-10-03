# Async Anthropic SDK Migration

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


**Date:** 2026-04-23
**Status:** Design approved
**Approach:** Big bang — all call sites converted in one pass

## Problem

The Pulse agent uses sync Anthropic SDK clients (`Anthropic`, `AnthropicVertex`) dispatched to threads via `asyncio.to_thread()` from FastAPI async handlers. GIL contention during LLM streaming causes intermittent `/healthz` probe timeouts (26 readiness failures in 10 hours with 3s timeout). The event loop is blocked when threads hold the GIL during CPU-bound work (JSON parsing, prompt construction).

A timeout bump from 3s to 5s was deployed as a quick fix. This migration eliminates the root cause.

## Solution

Replace all sync Anthropic SDK usage with native async clients (`AsyncAnthropic`, `AsyncAnthropicVertex`). LLM streaming runs directly on the event loop — no threads, no GIL contention, `/healthz` never blocked.

## Scope

16 call sites across 13 production files. ~165 test mocks need async rewrites.

---

## 1. Client Layer

**`agent.py`** — `create_client()` renamed to `create_async_client()`:

```python
def create_async_client():
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")
    if project and region:
        return anthropic.AsyncAnthropicVertex(region=region, project_id=project)
    return anthropic.AsyncAnthropic()
```

Old `create_client()` deleted. No compatibility shim.

**Client lifecycle change:** `client.close()` → `await client.aclose()` at all 11 sites:
- `agent_ws.py` (SkillExecutor cleanup)
- `investigations.py` (2 sites — proactive and security)
- `cluster_monitor.py` (cancel_pending_investigations)
- `skill_router.py` (_llm_classify)
- `tool_predictor.py` (llm_pick_tools)
- `inbox.py` — **excluded, keeps sync client (see section 7)**
- `plan_runtime.py` (2 sites — phase execution, parallel skills)

## 2. Agent Loop

**`agent.py:run_agent_streaming`** becomes `async def`.

### LLM streaming

```python
# Before
stream_ctx = client.messages.stream(model=..., ...)
with stream_ctx as stream:
    for event in stream:
        ...
    response = stream.get_final_message()

# After
stream_ctx = client.messages.stream(model=..., ...)
async with stream_ctx as stream:
    async for event in stream:
        ...
    response = await stream.get_final_message()
```

### Retry delays

`time.sleep(delay)` at lines 527 and 539 → `await asyncio.sleep(delay)`.

### Callbacks

All 7 callbacks become `async def`. The loop `await`s each:

```python
# Before
if on_text:
    on_text(event.delta.text)

# After
if on_text:
    await on_text(event.delta.text)
```

Callback signatures:
| Callback | Sync signature | Async signature |
|----------|---------------|-----------------|
| `on_text` | `(str) -> None` | `(str) -> Awaitable[None]` |
| `on_thinking` | `(str) -> None` | `(str) -> Awaitable[None]` |
| `on_tool_use` | `(str) -> None` | `(str) -> Awaitable[None]` |
| `on_confirm` | `(str, dict) -> bool` | `(str, dict) -> Awaitable[bool]` |
| `on_component` | `(str, dict) -> None` | `(str, dict) -> Awaitable[None]` |
| `on_tool_result` | `(dict) -> None` | `(dict) -> Awaitable[None]` |
| `on_usage` | `(**kwargs) -> None` | `(**kwargs) -> Awaitable[None]` |

### CircuitBreaker

Drop `threading.Lock` (line 122). Single-threaded event loop — all callers (`allow_request`, `record_success`, `record_failure`) are in `run_agent_streaming` which runs on the event loop. No thread pool worker ever touches the circuit breaker.

### Tool execution

Tools stay sync — the kubernetes Python client has no async API. Keep the dedicated `ThreadPoolExecutor(max_workers=4)`:

```python
# Before
futures = {_tool_pool.submit(_execute_tool, b.name, b.input, tool_map): b for b in read_blocks}
for future in concurrent.futures.as_completed(futures, timeout=timeout):
    text, component, exec_meta = future.result(timeout=timeout)

# After
loop = asyncio.get_running_loop()
task_to_block = {}
for b in read_blocks:
    task = asyncio.ensure_future(loop.run_in_executor(_tool_pool, _execute_tool, b.name, b.input, tool_map))
    task_to_block[task] = b

done = set()
try:
    async with asyncio.timeout(TOOL_TIMEOUT):
        for coro in asyncio.as_completed(task_to_block.keys()):
            text, component, exec_meta = await coro
            # find block via task_to_block lookup after coro resolves
            ...
except TimeoutError:
    # handle timed-out tools same as current code
    ...
```

Note: `asyncio.as_completed` has no `timeout` parameter (unlike `concurrent.futures.as_completed`). Use `async with asyncio.timeout(TOOL_TIMEOUT)` wrapping instead.

Write tools stay sequential:
```python
text, component, exec_meta = await loop.run_in_executor(
    _tool_pool, _execute_tool_with_timeout, block.name, block.input, tool_map
)
```

### Wrapper functions

`run_agent_turn_streaming` and `run_security_scan_streaming` (security_agent.py) both become `async def` — they are thin wrappers that delegate to `run_agent_streaming`.

## 3. WebSocket Handler (`agent_ws.py`)

### Dispatch

```python
# Before
full_response = await asyncio.to_thread(run_agent_streaming, client=client, ...)

# After
full_response = await run_agent_streaming(client=client, ...)
```

### Callbacks — native async

All `_schedule_send` / `run_coroutine_threadsafe` bridging deleted:

```python
# Before
def _schedule_send(data):
    asyncio.run_coroutine_threadsafe(_safe_send(data), self.loop)
def on_text(delta):
    _schedule_send({"type": "text_delta", "text": delta})

# After
async def on_text(delta):
    try:
        await self.websocket.send_json({"type": "text_delta", "text": delta})
    except Exception:
        pass
```

### Deleted

- `self.loop` parameter and references
- `_schedule_send()` helper
- `threading.Lock` for `tools_called` / `components` lists
- `concurrent.futures.Future` waiter pattern

`SkillExecutor.__init__` `loop` parameter removed. Callers updated: `agent_ws.py` and `plan_runtime.py:770-771`.

## 4. Confirmation Gate

Current: 25-line threading bridge. Proposed: ~8 lines:

```python
async def on_confirm(tool_name: str, tool_input: dict) -> bool:
    if skill_tag and not write_tools:
        logger.warning("Secondary skill '%s' attempted confirmation — denied", skill_tag)
        return False
    try:
        if not _ws_alive.get(self.session_id, True):
            return False
        confirm_future = await _create_and_register_future(
            self.session_id, tool_name, tool_input, self.websocket
        )
        return await asyncio.wait_for(confirm_future, timeout=120)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        return False
    finally:
        _pending_confirms.pop(self.session_id, None)
```

**CancelledError handling**: When the WebSocket disconnects, `ws_endpoints.py` cleanup cancels the pending Future. `CancelledError` propagates to the `await asyncio.wait_for(...)`, caught by the except clause, returns `False` (denied). Agent loop continues cleanly.

**Nonce validation**: Unchanged — stays in the receive loop. The nonce is generated in `_create_and_register_future` (runs on event loop before any `await` yields), so the receive loop is always ready to validate a fast `confirm_response` (including auto-approve at trust level 2+).

**What stays the same**: WebSocket message types (`confirm_request`, `confirm_response`), nonce round-trip, `_pending_nonces` / `_pending_confirms` / `_pending_timestamps` dicts, JIT nonce generation, TTL cleanup.

## 5. Monitor Investigations (`monitor/investigations.py`)

`_run_proactive_investigation_sync` → `async def _run_proactive_investigation`
`_run_security_followup_sync` → `async def _run_security_followup`

Both drop the `_sync` suffix. Internal changes:
- `run_agent_streaming(...)` → `await run_agent_streaming(...)`
- `client.close()` → `await client.aclose()`

### Callers in `cluster_monitor.py`

```python
# Before
await asyncio.wait_for(asyncio.to_thread(_run_proactive_investigation_sync, finding, client=self._client), timeout=t)

# After
await asyncio.wait_for(_run_proactive_investigation(finding, client=self._client), timeout=t)
```

4 sites: lines 800, 842, 1409, 1429.

Plan-based investigations (`asyncio.create_task(self._try_plan_execution(...))`) — already async, no change.

## 6. Synthesis (`synthesis.py`)

```python
# Before
_loop = asyncio.get_running_loop()
def _stream_synthesis():
    with client.messages.stream(...) as stream:
        for text in stream.text_stream:
            asyncio.run_coroutine_threadsafe(on_text_delta(text), _loop)
    return "".join(collected)
raw_text = await asyncio.to_thread(_stream_synthesis)

# After
collected = []
async with client.messages.stream(...) as stream:
    async for text in stream.text_stream:
        collected.append(text)
        if on_text_delta:
            await on_text_delta(text)
raw_text = "".join(collected)
```

Non-streaming fallback: `await asyncio.to_thread(client.messages.create, ...)` → `await client.messages.create(...)`.

SDK docs confirm `async for text in stream.text_stream` works natively with `AsyncAnthropic`.

## 7. Inbox Processing — Keeps Sync Client

**Key decision**: Inbox LLM functions (`_phase_a_triage`, `_phase_b_investigate`, `_phase_c_plan`, `_generate_smart_layout`) keep their own dedicated **sync** `anthropic.Anthropic()` / `anthropic.AnthropicVertex()` client. They do NOT use `create_async_client()`.

**Why**: `run_generator_cycle` is dispatched via `asyncio.to_thread(run_generator_cycle)` from `cluster_monitor.py:1316`. You cannot `await` async functions from inside a thread pool worker. The inbox generators also call sync K8s APIs. Forcing the entire generator pipeline async would require restructuring for no benefit — it already runs in a thread and doesn't block the event loop.

**Additionally**: `inbox.py:992` calls `_run_proactive_investigation_sync` directly (not through `asyncio.to_thread`). This call needs a sync investigation path. Solution: keep a small sync wrapper:

```python
def _run_investigation_sync(finding, **kwargs):
    """Sync wrapper for inbox's direct investigation calls."""
    client = _create_sync_client()
    try:
        return run_agent_streaming_sync(client=client, ...)
    finally:
        client.close()
```

Where `run_agent_streaming_sync` is a thin wrapper: `asyncio.run(run_agent_streaming(...))`. This is safe because inbox runs inside `asyncio.to_thread` (a separate thread with no running event loop), so `asyncio.run()` can create a new loop. This is the one place we need a sync shim.

**Files affected**: `inbox.py` keeps using `anthropic.Anthropic()` directly in its 3 LLM functions. Add a private `_create_sync_client()` helper in `inbox.py` to avoid importing the deleted `create_client`.

## 8. Non-Streaming Call Sites

4 remaining sites (inbox excluded per section 7):

### `skill_router.py` — `_llm_classify()`
- Becomes `async def`
- `client.messages.create(...)` → `await client.messages.create(...)`
- `client.close()` → `await client.aclose()`
- **Ripple chain**: `_llm_classify` → `classify_query` → `classify_query_multi` → `ws_endpoints.py` call site. Each function in the chain becomes `async def`.

### `tool_predictor.py` — `llm_pick_tools()`
- Becomes `async def`
- **Ripple chain**: `llm_pick_tools` → `select_tools_adaptive` → called from `run_agent_streaming` (already async). Clean.

### `evals/judge.py` — `judge_response()`
- Becomes `async def`
- Callers in eval CLI need `await` or `asyncio.run()`

### Plan runtime (`plan_runtime.py`)
- Phase execution and parallel skill execution: drop `asyncio.to_thread(run_agent_streaming, ...)` → `await run_agent_streaming(...)`
- `client.close()` → `await client.aclose()` at 2 sites

## 9. CLI (`main.py`)

`run_repl()` → `async def run_repl()`. Entry point: `asyncio.run(run_repl(mode))`.

```python
# Before
def run_repl(mode):
    client = create_client()
    while True:
        user_input = console.input(...)
        full_response = cfg["runner"](client, messages, ...)

# After
async def run_repl(mode):
    client = create_async_client()
    while True:
        user_input = console.input(...)  # sync blocking, fine for CLI
        full_response = await cfg["runner"](client, messages, ...)
```

Callbacks become `async def` (just `console.print`, no real async benefit but matches signature contract):

```python
async def on_text(delta):
    text_parts.append(delta)
    console.print(delta, end="", highlight=False)

async def _confirm_action(tool_name, tool_input):
    console.print(Panel(...))
    answer = console.input("Proceed? (y/N): ").strip().lower()
    return answer in ("y", "yes")
```

`console.input()` blocks the event loop — acceptable for single-user CLI with no concurrent tasks.

## 10. Eval Replay Harness

`evals/replay.py:117` calls `run_agent_streaming()` synchronously. After migration, wrap in `asyncio.run()`:

```python
# Before
result = run_agent_streaming(client, messages, ...)

# After
result = asyncio.run(run_agent_streaming(client, messages, ...))
```

`evals/replay_cli.py:158` — `_setup_model()` calls `create_client()` → `create_async_client()`. Replay harness is in the release gate (`make test-everything`), so this must work.

## 11. What Stays Sync

| Component | Why | Dispatch pattern |
|-----------|-----|-----------------|
| Tool execution (`_execute_tool`) | K8s client is sync | `loop.run_in_executor(_tool_pool, ...)` |
| K8s scanners (22 scanners) | K8s client is sync | `asyncio.to_thread(scanner)` (unchanged) |
| Inbox LLM functions (3) | Called from `asyncio.to_thread(run_generator_cycle)` | Own sync `Anthropic()` client |
| Inbox investigation call | Direct sync call from `_phase_b_investigate` | `run_agent_streaming_sync` wrapper |
| Memory augmentation | DB + string ops | Called inline (fast enough) |
| `build_tool_result_handler` | Fire-and-forget DB writes | Called from async callbacks |
| MCP client connection (`mcp_client.py`) | Sync startup | `time.sleep` in retry is fine |
| Validation helpers | Pure CPU, trivial | Inline |

## 12. What Gets Deleted

1. `create_client()` — replaced by `create_async_client()`
2. `_schedule_send()` / `run_coroutine_threadsafe` bridging in `agent_ws.py`
3. `self.loop` references in `SkillExecutor`
4. `threading.Lock` for `tools_called` / `components` lists in `agent_ws.py`
5. `concurrent.futures.Future` waiter in confirmation gate
6. `_sync` suffix on investigation function names
7. `CircuitBreaker._lock` (threading.Lock)

## 13. Cleanup (opportunistic)

- `asyncio.get_event_loop()` → `asyncio.get_running_loop()` at `cluster_monitor.py:1203`

## 14. Pre-Implementation Verification

Before writing any production code:

1. **SDK spike**: 20-line script that creates `AsyncAnthropicVertex(region=..., project_id=...)`, calls `async with client.messages.stream(...)`, iterates with `async for event in stream`, calls `await stream.get_final_message()`, and `await client.aclose()`. Run against the actual Vertex AI project.

2. **Exception types**: Verify `AsyncAnthropic` raises the same `anthropic.APIStatusError` and `anthropic.APIConnectionError` as the sync client. Error classification in `ws_endpoints.py` depends on this.

3. **Concurrent client usage**: Verify `httpx.AsyncClient` (used internally by `AsyncAnthropic`) supports concurrent `messages.stream()` calls on the same client instance. The monitor holds one `self._client` shared across up to 3 concurrent investigations.

## 15. Testing Strategy

### Mock migration

All test mocks that use sync context managers must switch to async:

```python
# Before
stream.__enter__ = MagicMock(return_value=stream)
stream.__exit__ = MagicMock(return_value=False)

# After
stream.__aenter__ = AsyncMock(return_value=stream)
stream.__aexit__ = AsyncMock(return_value=False)
```

Estimated breakage: ~165 tests. Need `pytest-asyncio` and `AsyncMock` fixtures.

### New tests to add

1. **Confirmation cancellation on disconnect** — connect, trigger write tool, close WebSocket before responding, verify agent returns denial and `_active_agent_count` decrements.

2. **Nonce round-trip** — send message triggering write tool, verify `confirm_request` includes nonce, send `confirm_response` with correct nonce → tool executes. Wrong nonce → denial.

3. **Parallel tool execution under async** — verify `asyncio.timeout` + `run_in_executor` maintains timeout semantics equivalent to the old `concurrent.futures.as_completed` pattern.

### Cluster validation

Deploy to cluster, run 3 concurrent agent sessions + 1 monitor scan, verify `/healthz` responds in <1s consistently over 24 hours. Keep probe timeout at 5s for 2+ weeks post-migration.

## 16. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Missed call site → runtime crash | High | Full grep for `create_client`, `client.messages`, `run_agent_streaming` before merging |
| Confirmation gate regression | High | Dedicated test for cancel/timeout/nonce |
| Test suite breakage (~165 tests) | Medium | Shared async mock fixtures, batch update |
| Inbox sync/async boundary | Medium | Dedicated sync client, sync investigation wrapper |
| Tool execution latency regression | Low | Keep dedicated ThreadPoolExecutor, `run_in_executor` |
| Eval replay harness | Medium | `asyncio.run()` wrapper |

## Files Changed

| File | Change |
|------|--------|
| `agent.py` | `run_agent_streaming` → async, `create_async_client`, drop CB lock, async tool dispatch, async callbacks, `asyncio.sleep` |
| `api/agent_ws.py` | Native async callbacks, delete bridging, simplify confirmation gate, remove `loop` param |
| `api/ws_endpoints.py` | Drop `asyncio.to_thread` dispatch, async ripple from skill_router |
| `security_agent.py` | `run_security_scan_streaming` → async |
| `monitor/investigations.py` | Both investigation functions → async, `aclose` |
| `monitor/cluster_monitor.py` | Drop `asyncio.to_thread` on investigations, `get_running_loop` cleanup |
| `synthesis.py` | Native async streaming, delete thread bridge |
| `plan_runtime.py` | Drop `asyncio.to_thread`, remove `loop` param from SkillExecutor calls, `aclose` |
| `skill_router.py` | `_llm_classify` → async, ripple to `classify_query` / `classify_query_multi` |
| `tool_predictor.py` | `llm_pick_tools` → async, ripple to `select_tools_adaptive` |
| `inbox.py` | Keep sync client for LLM functions, add `_create_sync_client` helper, sync investigation wrapper |
| `main.py` | `run_repl` → async, `asyncio.run()`, async callbacks |
| `evals/judge.py` | `judge_response` → async |
| `evals/replay.py` | `asyncio.run()` wrapper for `run_agent_streaming` |
| `evals/replay_cli.py` | `create_async_client` |
| ~10 test files | Async mocks, `pytest-asyncio`, ~165 tests |
