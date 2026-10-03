# Async Anthropic SDK Migration — Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all sync Anthropic SDK usage with native async clients to eliminate GIL contention that causes healthz probe timeouts on the cluster.

**Architecture:** Convert `run_agent_streaming` from sync to `async def`, replace `Anthropic`/`AnthropicVertex` with `AsyncAnthropic`/`AsyncAnthropicVertex`, convert all 7 callbacks to async, keep tool execution sync in a `ThreadPoolExecutor` via `loop.run_in_executor()`. Inbox processing keeps a dedicated sync client since it runs inside `asyncio.to_thread`.

**Tech Stack:** Python 3.11+, anthropic SDK (AsyncAnthropic, AsyncAnthropicVertex), asyncio, pytest-asyncio, FastAPI

**Spec:** `docs/superpowers/specs/2026-04-23-async-anthropic-migration-design.md`

---

## File Map

| File | Responsibility | Change type |
|------|---------------|-------------|
| `sre_agent/agent.py` | Core agent loop, client factory, circuit breaker, tool execution | Heavy modify |
| `sre_agent/api/agent_ws.py` | WebSocket callback bridging, confirmation gate, SkillExecutor | Heavy modify |
| `sre_agent/api/ws_endpoints.py` | WebSocket endpoint handlers, classify_query dispatch | Light modify |
| `sre_agent/security_agent.py` | Security agent wrapper | Light modify |
| `sre_agent/monitor/investigations.py` | Proactive + security investigation functions | Moderate modify |
| `sre_agent/monitor/cluster_monitor.py` | Investigation dispatch, generator dispatch | Light modify |
| `sre_agent/synthesis.py` | Parallel skill output merging | Moderate modify |
| `sre_agent/plan_runtime.py` | Phased plan + parallel skill execution | Moderate modify |
| `sre_agent/skill_router.py` | LLM classify + query routing chain | Moderate modify |
| `sre_agent/tool_predictor.py` | LLM tool selection + adaptive selection | Moderate modify |
| `sre_agent/inbox.py` | Sync client helper, investigation wrapper | Light modify |
| `sre_agent/main.py` | CLI REPL | Moderate modify |
| `sre_agent/evals/judge.py` | LLM judge | Light modify |
| `sre_agent/evals/replay.py` | Eval replay harness | Light modify |
| `sre_agent/evals/replay_cli.py` | Replay CLI setup | Light modify |
| `sre_agent/skill_loader.py` | Re-exports classify_query (no code change, verify) | Verify only |
| `tests/test_agent.py` | 21 tests — async mocks | Modify |
| `tests/test_api_websocket.py` | 16 tests — async client mocks | Modify |
| `tests/test_eval_replay.py` | 19 tests — async mocks | Modify |
| `tests/test_evals_judge.py` | 10 tests — async mocks | Modify |
| `tests/test_main.py` | 25 tests — async mocks | Modify |
| `tests/test_monitor.py` | 64 tests — async client mocks | Modify |
| `tests/test_multi_skill.py` | 25 tests — async client mocks | Modify |
| `tests/test_investigation_view_plan.py` | 5 tests — async client mocks | Modify |
| `tests/test_tool_predictor.py` | 27 tests — async mocks | Modify |

---

## Task 1: SDK Spike — Verify AsyncAnthropicVertex Works

**Files:**
- Create: `scripts/async_sdk_spike.py` (temporary, delete after verification)

- [ ] **Step 1: Write spike script**

```python
"""Verify AsyncAnthropic and AsyncAnthropicVertex work as expected."""
import asyncio
import os
import anthropic


async def main():
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")

    if project and region:
        client = anthropic.AsyncAnthropicVertex(region=region, project_id=project)
        print(f"Using AsyncAnthropicVertex (project={project}, region={region})")
    else:
        client = anthropic.AsyncAnthropic()
        print("Using AsyncAnthropic (direct API)")

    # 1. Verify streaming works
    print("\n--- Streaming test ---")
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=50,
        messages=[{"role": "user", "content": "Say hello in 5 words."}],
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                print(event.delta.text, end="", flush=True)
        response = await stream.get_final_message()
    print(f"\nStop reason: {response.stop_reason}")
    print(f"Usage: {response.usage.input_tokens} in, {response.usage.output_tokens} out")

    # 2. Verify text_stream lens works
    print("\n--- text_stream test ---")
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=50,
        messages=[{"role": "user", "content": "Count to 3."}],
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)
    print()

    # 3. Verify non-streaming create works
    print("\n--- Non-streaming test ---")
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'ok'."}],
    )
    print(f"Response: {response.content[0].text}")

    # 4. Verify exception types match sync client
    print("\n--- Exception type test ---")
    print(f"APIStatusError: {anthropic.APIStatusError}")
    print(f"APIConnectionError: {anthropic.APIConnectionError}")

    # 5. Verify aclose exists
    await client.aclose()
    print("\nclient.aclose() succeeded")

    # 6. Verify concurrent usage (same client, two streams)
    print("\n--- Concurrent streams test ---")
    client2 = anthropic.AsyncAnthropicVertex(region=region, project_id=project) if project else anthropic.AsyncAnthropic()

    async def stream_one(label):
        async with client2.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=20,
            messages=[{"role": "user", "content": f"Say '{label}'."}],
        ) as s:
            async for text in s.text_stream:
                pass
        return label

    r1, r2 = await asyncio.gather(stream_one("alpha"), stream_one("beta"))
    print(f"Concurrent results: {r1}, {r2}")
    await client2.aclose()

    print("\nAll checks passed.")


asyncio.run(main())
```

