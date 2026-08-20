"""The monitor has to run whether or not anyone is looking at it.

For most of this product's life run_loop() was started only inside the
/ws/monitor handler, so the agent scanned the cluster only while a browser had
the UI open. The scan history on a live cluster showed it plainly — bursts a
minute apart while somebody was watching, then hours of nothing — and an
overnight outage went partly unobserved.

Nothing caught it because nothing tested it. These do.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "sre_agent" / "api" / "app.py"
WS = pathlib.Path(__file__).resolve().parent.parent / "sre_agent" / "api" / "ws_endpoints.py"


def _lifespan_source() -> str:
    tree = ast.parse(APP.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return ast.get_source_segment(APP.read_text(), node) or ""
    raise AssertionError("no lifespan function in app.py")


def test_startup_starts_the_scan_loop():
    """Not on WebSocket connect. On startup."""
    assert "run_loop()" in _lifespan_source()


def test_the_loop_starts_before_the_app_serves_requests():
    """After yield is shutdown; the loop has to be started before it."""
    source = _lifespan_source()
    assert source.index("run_loop()") < source.index("yield")


def test_a_failure_to_start_is_reported_not_swallowed():
    """A monitor that silently never starts is the bug this file exists for."""
    source = _lifespan_source()
    start = source.index("run_loop()")
    nearby = source[start - 600 : start + 600]
    assert "logger.exception" in nearby or "logger.error" in nearby


def test_the_loop_is_cancelled_on_shutdown():
    source = _lifespan_source()
    after_yield = source[source.index("yield") :]
    assert "monitor_task.cancel()" in after_yield


def test_the_websocket_handler_still_starts_it_if_it_is_not_running():
    """Belt and braces: a client must never wait on a loop that failed to start."""
    ws_source = WS.read_text()
    assert "if not monitor.running:" in ws_source
    assert "run_loop()" in ws_source


def test_the_monitor_is_a_singleton_so_the_two_paths_cannot_race():
    """Both callers go through get_cluster_monitor and check .running first."""
    from sre_agent.monitor import get_cluster_monitor

    assert inspect.iscoroutinefunction(get_cluster_monitor)
    assert "if not _monitor.running:" in _lifespan_source()
