# Tool Usage Tracking — Pre-Requisite Refactoring Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix tech debt in agent.py and harness.py that blocks tool usage tracking implementation.

**Architecture:** Two independent refactors: (1) Add a post-execution callback to the agent loop so callers can track tool name, input, result status, duration, and confirmation state per tool call. (2) Add a reverse category lookup to the harness and fix missing category assignments.

**Tech Stack:** Python 3.11, pytest, anthropic SDK

---

### Task 1: Add `get_tool_category()` reverse lookup to harness.py

**Files:**
- Modify: `sre_agent/harness.py:218-248`
- Test: `tests/test_harness.py`

- [ ] **Step 1: Write failing tests for `get_tool_category`**

Add to `tests/test_harness.py`:

```python
from sre_agent.harness import get_tool_category


class TestGetToolCategory:
    def test_tool_in_single_category(self):
        assert get_tool_category("scale_deployment") == "workloads"

    def test_tool_in_multiple_categories_returns_first(self):
        # list_resources is in diagnostics, workloads, networking, storage
        result = get_tool_category("list_resources")
        assert result in ("diagnostics", "workloads", "networking", "storage")

    def test_tool_not_in_any_category(self):
        assert get_tool_category("nonexistent_tool_xyz") is None

    def test_always_include_tool_without_category(self):
        # record_audit_entry is in ALWAYS_INCLUDE but no category
        assert get_tool_category("record_audit_entry") is None

    def test_all_categorized_tools_return_category(self):
        """Every tool in TOOL_CATEGORIES should return a non-None category."""
        from sre_agent.harness import TOOL_CATEGORIES
        for cat_name, config in TOOL_CATEGORIES.items():
            for tool_name in config["tools"]:
                result = get_tool_category(tool_name)
                assert result is not None, f"{tool_name} returned None but is in {cat_name}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_harness.py::TestGetToolCategory -v`
Expected: FAIL with `ImportError: cannot import name 'get_tool_category'`

- [ ] **Step 3: Implement `get_tool_category` in harness.py**

Add after the `ALWAYS_INCLUDE` set definition (after line 238):

```python
# Reverse lookup: tool_name -> first matching category
_TOOL_CATEGORY_MAP: dict[str, str] = {}
for _cat_name, _cat_config in TOOL_CATEGORIES.items():
    for _tool_name in _cat_config["tools"]:
        if _tool_name not in _TOOL_CATEGORY_MAP:
            _TOOL_CATEGORY_MAP[_tool_name] = _cat_name


def get_tool_category(tool_name: str) -> str | None:
    """Return the primary category for a tool, or None if uncategorized."""
    return _TOOL_CATEGORY_MAP.get(tool_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_harness.py::TestGetToolCategory -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add sre_agent/harness.py tests/test_harness.py
git commit -m "feat: add get_tool_category() reverse lookup to harness"
```

---

### Task 2: Add missing tools to TOOL_CATEGORIES in harness.py

**Files:**
- Modify: `sre_agent/harness.py:22-217`
- Test: `tests/test_harness.py`

- [ ] **Step 1: Write failing test for category coverage**

Add to `tests/test_harness.py`:

```python
class TestCategoryCoverage:
    def test_all_registered_tools_have_category(self):
        """Every tool in the registry should be in at least one category or ALWAYS_INCLUDE."""
        from sre_agent.tool_registry import TOOL_REGISTRY
        from sre_agent.harness import TOOL_CATEGORIES, ALWAYS_INCLUDE

        all_categorized = set(ALWAYS_INCLUDE)
        for config in TOOL_CATEGORIES.values():
            all_categorized.update(config["tools"])

        # These tools are internal/meta and intentionally uncategorized
        EXCLUDED = {"set_store", "set_current_user", "get_current_user", "get_cluster_patterns"}

        missing = set()
        for tool_name in TOOL_REGISTRY:
            if tool_name not in all_categorized and tool_name not in EXCLUDED:
                missing.add(tool_name)

        assert missing == set(), f"Tools missing from categories: {sorted(missing)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_harness.py::TestCategoryCoverage -v`
Expected: FAIL listing the missing tools

