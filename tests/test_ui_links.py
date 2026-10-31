"""Every UI link the agent emits must be a route pulse-ui serves.

The activity feed linked to ``/incidents`` for months after the UI replaced it
with the Inbox; the UI's catch-all sent each click to ``/pulse`` and nothing
failed. This test scans ``sre_agent/`` for UI paths in string literals and
f-strings and checks each against ``sre_agent.ui_routes``, which mirrors the
UI's route table. A link to a page the UI does not have now fails here.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from sre_agent import ui_routes
from sre_agent.ui_routes import UI_ROUTES, is_gvr, is_ui_route

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sre_agent"

# First path segments of UI pages, plus ones the UI has dropped (a string
# starting with one of these is a link to the UI, current or stale). Agent REST
# paths share some of them (/views, /topology, /alerts, /admin/...); those
# only appear as route decorators, which the scan skips.
UI_PREFIXES = sorted(
    {r.split("/")[1] for r in UI_ROUTES} | {"incidents", "preview", "home", "settings", "dashboard", "investigate"}
)

# Values substituted for an f-string's {placeholders}: a plain segment, and a
# GVR for placeholders in a GVR position. A link passes if any combination is
# a route, so the scan only fails on what the static text itself gets wrong.
_PLACEHOLDER_VALUES = ("x", "v1~x")


# ---------------------------------------------------------------------------
# is_ui_route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        "/pulse",
        "/inbox",
        "/inbox?tab=activity",
        "/inbox?tab=activity#top",
        "/compute",
        "/workloads",
        "/r/v1~pods",
        "/r/v1~pods?ns=default",
        "/r/apps~v1~deployments",
        "/r/apps~v1~deployments/default/web",
        "/r/v1~nodes/_/worker-1",
        "/r/route.openshift.io~v1~routes/openshift-console/console",
        "/r/autoscaling~v2~horizontalpodautoscalers/ns/hpa",
        "/r/flowcontrol.apiserver.k8s.io~v1beta3~flowschemas",
        "/r/rbac.authorization.k8s.io~v1~clusterroles/_/system:auth-delegator",
        "/yaml/apps~v1~deployments/default/web",
        "/metrics/v1~nodes/_/worker-1",
        "/deps/apps~v1~deployments/default/web",
        "/create/v1~configmaps",
        "/logs/default/web-6d4cf56db6-abcde",
        "/node-logs/worker-1",
        "/custom/cv-1a2b3c",
        "/share/AbC_123-x",
        "/project/default",
        "/fleet/r/apps~v1~deployments",
        "/fleet/drift",
    ],
)
def test_valid_ui_routes(link):
    assert is_ui_route(link), link


@pytest.mark.parametrize(
    "link",
    [
        "",
        "inbox",
        "/",
        "/incidents",
        "/incidents?tab=actions",
        "/incidents?tab=postmortems",
        "/inbox/",
        "//inbox",
        "/preview",
        "/r/pods",  # no version
        "/r/~v1~pods",  # core group must be omitted, not empty
        "/r/core~v1~pods",
        "/r/v1/pods",  # slashes instead of ~
        "/r/apps~v1~Deployments",
        "/r/apps~v1~deployments/default",  # namespace without name
        "/r/apps~v1~deployments/Default/web",
        "/r/apps~v1~deployments/default/web/extra",
        "/logs/_/pod",  # logs need a real namespace
        "/project/_",
        "/custom/",
        "/agent/activity",  # an agent REST path, not a UI page
        "/views/abc",
        "https://example.com/inbox",
    ],
)
def test_invalid_ui_routes(link):
    assert not is_ui_route(link), link


def test_every_route_parameter_has_a_validator():
    for route in UI_ROUTES:
        for seg in route.split("/")[1:]:
            if seg.startswith(":"):
                assert seg[1:] in ui_routes._PARAMS, f"{route}: no validator for {seg}"


def test_is_gvr():
    assert is_gvr("v1~pods")
    assert is_gvr("apps~v1~deployments")
    assert not is_gvr("apps/v1/deployments")
    assert not is_gvr("~v1~pods")


# ---------------------------------------------------------------------------
# Static scan of sre_agent/
# ---------------------------------------------------------------------------


def _is_link_key(key: str) -> bool:
    return key == "link" or key.endswith("_link") or key == "href"


def _render(node: ast.AST) -> list[str] | None:
    """Possible values of a str constant or f-string; None if not a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts: list[tuple[str, ...]] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append((str(v.value),))
            else:
                parts.append(_PLACEHOLDER_VALUES)
        combos = list(itertools.product(*parts))[:64]
        return ["".join(c) for c in combos]
    return None