- [ ] **Step 2: Run the spike**

Run: `python scripts/async_sdk_spike.py`

Expected: All 6 checks pass. If `AsyncAnthropicVertex` is not available or `aclose()` doesn't exist, STOP — the SDK version needs upgrading before proceeding.

- [ ] **Step 3: Delete the spike and commit**

```bash
rm scripts/async_sdk_spike.py
```

No commit needed — this is throwaway verification.

---

## Task 2: Client Layer + CircuitBreaker

**Files:**
- Modify: `sre_agent/agent.py:103-165` (CircuitBreaker), `sre_agent/agent.py:246-258` (create_client)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write test for `create_async_client`**

Add to `tests/test_agent.py`:

```python
class TestCreateAsyncClient:
    @patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "test-proj", "CLOUD_ML_REGION": "us-east5"})
    def test_returns_async_vertex_when_configured(self):
        from sre_agent.agent import create_async_client
        client = create_async_client()
        assert isinstance(client, anthropic.AsyncAnthropicVertex)

    @patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "", "CLOUD_ML_REGION": ""})
    def test_returns_async_anthropic_when_no_vertex(self):
        from sre_agent.agent import create_async_client
        client = create_async_client()
        assert isinstance(client, anthropic.AsyncAnthropic)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent.py::TestCreateAsyncClient -v`

Expected: FAIL — `create_async_client` does not exist yet.

- [ ] **Step 3: Implement `create_async_client` and update CircuitBreaker**

In `sre_agent/agent.py`, replace `create_client`:

```python
def create_async_client():
    """Create an async Anthropic client.

    Uses Vertex AI if GCP project is configured,
    otherwise falls back to direct Anthropic API.
    """
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")

    if project and region:
        return anthropic.AsyncAnthropicVertex(region=region, project_id=project)

    return anthropic.AsyncAnthropic()
```

Remove `import threading` from the top of the file (if no other usage).

In `CircuitBreaker.__init__`, delete the lock:

```python
def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 60):
    self.failure_threshold = failure_threshold
    self.recovery_timeout = recovery_timeout
    self.state = self.CLOSED
    self.failure_count = 0
    self.last_failure_time: float = 0
```

In `allow_request`, `record_success`, `record_failure` — remove `with self._lock:` wrapping. Keep the body logic identical, just un-indent one level.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent.py::TestCreateAsyncClient -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/agent.py tests/test_agent.py
git commit -m "feat: add create_async_client, remove CircuitBreaker threading lock"
```

---

## Task 3: Core Agent Loop — `run_agent_streaming` async conversion

**Files:**
- Modify: `sre_agent/agent.py:390-700`
- Test: `tests/test_agent.py` (update existing mocks)

This is the largest single change. Convert `run_agent_streaming` to `async def`, change streaming to `async with`/`async for`, convert tool dispatch to `run_in_executor`, convert callbacks to async.

- [ ] **Step 1: Update the shared mock helper in test_agent.py**

Replace the `_make_stream_context` helper in `TestConfirmationGate` (and any other test class that uses it) with an async-compatible version:

```python
def _make_stream_context(self, responses):
    """Build a mock client that returns responses in sequence."""
    from unittest.mock import AsyncMock
    client = MagicMock()
    streams = []
    for resp in responses:
        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=False)
        stream.__aiter__ = MagicMock(return_value=iter([]))
        stream.get_final_message = AsyncMock(return_value=resp)
        streams.append(stream)
    client.messages.stream = MagicMock(side_effect=streams)
    return client
```

Update every test that calls `run_agent_streaming` to be `async def` and `await` the call:

```python
@pytest.mark.asyncio
async def test_write_tool_blocked_without_confirm(self):
    # ... same setup ...
    await run_agent_streaming(
        client=client,
        messages=[{"role": "user", "content": "delete pod"}],
        system_prompt="test",
        tool_defs=[],
        tool_map={"delete_pod": mock_tool},
        write_tools={"delete_pod"},
        on_confirm=AsyncMock(return_value=False),
    )
    mock_tool.call.assert_not_called()