- [ ] **Step 3: Add missing tools to appropriate categories**

In `sre_agent/harness.py`, update the `TOOL_CATEGORIES` dict. Add these tools to the categories listed:

**diagnostics** — add:
```python
"describe_node",
"describe_resource",
"list_namespaces",
"list_replicasets",
"list_statefulsets",
"list_daemonsets",
"get_persistent_volume_claims",
"get_pod_disruption_budgets",
"get_resource_quotas",
"list_limit_ranges",
"search_past_incidents",
"get_resource_relationships",
```

**networking** — add:
```python
"get_services",
```

**workloads** — add:
```python
"list_statefulsets",
"list_daemonsets",
"list_replicasets",
```

**gitops** — add:
```python
"get_argo_applications",
"get_argo_app_detail",
"get_argo_app_source",
"get_argo_sync_diff",
"check_argo_auto_sync",
"install_gitops_operator",
"create_argo_application",
```

**operations** — add:
```python
"get_learned_runbooks",
```

**security** — add:
```python
"request_security_scan",
```

**Also add to ALWAYS_INCLUDE:**
```python
"request_sre_investigation",
```

**Also add view tools to a scope:** Add them to ALWAYS_INCLUDE (they are already partly there). Add the missing ones:
```python
"get_view_details",
"update_view_widgets",
"add_widget_to_view",
"undo_view_change",
"get_view_versions",
```

- [ ] **Step 4: Run the coverage test to verify it passes**

Run: `python3 -m pytest tests/test_harness.py::TestCategoryCoverage -v`
Expected: PASS

- [ ] **Step 5: Run all harness tests to check for regressions**

Run: `python3 -m pytest tests/test_harness.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add sre_agent/harness.py tests/test_harness.py
git commit -m "fix: add missing tools to TOOL_CATEGORIES for full coverage"
```

---

### Task 3: Expose tools_offered from select_tools

**Files:**
- Modify: `sre_agent/harness.py:250-278`
- Modify: `sre_agent/agent.py:397-404`
- Test: `tests/test_harness.py`

- [ ] **Step 1: Write failing tests for select_tools returning offered list**

Add to `tests/test_harness.py`:

```python
class TestSelectToolsOffered:
    def _all_tools(self):
        names = set(ALWAYS_INCLUDE)
        for config in TOOL_CATEGORIES.values():
            names.update(config["tools"])
        tools = [_make_tool(n) for n in sorted(names)]
        tool_map = {t.name: t for t in tools}
        return tools, tool_map

    def test_returns_three_tuple(self):
        all_tools, tool_map = self._all_tools()
        result = select_tools("check health", all_tools, tool_map, mode="sre")
        assert len(result) == 3, "select_tools should return (defs, map, offered_names)"

    def test_offered_names_is_list(self):
        all_tools, tool_map = self._all_tools()
        _defs, _map, offered = select_tools("check health", all_tools, tool_map, mode="sre")
        assert isinstance(offered, list)
        assert all(isinstance(n, str) for n in offered)

    def test_offered_matches_filtered(self):
        all_tools, tool_map = self._all_tools()
        defs, selected, offered = select_tools("check health", all_tools, tool_map, mode="sre")
        assert set(offered) == set(selected.keys())

    def test_both_mode_returns_all_names(self):
        all_tools, tool_map = self._all_tools()
        _defs, _map, offered = select_tools("hello", all_tools, tool_map, mode="both")
        assert len(offered) == len(all_tools)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_harness.py::TestSelectToolsOffered -v`
Expected: FAIL — `select_tools` returns 2-tuple, not 3-tuple

- [ ] **Step 3: Update `select_tools` to return offered tool names**

In `sre_agent/harness.py`, change `select_tools` to return a 3-tuple:

