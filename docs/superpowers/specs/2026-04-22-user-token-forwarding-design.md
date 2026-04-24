# User OAuth Token Forwarding

**Date:** 2026-04-22
**Status:** Implemented
**Author:** Ali + Claude

## Problem

Pulse Agent executes all K8s API calls using its ServiceAccount token. Users see resources they shouldn't have access to, and write operations execute under the agent's identity rather than the user's. This bypasses RBAC enforcement and breaks audit trail attribution.

## Goal

When a user is connected via WebSocket or REST API, all K8s API calls (reads and writes) execute using the user's OAuth token. The K8s API server enforces RBAC — the agent never does authorization itself. Monitor background scans continue using the ServiceAccount token.

## Architecture Decision

**Direct token forwarding** — the same pattern used by OpenShift Console and Kubernetes Dashboard. The user's OAuth token is forwarded to the K8s API server as `Authorization: Bearer <token>`. The API server validates the token and enforces RBAC. No impersonation, no SubjectAccessReviews.

Why not impersonation: impersonation requires explicit group forwarding. OpenShift RBAC is heavily group-based (`dedicated-admins`, project groups). Missing a group silently drops permissions. The user's real token carries all identity (user, groups, scopes) natively.

## Token Source

The oauth-proxy sidecar handles the OpenShift OAuth flow and passes the user's access token to the backend via `X-Forwarded-Access-Token` HTTP header on every request, including the WebSocket upgrade. The frontend never touches the token directly — it lives in an HTTP-only cookie managed by the proxy.

The token is available at WebSocket handshake time. `ws_endpoints.py` already reads this header (lines 99-100) for `_get_current_user()`. For REST endpoints, the same header is available on every HTTP request.

## Configuration

New setting in `PulseAgentSettings`:

```python
token_forwarding: bool = True  # PULSE_AGENT_TOKEN_FORWARDING
```

When `False`, all K8s calls use the SA token (current behavior). When `True` and a user token is present, K8s calls use the user's token. Defaults to `True` in production. Useful for local dev (no oauth-proxy) and testing.

## Design

### 1. Token Flow

The token is an explicit named parameter at every layer:

```
Browser
  → oauth-proxy (cookie → X-Forwarded-Access-Token header)
  → nginx (forwards header)
  → ws_endpoints.py: extract from websocket.headers at connect time
  → session_state["user_token"]
  → _run_agent_ws(user_token=token)
  → SkillExecutor.__init__(user_token=token)
  → run_agent_streaming(user_token=token)
  → _execute_tool(user_token=token)       # threaded pool
  → _execute_tool_with_timeout(user_token=token)
```

At the `tool.call()` boundary, `_execute_tool` sets a `ContextVar` before invoking the tool and resets it in a `finally` block. This is the only point where implicit state is used — the `@beta_tool` function signature is fixed by Claude's tool schema and cannot accept extra parameters.

```python
# agent.py — _execute_tool
def _execute_tool(name, input_data, tool_map, user_token=None):
    from .k8s_client import _user_token_var

    reset_token = _user_token_var.set(user_token)
    try:
        result = tool.call(input_data)
    finally:
        _user_token_var.reset(reset_token)
```

### 2. k8s_client.py Changes

Add a `ContextVar` that stores a cached `ApiClient` (not just the token string) to avoid creating multiple clients per tool call:

```python
from contextvars import ContextVar

_user_token_var: ContextVar[str | None] = ContextVar("_user_token", default=None)
_user_api_client_var: ContextVar[client.ApiClient | None] = ContextVar(
    "_user_api_client", default=None
)

def _get_user_api_client(token: str) -> client.ApiClient:
    """Return a cached ApiClient for the current token, or create one."""
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
```

Each `get_*_client()` function checks the contextvar:

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

When `_user_token_var` is `None` (default), the SA singleton is returned — identical to current behavior. When set, all `get_*_client()` calls within the same tool invocation share a single `ApiClient` instance, avoiding connection pool exhaustion.

All 8 `get_*_client()` functions follow this pattern: `get_core_client`, `get_apps_client`, `get_custom_client`, `get_version_client`, `get_rbac_client`, `get_networking_client`, `get_batch_client`, `get_autoscaling_client`.

The `_execute_tool` bridge sets both contextvars and resets both in `finally`:

```python
def _execute_tool(name, input_data, tool_map, user_token=None):
    from .k8s_client import _user_api_client_var, _user_token_var

    reset_token = _user_token_var.set(user_token)
    reset_client = _user_api_client_var.set(None)  # fresh per tool call
    try:
        result = tool.call(input_data)
    finally:
        _user_token_var.reset(reset_token)
        _user_api_client_var.reset(reset_client)
```

