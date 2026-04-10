"""MCP Tool Renderer — converts MCP text output to UI components.

Three-tier rendering:
1. Skill-defined renderer (from mcp.yaml tool_renderers)
2. Auto-detect from output format (JSON, CSV, key-value, etc.)
3. Fallback to log_viewer (never plain text)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re

logger = logging.getLogger("pulse_agent.mcp_renderer")


def _humanize(snake_case: str) -> str:
    """Convert snake_case tool name to Title Case."""
    return snake_case.replace("_", " ").title()


def render_mcp_output(
    tool_name: str,
    output: str,
    renderer_config: dict | None = None,
) -> tuple[str, dict]:
    """Convert MCP tool output to (text, component_spec).

    Parameters
    ----------
    tool_name : Name of the MCP tool.
    output : Raw text output from the MCP tool.
    renderer_config : Optional skill-defined renderer from mcp.yaml tool_renderers.

    Returns
    -------
    Tuple of (text_for_llm, component_spec).
    """
    # Tier 1: Skill-defined renderer
    if renderer_config:
        try:
            spec = _apply_renderer(tool_name, output, renderer_config)
            if spec:
                return output, spec
        except Exception:
            logger.debug("Skill renderer failed for %s, falling back to auto-detect", tool_name)

    # Tier 2: Auto-detect
    spec = _auto_detect(tool_name, output)
    return output, spec


def _apply_renderer(tool_name: str, output: str, config: dict) -> dict | None:
    """Apply a skill-defined renderer configuration."""
    kind = config.get("kind", "log_viewer")
    parser = config.get("parser", "lines")
    columns = config.get("columns")
    item_mapping = config.get("item_mapping")

    parsed = _parse(output, parser)
    if parsed is None:
        return None

    if kind == "data_table" and isinstance(parsed, list):
        if columns:
            col_defs = [{"id": c, "header": c.replace("_", " ").title()} for c in columns]
        elif parsed and isinstance(parsed[0], dict):
            col_defs = [{"id": k, "header": k.replace("_", " ").title()} for k in parsed[0]]
        else:
            return None
        return {"kind": "data_table", "title": _humanize(tool_name), "columns": col_defs, "rows": parsed}

    elif kind == "key_value" and isinstance(parsed, dict):
        pairs = [{"key": k, "value": str(v)} for k, v in parsed.items()]
        return {"kind": "key_value", "title": _humanize(tool_name), "pairs": pairs}

    elif kind == "status_list" and isinstance(parsed, list):
        items = []
        for item in parsed:
            if item_mapping and isinstance(item, dict):
                mapped = {k: _template(v, item) for k, v in item_mapping.items()}
                items.append(mapped)
            elif isinstance(item, dict):
                items.append(
                    {
                        "label": str(item.get("name", item.get("label", ""))),
                        "status": str(item.get("status", item.get("state", "info"))),
                        "detail": str(item.get("detail", item.get("description", item.get("message", "")))),
                    }
                )
            else:
                items.append({"label": str(item), "status": "info"})
        return {"kind": "status_list", "title": _humanize(tool_name), "items": items}

    elif kind == "metric_card" and isinstance(parsed, dict):
        return {
            "kind": "metric_card",
            "title": _humanize(tool_name),
            "value": str(parsed.get("value", parsed.get(next(iter(parsed.keys())), ""))),
        }

    return None


def _parse(output: str, parser: str) -> list | dict | None:
    """Parse output using the specified parser."""
    if parser == "json":
        return _parse_json(output)
    elif parser == "csv":
        return _parse_csv(output)
    elif parser == "key_value":
        return _parse_key_value(output)
    elif parser == "lines":
        return _parse_lines(output)
    return None


def _template(template: str, data: dict) -> str:
    """Simple template substitution: {{field}} → value."""
    result = template
    for key, value in data.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def _auto_detect(tool_name: str, output: str) -> dict:
    """Auto-detect output format and return best component spec."""
    title = _humanize(tool_name)

    if not output or not output.strip():
        return {"kind": "metric_card", "title": title, "value": "empty", "status": "warning"}

    stripped = output.strip()

    # Try JSON first
    if stripped.startswith(("[", "{")):
        parsed = _parse_json(stripped)
        if parsed is not None:
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                # JSON array of objects → data_table
                keys = list(parsed[0].keys())
                columns = [{"id": k, "header": k.replace("_", " ").title()} for k in keys]
                return {"kind": "data_table", "title": title, "columns": columns, "rows": parsed}
            elif isinstance(parsed, dict):
                # JSON object → key_value
                pairs = [{"key": k, "value": str(v)} for k, v in parsed.items()]
                return {"kind": "key_value", "title": title, "pairs": pairs}

    # Key-value lines (key: value or key=value)
    kv = _parse_key_value(stripped)
    if kv and len(kv) >= 2:
        pairs = [{"key": k, "value": str(v)} for k, v in kv.items()]
        return {"kind": "key_value", "title": title, "pairs": pairs}

    # Tab/comma-separated (CSV-like)
    lines = stripped.split("\n")
    if len(lines) >= 2:
        first = lines[0]
        if "\t" in first or (first.count(",") >= 2 and not first.startswith("{")):
            parsed = _parse_csv(stripped)
            if parsed and len(parsed) >= 2:
                keys = list(parsed[0].keys())
                columns = [{"id": k, "header": k} for k in keys]
                return {"kind": "data_table", "title": title, "columns": columns, "rows": parsed}

    # Numbered/bulleted list
    if _is_list(stripped):
        items = []
        for line in lines:
            clean = re.sub(r"^[\d\.\-\*\•]\s*", "", line.strip())
            if clean:
                items.append({"label": clean, "status": "info"})
        if items:
            return {"kind": "status_list", "title": title, "items": items}

    # Single short value → metric_card
    if len(stripped) < 50 and "\n" not in stripped:
        return {"kind": "metric_card", "title": title, "value": stripped}

    # Fallback: log_viewer (searchable, preserves formatting)
    log_lines = []
    for line in lines:
        level = "info"
        if re.search(r"\b(error|fail|fatal)\b", line, re.IGNORECASE):
            level = "error"
        elif re.search(r"\b(warn|warning)\b", line, re.IGNORECASE):
            level = "warning"
        elif re.search(r"\b(debug)\b", line, re.IGNORECASE):
            level = "debug"
        log_lines.append({"message": line, "level": level})

    return {"kind": "log_viewer", "title": title, "lines": log_lines}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_json(text: str) -> list | dict | None:
    """Try to parse as JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_csv(text: str) -> list[dict] | None:
    """Parse tab or comma-separated text into list of dicts."""
    try:
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return None
        delimiter = "\t" if "\t" in lines[0] else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        return rows if rows else None
    except Exception:
        return None


def _parse_key_value(text: str) -> dict | None:
    """Parse key: value or key=value lines. Skips URLs."""
    result: dict[str, str] = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or re.match(r"^https?://", line):
            continue
        # Match key: value but not URLs (value starting with //)
        match = re.match(r"^([^:=]+?)\s*[:=]\s*(?!//)(.+)$", line)
        if match:
            result[match.group(1).strip()] = match.group(2).strip()
    return result if len(result) >= 2 else None


def _parse_lines(text: str) -> list[str]:
    """Split into lines."""
    return [line for line in text.strip().split("\n") if line.strip()]


def _is_list(text: str) -> bool:
    """Check if text looks like a numbered or bulleted list."""
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    list_patterns = 0
    for line in lines:
        if re.match(r"^[\d]+[\.\)]\s", line) or re.match(r"^[\-\*\•]\s", line):
            list_patterns += 1
    return list_patterns >= len(lines) * 0.6