```python
def select_tools(query: str, all_tools: list, all_tool_map: dict, mode: str = "sre") -> tuple[list, dict, list[str]]:
    """Select tools based on agent mode.

    Mode-aware: each orchestrator mode maps to a set of tool categories.
    Tools in ALWAYS_INCLUDE are always returned regardless of mode.
    If mode is 'both' or unknown, all tools are returned.

    Returns:
        (tool_defs, tool_map, offered_names) — offered_names is the list of
        tool names selected for this turn, used for tracking harness efficiency.
    """
    categories = MODE_CATEGORIES.get(mode)

    # Fallback: return all tools for 'both' or unknown modes
    if categories is None:
        logger.info("Tool selection: returning all %d tools for mode=%s", len(all_tools), mode)
        tool_map = {t.name: t for t in all_tools}
        return [t.to_dict() for t in all_tools], tool_map, [t.name for t in all_tools]

    # Collect tool names from the mode's categories
    mode_tool_names = set(ALWAYS_INCLUDE)
    for cat_name in categories:
        cat = TOOL_CATEGORIES.get(cat_name, {})
        mode_tool_names.update(cat.get("tools", []))

    filtered = [t for t in all_tools if t.name in mode_tool_names]

    # Safety: if filtering removed too many, return all
    if len(filtered) < 5:
        logger.warning("Tool selection: mode=%s matched only %d tools, returning all", mode, len(filtered))
        tool_map = {t.name: t for t in all_tools}
        return [t.to_dict() for t in all_tools], tool_map, [t.name for t in all_tools]

    logger.info("Tool selection: %d/%d tools for mode=%s", len(filtered), len(all_tools), mode)
    tool_map = {t.name: t for t in filtered}
    return [t.to_dict() for t in filtered], tool_map, [t.name for t in filtered]
```

- [ ] **Step 4: Update callers of `select_tools` in agent.py**

In `sre_agent/agent.py`, update lines 401-404:

```python
        if use_harness and messages:
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            if isinstance(last_user, str) and last_user:
                filtered_defs, filtered_map, _offered = select_tools(last_user, list(tool_map.values()), tool_map, mode=mode)
                if len(filtered_defs) < len(tool_defs):
                    tool_defs = filtered_defs
                    tool_map = {**filtered_map}  # Don't mutate the original
```

- [ ] **Step 5: Update any other callers of `select_tools`**

Search for other callers and update them. Check `monitor.py`:

Run: `grep -rn 'select_tools' sre_agent/`

