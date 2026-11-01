"""The startup MCP connect loop keeps retrying a failed server, and stops on shutdown.

Startup used to make exactly one attempt per skill. When it failed the failure
was cached, and the agent reported "Connection refused" against a server that
had been healthy for an hour until the pod was restarted.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from types import SimpleNamespace

import pytest

# `sre_agent.api.app` as an attribute is the FastAPI instance the package
# re-exports, not the module.
app_module = importlib.import_module("sre_agent.api.app")


def _skill(tmp_path, name: str) -> SimpleNamespace:
    path = tmp_path / name
    path.mkdir()
    (path / "mcp.yaml").write_text("server: {}\n")
    return SimpleNamespace(name=name, path=path, builtin=True)


@pytest.fixture
def shutdown_event(monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(app_module, "_mcp_shutdown", event)
    return event


@pytest.fixture
def revalidations(monkeypatch):
    calls: list[int] = []
    from sre_agent import skill_loader

    monkeypatch.setattr(skill_loader, "revalidate_skills", lambda: calls.append(1))
    return calls


def test_failed_first_attempt_is_retried_until_it_connects(tmp_path, monkeypatch, shutdown_event, revalidations):
    skill = _skill(tmp_path, "openshift")
    attempts: list[str] = []

    def fake_connect(name, path, builtin=False):
        attempts.append(name)
        if len(attempts) < 3:
            return SimpleNamespace(connected=False, error="Connection refused", tools=[])
        return SimpleNamespace(connected=True, error=None, tools=["a", "b"])

    from sre_agent import mcp_client

    monkeypatch.setattr(mcp_client, "connect_skill_mcp", fake_connect)

    asyncio.run(
        asyncio.wait_for(
            app_module._connect_mcp_with_retry({"openshift": skill}, initial_backoff=0.01, max_backoff=0.02),
            timeout=5,
        )
    )

    assert attempts == ["openshift"] * 3, "a failed connection must be retried until it succeeds, then stop"
    # Tools from a late connection are registered the same way first-attempt ones are.
    assert len(revalidations) == 3


def test_only_failing_skills_are_retried(tmp_path, monkeypatch, shutdown_event, revalidations):
    good = _skill(tmp_path, "good")
    flaky = _skill(tmp_path, "flaky")
    attempts: list[str] = []

    def fake_connect(name, path, builtin=False):
        attempts.append(name)
        ok = name == "good" or attempts.count("flaky") >= 2
        return SimpleNamespace(connected=ok, error=None if ok else "refused", tools=[])

    from sre_agent import mcp_client

    monkeypatch.setattr(mcp_client, "connect_skill_mcp", fake_connect)

    asyncio.run(
        asyncio.wait_for(
            app_module._connect_mcp_with_retry({"good": good, "flaky": flaky}, initial_backoff=0.01),
            timeout=5,
        )
    )

    assert attempts.count("good") == 1
    assert attempts.count("flaky") == 2


def test_shutdown_during_backoff_returns_promptly(tmp_path, monkeypatch, shutdown_event, revalidations):
    skill = _skill(tmp_path, "openshift")
    attempts: list[str] = []

    def always_refused(name, path, builtin=False):
        attempts.append(name)
        return SimpleNamespace(connected=False, error="Connection refused", tools=[])

    from sre_agent import mcp_client

    monkeypatch.setattr(mcp_client, "connect_skill_mcp", always_refused)

    async def scenario() -> float:
        # A backoff far longer than the test may take: the loop must leave on
        # the shutdown signal, not by running out its wait.
        task = asyncio.create_task(app_module._connect_mcp_with_retry({"openshift": skill}, initial_backoff=60))
        while not attempts:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # now inside the backoff wait
        started = time.monotonic()
        shutdown_event.set()
        await asyncio.wait_for(task, timeout=2)
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())

    assert elapsed < 2
    assert attempts == ["openshift"], "no further attempt after shutdown"


def test_no_mcp_skills_does_nothing(tmp_path, monkeypatch, shutdown_event, revalidations):
    path = tmp_path / "plain"
    path.mkdir()
    called: list[str] = []

    from sre_agent import mcp_client

    monkeypatch.setattr(mcp_client, "connect_skill_mcp", lambda *a, **k: called.append("x"))

    asyncio.run(app_module._connect_mcp_with_retry({"plain": SimpleNamespace(name="plain", path=path, builtin=True)}))

    assert called == []
    assert revalidations == []
