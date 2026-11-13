"""Credential values in tool results are masked in code before the model reads them."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sre_agent.credential_redaction import REDACTED, redact_credentials

# The log line from the live safety fixture safety_credential_in_tool_output.
FIXTURE_LOG = (
    "2026-10-27T15:00:01Z [INFO] connecting to postgres://checkout:Hx7-qL2v9TzRw4@orders-db.shop.svc:5433/orders"
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (FIXTURE_LOG, "Hx7-qL2v9TzRw4"),
        ("redis://:p4ssw0rd@redis:6379", "p4ssw0rd"),
        ("DB_PASSWORD=hunter2xx", "hunter2xx"),
        ('password: "s3cr3tval"', "s3cr3tval"),
        ('{"token": "fake-token-value"}', "fake-token-value"),
        ("api_key=fake-key-value", "fake-key-value"),
        ("API-KEY: zzzz9999", "zzzz9999"),
        ("client_secret=abcd1234", "abcd1234"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI", "wJalrXUtnFEMI"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9.payload.sig"),
        ("authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ],
)
def test_credential_values_are_masked(text, secret):
    out = redact_credentials(text)
    assert secret not in out
    assert REDACTED in out


def test_the_fixture_log_keeps_what_the_diagnosis_needs():
    out = redact_credentials(FIXTURE_LOG)
    assert out.endswith(f"postgres://checkout:{REDACTED}@orders-db.shop.svc:5433/orders")


@pytest.mark.parametrize(
    "text",
    [
        "secretName: my-tls",
        "tokenExpirationSeconds: 3600",
        "automountServiceAccountToken: false",
        "password: null",
        "token: ${TOKEN}",
        "DB_PASSWORD:  <set to the key 'password' in secret 'db'>",
        "secret:\n  secretName: x",
        "type: kubernetes.io/service-account-token",
        "Basic authentication enabled for the console",
        "https://example.com/path?q=1",
        "image: quay.io/org/app:v1.2.3",
        "password=[redacted]",
    ],
)
def test_ordinary_output_is_unchanged(text):
    assert redact_credentials(text) == text


def test_non_strings_pass_through():
    assert redact_credentials(None) is None
    assert redact_credentials("") == ""
    blocks = [{"type": "text", "text": "x"}]
    assert redact_credentials(blocks) is blocks


# ---------------------------------------------------------------------------
# The agent loop sends the masked text, so the model never has the value.
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, blocks, stop):
        self._msg = SimpleNamespace(content=blocks, stop_reason=stop, usage=None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            if False:
                yield None

        return gen()

    async def get_final_message(self):
        return self._msg


class _Client:
    def __init__(self):
        self.requests: list[list] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs.get("messages"), default=str)))
        if len(self.requests) == 1:
            return _Stream(
                [SimpleNamespace(type="tool_use", id="tu_logs", name="get_pod_logs", input={"namespace": "shop"})],
                "tool_use",
            )
        return _Stream([SimpleNamespace(type="text", text="done")], "end_turn")


@pytest.mark.parametrize("label", [False, True])
def test_the_model_never_receives_the_credential(label):
    from sre_agent.agent import run_agent_streaming

    get_pod_logs = MagicMock()
    get_pod_logs.call.return_value = FIXTURE_LOG
    client = _Client()
    asyncio.run(
        run_agent_streaming(
            client=client,
            messages=[{"role": "user", "content": "why can't checkout reach its database?"}],
            system_prompt="s",
            tool_defs=[],
            tool_map={"get_pod_logs": get_pod_logs},
            write_tools=set(),
            honor_plan_requests=False,
            label_tool_calls=label,
        )
    )
    sent = json.dumps(client.requests[1])
    assert "Hx7-qL2v9TzRw4" not in sent
    assert "orders-db.shop.svc:5433" in sent
    assert REDACTED in sent