Update each caller to accept the 3-tuple (unpack with `_offered` variable for callers that don't need it yet).

- [ ] **Step 6: Fix existing test assertions**

Update `tests/test_harness.py` — existing `TestSelectTools` methods that unpack 2-tuple need to unpack 3:

```python
# Change all instances of:
_defs, selected = select_tools(...)
# To:
_defs, selected, _offered = select_tools(...)
```

- [ ] **Step 7: Run all tests**

Run: `python3 -m pytest tests/test_harness.py tests/test_agent.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add sre_agent/harness.py sre_agent/agent.py sre_agent/monitor.py tests/test_harness.py
git commit -m "feat: select_tools returns offered tool names for tracking"
```

---

### Task 4: Add `on_tool_result` callback to `run_agent_streaming`

This is the core refactor. Currently `on_tool_use` fires before execution (only gets tool name). We add `on_tool_result` that fires after each tool execution with full metadata.

**Files:**
- Modify: `sre_agent/agent.py:262-543`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write failing tests for on_tool_result callback**

Add to `tests/test_agent.py`:

```python
class TestOnToolResult:
    def _make_stream_context(self, responses):
        """Build a mock client that returns responses in sequence."""
        client = MagicMock()
        streams = []
        for resp in responses:
            stream = MagicMock()
            stream.__enter__ = MagicMock(return_value=stream)
            stream.__exit__ = MagicMock(return_value=False)
            stream.__iter__ = MagicMock(return_value=iter([]))
            stream.get_final_message.return_value = resp
            streams.append(stream)
        client.messages.stream = MagicMock(side_effect=streams)
        return client

    def test_on_tool_result_called_for_read_tool(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={"namespace": "default"}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Done.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "pod-1 Running"

        results = []

        def on_tool_result(info: dict):
            results.append(info)

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list pods"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
            on_tool_result=on_tool_result,
        )

        assert len(results) == 1
        r = results[0]
        assert r["tool_name"] == "list_pods"
        assert r["input"] == {"namespace": "default"}
        assert r["status"] == "success"
        assert r["error_message"] is None
        assert r["duration_ms"] >= 0
        assert r["result_bytes"] > 0
        assert r["was_confirmed"] is None  # not a write tool

    def test_on_tool_result_called_for_write_tool_confirmed(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="delete_pod", input={"pod_name": "x"}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Done.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "deleted"

        results = []

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=lambda name, inp: True,
            on_tool_result=lambda info: results.append(info),
        )

        assert len(results) == 1
        assert results[0]["was_confirmed"] is True
        assert results[0]["status"] == "success"

    def test_on_tool_result_called_for_write_tool_denied(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="delete_pod", input={"pod_name": "x"}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Cancelled.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()

        results = []

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "delete pod"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"delete_pod": mock_tool},
            write_tools={"delete_pod"},
            on_confirm=lambda name, inp: False,
            on_tool_result=lambda info: results.append(info),
        )

        assert len(results) == 1
        assert results[0]["was_confirmed"] is False
        assert results[0]["status"] == "denied"

    def test_on_tool_result_captures_error(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="bad_tool", input={}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Error.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.side_effect = RuntimeError("k8s unreachable")

        results = []

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "do thing"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"bad_tool": mock_tool},
            on_tool_result=lambda info: results.append(info),
        )

        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "RuntimeError" in results[0]["error_message"]

    def test_on_tool_result_includes_iteration(self):
        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="t1", name="list_pods", input={}),
            ],
        )
        final_response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Done.")],
        )
        client = self._make_stream_context([tool_use_response, final_response])

        mock_tool = MagicMock()
        mock_tool.call.return_value = "pods"

        results = []

        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "list"}],
            system_prompt="test",
            tool_defs=[],
            tool_map={"list_pods": mock_tool},
            on_tool_result=lambda info: results.append(info),
        )

        assert results[0]["turn_number"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent.py::TestOnToolResult -v`
Expected: FAIL — `run_agent_streaming() got an unexpected keyword argument 'on_tool_result'`

- [ ] **Step 3: Add `on_tool_result` parameter and emit from `_execute_tool`**

First, update `_execute_tool` in `sre_agent/agent.py` to return status metadata alongside the result. Change its return type:

```python
def _execute_tool(name: str, input_data: dict, tool_map: dict) -> tuple[str, dict | None, dict]:
    """Execute a tool by name. Returns (text_result, component_spec_or_None, exec_meta).

    exec_meta contains: status, error_message, error_category, result_bytes.
    """
    tool = tool_map.get(name)
    if not tool:
        return f"Error: unknown tool '{name}'", None, {
            "status": "error",
            "error_message": f"unknown tool '{name}'",
            "error_category": "not_found",
            "result_bytes": 0,
        }
    try:
        result = tool.call(input_data)
        # Tools can return a tuple (text, component_spec) for rich UI rendering
        if isinstance(result, tuple) and len(result) == 2:
            text, component = result
        else:
            text, component = result, None
        result_bytes = len(text)
        # Cap result size to prevent WebSocket overflow
        if len(text) > MAX_TOOL_RESULT_LENGTH:
            original_len = len(text)
            text = text[:MAX_TOOL_RESULT_LENGTH] + f"\n\n... (truncated, {original_len} total chars)"
        logger.info(
            json.dumps(
                {
                    "event": "tool_executed",
                    "tool": name,
                    "input": _redact_input(name, input_data),
                    "result_length": len(text),
                    "has_component": component is not None,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )
        return text, component, {
            "status": "success",
            "error_message": None,
            "error_category": None,
            "result_bytes": result_bytes,
        }
    except Exception as e:
        from .error_tracker import get_tracker
        from .errors import classify_exception

        err = classify_exception(e, name)
        get_tracker().record(err)
        logger.exception(
            json.dumps(
                {
                    "event": "tool_error",
                    "tool": name,
                    "input": _redact_input(name, input_data),
                    "error": type(e).__name__,
                    "error_detail": str(e)[:500],
                    "category": err.category,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )
        # Only return type name to LLM — don't leak internal details
        return f"Error executing {name}: {type(e).__name__}", None, {
            "status": "error",
            "error_message": f"{type(e).__name__}: {str(e)[:200]}",
            "error_category": err.category,
            "result_bytes": 0,
        }
```

Update `_execute_tool_with_timeout` to propagate the 3-tuple:

```python
def _execute_tool_with_timeout(
    name: str, input_data: dict, tool_map: dict, timeout: int | None = None
) -> tuple[str, dict | None, dict]:
    """Execute a tool with a timeout guard."""
    timeout = timeout or TOOL_TIMEOUT
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute_tool, name, input_data, tool_map)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.error(
                json.dumps(
                    {
                        "event": "tool_timeout",
                        "tool": name,
                        "timeout": timeout,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )
            )
            from .error_tracker import get_tracker
            from .errors import ToolError

            err = ToolError(message=f"{name} timed out after {timeout}s", category="server", operation=name)
            get_tracker().record(err)
            return f"Error: {name} timed out after {timeout}s", None, {
                "status": "error",
                "error_message": f"{name} timed out after {timeout}s",
                "error_category": "server",
                "result_bytes": 0,
            }
```

- [ ] **Step 4: Add `on_tool_result` to `run_agent_streaming` and emit after each tool**

In `run_agent_streaming`, add the parameter and emit after execution:

```python
def run_agent_streaming(
    client,
    messages: list[dict],
    system_prompt: str,
    tool_defs: list,
    tool_map: dict,
    write_tools: set[str] | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
    on_component=None,
    on_tool_result=None,
    mode: str = "sre",
) -> str:
```

Update the tool execution section (read tools):

```python
            # Execute read tools in parallel
            if read_blocks:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(read_blocks), 5)) as pool:
                    futures = {}
                    start_times = {}
                    for b in read_blocks:
                        start_times[b.id] = time.time()
                        futures[pool.submit(_execute_tool_with_timeout, b.name, b.input, tool_map)] = b
                    for future in concurrent.futures.as_completed(futures):
                        block = futures[future]
                        elapsed_ms = int((time.time() - start_times[block.id]) * 1000)
                        try:
                            text, component, exec_meta = future.result()
                            results_map[block.id] = (text, component)
                        except Exception:
                            text = f"Error executing {block.name}"
                            results_map[block.id] = (text, None)
                            exec_meta = {"status": "error", "error_message": text, "error_category": "unknown", "result_bytes": 0}
                        if on_tool_result:
                            on_tool_result({
                                "tool_name": block.name,
                                "input": block.input,
                                "status": exec_meta["status"],
                                "error_message": exec_meta.get("error_message"),
                                "error_category": exec_meta.get("error_category"),
                                "duration_ms": elapsed_ms,
                                "result_bytes": exec_meta.get("result_bytes", 0),
                                "was_confirmed": None,
                                "turn_number": iterations,
                            })
```

Update the write tools section:

```python
            # Execute write tools sequentially (need confirmation gate)
            for block in write_blocks:
                confirmed = on_confirm(block.name, block.input) if on_confirm else False
                if not confirmed:
                    results_map[block.id] = ("Operation denied. No confirmation callback or user rejected.", None)
                    if on_tool_result:
                        on_tool_result({
                            "tool_name": block.name,
                            "input": block.input,
                            "status": "denied",
                            "error_message": None,
                            "error_category": None,
                            "duration_ms": 0,
                            "result_bytes": 0,
                            "was_confirmed": False,
                            "turn_number": iterations,
                        })
                    continue
                start_t = time.time()
                text, component, exec_meta = _execute_tool_with_timeout(block.name, block.input, tool_map)
                elapsed_ms = int((time.time() - start_t) * 1000)
                results_map[block.id] = (text, component)
                if on_tool_result:
                    on_tool_result({
                        "tool_name": block.name,
                        "input": block.input,
                        "status": exec_meta["status"],
                        "error_message": exec_meta.get("error_message"),
                        "error_category": exec_meta.get("error_category"),
                        "duration_ms": elapsed_ms,
                        "result_bytes": exec_meta.get("result_bytes", 0),
                        "was_confirmed": True,
                        "turn_number": iterations,
                    })
```

- [ ] **Step 5: Update results_map unpacking**

The `results_map` values are now still 2-tuples (text, component) — we only changed what gets stored. Update the assembly loop:

```python
            # Assemble results in original order (unchanged — results_map still stores (text, component))
            for block in tool_blocks:
                text, component = results_map.get(block.id, (f"Error: no result for {block.name}", None))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                    }
                )
                if component and on_component:
                    on_component(block.name, component)
```

- [ ] **Step 6: Fix existing tests for `_execute_tool` 3-tuple return**

Update `tests/test_agent.py` `TestExecuteTool`:

```python
class TestExecuteTool:
    def test_success(self):
        tool = MagicMock()
        tool.call.return_value = "result data"
        tool_map = {"my_tool": tool}
        text, component, meta = _execute_tool("my_tool", {"arg": "val"}, tool_map)
        assert text == "result data"
        assert component is None
        assert meta["status"] == "success"
        assert meta["result_bytes"] == len("result data")
        tool.call.assert_called_once_with({"arg": "val"})

    def test_success_with_component(self):
        tool = MagicMock()
        tool.call.return_value = ("result data", {"kind": "data_table"})
        tool_map = {"my_tool": tool}
        text, component, meta = _execute_tool("my_tool", {}, tool_map)
        assert text == "result data"
        assert component == {"kind": "data_table"}
        assert meta["status"] == "success"

    def test_unknown_tool(self):
        text, component, meta = _execute_tool("nonexistent", {}, {})
        assert "unknown tool" in text
        assert component is None
        assert meta["status"] == "error"

    def test_exception_returns_type_only(self):
        tool = MagicMock()
        tool.call.side_effect = ValueError("secret details here")
        tool_map = {"bad_tool": tool}
        text, component, meta = _execute_tool("bad_tool", {}, tool_map)
        assert "ValueError" in text
        assert "secret details" not in text
        assert component is None
        assert meta["status"] == "error"
        assert "ValueError" in meta["error_message"]
```

- [ ] **Step 7: Also pass `on_tool_result` through `run_agent_turn_streaming`**

In `sre_agent/agent.py`, update `run_agent_turn_streaming`:

```python
def run_agent_turn_streaming(
    client,
    messages: list[dict],
    system_prompt: str | None = None,
    extra_tool_defs: list | None = None,
    extra_tool_map: dict | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
    on_component=None,
    on_tool_result=None,
) -> str:
    """Run the SRE agent. Delegates to the shared agent loop."""
    effective_defs = TOOL_DEFS + (extra_tool_defs or [])
    effective_map = {**TOOL_MAP, **(extra_tool_map or {})}

    return run_agent_streaming(
        client=client,
        messages=messages,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        tool_defs=effective_defs,
        tool_map=effective_map,
        write_tools=WRITE_TOOLS,
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_confirm=on_confirm,
        on_component=on_component,
        on_tool_result=on_tool_result,
    )
```

- [ ] **Step 8: Run all tests**

Run: `python3 -m pytest tests/test_agent.py tests/test_harness.py -v`
Expected: All PASS

- [ ] **Step 9: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS (no regressions)

- [ ] **Step 10: Commit**

```bash
git add sre_agent/agent.py tests/test_agent.py
git commit -m "feat: add on_tool_result callback with execution metadata"
```

---

### Task 5: Final verification

**Files:**
- None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass, no regressions.

- [ ] **Step 2: Verify the refactored interfaces**

Check that these are now available for the tool usage tracking implementation:

```python
# In a Python shell or test:
from sre_agent.harness import get_tool_category, select_tools

# Reverse lookup works
assert get_tool_category("list_pods") == "diagnostics"

# select_tools returns 3-tuple with offered names
defs, tool_map, offered = select_tools("test", [], {}, mode="both")

# on_tool_result callback signature is accepted
from sre_agent.agent import run_agent_streaming
import inspect
sig = inspect.signature(run_agent_streaming)
assert "on_tool_result" in sig.parameters
```

- [ ] **Step 3: Commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: final cleanup for tool tracking prerequisites"
```