### 3. agent.py Changes

New `user_token` parameter on:

- `run_agent_streaming()` — passed from `SkillExecutor`
- `_execute_tool()` — sets/resets contextvar around `tool.call()`
- `_execute_tool_with_timeout()` — passes through to `_execute_tool`

In the tool execution loop, the token is passed to pool submissions:

```python
# Read tools (parallel)
futures = {
    _tool_pool.submit(_execute_tool, b.name, b.input, tool_map, user_token): b
    for b in read_blocks
}

# Write tools (sequential, after confirmation)
text, component, exec_meta = _execute_tool_with_timeout(
    block.name, block.input, tool_map, user_token=user_token
)
```

### 4. WebSocket Layer Changes

**ws_endpoints.py**: Extract token at connect time, store on session state, pass to `_run_agent_ws`:

```python
user_token = websocket.headers.get("x-forwarded-access-token")
# ... later
await _run_agent_ws(websocket, ..., user_token=user_token)
```

**agent_ws.py**: `_run_agent_ws` passes token to `SkillExecutor`, which passes it to `run_agent_streaming`:

```python
class SkillExecutor:
    def __init__(self, ..., user_token: str | None = None):
        self._user_token = user_token

    def run(self):
        run_agent_streaming(..., user_token=self._user_token)
```

### 5. REST API Token Forwarding

REST endpoints that make K8s API calls must also extract the user token from HTTP headers and set the contextvar before calling K8s client functions.

Add a FastAPI dependency that extracts the token:

```python
# api/auth.py
def get_user_token(request: Request) -> str | None:
    return request.headers.get("x-forwarded-access-token")
```

**Affected REST endpoints:**

| Endpoint | File | Why |
|----------|------|-----|
| `POST /views/{id}/actions` | `api/views.py` | Executes tools via `_execute_tool` — action buttons must run as user |
| `GET /topology` | `api/topology_rest.py` | Calls `get_dependency_graph()` which reads K8s resources |
| `GET /incidents/{id}/impact` | `api/topology_rest.py` | Blast radius reads K8s resources |
| `GET /views/query` | `api/views.py` | PromQL proxy (lower priority — Prometheus not K8s RBAC) |

Each endpoint extracts the token and wraps K8s calls with the contextvar:

```python
@router.post("/views/{view_id}/actions")
async def execute_action(view_id: str, ..., user_token: str | None = Depends(get_user_token)):
    # Pass user_token to _execute_tool
    text, component, meta = await asyncio.to_thread(
        _execute_tool, action, action_input, tool_map, user_token
    )
```

For non-tool REST endpoints that call `get_*_client()` directly (topology, impact), use a context manager:

```python
# k8s_client.py
@contextmanager
def user_token_context(token: str | None):
    """Set user token contextvar for a block of K8s calls."""
    if not token:
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

```python
@router.get("/topology")
async def get_topology(..., user_token: str | None = Depends(get_user_token)):
    with user_token_context(user_token):
        graph = await asyncio.to_thread(get_dependency_graph().to_dict, ...)
```

### 6. View Executor Token Forwarding

`view_executor.py` calls tools directly in its own `ThreadPoolExecutor`, bypassing `_execute_tool()`. It must set the contextvar before tool calls.

The view executor is triggered when a user claims a view (`POST /views/{id}/claim`). The user token is available from the HTTP request header.

```python
# view_executor.py
from .k8s_client import _user_api_client_var, _user_token_var

def _execute_widget(tool_obj, args, user_token=None):
    reset_tok = _user_token_var.set(user_token)
    reset_cli = _user_api_client_var.set(None)
    try:
        return tool_obj.call(args)
    finally:
        _user_token_var.reset(reset_tok)
        _user_api_client_var.reset(reset_cli)
```

`execute_view_plan()` receives `user_token` from the claim endpoint and passes it to each widget execution.

### 7. MCP Token Forwarding

MCP tools execute via `call_mcp_tool()` → `_mcp_post()`. The contextvar is already set by `_execute_tool` before the MCP tool wrapper runs.

`_mcp_post()` reads the contextvar and adds the `Authorization` header:

```python
from .k8s_client import _user_token_var

