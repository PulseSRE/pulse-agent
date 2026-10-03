"""One execution policy applies independently of approval transport."""

import pytest

from sre_agent.action_policy import (
    approved_phase_can_write,
    effective_write_tools,
    is_explicit_approval,
    mcp_requires_confirmation,
)
from sre_agent.event_bus import EventBus


@pytest.mark.parametrize("value", [False, None, "false", "true", 0, 1, {}, [True]])
def test_truthy_or_malformed_consent_never_grants_authority(value):
    assert not is_explicit_approval(value)
    assert not approved_phase_can_write(value, 3)


@pytest.mark.parametrize("trust", [0, 1, 2, 3])
def test_durable_phase_preserves_both_approval_and_server_trust(trust):
    assert approved_phase_can_write(True, trust) is (trust >= 2)


def test_stale_write_set_cannot_demote_offered_registered_write():
    assert effective_write_tools([], ["mcp_patch", "absent"], ["mcp_patch", "list_pods"]) == {"mcp_patch"}
    assert effective_write_tools(["custom_write"], [], ["custom_write"]) == {"custom_write"}


@pytest.mark.parametrize(
    "annotations,required",
    [
        (None, True),
        ({}, True),
        ({"readOnlyHint": "true"}, True),
        ({"readOnlyHint": True}, False),
        ({"readOnlyHint": True, "destructiveHint": True}, True),
        ("read", True),
    ],
)
def test_mcp_unknown_or_mutating_tools_stay_gated(annotations, required):
    assert mcp_requires_confirmation(annotations) is required


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("false", False), (1, False), (None, False)])
async def test_callback_transport_uses_explicit_boolean_approval(value, expected):
    bus = EventBus.from_callbacks(on_confirm=lambda _name, _input: value)
    assert await bus.on_confirm("restart_deployment", {"name": "api"}) is expected