def _static_prefix(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if not isinstance(v, ast.Constant):
                break
            out.append(str(v.value))
        return "".join(out)
    return ""


def _looks_like_ui_path(prefix: str) -> bool:
    if not prefix.startswith("/"):
        return False
    first = prefix[1:]
    for sep in ("/", "?", "#"):
        first = first.split(sep, 1)[0]
    return first in UI_PREFIXES


# Methods whose first argument is a path on the agent's own HTTP server
# (``app.mount("/metrics", ...)``, ``router.get("/views")``), not a UI link.
_SERVER_ROUTE_METHODS = {
    "get", "post", "put", "patch", "delete", "head", "options",
    "api_route", "add_api_route", "websocket", "mount", "include_router",
}  # fmt: skip


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """Docstrings and agent REST route paths (decorators, app.mount) are not links."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SERVER_ROUTE_METHODS
            and node.args
        ):
            skip.add(id(node.args[0]))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
            if not isinstance(node, ast.Module):
                for dec in node.decorator_list:
                    for sub in ast.walk(dec):
                        skip.add(id(sub))
    return skip


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _link_valued(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    if isinstance(parent, ast.Dict):
        for k, v in zip(parent.keys, parent.values, strict=True):
            if v is node and isinstance(k, ast.Constant) and isinstance(k.value, str):
                return _is_link_key(k.value)
    if isinstance(parent, ast.keyword) and parent.arg:
        return _is_link_key(parent.arg)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        for t in targets:
            name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
            if name and _is_link_key(name):
                return True
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                if _is_link_key(t.slice.value):
                    return True
    return False


def _gvr_valued(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    if isinstance(parent, ast.Dict):
        for k, v in zip(parent.keys, parent.values, strict=True):
            if v is node and isinstance(k, ast.Constant) and k.value in ("gvr", "_gvr"):
                return True
    return False


def _collect() -> tuple[list[tuple[str, list[str]]], list[tuple[str, str]]]:
    """(links, gvrs) found in sre_agent/, each as (location, rendered values)."""
    links: list[tuple[str, list[str]]] = []
    gvrs: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if path.name == "ui_routes.py":  # the route table itself
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        skip = _skipped_nodes(tree)
        parents = _parents(tree)
        inner_of_fstring = {
            id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in ast.walk(n) if v is not n
        }
        for node in ast.walk(tree):
            if id(node) in skip or id(node) in inner_of_fstring:
                continue
            values = _render(node)
            if values is None:
                continue
            loc = f"{rel}:{node.lineno}"
            if _gvr_valued(node, parents) and isinstance(node, ast.Constant) and node.value:
                gvrs.append((loc, node.value))
                continue
            if _link_valued(node, parents):
                if values == [""] or values[0].startswith(("http://", "https://")):
                    continue
                links.append((loc, values))
            elif _looks_like_ui_path(_static_prefix(node)):
                links.append((loc, values))
    return links, gvrs


_LINKS, _GVRS = _collect()


def test_scan_finds_the_links_it_should():
    """Guard the scanner itself: if it found nothing it would pass vacuously."""
    locs = {loc.split(":")[0] for loc, _ in _LINKS}
    assert "sre_agent/api/monitor_rest.py" in locs  # activity feed links
    assert "sre_agent/view_tools.py" in locs  # metric-card links, /custom/<id>
    assert "sre_agent/k8s_tools/pods.py" in locs  # /logs/<ns>/<pod>
    assert len(_GVRS) >= 5


@pytest.mark.parametrize("loc,values", _LINKS, ids=[loc for loc, _ in _LINKS])
def test_emitted_link_is_a_ui_route(loc, values):
    assert any(is_ui_route(v) for v in values), (
        f"{loc} emits {values[0]!r}, which is not a pulse-ui route. Link to a page the UI "
        f"serves (see sre_agent/ui_routes.py), or add the route there in step with pulse-ui."
    )


@pytest.mark.parametrize("loc,gvr", _GVRS, ids=[loc for loc, _ in _GVRS])
def test_emitted_gvr_has_ui_shape(loc, gvr):
    assert is_gvr(gvr), f"{loc}: {gvr!r} is not group~version~plural (core group: v1~plural)"