def _mcp_post(base_url, payload, session_id=""):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = _user_token_var.get()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    # ... rest unchanged
```

Import `_user_token_var` at module level (not inside the function).

**MCP server fork change required** (separate PR): The openshift-mcp-server must check for an incoming `Authorization` header and use that token for K8s API calls instead of its SA. When no header is present, fall back to SA (backward compatible).

**MCP stdio transport limitation:** The stdio transport pipes JSON-RPC directly — no HTTP headers. Token forwarding only works with SSE transport. Document this limitation; stdio is dev-only and not used in production deployments.

**Rollout strategy:** The agent should send the `Authorization` header unconditionally when a user token is present. If the MCP server doesn't support it yet, the header is ignored (standard HTTP behavior). No version gating needed.

### 8. Cluster Context (Harness) Scoping

`harness.py` `gather_cluster_context()` pre-fetches node count, namespaces, and OCP version for the system prompt. This runs once per agent turn via `run_agent_streaming()`, before any tool calls, using the SA token.

This is intentional — the cluster context is informational metadata used by Claude to reason about the cluster. It does not expose sensitive resource data. The user's RBAC is enforced at the tool call level, where actual resource data is fetched.

No changes to harness.py. The system prompt may reference namespaces the user can't access, but tool calls to those namespaces will return 403 errors, which the agent will report to the user.

### 9. Monitor Isolation

`MonitorSession` never receives a `user_token` parameter. The contextvar default is `None`. All monitor paths — scanners, `auto_fix()`, `execute_targeted_fix()` in `fix_planner.py`, inbox generators — call `get_*_client()` which returns the SA singleton.

No code changes to `monitor/session.py`, `monitor/scanners.py`, `fix_planner.py`, `trend_scanners.py`, or `inbox_generators.py`.

### 10. 401 Handling (Token Expiry)

OpenShift OAuth tokens default to 24h. The oauth-proxy refreshes cookies hourly (`--cookie-refresh=1h`), but the WebSocket upgrade headers are fixed at connect time. The token may expire mid-session.

**Distinguishing 401 from 403:** Update `classify_api_error()` in `errors.py` to split the `"permission"` category:

```python
if status == 401:
    return ToolError(
        message=msg,
        category="unauthorized",
        status_code=status,
        suggestion="Session expired — reconnect to refresh credentials",
    )
if status == 403:
    return ToolError(
        message=msg,
        category="forbidden",
        status_code=status,
        suggestion="You don't have permission for this resource",
    )
