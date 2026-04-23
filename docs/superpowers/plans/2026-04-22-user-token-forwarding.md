# User OAuth Token Forwarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Forward the user's OpenShift OAuth token to all K8s API calls so the API server enforces RBAC per-user, instead of using the agent's ServiceAccount for everything.

**Architecture:** Explicit parameter threading from WebSocket/REST handlers through the agent loop. A ContextVar bridge at the `tool.call()` boundary (where `@beta_tool` signatures can't accept extra params) lets `get_*_client()` return a per-request K8s client with the user's bearer token. A cached `ApiClient` contextvar prevents connection pool churn. Monitor paths never set the contextvar, so they stay on the SA singleton.

**Tech Stack:** Python 3.11+, `contextvars`, kubernetes-python client, FastAPI WebSocket + REST, Pydantic Settings

**Spec:** `docs/superpowers/specs/2026-04-22-user-token-forwarding-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `sre_agent/config.py` | Modify | Add `token_forwarding` setting |
| `sre_agent/errors.py` | Modify | Split 401/403 into separate categories |
| `sre_agent/k8s_client.py` | Modify | Add contextvars, cached ApiClient, `user_token_context()`, update 8 getters |
| `sre_agent/agent.py` | Modify | Thread `user_token` through execution chain |
| `sre_agent/mcp_client.py` | Modify | Forward token as `Authorization` header |
| `sre_agent/view_executor.py` | Modify | Set contextvar around widget tool calls |
| `sre_agent/api/auth.py` | Modify | Add `get_user_token()` FastAPI dependency |
| `sre_agent/api/agent_ws.py` | Modify | Thread token through SkillExecutor, detect 401 |
| `sre_agent/api/ws_endpoints.py` | Modify | Extract token at WS connect, pass downstream |
| `sre_agent/api/views.py` | Modify | Forward token to action execution + claim |
| `sre_agent/api/topology_rest.py` | Modify | Wrap K8s calls in `user_token_context()` |
| `tests/test_k8s_client_token.py` | Create | k8s_client contextvar + client factory tests |
| `tests/test_errors.py` | Modify | Add 401/403 split tests |
| `tests/test_agent.py` | Modify | Add token threading tests |
| `tests/test_mcp_client.py` | Modify | Add Authorization header tests |
| `tests/test_view_executor.py` | Modify | Add token forwarding tests |
| `tests/test_token_forwarding_integration.py` | Create | End-to-end mock K8s API tests |

---

### Task 1: Config + Error Category Split

**Files:**
- Modify: `sre_agent/config.py:92-98`
- Modify: `sre_agent/errors.py:65-75`
- Modify: `tests/test_errors.py:31-33`

- [ ] **Step 1: Add `token_forwarding` setting to config**

In `sre_agent/config.py`, add after the `chain_min_frequency` field (line 100):

```python
    # Token forwarding
    token_forwarding: bool = True
```

- [ ] **Step 2: Write failing tests for 401/403 split**

In `tests/test_errors.py`, update the existing `test_401_permission` test and add a new one:

```python
    def test_401_unauthorized(self):
        err = classify_api_error(_make_api_error(401, "Unauthorized"))
        assert err.category == "unauthorized"
        assert err.status_code == 401
        assert "expired" in err.suggestions[0].lower() or "reconnect" in err.suggestions[0].lower()

    def test_403_forbidden(self):
        err = classify_api_error(_make_api_error(403, "Forbidden", "pods is forbidden"))
        assert err.category == "forbidden"
        assert err.status_code == 403

    def test_403_quota_still_quota(self):
        err = classify_api_error(_make_api_error(403, "Forbidden", "exceeded quota"))
        assert err.category == "quota"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_errors.py::TestClassifyApiError::test_401_unauthorized tests/test_errors.py::TestClassifyApiError::test_403_forbidden -v`

Expected: FAIL — `test_401_unauthorized` expects `"unauthorized"` but gets `"permission"`.

- [ ] **Step 4: Implement 401/403 split in errors.py**

In `sre_agent/errors.py`, replace the combined 401/403 block (lines 65-75):

```python
    if status == 401:
        return ToolError(
            message=msg,
            category="unauthorized",
            status_code=status,
            operation=operation,
            suggestions=[
                "Session expired — reconnect to refresh credentials",
            ],
        )

    if status == 403:
        return ToolError(
            message=msg,
            category="forbidden",
            status_code=status,
            operation=operation,
            suggestions=[
                "You don't have permission for this resource",
                "Check your RBAC role bindings in this namespace",
            ],
        )
```

Also update the docstring for `ToolError.category` (line 23):

```python
    category: str  # unauthorized, forbidden, not_found, conflict, validation, server, network, quota
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_errors.py -v`
Expected: All pass. The existing `test_403_quota` test still passes (quota check runs before the 403 check).

- [ ] **Step 6: Commit**

```bash
git add sre_agent/config.py sre_agent/errors.py tests/test_errors.py
git commit -m "feat: split 401/403 error categories, add token_forwarding config"
```

---

### Task 2: k8s_client.py — ContextVars + Cached ApiClient

**Files:**
- Modify: `sre_agent/k8s_client.py`
- Create: `tests/test_k8s_client_token.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_k8s_client_token.py`:

```python
"""Tests for user token forwarding in k8s_client."""

from unittest.mock import MagicMock, patch

import pytest


class TestUserTokenVar:
    def test_sa_singleton_when_no_token(self):
        """get_core_client returns cached SA singleton when no user token set."""
        from sre_agent.k8s_client import _user_token_var, get_core_client

        assert _user_token_var.get() is None
        with patch("sre_agent.k8s_client._load_k8s"):
            c1 = get_core_client()
            c2 = get_core_client()
            assert c1 is c2  # same singleton

    def test_user_token_returns_new_client(self):
        """get_core_client returns a different client when user token is set."""
        from sre_agent.k8s_client import _user_api_client_var, _user_token_var, get_core_client

        reset_tok = _user_token_var.set("test-bearer-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                user_client = get_core_client()
                sa_token = _user_token_var.set(None)
                sa_client = get_core_client()
                _user_token_var.reset(sa_token)
                assert user_client is not sa_client
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_api_client_cached_within_scope(self):
        """Multiple get_*_client calls share one ApiClient per token scope."""
        from sre_agent.k8s_client import (
            _user_api_client_var,
            _user_token_var,
            get_apps_client,
            get_core_client,
        )

        reset_tok = _user_token_var.set("test-bearer-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                core = get_core_client()
                apps = get_apps_client()
                assert core.api_client is apps.api_client  # shared ApiClient
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_reset_returns_to_sa(self):
        """After resetting contextvar, SA singleton is returned again."""
        from sre_agent.k8s_client import _user_api_client_var, _user_token_var, get_core_client

        with patch("sre_agent.k8s_client._load_k8s"):
            sa_before = get_core_client()
            reset_tok = _user_token_var.set("test-token")
            reset_cli = _user_api_client_var.set(None)
            _user_client = get_core_client()
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)
            sa_after = get_core_client()
            assert sa_before is sa_after

    def test_bearer_token_configured(self):
        """ApiClient created with user token has correct auth header."""
        from sre_agent.k8s_client import _get_user_api_client, _user_api_client_var

        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                api_client = _get_user_api_client("my-secret-token")
                assert api_client.configuration.api_key["authorization"] == "Bearer my-secret-token"
        finally:
            _user_api_client_var.reset(reset_cli)


class TestUserTokenContext:
    def test_context_manager_sets_and_resets(self):
        """user_token_context sets token for the block and resets after."""
        from sre_agent.k8s_client import _user_token_var, user_token_context

        assert _user_token_var.get() is None
        with user_token_context("ctx-token"):
            assert _user_token_var.get() == "ctx-token"
        assert _user_token_var.get() is None

    def test_context_manager_noop_when_none(self):
        """user_token_context with None does nothing."""
        from sre_agent.k8s_client import _user_token_var, user_token_context

        assert _user_token_var.get() is None
        with user_token_context(None):
            assert _user_token_var.get() is None

    def test_context_manager_resets_on_exception(self):
        """user_token_context resets even if the block raises."""
        from sre_agent.k8s_client import _user_token_var, user_token_context

        with pytest.raises(ValueError):
            with user_token_context("err-token"):
                assert _user_token_var.get() == "err-token"
                raise ValueError("boom")
        assert _user_token_var.get() is None

    def test_token_forwarding_disabled(self):
        """When token_forwarding=False, user_token_context is a noop."""
        from sre_agent.k8s_client import _user_token_var, user_token_context

        with patch("sre_agent.k8s_client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(token_forwarding=False)
            with user_token_context("should-be-ignored"):
                assert _user_token_var.get() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_k8s_client_token.py -v`
Expected: FAIL — `_user_token_var`, `_user_api_client_var`, `_get_user_api_client`, `user_token_context` don't exist.

- [ ] **Step 3: Implement k8s_client changes**

In `sre_agent/k8s_client.py`, add imports and contextvars after line 5:

```python
from contextlib import contextmanager
from contextvars import ContextVar
```

Add after line 25 (after `CONTAINER_RUNTIME_SOCKET = None`):

```python
_user_token_var: ContextVar[str | None] = ContextVar("_user_token", default=None)
_user_api_client_var: ContextVar[Any] = ContextVar("_user_api_client", default=None)


def _get_user_api_client(token: str) -> client.ApiClient:
    """Return a cached ApiClient for the current user token, or create one."""
    cached = _user_api_client_var.get()
    if cached is not None:
        return cached
    _load_k8s()
    cfg = client.Configuration.get_default_copy()
    cfg.api_key = {"authorization": f"Bearer {token}"}
    cfg.api_key_prefix = {}
    api_client = client.ApiClient(configuration=cfg)
    _user_api_client_var.set(api_client)
    return api_client


@contextmanager
def user_token_context(token: str | None):
    """Set user token contextvar for a block of K8s calls."""
    from .config import get_settings

    if not token or not get_settings().token_forwarding:
        yield
        return
    reset_tok = _user_token_var.set(token)
    reset_cli = _user_api_client_var.set(None)
    try:
        yield
    finally:
        _user_token_var.reset(reset_tok)
        _user_api_client_var.reset(reset_cli)
```

Update all 8 `get_*_client()` functions. Example for `get_core_client` (replace lines 67-71):

```python
def get_core_client() -> client.CoreV1Api:
    token = _user_token_var.get()
    if token:
        return client.CoreV1Api(api_client=_get_user_api_client(token))
    _load_k8s()
    if "core" not in _clients:
        _clients["core"] = client.CoreV1Api()
    return _clients["core"]
```

Apply the same pattern to `get_apps_client`, `get_custom_client`, `get_version_client`, `get_rbac_client`, `get_networking_client`, `get_batch_client`, `get_autoscaling_client` — each gets a 3-line `if token:` block at the top returning a new `*Api(api_client=_get_user_api_client(token))`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_k8s_client_token.py -v`
Expected: All pass.

- [ ] **Step 5: Run full test suite for regressions**

Run: `python3 -m pytest tests/ -v`
Expected: All existing tests still pass — SA singleton path unchanged.

- [ ] **Step 6: Commit**

```bash
git add sre_agent/k8s_client.py tests/test_k8s_client_token.py
git commit -m "feat: add user token contextvars and cached ApiClient to k8s_client"
```

---

### Task 3: agent.py — Thread Token Through Execution Chain

**Files:**
- Modify: `sre_agent/agent.py:291-388, 390-700`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_agent.py`:

```python
class TestTokenForwarding:
    def test_execute_tool_sets_contextvar(self):
        """_execute_tool sets _user_token_var before calling tool and resets after."""
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        captured_token = []

        def capture_tool(**kwargs):
            captured_token.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = lambda inp: capture_tool(**inp)
        tool_map = {"test_tool": mock_tool}

        _execute_tool("test_tool", {}, tool_map, user_token="my-token")
        assert captured_token == ["my-token"]
        assert _user_token_var.get() is None  # reset after

    def test_execute_tool_resets_on_exception(self):
        """_execute_tool resets contextvar even when tool raises."""
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        mock_tool = MagicMock()
        mock_tool.call.side_effect = RuntimeError("boom")
        tool_map = {"bad_tool": mock_tool}

        _execute_tool("bad_tool", {}, tool_map, user_token="leaked?")
        assert _user_token_var.get() is None  # must be reset

    def test_execute_tool_no_token(self):
        """_execute_tool with no user_token leaves contextvar as None."""
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.k8s_client import _user_token_var

        captured = []

        def capture(**kw):
            captured.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = lambda inp: capture(**inp)
        tool_map = {"t": mock_tool}

        _execute_tool("t", {}, tool_map, user_token=None)
        assert captured == [None]

    def test_exec_meta_has_status_code_on_error(self):
        """exec_meta includes status_code when tool returns a ToolError."""
        from unittest.mock import MagicMock

        from sre_agent.agent import _execute_tool
        from sre_agent.errors import ToolError

        mock_tool = MagicMock()
        mock_tool.call.side_effect = Exception("fail")
        tool_map = {"err_tool": mock_tool}

        _text, _component, meta = _execute_tool("err_tool", {}, tool_map)
        assert meta["status"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent.py::TestTokenForwarding -v`
Expected: FAIL — `_execute_tool` doesn't accept `user_token` param.

- [ ] **Step 3: Implement agent.py changes**

Add `user_token` parameter to `_execute_tool` (line 291):

```python
def _execute_tool(name: str, input_data: dict, tool_map: dict, user_token: str | None = None) -> tuple[str, dict | None, dict]:
```

At the top of the function body, before the existing `tool = tool_map.get(name)` line, add contextvar management:

```python
    from .k8s_client import _user_api_client_var, _user_token_var

    reset_token = _user_token_var.set(user_token)
    reset_client = _user_api_client_var.set(None)
```

Wrap the entire existing function body (from `tool = tool_map.get(name)` to the end) in a `try/finally`:

```python
    try:
        tool = tool_map.get(name)
        # ... existing body unchanged ...
    finally:
        _user_token_var.reset(reset_token)
        _user_api_client_var.reset(reset_client)
```

Add `user_token` parameter to `_execute_tool_with_timeout` (line 357):

```python
def _execute_tool_with_timeout(
    name: str, input_data: dict, tool_map: dict, timeout: int | None = None, user_token: str | None = None
) -> tuple[str, dict | None, dict]:
```

Update the `_tool_pool.submit` call inside it:

```python
    future = _tool_pool.submit(_execute_tool, name, input_data, tool_map, user_token)
```

Add `user_token` parameter to `run_agent_streaming` (line 390):

```python
def run_agent_streaming(
    client,
    messages: list[dict],
    system_prompt: str | list[dict[str, Any]],
    tool_defs: list,
    tool_map: dict,
    write_tools: set[str] | None = None,
    on_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_confirm=None,
    on_component=None,
    on_tool_result=None,
    on_usage=None,
    mode: str = "sre",
    thinking: dict | None = None,
    user_token: str | None = None,
) -> str:
```

Update the two places in the tool execution loop where `_execute_tool` is submitted to the pool:

Read tools (parallel, ~line 600):
```python
futures = {_tool_pool.submit(_execute_tool, b.name, b.input, tool_map, user_token): b for b in read_blocks}
```

Write tools (sequential, ~line 660):
```python
text, component, exec_meta = _execute_tool_with_timeout(block.name, block.input, tool_map, user_token=user_token)
```

Add `user_token` to `run_agent_turn_streaming` (line 703) and pass it through:

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
    user_token: str | None = None,
) -> str:
```

And in its `return run_agent_streaming(...)` call, add `user_token=user_token`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agent.py -v`
Expected: All pass (new and existing).

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass — `user_token=None` default preserves existing behavior everywhere.

- [ ] **Step 6: Commit**

```bash
git add sre_agent/agent.py tests/test_agent.py
git commit -m "feat: thread user_token through agent execution chain"
```

---

### Task 4: MCP Token Forwarding

**Files:**
- Modify: `sre_agent/mcp_client.py:1-10, 181-200`
- Modify: `tests/test_mcp_client.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_mcp_client.py`:

```python
class TestMcpTokenForwarding:
    def test_mcp_post_includes_auth_header_when_token_set(self):
        """_mcp_post adds Authorization header when user token contextvar is set."""
        from unittest.mock import patch

        from sre_agent.k8s_client import _user_api_client_var, _user_token_var

        reset_tok = _user_token_var.set("user-oauth-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.mcp_client.urllib.request.urlopen") as mock_urlopen:
                mock_resp = mock_urlopen.return_value.__enter__.return_value
                mock_resp.headers = {}
                mock_resp.read.return_value = b'{"result": {}}'

                from sre_agent.mcp_client import _mcp_post

                _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 1})
                call_args = mock_urlopen.call_args
                req = call_args[0][0]
                assert req.get_header("Authorization") == "Bearer user-oauth-token"
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_mcp_post_no_auth_header_when_no_token(self):
        """_mcp_post omits Authorization header when no user token."""
        from unittest.mock import patch

        from sre_agent.k8s_client import _user_token_var

        assert _user_token_var.get() is None
        with patch("sre_agent.mcp_client.urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock_urlopen.return_value.__enter__.return_value
            mock_resp.headers = {}
            mock_resp.read.return_value = b'{"result": {}}'

            from sre_agent.mcp_client import _mcp_post

            _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 1})
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert not req.has_header("Authorization")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_client.py::TestMcpTokenForwarding -v`
Expected: FAIL — `_mcp_post` doesn't read the contextvar or set Authorization.

- [ ] **Step 3: Implement MCP changes**

In `sre_agent/mcp_client.py`, add import at module level (after line 17):

```python
from .k8s_client import _user_token_var
```

In `_mcp_post()` (line 181), add the Authorization header after the existing headers dict:

```python
def _mcp_post(base_url: str, payload: dict, session_id: str = "") -> tuple[dict, str]:
    """POST to MCP /mcp endpoint with SSE headers. Returns (response_dict, session_id)."""
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = _user_token_var.get()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
```

The rest of the function stays the same.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_mcp_client.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add sre_agent/mcp_client.py tests/test_mcp_client.py
git commit -m "feat: forward user token as Authorization header in MCP calls"
```

---

### Task 5: View Executor Token Forwarding

**Files:**
- Modify: `sre_agent/view_executor.py:82-100, 144-218`
- Modify: `tests/test_view_executor.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_view_executor.py`:

```python
class TestTokenForwarding:
    def test_execute_tool_widget_sets_contextvar(self):
        """_execute_tool_widget sets user token contextvar before calling tool."""
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_token_var

        captured = []

        def mock_call(args):
            captured.append(_user_token_var.get())
            return ("result", {"kind": "info_card_grid", "props": {}})

        mock_tool = MagicMock()
        mock_tool.call = mock_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            from sre_agent.view_executor import _execute_tool_widget

            widget = {"tool": "test_tool", "args": {}, "kind": "info_card_grid", "title": "Test"}
            _execute_tool_widget(widget, item_id="test", user_token="view-token")

        assert captured == ["view-token"]
        assert _user_token_var.get() is None  # reset after

    def test_execute_tool_widget_no_token(self):
        """_execute_tool_widget with no token uses SA (contextvar stays None)."""
        from unittest.mock import MagicMock, patch

        from sre_agent.k8s_client import _user_token_var

        captured = []

        def mock_call(args):
            captured.append(_user_token_var.get())
            return "result"

        mock_tool = MagicMock()
        mock_tool.call = mock_call

        with patch("sre_agent.view_executor._resolve_tool", return_value=mock_tool):
            from sre_agent.view_executor import _execute_tool_widget

            widget = {"tool": "test_tool", "args": {}, "kind": "info_card_grid", "title": "Test"}
            _execute_tool_widget(widget, item_id="test")

        assert captured == [None]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_view_executor.py::TestTokenForwarding -v`
Expected: FAIL — `_execute_tool_widget` doesn't accept `user_token`.

- [ ] **Step 3: Implement view_executor changes**

In `sre_agent/view_executor.py`, add import after line 11:

```python
from .k8s_client import _user_api_client_var, _user_token_var
```

Update `_execute_tool_widget` signature (line 82) to accept `user_token`:

```python
def _execute_tool_widget(widget: dict[str, Any], item_id: str = "", user_token: str | None = None) -> dict[str, Any] | None:
```

Wrap the tool call at line 100 with contextvar management:

```python
    reset_tok = _user_token_var.set(user_token)
    reset_cli = _user_api_client_var.set(None)
    try:
        result = tool_obj.call(args)
    finally:
        _user_token_var.reset(reset_tok)
        _user_api_client_var.reset(reset_cli)
```

Update `execute_view_plan` signature (line 144) to accept `user_token`:

```python
def execute_view_plan(view_plan: list[dict[str, Any]], item: dict[str, Any], user_token: str | None = None) -> list[dict[str, Any]]:
```

Update the `_executor.submit` call (line 193) to pass `user_token`:

```python
        future = _executor.submit(_execute_tool_widget, widget, item_id, user_token)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_executor.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add sre_agent/view_executor.py tests/test_view_executor.py
git commit -m "feat: forward user token through view executor widget calls"
```

---

### Task 6: WebSocket Layer — Extract + Thread Token

**Files:**
- Modify: `sre_agent/api/auth.py`
- Modify: `sre_agent/api/agent_ws.py`
- Modify: `sre_agent/api/ws_endpoints.py`

- [ ] **Step 1: Add `get_user_token` dependency to auth.py**

In `sre_agent/api/auth.py`, add after the `get_owner` function (after line 136):

```python
def get_user_token(
    x_forwarded_access_token: str | None = Header(None, alias="X-Forwarded-Access-Token"),
) -> str | None:
    """FastAPI dependency — extracts user OAuth token from proxy header. Returns None if absent."""
    from ..config import get_settings

    if not get_settings().token_forwarding:
        return None
    return x_forwarded_access_token or None
```

- [ ] **Step 2: Thread token through agent_ws.py**

In `sre_agent/api/agent_ws.py`, update `SkillExecutor.__init__` to accept `user_token`:

Find the `__init__` method and add `user_token: str | None = None` to its parameters. Store it as `self._user_token = user_token`.

In `SkillExecutor.run()`, where it calls `run_agent_streaming(...)`, add `user_token=self._user_token`.

In `_run_agent_ws()`, add `user_token: str | None = None` parameter. Pass it to `SkillExecutor(user_token=user_token)`.

Add 401 detection in the `on_tool_result` callback inside `_run_agent_ws`. After the existing callback body, add:

```python
if result_meta.get("status_code") == 401:
    _schedule_send({"type": "session_expired", "reason": "K8s API returned 401 — token may have expired"})
```

- [ ] **Step 3: Thread token through ws_endpoints.py**

In `sre_agent/api/ws_endpoints.py`, at the WebSocket connect point for both `websocket_agent` and `websocket_auto_agent`, extract the token:

```python
user_token = websocket.headers.get("x-forwarded-access-token") if get_settings().token_forwarding else None
```

Pass `user_token=user_token` to all calls to `_run_agent_ws()`.

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass — WS tests don't send the header, so `user_token=None` flows through.

- [ ] **Step 5: Commit**

```bash
git add sre_agent/api/auth.py sre_agent/api/agent_ws.py sre_agent/api/ws_endpoints.py
git commit -m "feat: extract and thread user token through WebSocket layer"
```

---

### Task 7: REST Endpoints — Views + Topology

**Files:**
- Modify: `sre_agent/api/views.py:401-459, 531-549`
- Modify: `sre_agent/api/topology_rest.py:24-31, 273-276, 355-360`

- [ ] **Step 1: Update views.py action execution endpoint**

In `sre_agent/api/views.py`, update `rest_execute_action` (line 402):

Add `get_user_token` import and dependency:

```python
from .auth import get_owner, get_user_token
```

```python
@router.post("/views/{view_id}/actions")
async def rest_execute_action(
    view_id: str,
    request: Request,
    owner: str = Depends(get_owner),
    user_token: str | None = Depends(get_user_token),
):
```

Update the `_execute_tool` call (line 459):

```python
    text, component, meta = await asyncio.to_thread(_execute_tool, action, action_input, tool_map, user_token)
```

- [ ] **Step 2: Update views.py claim endpoint**

In `rest_claim_view` (line 532), add token dependency and pass to `execute_view_plan`:

```python
@router.post("/views/{view_id}/claim")
async def rest_claim_view(
    view_id: str,
    owner: str = Depends(get_owner),
    user_token: str | None = Depends(get_user_token),
):
```

Where `execute_view_plan` is called (if it's called during claim — check the actual code path), pass `user_token=user_token`.

Note: The current `rest_claim_view` only calls `db.claim_view()`. The `execute_view_plan` is called elsewhere when the view plan is executed. Trace the actual call site and add `user_token` there.

- [ ] **Step 3: Update topology_rest.py endpoints**

In `sre_agent/api/topology_rest.py`, add imports:

```python
from ..k8s_client import user_token_context
from .auth import get_user_token
```

Update `get_topology` (line 24):

```python
async def get_topology(
    # ... existing params ...
    user_token: str | None = Depends(get_user_token),
    _auth=Depends(verify_token),
):
```

Wrap the `get_dependency_graph()` calls with `user_token_context`:

```python
    with user_token_context(user_token):
        graph = await asyncio.to_thread(...)
```

Apply the same pattern to `get_blast_radius` (line 273) and `get_finding_impact` (line 355).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_view_action_endpoint.py tests/test_view_lifecycle_api.py tests/test_ws_contract.py -v`
Expected: All pass — test requests don't include the header, so `user_token=None`.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add sre_agent/api/views.py sre_agent/api/topology_rest.py
git commit -m "feat: forward user token in REST endpoints (views, topology)"
```

---

### Task 8: Integration Tests

**Files:**
- Create: `tests/test_token_forwarding_integration.py`

- [ ] **Step 1: Write integration tests**

Create `tests/test_token_forwarding_integration.py`:

```python
"""Integration tests for user token forwarding end-to-end."""

from unittest.mock import MagicMock, patch

from sre_agent.k8s_client import _user_api_client_var, _user_token_var, user_token_context


class TestEndToEndTokenFlow:
    def test_execute_tool_with_token_creates_user_client(self):
        """Full flow: _execute_tool sets contextvar, get_core_client returns user client."""
        from sre_agent.agent import _execute_tool

        captured_clients = []

        def fake_tool(**kwargs):
            from sre_agent.k8s_client import get_core_client

            with patch("sre_agent.k8s_client._load_k8s"):
                c = get_core_client()
                captured_clients.append(c)
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = lambda inp: fake_tool(**inp)
        tool_map = {"test": mock_tool}

        _execute_tool("test", {}, tool_map, user_token="integration-token")

        assert len(captured_clients) == 1
        api_client = captured_clients[0].api_client
        assert api_client.configuration.api_key["authorization"] == "Bearer integration-token"

    def test_execute_tool_without_token_uses_sa(self):
        """Without user token, get_core_client returns SA singleton."""
        from sre_agent.agent import _execute_tool

        captured_tokens = []

        def fake_tool(**kwargs):
            captured_tokens.append(_user_token_var.get())
            return "ok"

        mock_tool = MagicMock()
        mock_tool.call.side_effect = lambda inp: fake_tool(**inp)
        tool_map = {"test": mock_tool}

        _execute_tool("test", {}, tool_map, user_token=None)

        assert captured_tokens == [None]

    def test_monitor_path_never_gets_token(self):
        """Simulate monitor calling get_core_client — no contextvar set."""
        assert _user_token_var.get() is None
        with patch("sre_agent.k8s_client._load_k8s"):
            from sre_agent.k8s_client import get_core_client

            c1 = get_core_client()
            c2 = get_core_client()
            assert c1 is c2  # SA singleton

    def test_concurrent_users_isolated(self):
        """Two concurrent tool calls with different tokens don't cross-contaminate."""
        import concurrent.futures

        from sre_agent.agent import _execute_tool

        results = {}

        def capture_token(name, **kwargs):
            results[name] = _user_token_var.get()
            return "ok"

        def make_tool(name):
            mock = MagicMock()
            mock.call.side_effect = lambda inp: capture_token(name, **inp)
            return mock

        tool_a = make_tool("a")
        tool_b = make_tool("b")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_execute_tool, "a", {}, {"a": tool_a}, "token-alice")
            f2 = pool.submit(_execute_tool, "b", {}, {"b": tool_b}, "token-bob")
            f1.result()
            f2.result()

        assert results["a"] == "token-alice"
        assert results["b"] == "token-bob"
        assert _user_token_var.get() is None

    def test_user_token_context_with_dependency_graph(self):
        """user_token_context wraps non-tool K8s calls (topology path)."""
        captured = []

        with patch("sre_agent.k8s_client._load_k8s"):
            with user_token_context("topo-token"):
                from sre_agent.k8s_client import get_core_client

                c = get_core_client()
                captured.append(c.api_client.configuration.api_key.get("authorization"))

        assert captured == ["Bearer topo-token"]
        assert _user_token_var.get() is None
```

- [ ] **Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_token_forwarding_integration.py -v`
Expected: All pass.

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_token_forwarding_integration.py
git commit -m "test: add token forwarding integration tests"
```

---

### Task 9: Documentation Updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-04-22-user-token-forwarding-design.md`

- [ ] **Step 1: Update CLAUDE.md**

Add `token_forwarding` to the Environment Variables table:

```markdown
| `PULSE_AGENT_TOKEN_FORWARDING` | Forward user OAuth token to K8s API | `true` |
```

Add to the Security section:

```markdown
- Token forwarding: user OAuth token from `X-Forwarded-Access-Token` header forwarded to K8s API calls (reads and writes) for RBAC enforcement. Monitor scans use SA token. Configurable via `PULSE_AGENT_TOKEN_FORWARDING`.
```

Update the project description line to mention token forwarding.

- [ ] **Step 2: Update spec status**

In `docs/superpowers/specs/2026-04-22-user-token-forwarding-design.md`, change:

```markdown
**Status:** Draft
```

to:

```markdown
**Status:** Implemented
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-04-22-user-token-forwarding-design.md
git commit -m "docs: update CLAUDE.md and spec for token forwarding"
```

---

## Task Dependency Graph

```
Task 1 (config + errors) ─┐
                           ├─→ Task 3 (agent.py) ─→ Task 6 (WS layer) ─→ Task 7 (REST endpoints)
Task 2 (k8s_client)  ──────┤                                                      │
                           ├─→ Task 4 (MCP)                                        │
                           ├─→ Task 5 (view executor)                              │
                           └───────────────────────────────────────────────→ Task 8 (integration tests)
                                                                                   │
                                                                           Task 9 (docs)
```

Tasks 1 and 2 can run in parallel. Tasks 3-5 depend on Task 2 and can run in parallel. Task 6 depends on Task 3. Task 7 depends on Tasks 5+6. Task 8 depends on all implementation tasks. Task 9 is last.