```

Do this for all 21 tests in `test_agent.py` that call `run_agent_streaming` or use the stream mock helper.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent.py -v`

Expected: FAIL — `run_agent_streaming` is still sync.

- [ ] **Step 3: Convert `run_agent_streaming` to async**

In `sre_agent/agent.py`, make these changes:

1. Change function signature:
```python
async def run_agent_streaming(
    client,
    messages: list[dict],
    system_prompt: str | list[dict[str, Any]],
    ...
) -> str:
```

2. Add `import asyncio` at the top if not present.

3. LLM streaming — change `with`/`for` to `async with`/`async for`:
```python
async with stream_ctx as stream:
    async for event in stream:
        if event.type == "content_block_start":
            if hasattr(event.content_block, "name"):
                if on_tool_use:
                    await on_tool_use(event.content_block.name)
        elif event.type == "content_block_delta":
            if event.delta.type == "text_delta":
                if on_text:
                    await on_text(event.delta.text)
                full_text_parts.append(event.delta.text)
            elif event.delta.type == "thinking_delta":
                if on_thinking:
                    await on_thinking(event.delta.thinking)

    response = await stream.get_final_message()
```

4. Callback invocations — add `await` to every callback call:
```python
if on_usage:
    await on_usage(...)

if on_tool_result:
    await on_tool_result({...})

if on_component:
    await on_component(block.name, component)
```

5. Retry delays — change `time.sleep` to `await asyncio.sleep`:
```python
# Line ~527
await asyncio.sleep(min(delay, 30))
# Line ~539
await asyncio.sleep(retry_delays[attempt])
```

6. Parallel tool execution — replace `concurrent.futures` with `asyncio`:
```python
if read_blocks:
    start_time = time.time()
    loop = asyncio.get_running_loop()
    # Submit all read tools to thread pool
    task_to_block: dict[asyncio.Task, Any] = {}
    for b in read_blocks:
        task = asyncio.ensure_future(
            loop.run_in_executor(_tool_pool, _execute_tool, b.name, b.input, tool_map)
        )
        task_to_block[task] = b

    try:
        done, pending = await asyncio.wait(
            task_to_block.keys(), timeout=TOOL_TIMEOUT
        )
    except Exception:
        done, pending = set(), set(task_to_block.keys())

    # Cancel any pending tasks
    for p in pending:
        p.cancel()
        block = task_to_block[p]
        elapsed_ms = int((time.time() - start_time) * 1000)
        results_map[block.id] = (f"Error: {block.name} timed out", None)
        if on_tool_result:
            await on_tool_result({
                "tool_name": block.name, "input": block.input,
                "status": "error", "error_message": f"{block.name} timed out",
                "error_category": "server", "duration_ms": elapsed_ms,
                "result_bytes": 0, "was_confirmed": None, "turn_number": iterations,
            })

    for task in done:
        block = task_to_block[task]
        elapsed_ms = int((time.time() - start_time) * 1000)
        try:
            text, component, exec_meta = task.result()
            results_map[block.id] = (text, component)
            if on_tool_result:
                await on_tool_result({
                    "tool_name": block.name, "input": block.input,
                    "status": exec_meta["status"],
                    "error_message": exec_meta["error_message"],
                    "error_category": exec_meta["error_category"],
                    "duration_ms": elapsed_ms,
                    "result_bytes": exec_meta["result_bytes"],
                    "was_confirmed": None, "turn_number": iterations,
                })
        except Exception:
            results_map[block.id] = (f"Error executing {block.name}", None)
            if on_tool_result:
                await on_tool_result({
                    "tool_name": block.name, "input": block.input,
                    "status": "error",
                    "error_message": f"Error executing {block.name}",
                    "error_category": "server", "duration_ms": elapsed_ms,
                    "result_bytes": 0, "was_confirmed": None,
                    "turn_number": iterations,
                })
```

7. Write tool execution — use `run_in_executor`:
```python
for block in write_blocks:
    confirmed = await on_confirm(block.name, block.input) if on_confirm else False
    if not confirmed:
        results_map[block.id] = ("Operation denied. No confirmation callback or user rejected.", None)
        if on_tool_result:
            await on_tool_result({...})
        continue
    write_start = time.time()
    loop = asyncio.get_running_loop()
    text, component, exec_meta = await loop.run_in_executor(
        _tool_pool, _execute_tool_with_timeout, block.name, block.input, tool_map
    )
    write_elapsed_ms = int((time.time() - write_start) * 1000)
    results_map[block.id] = (text, component)
    if on_tool_result:
        await on_tool_result({...})
```

