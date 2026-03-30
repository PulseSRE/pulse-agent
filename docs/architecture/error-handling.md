# Error Handling Patterns

## Tool Functions (`@beta_tool`)

```python
@beta_tool
def my_tool(namespace: str, name: str) -> str:
    # 1. Validate inputs
    if err := _validate_k8s_namespace(namespace):
        return err

    # 2. Wrap K8s calls in safe()
    result = safe(lambda: get_core_client().read_namespaced_pod(name, namespace))
    if isinstance(result, ToolError):
        return str(result)  # Returns classified error with suggestions

    # 3. Never raise — always return a string
    return f"Pod {name} is {result.status.phase}"
```

## Database Operations (`@db_safe`)

```python
@db_safe(default=[])  # Returns default on failure, logs error
def search_incidents(self, query: str, limit: int = 5) -> list[dict]:
    # Database errors caught by decorator
    # Error tracked via error_tracker
    # Function returns default value on failure
```

## WebSocket Handlers

```python
# Use _make_receive_loop for shared protocol handling
# Agent errors: catch Exception, classify, send descriptive error to UI
except Exception as exc:
    err_type = type(exc).__name__
    err_msg = str(exc)[:200]
    if "DefaultCredentialsError" in err_type:
        detail = "AI backend credentials not configured."
    elif "rate" in err_msg.lower():
        detail = "API rate limit reached."
    else:
        detail = f"Agent error: {err_type} — {err_msg}"
```

## Scanner Functions

```python
def scan_xyz() -> list[dict]:
    findings = []
    try:
        result = safe(lambda: get_core_client().list_pods())
        if isinstance(result, ToolError):
            return findings  # Empty list, don't crash monitor
        # ... scanner logic
    except Exception as e:
        logger.error("XYZ scan failed: %s", e)  # Always log
    return findings  # Always return list
```

## Rules

1. **Never `except Exception: pass`** without at least `logger.debug`
2. **Tools return strings**, never raise exceptions
3. **Scanners return empty lists** on failure, never crash the monitor loop
4. **DB operations use `@db_safe`** with appropriate defaults
5. **WebSocket errors send descriptive messages** to the UI with category + suggestions
6. **K8s API calls wrapped in `safe()`** which returns `ToolError` on failure
