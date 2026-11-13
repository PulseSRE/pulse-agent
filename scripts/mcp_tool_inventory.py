#!/usr/bin/env python3
"""List every tool of an openshift-mcp-server checkout, with its annotations.

Writes the inventory ``tests/test_mcp_tool_policy.py`` checks
``sre_agent/mcp_tool_policy.py`` against. Run it whenever ``MCP_SERVER_REF``
in ``.github/workflows/build-push.yml`` moves:

    git clone https://github.com/openshift/openshift-mcp-server /tmp/oms
    git -C /tmp/oms checkout <MCP_SERVER_REF>
    python3 scripts/mcp_tool_inventory.py /tmp/oms <MCP_SERVER_REF> > tests/fixtures/mcp_sidecar_tools.json

It reads the Go source statically (no build): each ``api.Tool{...}`` literal
under ``pkg/toolsets``. A tool whose name or toolset it cannot resolve stops
the script rather than being left out of the inventory.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_TOOL = re.compile(r"api\.Tool\{")
_HINT = r"{key}:\s*(?:ptr\.To|new)\((true|false)\)"
_HELPER_READ_ONLY = re.compile(r"Annotations:\s*(?:\w+\.)?[rR]eadOnlyAnnotations\(")


def _block(src: str, start: int) -> str:
    depth, i = 1, start
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _toolset_name(toolset_dir: Path) -> str:
    src = (toolset_dir / "toolset.go").read_text()
    m = re.search(r'GetName\(\) string \{\s*return "([^"]+)"', src)
    if m:
        return m.group(1)
    m = re.search(r"GetName\(\) string \{\s*return (\w+)\b", src)
    if m:
        const = re.search(rf'{m.group(1)}\s*=\s*"([^"]+)"', "".join(p.read_text() for p in toolset_dir.glob("*.go")))
        if const:
            return const.group(1)
    return _defaults_name(toolset_dir)


def _defaults_name(toolset_dir: Path) -> str:
    """Toolsets that name themselves via internal/defaults (with an optional override)."""
    defaults = "".join(p.read_text() for p in (toolset_dir / "internal" / "defaults").glob("*.go"))
    override = re.search(r'toolsetNameOverride\s*=\s*"([^"]*)"', defaults)
    if override and override.group(1):
        return override.group(1)
    default = re.search(r'DefaultToolsetName\s*=\s*"([^"]+)"', defaults) or re.search(
        r'func ToolsetName\(\) string \{\s*return "([^"]+)"', defaults
    )
    if not default:
        raise SystemExit(f"cannot resolve toolset name in {toolset_dir}")
    return default.group(1)


def _top_level_name(block: str, src: str, toolset_dir: Path) -> str:
    depth = 0
    for line in block.splitlines():
        if depth == 0:
            m = re.match(r"\s*Name:\s*(.+?),\s*$", line)
            if m:
                expr = m.group(1)
                if expr.startswith('"'):
                    return expr.strip('"')
                if "ToolsetName() +" in expr:
                    return _defaults_name(toolset_dir) + expr.split("+", 1)[1].strip().strip('"')
                var = re.search(rf'{re.escape(expr)} := defaults\.ToolsetName\(\) \+ "([^"]+)"', src)
                if var:
                    return _defaults_name(toolset_dir) + var.group(1)
                raise SystemExit(f"cannot resolve tool name {expr!r} in {toolset_dir}")
        depth += line.count("{") - line.count("}")
    raise SystemExit(f"tool without a Name in {toolset_dir}")


def _hint(block: str, key: str) -> bool | None:
    m = re.search(_HINT.format(key=key), block)
    if m:
        return m.group(1) == "true"
    if _HELPER_READ_ONLY.search(block):
        return {"ReadOnlyHint": True, "DestructiveHint": False}[key]
    return None


def inventory(root: Path) -> list[dict]:
    toolsets = root / "pkg" / "toolsets"
    rows = []
    for toolset_dir in sorted(p for p in toolsets.iterdir() if (p / "toolset.go").exists()):
        name = _toolset_name(toolset_dir)
        for f in sorted(toolset_dir.rglob("*.go")):
            if f.name.endswith("_test.go"):
                continue
            src = f.read_text()
            for m in _TOOL.finditer(src):
                block = _block(src, m.end())
                rows.append(
                    {
                        "name": _top_level_name(block, src, toolset_dir),
                        "toolset": name,
                        "readOnlyHint": _hint(block, "ReadOnlyHint"),
                        "destructiveHint": _hint(block, "DestructiveHint"),
                    }
                )
    names = [r["name"] for r in rows]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SystemExit(f"duplicate tool names: {dupes}")
    return sorted(rows, key=lambda r: (r["toolset"], r["name"]))


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: mcp_tool_inventory.py <openshift-mcp-server checkout> <commit>")
    rows = inventory(Path(sys.argv[1]))
    json.dump({"server_ref": sys.argv[2], "tools": rows}, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