8. Harness tool selection — `select_tools_adaptive` will become async in Task 9, but for now keep it sync. Wrap in `await asyncio.to_thread(...)` temporarily:
```python
if use_harness and messages and mode not in ("view_designer", "both"):
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if isinstance(last_user, str) and last_user:
        from .skill_loader import MODE_CATEGORIES
        from .tool_predictor import select_tools_adaptive

        fallback_cats = MODE_CATEGORIES.get(mode)
        filtered_defs, filtered_map, _offered = select_tools_adaptive(
            last_user, all_tool_map=tool_map, fallback_categories=fallback_cats,
        )
        tool_defs = filtered_defs
        tool_map = {**filtered_map}
```

Note: `select_tools_adaptive` only calls the LLM as a fallback (rare). The TF-IDF path is sync and fast. Keep it sync for now — Task 9 converts it to async.

- [ ] **Step 4: Convert wrapper functions to async**

In `sre_agent/agent.py`, change `run_agent_turn_streaming`:
```python
async def run_agent_turn_streaming(
    client, messages, system_prompt=None, extra_tool_defs=None,
    extra_tool_map=None, on_text=None, on_thinking=None,
    on_tool_use=None, on_confirm=None, on_component=None, on_tool_result=None,
) -> str:
    effective_defs = TOOL_DEFS + (extra_tool_defs or [])
    effective_map = {**TOOL_MAP, **(extra_tool_map or {})}
    return await run_agent_streaming(
        client=client, messages=messages,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        tool_defs=effective_defs, tool_map=effective_map,
        write_tools=WRITE_TOOLS, on_text=on_text, on_thinking=on_thinking,
        on_tool_use=on_tool_use, on_confirm=on_confirm,
        on_component=on_component, on_tool_result=on_tool_result,
    )
```

In `sre_agent/security_agent.py`, change `run_security_scan_streaming`:
```python
async def run_security_scan_streaming(
    client, messages, system_prompt=None, extra_tool_defs=None,
    extra_tool_map=None, on_text=None, on_thinking=None,
    on_tool_use=None, on_confirm=None,
) -> str:
    effective_defs = TOOL_DEFS + (extra_tool_defs or [])
    effective_map = {**TOOL_MAP, **(extra_tool_map or {})}
    return await run_agent_streaming(
        client=client, messages=messages,
        system_prompt=system_prompt or SECURITY_SYSTEM_PROMPT,
        tool_defs=effective_defs, tool_map=effective_map,
        write_tools=set(), on_text=on_text, on_thinking=on_thinking,
        on_tool_use=on_tool_use, on_confirm=on_confirm,
    )
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_agent.py -v`

Expected: PASS (all 21 tests with async mocks)

- [ ] **Step 6: Commit**

```bash
git add sre_agent/agent.py sre_agent/security_agent.py tests/test_agent.py
git commit -m "feat: convert run_agent_streaming to async def"
```

---

## Task 4: WebSocket Handler — Native Async Callbacks

**Files:**
- Modify: `sre_agent/api/agent_ws.py`
- Modify: `sre_agent/api/ws_endpoints.py`
- Test: `tests/test_api_websocket.py`

- [ ] **Step 1: Update test mocks in `test_api_websocket.py`**

Change all `patch("sre_agent.api.agent_ws.create_client", ...)` to `patch("sre_agent.api.agent_ws.create_async_client", ...)`. Change all `patch("sre_agent.agent.create_client", ...)` to `patch("sre_agent.agent.create_async_client", ...)`.

- [ ] **Step 2: Rewrite SkillExecutor in `agent_ws.py`**

Remove `self.loop` parameter from `__init__`. Delete `_schedule_send`, `_safe_send` helpers. Delete `threading.Lock`. Delete the entire `concurrent.futures.Future` waiter pattern in `on_confirm`.

Replace the import of `create_client` with `create_async_client`:
```python
from ..agent import create_async_client, run_agent_streaming
```

Convert all callbacks to native async:

```python
async def on_text(delta: str):
    if not skill_tag:
        try:
            await self.websocket.send_json({"type": "text_delta", "text": delta})
        except Exception:
            pass

async def on_thinking(delta: str):
    if not skill_tag:
        try:
            await self.websocket.send_json({"type": "thinking_delta", "thinking": delta})
        except Exception:
            pass

async def on_tool_use(name: str):
    tools_called.append(name)
    try:
        if skill_tag:
            await self.websocket.send_json({"type": "skill_progress", "skill": skill_tag, "status": "tool_use", "tool": name})
        else:
            await self.websocket.send_json({"type": "tool_use", "tool": name})
    except Exception:
        pass

async def on_component(name: str, spec: dict):
    components.append(spec)
    if not skill_tag:
        try:
            await self.websocket.send_json({"type": "component", "spec": spec, "tool": name})
        except Exception:
            pass

async def on_usage(**kwargs):
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
        token_usage[key] = token_usage.get(key, 0) + kwargs.get(key, 0)

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

async def on_tool_result(info: dict):
    _base_tool_result_handler(info)
    if skill_tag and info.get("status") == "success":
        try:
            await self.websocket.send_json({
                "type": "skill_progress", "skill": skill_tag,
                "status": "tool_complete", "tool": info["tool_name"],
                "duration_ms": info.get("duration_ms", 0),
            })
        except Exception:
            pass
```