```

**Propagating status_code through exec_meta:** `_execute_tool` includes `status_code` in `exec_meta` when the tool result is a `ToolError`:

```python
meta = {
    "status": "error",
    "error_message": error_message,
    "error_category": err.category,
    "status_code": getattr(err, "status_code", None),
    "result_bytes": 0,
}
```

**401 detection flow:**

1. K8s API returns 401
2. `safe()` catches `ApiException`, `classify_api_error()` returns `ToolError` with `category="unauthorized"`
3. Tool returns the error string to `_execute_tool`
4. `_execute_tool` records `status_code: 401` in `exec_meta`
5. `on_tool_result` callback fires with `exec_meta` containing `status_code=401`
6. WS handler (`_run_agent_ws`) checks `on_tool_result` for `status_code=401`, emits `session_expired` WebSocket event
7. Frontend (Shell.tsx) shows countdown modal and redirects to re-auth

**Mid-turn 401:** If a 401 occurs during a multi-tool turn (e.g., 3 of 4 parallel reads succeed, 1 returns 401), the `on_tool_result` callback emits `session_expired` immediately. The agent turn continues with mixed results — the LLM will see the error and likely report it. On the next turn, the WS handler blocks further tool execution and prompts reconnection.

**403 handling:** Normal operation — the user lacks RBAC for that resource. The tool returns an error, the agent explains the permission gap. No session expiry.

### 11. CLI Path

The CLI (`main.py`) calls `run_agent_turn_streaming` which delegates to `run_agent_streaming` with `user_token=None`. CLI uses kubeconfig auth (local dev) or SA (in-cluster). No oauth-proxy, no user token. This is correct — CLI is single-user and already authenticates via kubeconfig.

### 12. Security

**Token never logged:** The token is a function parameter, not part of tool `input_data`. It never passes through `_redact_input()`. Verify that no `logger.*` call in the token forwarding path includes the token value. The contextvar stores the token but contextvars are not serialized or logged.

**Token never stored:** The token exists only as a function parameter and a scoped contextvar. It is not persisted to the database, not written to disk, not stored in any long-lived data structure. The cached `ApiClient` on `_user_api_client_var` is reset after every tool call.

**Token never sent to Claude:** The token is used exclusively in K8s API calls and MCP HTTP requests. It is not included in tool results, system prompts, or conversation messages.

**Token never in error messages:** `_execute_tool` catches exceptions and returns only `type(e).__name__` to the LLM. Internal error details are logged (without the token) but not exposed.

**Contextvar scoping:** The `finally` block in `_execute_tool` guarantees both `_user_token_var` and `_user_api_client_var` are reset after every tool call, even on exceptions. Thread pool worker reuse is safe — `ContextVar.reset()` restores the previous value (`None`).

**No cross-session contamination:** Each WebSocket connection has its own `user_token` parameter. The contextvar is set and reset within a single `_execute_tool` invocation. Two concurrent users cannot see each other's tokens.

## What This Does NOT Cover

- Scanner transparency (exposing scanner metadata and results to users) — separate spec
- New scanners (resource quota exhaustion, endpoint health) — separate spec
- MCP server fork changes (accepting and using forwarded tokens) — separate PR
- Frontend changes — none needed (token already forwarded by oauth-proxy)

## Files Changed

| File | Change |
|------|--------|
| `sre_agent/k8s_client.py` | Add `_user_token_var` + `_user_api_client_var` contextvars, `_get_user_api_client()`, `user_token_context()` context manager, update 8 `get_*_client()` functions |
| `sre_agent/agent.py` | Add `user_token` param to `run_agent_streaming`, `_execute_tool`, `_execute_tool_with_timeout`; set/reset both contextvars in `_execute_tool`; propagate `status_code` in `exec_meta` |
| `sre_agent/errors.py` | Split `"permission"` category into `"unauthorized"` (401) and `"forbidden"` (403); add `status_code` to `ToolError` |
| `sre_agent/mcp_client.py` | Import `_user_token_var` at module level, read in `_mcp_post()`, add `Authorization` header |
| `sre_agent/api/auth.py` | Add `get_user_token()` FastAPI dependency |
| `sre_agent/api/agent_ws.py` | Add `user_token` param to `SkillExecutor.__init__`, `_run_agent_ws`; detect 401 in `on_tool_result`, emit `session_expired` event |
| `sre_agent/api/ws_endpoints.py` | Extract `X-Forwarded-Access-Token` at connect time, pass to `_run_agent_ws` |
| `sre_agent/api/views.py` | Add `user_token` dependency to action execution, view claim endpoints; pass to `_execute_tool` and `execute_view_plan` |
| `sre_agent/api/topology_rest.py` | Add `user_token` dependency, wrap `get_dependency_graph()` calls in `user_token_context()` |
| `sre_agent/view_executor.py` | Accept `user_token` in `execute_view_plan()`, set contextvar around each widget tool call |
| `sre_agent/config.py` | Add `token_forwarding: bool = True` setting |

**Files NOT changed:**
- `sre_agent/monitor/session.py` — no changes
- `sre_agent/monitor/scanners.py` — no changes
- `sre_agent/monitor/fix_planner.py` — no changes
- `sre_agent/trend_scanners.py` — no changes
- `sre_agent/inbox_generators.py` — no changes
- `sre_agent/harness.py` — no changes (SA for cluster context is intentional)
- `sre_agent/k8s_tools/*` — no changes (0 of 60 call sites modified)
- `sre_agent/main.py` — no changes (CLI uses kubeconfig, no user token)
- Frontend (OpenshiftPulse) — no changes

## Testing

**k8s_client.py unit tests:**
- `get_core_client()` returns SA singleton when contextvar unset
- `get_core_client()` returns user-token client when contextvar set
- Multiple `get_*_client()` calls within one contextvar scope share the same `ApiClient`
- After `reset()`, SA singleton is returned (thread reuse safety)
- `user_token_context()` context manager sets and resets correctly

**agent.py unit tests:**
- Both contextvars set before `tool.call()` and reset after
- Both contextvars reset even when `tool.call()` raises
- `user_token=None` results in SA client (monitor path)
- `exec_meta` includes `status_code` for 401 and 403 errors

**errors.py unit tests:**
- 401 → `category="unauthorized"`, `status_code=401`
- 403 → `category="forbidden"`, `status_code=403`
- Other status codes unchanged

**MCP unit tests:**
- `_mcp_post()` includes `Authorization` header when contextvar set
- `_mcp_post()` omits `Authorization` header when contextvar unset

**REST endpoint tests:**
- `POST /views/{id}/actions` extracts user token and passes to `_execute_tool`
- `GET /topology` wraps K8s calls in `user_token_context()`
- `POST /views/{id}/claim` passes user token to `execute_view_plan()`

**view_executor.py tests:**
- Widget tool calls use user token when provided
- Widget tool calls fall back to SA when `user_token=None`

**Integration tests:**
- Mock K8s API: user-token path sends `Authorization: Bearer <user_token>`
- Mock K8s API: SA path sends SA token
- 401 response triggers `session_expired` event on WebSocket
- 403 response does NOT trigger `session_expired`
- Mid-turn 401: `session_expired` emitted, remaining tools still execute

**RBAC filtering test:**
- Two mock tokens with different RBAC — verify tool results differ

**Config toggle test:**
- `PULSE_AGENT_TOKEN_FORWARDING=false` → all calls use SA regardless of user token
