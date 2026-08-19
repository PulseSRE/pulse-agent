"""Guards for direct ApiClient.call_api() use.

Three tools — kubectl-explain, API group discovery, and the generic describe —
were dead simultaneously because each passed ``response_type="object"``, a
kwarg the client dropped years ago. Every call raised TypeError, and every call
site caught it and returned its own "Error fetching ..." string, so the tools
appeared to work and merely never found anything.

The sweep below is the guard: a kwarg the installed client cannot accept is a
failed test, not a plausible-looking error message.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from kubernetes.client import ApiClient

from sre_agent.errors import ToolError
from sre_agent.k8s_client import get_raw_json

SRE_AGENT = pathlib.Path(__file__).resolve().parent.parent / "sre_agent"


def _call_api_keywords() -> list[tuple[str, int, str]]:
    """Every keyword passed to a .call_api(...) call anywhere in sre_agent."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRE_AGENT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "call_api"):
                continue
            for kw in node.keywords:
                if kw.arg is not None:
                    found.append((str(path.relative_to(SRE_AGENT.parent)), node.lineno, kw.arg))
    return found


def test_every_call_api_keyword_exists_on_the_installed_client():
    accepted = set(inspect.signature(ApiClient.call_api).parameters)
    bad = [(f, line, kw) for f, line, kw in _call_api_keywords() if kw not in accepted]
    assert not bad, "call_api() keywords the installed kubernetes client rejects: " + ", ".join(
        f"{f}:{line} {kw}=" for f, line, kw in bad
    )


def test_the_sweep_actually_finds_call_sites():
    """A sweep that matches nothing would pass forever and guard nothing."""
    assert len(_call_api_keywords()) > 0


def test_get_raw_json_returns_a_decoded_body(monkeypatch):
    class _Resp:
        data = b'{"metadata": {"name": "demo"}}'

    monkeypatch.setattr(
        "sre_agent.k8s_client.get_core_client",
        lambda: type(
            "C", (), {"api_client": type("A", (), {"call_api": staticmethod(lambda *a, **k: (_Resp(), 200, {}))})()}
        )(),
    )
    assert get_raw_json("/api/v1/namespaces/demo") == {"metadata": {"name": "demo"}}


def test_get_raw_json_converts_api_errors_into_tool_errors(monkeypatch):
    from kubernetes.client.rest import ApiException

    def _raise(*a, **k):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(
        "sre_agent.k8s_client.get_core_client",
        lambda: type("C", (), {"api_client": type("A", (), {"call_api": staticmethod(_raise)})()})(),
    )
    result = get_raw_json("/api/v1/namespaces/gone", "test")
    assert isinstance(result, ToolError)
    assert result.category == "not_found"


@pytest.mark.parametrize("tool_module,tool_name", [("sre_agent.k8s_tools.generic", "describe_resource")])
def test_generic_describe_surfaces_api_errors_rather_than_swallowing_them(monkeypatch, tool_module, tool_name):
    import importlib

    mod = importlib.import_module(tool_module)
    monkeypatch.setattr(
        "sre_agent.k8s_client.get_raw_json",
        lambda path, operation="": ToolError("secrets is forbidden", "forbidden", 403),
    )
    out = getattr(mod, tool_name).func("demo", "thing", "Secret")
    assert "forbidden" in str(out)