Change the dispatch call:
```python
# Before
full_response = await asyncio.to_thread(run_agent_streaming, client=client, ...)

# After
full_response = await run_agent_streaming(client=client, ...)
```

Change client creation:
```python
# Before
client = create_client()
# After
client = create_async_client()
```

Change client cleanup:
```python
# Before
client.close()
# After
await client.aclose()
```

- [ ] **Step 3: Update `ws_endpoints.py`**

Change import:
```python
from ..agent import create_async_client
```

Change any `create_client()` call to `create_async_client()`. Remove `asyncio.to_thread` wrapping around agent dispatch if present.

Remove `loop` parameter when constructing `SkillExecutor`:
```python
# Before
executor = SkillExecutor(websocket, session_id, loop=asyncio.get_running_loop())
# After
executor = SkillExecutor(websocket, session_id)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_api_websocket.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/agent_ws.py sre_agent/api/ws_endpoints.py tests/test_api_websocket.py
git commit -m "feat: native async callbacks in WebSocket handler, simplify confirmation gate"
```

---

## Task 5: Monitor Investigations — Async Conversion

**Files:**
- Modify: `sre_agent/monitor/investigations.py`
- Modify: `sre_agent/monitor/cluster_monitor.py`
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Update test mocks in `test_monitor.py`**

Change all `patch("sre_agent.agent.create_client", ...)` to `patch("sre_agent.agent.create_async_client", ...)`. Update any mocks that reference `_run_proactive_investigation_sync` to `_run_proactive_investigation` (no `_sync` suffix).

- [ ] **Step 2: Convert investigation functions to async**

In `sre_agent/monitor/investigations.py`:

Rename `_run_proactive_investigation_sync` → `_run_proactive_investigation`. Make it `async def`. Change:
- `run_agent_streaming(...)` → `await run_agent_streaming(...)`
- `client.close()` → `await client.aclose()`
- `create_client()` → `create_async_client()` (where client is created locally)

Same for `_run_security_followup_sync` → `async def _run_security_followup`.

- [ ] **Step 3: Update callers in `cluster_monitor.py`**

At lines ~800, 842, 1409, 1429, change:
```python
# Before
await asyncio.wait_for(
    asyncio.to_thread(_run_proactive_investigation_sync, finding, client=self._client),
    timeout=timeout_seconds,
)

# After
await asyncio.wait_for(
    _run_proactive_investigation(finding, client=self._client),
    timeout=timeout_seconds,
)
```

Same pattern for security followup calls.

Change `self._client = create_client()` → `self._client = create_async_client()` in `ClusterMonitor.__init__`.

