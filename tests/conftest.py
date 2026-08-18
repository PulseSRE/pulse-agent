"""Shared pytest fixtures for Pulse Agent tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

# Default test database URL — local PostgreSQL via Podman
_TEST_DB_URL = os.environ.get(
    "PULSE_AGENT_TEST_DATABASE_URL",
    "postgresql://pulse:pulse@localhost:5433/pulse_test",
)


# ---------------------------------------------------------------------------
# Anthropic client stub
# ---------------------------------------------------------------------------
#
# The WebSocket tests drive the real agent loop, and the ws_client fixtures
# patch the Kubernetes clients but never the Anthropic one. With no backend
# configured the SDK raises "Could not resolve authentication method" while
# building request headers, the session never completes normally, and the
# test's receive loop blocks -- which wedged CI on every run from 2026-06-26
# onward until a per-test timeout made it visible.
#
# The patch targets anthropic.AsyncAnthropic itself, NOT
# sre_agent.agent.create_async_client. Patching the factory does not work:
# sre_agent/api/agent_ws.py does `from ..agent import create_async_client` at
# module level, so it holds its own binding and never sees a patch applied to
# sre_agent.agent -- the same by-value import problem that made the auto-fix
# kill switch inert. create_async_client() looks up `anthropic.AsyncAnthropic`
# as a module attribute at call time, so patching there reaches every caller
# regardless of how it imported the factory, and regardless of import order.
#
# Only the *async* client is stubbed: that is what the agent loop and the
# WebSocket endpoints use. The sync one (borrow_client, used by
# skill_router/tool_predictor/inbox for best-effort classification) fails fast
# with the same TypeError rather than hanging, and those call sites already
# swallow it, so leaving it alone avoids changing behaviour tests rely on.
#
# Skipped entirely when a real backend is configured, so the live-judge and
# replay eval runs still exercise the real client.


class _StubUsage:
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _StubFinalMessage:
    """Minimal stand-in for the object returned by stream.get_final_message()."""

    # end_turn makes run_agent_streaming break out of the tool loop immediately,
    # so `content` is never inspected on this path.
    stop_reason = "end_turn"
    content: ClassVar[list] = []
    usage = _StubUsage()


class _StubStream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _events():
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=self._text),
            )

        return _events()

    async def get_final_message(self):
        return _StubFinalMessage()


class _StubAsyncClient:
    """Async Anthropic client stub: one text delta, then a clean end_turn."""

    STUB_TEXT = "[stubbed model response]"

    def __init__(self, *args, **kwargs) -> None:
        # Accepts and ignores constructor args so it can stand in for both
        # AsyncAnthropic() and AsyncAnthropicVertex(region=..., project_id=...).
        self.messages = SimpleNamespace(stream=lambda **kw: _StubStream(self.STUB_TEXT))

    async def close(self) -> None:
        return None


def _has_live_ai_backend() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"))


@pytest.fixture(autouse=True)
def _stub_async_anthropic_client(monkeypatch):
    """Prevent any test from constructing a real async Anthropic client."""
    if _has_live_ai_backend():
        yield
        return
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _StubAsyncClient)
    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", _StubAsyncClient, raising=False)
    yield


@pytest.fixture(autouse=True)
def _set_test_db_url(monkeypatch):
    """Ensure all tests use the test PostgreSQL database."""
    monkeypatch.setenv("PULSE_AGENT_DATABASE_URL", _TEST_DB_URL)
    from sre_agent.config import _reset_settings

    _reset_settings()
    yield
    _reset_settings()


def _text(result):
    """Extract text from tool result (handles both str and (str, component) tuple)."""
    return result[0] if isinstance(result, tuple) else result


def _ts(minutes_ago: int = 5) -> datetime:
    """Create a timezone-aware timestamp N minutes ago."""
    return datetime.now(UTC).replace(microsecond=0) - __import__("datetime").timedelta(minutes=minutes_ago)


def _mock_skill(name: str, **overrides):
    """Build a minimal Skill object for tests."""
    from pathlib import Path

    from sre_agent.skill_loader import Skill

    defaults = dict(
        name=name,
        version=1,
        description=f"{name} skill",
        keywords=[],
        categories=[],
        write_tools=False,
        priority=1,
        system_prompt="",
        path=Path("."),
    )
    defaults.update(overrides)
    return Skill(**defaults)


@pytest.fixture
def set_orca_result():
    """Set the ORCA selection result contextvar, auto-reset on teardown."""
    from sre_agent.skill_selector import _last_selection_result_var

    def _set(result):
        _last_selection_result_var.set(result)

    yield _set
    _last_selection_result_var.set(None)


def _make_pod(
    name="test-pod",
    namespace="default",
    phase="Running",
    restarts=0,
    ready=True,
    privileged=False,
    host_network=False,
    run_as_non_root=True,
    image="registry.redhat.io/ubi9:latest",
    node_name="node-1",
):
    """Build a mock V1Pod object."""
    container_state = SimpleNamespace(
        running=SimpleNamespace() if phase == "Running" else None,
        waiting=SimpleNamespace(reason="CrashLoopBackOff") if phase == "Waiting" else None,
        terminated=SimpleNamespace(reason="OOMKilled") if phase == "Terminated" else None,
    )
    sc = SimpleNamespace(
        privileged=privileged,
        run_as_non_root=run_as_non_root,
        run_as_user=1000 if run_as_non_root else 0,
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        capabilities=SimpleNamespace(add=None, drop=["ALL"]),
    )
    container = SimpleNamespace(
        name="main",
        image=image,
        security_context=sc,
        resources=SimpleNamespace(
            requests={"cpu": "100m", "memory": "128Mi"}, limits={"cpu": "500m", "memory": "512Mi"}
        ),
        ports=[SimpleNamespace(container_port=8080, protocol="TCP")],
        env=[],
        image_pull_policy="IfNotPresent",
    )
    container_status = SimpleNamespace(
        name="main",
        image=image,
        ready=ready,
        restart_count=restarts,
        state=container_state,
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={"app": name},
            annotations={},
            creation_timestamp=_ts(30),
        ),
        spec=SimpleNamespace(
            node_name=node_name,
            containers=[container],
            init_containers=[],
            volumes=[],
            host_network=host_network,
            host_pid=False,
            host_ipc=False,
        ),
        status=SimpleNamespace(
            phase=phase,
            pod_ip="10.0.0.1",
            qos_class="Burstable",
            conditions=[
                SimpleNamespace(type="Ready", status="True" if ready else "False", reason=None, message=None),
            ],
            container_statuses=[container_status],
        ),
    )


def _make_node(name="node-1", ready=True, cpu="4", memory="16Gi", roles=None):
    """Build a mock V1Node object."""
    labels = {}
    for role in roles or ["worker"]:
        labels[f"node-role.kubernetes.io/{role}"] = ""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels,
            annotations={},
            creation_timestamp=_ts(60 * 24),
        ),
        spec=SimpleNamespace(
            taints=[],
            unschedulable=False,
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type="Ready", status="True" if ready else "False", reason=None, message=None),
            ],
            capacity={"cpu": cpu, "memory": memory},
            allocatable={"cpu": cpu, "memory": memory},
            node_info=SimpleNamespace(
                operating_system="linux",
                architecture="amd64",
                kernel_version="5.14.0",
                container_runtime_version="cri-o://1.28.0",
                kubelet_version="v1.28.0",
            ),
        ),
    )


def _make_deployment(name="nginx", namespace="default", replicas=3, ready=3, available=3):
    """Build a mock V1Deployment object."""
    container = SimpleNamespace(
        name="nginx",
        image="nginx:1.25",
        resources=SimpleNamespace(requests={"cpu": "100m"}, limits={"cpu": "500m"}),
        ports=[SimpleNamespace(container_port=80, protocol="TCP")],
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={"app": name},
            creation_timestamp=_ts(60),
        ),
        spec=SimpleNamespace(
            replicas=replicas,
            strategy=SimpleNamespace(type="RollingUpdate"),
            selector=SimpleNamespace(match_labels={"app": name}),
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
        ),
        status=SimpleNamespace(
            replicas=replicas,
            ready_replicas=ready,
            updated_replicas=replicas,
            available_replicas=available,
            conditions=[
                SimpleNamespace(
                    type="Available",
                    status="True",
                    reason="MinimumReplicasAvailable",
                    message="Deployment has minimum availability.",
                ),
            ],
        ),
    )


def _make_event(
    reason="Scheduled", message="Successfully assigned", event_type="Normal", kind="Pod", obj_name="test-pod"
):
    return SimpleNamespace(
        type=event_type,
        reason=reason,
        message=message,
        last_timestamp=_ts(2),
        involved_object=SimpleNamespace(kind=kind, name=obj_name),
    )


def _make_namespace(name="default"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=_ts(60 * 24)),
        status=SimpleNamespace(phase="Active"),
    )


def _list_result(items):
    return SimpleNamespace(items=items)


@pytest.fixture
def mock_k8s():
    """Patch all k8s_client getters to return mocks."""
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.k8s_client.get_core_client") as core,
        patch("sre_agent.k8s_client.get_apps_client") as apps,
        patch("sre_agent.k8s_client.get_custom_client") as custom,
        patch("sre_agent.k8s_client.get_version_client") as version,
        patch("sre_agent.k8s_tools.advanced.k8s_stream") as stream,
    ):
        core_mock = MagicMock()
        apps_mock = MagicMock()
        custom_mock = MagicMock()
        version_mock = MagicMock()

        core.return_value = core_mock
        apps.return_value = apps_mock
        custom.return_value = custom_mock
        version.return_value = version_mock

        yield {
            "core": core_mock,
            "apps": apps_mock,
            "custom": custom_mock,
            "version": version_mock,
            "stream": stream,
        }


@pytest.fixture
def mock_security_k8s():
    """Patch k8s clients for security_tools."""
    with (
        patch("sre_agent.k8s_client._initialized", True),
        patch("sre_agent.k8s_client._load_k8s"),
        patch("sre_agent.security_tools.get_core_client") as core,
        patch("sre_agent.security_tools.get_rbac_client") as rbac,
        patch("sre_agent.security_tools.get_networking_client") as networking,
        patch("sre_agent.security_tools.get_custom_client") as custom,
    ):
        core_mock = MagicMock()
        rbac_mock = MagicMock()
        networking_mock = MagicMock()
        custom_mock = MagicMock()

        core.return_value = core_mock
        rbac.return_value = rbac_mock
        networking.return_value = networking_mock
        custom.return_value = custom_mock

        yield {
            "core": core_mock,
            "rbac": rbac_mock,
            "networking": networking_mock,
            "custom": custom_mock,
        }