Change `asyncio.get_event_loop()` → `asyncio.get_running_loop()` at line ~1203 (cleanup).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_monitor.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/monitor/investigations.py sre_agent/monitor/cluster_monitor.py tests/test_monitor.py
git commit -m "feat: convert monitor investigations to async"
```

---

## Task 6: Synthesis — Native Async Streaming

**Files:**
- Modify: `sre_agent/synthesis.py`
- Test: `tests/test_synthesis.py`, `tests/test_multi_skill.py`

- [ ] **Step 1: Update test mocks**

In `tests/test_synthesis.py` and `tests/test_multi_skill.py`, update any `client.messages.create` or `client.messages.stream` mocks to use `AsyncMock` and async context managers.

Change `patch("sre_agent.agent.create_client", ...)` to `patch("sre_agent.agent.create_async_client", ...)`.

- [ ] **Step 2: Convert synthesis to native async**

In `sre_agent/synthesis.py`, replace the streaming path:

```python
async def synthesize_parallel_outputs(
    result: ParallelSkillResult, query: str, client, on_text_delta=None,
) -> SynthesisResult:
    try:
        user_content = (
            f"Original user query: {query}\n\n"
            f"--- {result.primary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.primary_output}\n\n"
            f"--- {result.secondary_skill.upper()} SKILL OUTPUT ---\n"
            f"{result.secondary_output}"
        )

        if on_text_delta:
            collected: list[str] = []
            async with client.messages.stream(
                model=SYNTHESIS_MODEL,
                max_tokens=4096,
                system=_SYNTHESIS_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                async for text in stream.text_stream:
                    collected.append(text)
                    await on_text_delta(text)
            raw_text = "".join(collected)
        else:
            response = await client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=4096,
                system=_SYNTHESIS_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = response.content[0].text

        clean_text, conflicts = _parse_conflicts(raw_text)
        return SynthesisResult(
            unified_response=clean_text, conflicts=conflicts,
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )

    except Exception:
        logger.warning("Synthesis failed, falling back to concatenation", exc_info=True)
        fallback = _build_fallback_response(result)
        if on_text_delta:
            try:
                await on_text_delta(fallback)
            except Exception:
                pass
        return SynthesisResult(
            unified_response=fallback, conflicts=[],
            sources={
                result.primary_skill: result.primary_output[:200],
                result.secondary_skill: result.secondary_output[:200],
            },
        )
```

Delete `import asyncio` if it was only used for `asyncio.to_thread` / `asyncio.get_running_loop` / `asyncio.run_coroutine_threadsafe`. Keep it if used elsewhere.

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_synthesis.py tests/test_multi_skill.py -v`

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add sre_agent/synthesis.py tests/test_synthesis.py tests/test_multi_skill.py
git commit -m "feat: convert synthesis to native async streaming"
```

---

## Task 7: Plan Runtime — Drop asyncio.to_thread

**Files:**
- Modify: `sre_agent/plan_runtime.py`

- [ ] **Step 1: Update plan_runtime.py**

At lines ~323 and ~858, change:
```python
# Before
full_response = await asyncio.to_thread(run_agent_streaming, ...)

# After
full_response = await run_agent_streaming(...)
```

Remove `loop` parameter from `SkillExecutor` construction:
```python
# Before
executor = SkillExecutor(websocket, session_id, loop=asyncio.get_running_loop())
# After
executor = SkillExecutor(websocket, session_id)
```

Change `client.close()` → `await client.aclose()` at both sites (~338 and ~888).

Change `create_client()` → `create_async_client()` wherever present.

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/ -v -k "plan_runtime or plan" --timeout 30`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add sre_agent/plan_runtime.py
git commit -m "feat: convert plan_runtime to direct async agent calls"
```

---

## Task 8: Skill Router — Async LLM Classify Chain

**Files:**
- Modify: `sre_agent/skill_router.py`
- Modify: `sre_agent/api/ws_endpoints.py` (classify_query call sites)
- Test: `tests/test_api_websocket.py`

- [ ] **Step 1: Convert `_llm_classify` to async**

In `sre_agent/skill_router.py`:

```python
async def _llm_classify(query: str):
    """Use a lightweight LLM call to classify ambiguous queries."""
    from .skill_loader import list_skills

    skills = {s.name: s for s in list_skills()}
    query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:16]

    cached = _llm_cache.get(query_hash)
    if cached:
        name, ts = cached
        if time.time() - ts < _LLM_CACHE_TTL:
            skill = skills.get(name)
            if skill:
                return skill

    try:
        from .agent import create_async_client
        client = create_async_client()
        try:
            skill_options = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=20,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Classify this user query into exactly one skill.\n\n"
                        f"Available skills:\n{skill_options}\n\n"
                        f"Query: {query}\n\n"
                        f"Reply with ONLY the skill name, nothing else."
                    ),
                }],
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
        # ... rest of parsing logic unchanged ...
```

- [ ] **Step 2: Convert `classify_query` to async**

```python
async def classify_query(query: str, *, context: dict | None = None):
    # ... all the pre-route logic stays sync (no awaits needed) ...

    # Only the LLM fallback path needs await:
    if result.source == "fallback" and not best_skill:
        llm_result = await _llm_classify(query)
        if llm_result:
            best_skill = llm_result
    # ... rest unchanged ...
```

- [ ] **Step 3: Convert `classify_query_multi` to async**

```python
async def classify_query_multi(query: str, *, context: dict | None = None) -> tuple:
    primary = await classify_query(query, context=context)
    # ... rest unchanged (no other await points) ...
```

- [ ] **Step 4: Update `ws_endpoints.py` call sites**

At line ~397:
```python
# Before
skill, secondary_skill = classify_query_multi(content)
# After
skill, secondary_skill = await classify_query_multi(content)
```

At line ~404:
```python
# Before
skill = classify_query(content)
# After
skill = await classify_query(content)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_api_websocket.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/skill_router.py sre_agent/api/ws_endpoints.py tests/test_api_websocket.py
git commit -m "feat: convert skill_router LLM classify chain to async"
```

---

## Task 9: Tool Predictor — Async LLM Fallback

**Files:**
- Modify: `sre_agent/tool_predictor.py`
- Test: `tests/test_tool_predictor.py`

- [ ] **Step 1: Update test mocks**

In `tests/test_tool_predictor.py`, change `patch("sre_agent.agent.create_client")` to `patch("sre_agent.agent.create_async_client")`. Update mocks: `client.messages.create` becomes `AsyncMock`. Tests calling `select_tools_adaptive` or `llm_pick_tools` must be `@pytest.mark.asyncio` and `await` the calls.

- [ ] **Step 2: Convert `llm_pick_tools` to async**

```python
async def llm_pick_tools(*, query, tool_names, top_k=10):
    from .agent import create_async_client
    client = create_async_client()
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": query}],
            system=(...),
        )
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
    # ... rest of parsing unchanged ...
```

- [ ] **Step 3: Convert `select_tools_adaptive` to async**

The function mostly does sync TF-IDF work. Only the LLM fallback path is async:

```python
async def select_tools_adaptive(query, *, all_tool_map, fallback_categories=None):
    # ... TF-IDF scoring (sync, fast) ...

    # LLM fallback:
    if needs_llm_fallback:
        llm_picks = await llm_pick_tools(query=query, tool_names=..., top_k=...)
        # ... merge results ...
```

- [ ] **Step 4: Update caller in `agent.py`**

In `run_agent_streaming`, the call to `select_tools_adaptive` now needs `await`:
```python
# This is already inside an async function after Task 3
filtered_defs, filtered_map, _offered = await select_tools_adaptive(
    last_user, all_tool_map=tool_map, fallback_categories=fallback_cats,
)
```

Remove the temporary `asyncio.to_thread` wrapper if one was added in Task 3.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_tool_predictor.py tests/test_agent.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/tool_predictor.py sre_agent/agent.py tests/test_tool_predictor.py
git commit -m "feat: convert tool_predictor LLM fallback to async"
```

---

## Task 10: Inbox — Sync Client Boundary

**Files:**
- Modify: `sre_agent/inbox.py`

- [ ] **Step 1: Add `_create_sync_client` helper**

At the top of `sre_agent/inbox.py` (after imports), add:

```python
def _create_sync_client():
    """Create a sync Anthropic client for inbox processing.

    Inbox runs inside asyncio.to_thread (no event loop), so it needs
    a sync client. This is separate from the async client used by the
    main agent loop.
    """
    import anthropic
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")
    if project and region:
        return anthropic.AnthropicVertex(region=region, project_id=project)
    return anthropic.Anthropic()
```

- [ ] **Step 2: Replace `create_client` imports with `_create_sync_client`**

At lines ~474, ~884, ~1096, change:
```python
# Before
from .agent import create_client
client = create_client()

# After
client = _create_sync_client()
```

- [ ] **Step 3: Add sync investigation wrapper**

Replace the direct call at line ~992:
```python
# Before
from .monitor.investigations import _run_proactive_investigation_sync
result = _run_proactive_investigation_sync(finding_dict)

# After
import asyncio
from .monitor.investigations import _run_proactive_investigation
result = asyncio.run(_run_proactive_investigation(finding_dict))
```

This works because inbox runs inside `asyncio.to_thread` (a separate thread with no running event loop), so `asyncio.run()` can create a new loop.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/ -v -k "inbox" --timeout 30`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/inbox.py
git commit -m "feat: inbox keeps sync client boundary, add investigation wrapper"
```

---

## Task 11: CLI — Async REPL

**Files:**
- Modify: `sre_agent/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Update test mocks**

In `tests/test_main.py`, change `patch("sre_agent.main.create_client", ...)` to `patch("sre_agent.main.create_async_client", ...)`. Update runner mocks to return coroutines (`AsyncMock`).

- [ ] **Step 2: Convert `run_repl` to async**

```python
async def run_repl(mode: str):
    # ... same setup ...
    client = create_async_client()  # was create_client()

    # ... K8s connectivity check stays sync ...

    while True:
        user_input = console.input(...)  # blocking, fine for CLI

        # ... command handling unchanged ...

        async def on_text(delta: str):
            text_parts.append(delta)
            console.print(delta, end="", highlight=False)

        async def on_tool_use(name: str):
            console.print(f"\n  [dim]> calling {name}...[/dim]")
            if memory_mgr:
                memory_mgr.record_tool_call(name, {})

        async def _confirm_action(tool_name: str, tool_input: dict) -> bool:
            console.print(Panel(...))
            try:
                answer = console.input("[bold yellow]Proceed? (y/N):[/bold yellow] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("y", "yes")

        try:
            full_response = await cfg["runner"](
                client, messages, system_prompt=augmented_prompt,
                extra_tool_defs=extra_defs, extra_tool_map=extra_map,
                on_text=on_text, on_tool_use=on_tool_use, on_confirm=_confirm_action,
            )
        # ... error handling unchanged ...
```

- [ ] **Step 3: Update entry point**

Change the `main()` function (or wherever `run_repl` is called) to use `asyncio.run()`:

```python
# Before
result = run_repl(mode)

# After
result = asyncio.run(run_repl(mode))
```

Update import: `from .agent import create_async_client` (was `create_client`).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_main.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/main.py tests/test_main.py
git commit -m "feat: convert CLI REPL to async"
```

---

## Task 12: Evals — Judge + Replay Harness

**Files:**
- Modify: `sre_agent/evals/judge.py`
- Modify: `sre_agent/evals/replay.py`
- Modify: `sre_agent/evals/replay_cli.py`
- Test: `tests/test_evals_judge.py`, `tests/test_eval_replay.py`

- [ ] **Step 1: Convert `judge_response` to async**

In `sre_agent/evals/judge.py`:

```python
async def judge_response(...) -> dict:
    # ... setup ...
    from ..agent import create_async_client
    client = create_async_client()
    try:
        message = await client.messages.create(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": judge_prompt}],
        )
    finally:
        await client.aclose()
    # ... parsing unchanged ...
```

- [ ] **Step 2: Update eval replay harness**

In `sre_agent/evals/replay.py`, wrap the sync call in `asyncio.run()`:

```python
# Line ~117
response = asyncio.run(run_agent_streaming(**kwargs))
```

Add `import asyncio` at top.

In `replay_cli.py`, change `create_client()` → `create_async_client()`.

- [ ] **Step 3: Update test mocks**

In `tests/test_evals_judge.py`:
- `client.messages.create` → `AsyncMock`
- `patch("sre_agent.agent.create_client", ...)` → `patch("sre_agent.agent.create_async_client", ...)`
- Tests calling `judge_response` become `@pytest.mark.asyncio` with `await`

In `tests/test_eval_replay.py`:
- Stream mocks: `__enter__` → `__aenter__`, `__exit__` → `__aexit__`, `get_final_message` → `AsyncMock`
- `__iter__` → `__aiter__`

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_evals_judge.py tests/test_eval_replay.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/evals/judge.py sre_agent/evals/replay.py sre_agent/evals/replay_cli.py tests/test_evals_judge.py tests/test_eval_replay.py
git commit -m "feat: convert evals judge and replay to async"
```

---

## Task 13: Remaining Test Files + Investigation View Plan

**Files:**
- Modify: `tests/test_investigation_view_plan.py`

- [ ] **Step 1: Update mocks**

Change `patch("sre_agent.agent.create_client")` to `patch("sre_agent.agent.create_async_client")`. Update any stream or agent mocks to async versions.

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_investigation_view_plan.py -v`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigation_view_plan.py
git commit -m "fix: update investigation_view_plan tests for async agent"
```

---

## Task 14: Delete Old Sync References + Full Test Suite

**Files:**
- Modify: `sre_agent/agent.py` (delete old `create_client` if still present)
- All files

- [ ] **Step 1: Grep for any remaining sync references**

```bash
grep -rn 'create_client\b' sre_agent/ --include='*.py' | grep -v 'create_async_client\|_create_sync_client\|__pycache__'
grep -rn 'client\.close()' sre_agent/ --include='*.py' | grep -v 'aclose\|_create_sync_client\|__pycache__'
grep -rn '_run_proactive_investigation_sync\|_run_security_followup_sync' sre_agent/ --include='*.py' | grep -v '__pycache__'
grep -rn 'asyncio\.to_thread.*run_agent_streaming\|asyncio\.to_thread.*_run_proactive\|asyncio\.to_thread.*_run_security' sre_agent/ --include='*.py' | grep -v '__pycache__'
```

Fix any remaining references found.

- [ ] **Step 2: Delete old `create_client` function**

If it still exists in `agent.py`, delete it. Verify no imports reference it.

- [ ] **Step 3: Run the full test suite**

Run: `python3 -m pytest tests/ -v`

Expected: ALL PASS

- [ ] **Step 4: Run type checker**

Run: `make verify`

Expected: PASS (0 mypy errors, 0 ruff errors, all tests pass)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove all sync Anthropic client references"
```

---

## Task 15: Final Verification + Docs

**Files:**
- Modify: `CLAUDE.md` (update architecture docs)

- [ ] **Step 1: Update CLAUDE.md**

In the Architecture section, update these references:
- `create_client()` → `create_async_client()`
- Add note: "Agent loop (`run_agent_streaming`) is `async def`. Tool execution stays sync in `ThreadPoolExecutor` via `loop.run_in_executor()`."
- Update the agent loop description: remove "parallel for reads" ThreadPoolExecutor reference, note async dispatch pattern

- [ ] **Step 2: Run release eval gate**

Run: `python -m sre_agent.evals.cli --suite release --fail-on-gate`

Expected: PASS (99.6%+ score)

- [ ] **Step 3: Final commit**

```bash
git add CLAUDE.md
git commit -m "docs: update architecture docs for async Anthropic migration"
```
