# Slack Claude Bridge Implementation Plan

> Historical design/implementation record. This preserves the proposal and tasks at the date in the filename; unchecked tasks, paths, commands, and security claims are not evidence of the current implementation. Use [current documentation](../../README.md) and verify behavior against code before applying a procedure.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a personal Slack bot that provides Claude chat via Vertex AI and remote control of Claude Code, accessible from mobile.

**Architecture:** Single Python process on macOS. Slack bolt in socket mode (no public URL). Three layers: auth gate, Claude chat via Vertex AI, and a Claude Code bridge using `claude -p` subprocess. Two-gate authentication (Slack user ID + session token) protects shell access.

**Tech Stack:** Python 3.12, slack-bolt, anthropic SDK (Vertex AI), pydantic-settings, subprocess (Claude Code CLI)

**Spec:** `docs/superpowers/specs/2026-04-14-slack-claude-bridge-design.md`

---

### Task 1: Project Setup + Config

**Files:**
- Create: `/Users/amobrem/ali/slack/config.py`
- Create: `/Users/amobrem/ali/slack/requirements.txt`
- Create: `/Users/amobrem/ali/slack/.env.example`
- Create: `/Users/amobrem/ali/slack/.gitignore`
- Test: `/Users/amobrem/ali/slack/tests/test_config.py`

- [ ] **Step 1: Create project directory and git init**

```bash
mkdir -p /Users/amobrem/ali/slack/tests
cd /Users/amobrem/ali/slack && git init
```

- [ ] **Step 2: Write .gitignore**

Create `/Users/amobrem/ali/slack/.gitignore`:

```
.env
__pycache__/
*.pyc
.venv/
```

- [ ] **Step 3: Write requirements.txt**

Create `/Users/amobrem/ali/slack/requirements.txt`:

```
slack-bolt>=1.18.0
anthropic[vertex]>=0.42.0
pydantic-settings>=2.0.0
pytest>=8.0.0
```

- [ ] **Step 4: Write the failing test for config**

Create `/Users/amobrem/ali/slack/tests/test_config.py`:

```python
import os
import pytest
from config import SlackBridgeSettings


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test-token")
    monkeypatch.setenv("SLACK_USER_ID", "U12345")
    monkeypatch.setenv("SESSION_TOKEN", "my-secret-token")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "my-project")
    monkeypatch.setenv("VERTEX_REGION", "us-east5")
    monkeypatch.setenv("CLAUDE_CODE_CWD", "/Users/amobrem/ali/pulse-agent")

    settings = SlackBridgeSettings()
    assert settings.slack_bot_token == "xoxb-test-token"
    assert settings.slack_app_token == "xapp-test-token"
    assert settings.slack_user_id == "U12345"
    assert settings.session_token == "my-secret-token"
    assert settings.vertex_project_id == "my-project"
    assert settings.vertex_region == "us-east5"
    assert settings.claude_code_cwd == "/Users/amobrem/ali/pulse-agent"


def test_config_defaults():
    """Model and timeout have sensible defaults."""
    settings = SlackBridgeSettings(
        slack_bot_token="x",
        slack_app_token="x",
        slack_user_id="U1",
        session_token="s",
        vertex_project_id="p",
        vertex_region="r",
    )
    assert settings.model == "claude-opus-4-6"
    assert settings.claude_code_timeout == 120
    assert settings.max_message_length == 3500


def test_config_rejects_empty_session_token():
    """Session token cannot be empty — it protects shell access."""
    with pytest.raises(Exception):
        SlackBridgeSettings(
            slack_bot_token="x",
            slack_app_token="x",
            slack_user_id="U1",
            session_token="",
            vertex_project_id="p",
            vertex_region="r",
        )
```

- [ ] **Step 5: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 6: Write config.py**

Create `/Users/amobrem/ali/slack/config.py`:

```python
"""Settings for the Slack Claude Bridge."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SlackBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_user_id: str

    # Security
    session_token: str

    # Vertex AI
    vertex_project_id: str
    vertex_region: str

    # Claude
    model: str = "claude-opus-4-6"

    # Claude Code
    claude_code_cwd: str = ""
    claude_code_timeout: int = 120

    # Slack limits
    max_message_length: int = 3500

    @field_validator("session_token")
    @classmethod
    def session_token_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("session_token must not be empty — it protects shell access")
        return v
```

- [ ] **Step 7: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_config.py -v
```

Expected: 3 passed

- [ ] **Step 8: Write .env.example**

Create `/Users/amobrem/ali/slack/.env.example`:

```bash
# Slack app credentials (from api.slack.com/apps)
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token

# Your Slack user ID (find in Slack profile > ⋮ > Copy member ID)
SLACK_USER_ID=U0000000000

# Session token — first message in each thread must contain this
# Use a strong random value: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
SESSION_TOKEN=change-me-to-a-random-value

# Vertex AI
VERTEX_PROJECT_ID=your-gcp-project
VERTEX_REGION=us-east5

# Claude Code working directory (where claude -p runs from)
CLAUDE_CODE_CWD=/Users/amobrem/ali/pulse-agent
```

- [ ] **Step 9: Commit**

```bash
cd /Users/amobrem/ali/slack
git add .gitignore requirements.txt config.py .env.example tests/test_config.py
git commit -m "feat: project setup with Pydantic config and security validation"
```

---

### Task 2: Authentication

**Files:**
- Create: `/Users/amobrem/ali/slack/auth.py`
- Test: `/Users/amobrem/ali/slack/tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/amobrem/ali/slack/tests/test_auth.py`:

```python
import hmac
import pytest
from auth import AuthGate


@pytest.fixture
def gate():
    return AuthGate(allowed_user_id="U12345", session_token="secret-pulse-token")


def test_rejects_wrong_user_id(gate):
    assert gate.check_user("U99999") is False


def test_accepts_correct_user_id(gate):
    assert gate.check_user("U12345") is True


def test_first_message_must_contain_token(gate):
    """Thread not yet authenticated — message must include token."""
    assert gate.check_thread_auth("T1", "hello world") is False


def test_first_message_with_token_authenticates_thread(gate):
    """Token in first message authenticates the whole thread."""
    assert gate.check_thread_auth("T1", "secret-pulse-token what is the status") is True


def test_subsequent_messages_skip_token(gate):
    """Once thread is authenticated, token is not required."""
    gate.check_thread_auth("T1", "secret-pulse-token initial message")
    assert gate.check_thread_auth("T1", "follow up without token") is True


def test_different_thread_needs_own_token(gate):
    """Each thread must be independently authenticated."""
    gate.check_thread_auth("T1", "secret-pulse-token auth this")
    assert gate.check_thread_auth("T2", "no token here") is False


def test_token_check_is_constant_time(gate):
    """Token comparison must use hmac.compare_digest, not ==."""
    # Verify the method exists and works — timing attack resistance
    assert gate.check_thread_auth("T1", "wrong-token") is False
    assert gate.check_thread_auth("T1", "secret-pulse-token") is True


def test_strips_token_from_message(gate):
    """After auth, the token prefix should be removable from the message."""
    text = "secret-pulse-token what's the git status"
    cleaned = gate.strip_token(text)
    assert cleaned == "what's the git status"
    assert "secret-pulse-token" not in cleaned


def test_strip_token_leaves_non_token_messages(gate):
    """Messages without the token are returned unchanged."""
    text = "just a normal message"
    assert gate.strip_token(text) == "just a normal message"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_auth.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Write auth.py**

Create `/Users/amobrem/ali/slack/auth.py`:

```python
"""Two-gate authentication: Slack user ID + per-thread session token."""

from __future__ import annotations

import hmac


class AuthGate:
    def __init__(self, allowed_user_id: str, session_token: str) -> None:
        self._allowed_user_id = allowed_user_id
        self._session_token = session_token
        self._authenticated_threads: set[str] = set()

    def check_user(self, user_id: str) -> bool:
        """Gate 1: constant-time comparison of Slack user ID."""
        return hmac.compare_digest(user_id, self._allowed_user_id)

    def check_thread_auth(self, thread_ts: str, text: str) -> bool:
        """Gate 2: thread must be authenticated via session token."""
        if thread_ts in self._authenticated_threads:
            return True
        if self._session_token in text:
            self._authenticated_threads.add(thread_ts)
            return True
        return False

    def strip_token(self, text: str) -> str:
        """Remove the session token from the message text."""
        return text.replace(self._session_token, "").strip()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_auth.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/slack
git add auth.py tests/test_auth.py
git commit -m "feat: two-gate auth with constant-time token comparison"
```

---

### Task 3: Router (Intent Detection)

**Files:**
- Create: `/Users/amobrem/ali/slack/router.py`
- Test: `/Users/amobrem/ali/slack/tests/test_router.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/amobrem/ali/slack/tests/test_router.py`:

```python
import pytest
from router import Route, route_message


def test_explicit_cc_prefix_routes_to_bridge():
    assert route_message("cc: run the tests") == Route.BRIDGE


def test_explicit_chat_prefix_routes_to_chat():
    assert route_message("chat: tell me a joke") == Route.CHAT


def test_file_mention_routes_to_bridge():
    assert route_message("what does config.py do") == Route.BRIDGE


def test_git_keyword_routes_to_bridge():
    assert route_message("what's the git status") == Route.BRIDGE


def test_run_tests_routes_to_bridge():
    assert route_message("run the tests") == Route.BRIDGE


def test_commit_routes_to_bridge():
    assert route_message("commit these changes") == Route.BRIDGE


def test_general_question_routes_to_chat():
    assert route_message("explain kubernetes pod scheduling") == Route.CHAT


def test_greeting_routes_to_chat():
    assert route_message("hello") == Route.CHAT


def test_path_mention_routes_to_bridge():
    assert route_message("read /Users/amobrem/ali/pulse-agent/agent.py") == Route.BRIDGE


def test_deploy_routes_to_bridge():
    assert route_message("deploy to staging") == Route.BRIDGE


def test_empty_message_routes_to_chat():
    assert route_message("") == Route.CHAT
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_router.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'router'`

- [ ] **Step 3: Write router.py**

Create `/Users/amobrem/ali/slack/router.py`:

```python
"""Intent routing: decide whether a message goes to Claude chat or Claude Code bridge."""

from __future__ import annotations

import re
from enum import Enum


class Route(Enum):
    CHAT = "chat"
    BRIDGE = "bridge"


# Explicit prefixes
_CC_PREFIX = re.compile(r"^cc:\s*", re.IGNORECASE)
_CHAT_PREFIX = re.compile(r"^chat:\s*", re.IGNORECASE)

# Patterns that indicate Claude Code should handle the message
_BRIDGE_PATTERNS = [
    re.compile(r"\.\w{1,5}\b"),           # file extensions (.py, .yaml, .ts)
    re.compile(r"(/[\w./-]+){2,}"),       # file paths (/foo/bar/baz)
    re.compile(r"\bgit\b", re.IGNORECASE),
    re.compile(r"\bcommit\b", re.IGNORECASE),
    re.compile(r"\bbranch\b", re.IGNORECASE),
    re.compile(r"\bdiff\b", re.IGNORECASE),
    re.compile(r"\brun\s+(the\s+)?tests?\b", re.IGNORECASE),
    re.compile(r"\bpytest\b", re.IGNORECASE),
    re.compile(r"\bmake\b", re.IGNORECASE),
    re.compile(r"\bdeploy\b", re.IGNORECASE),
    re.compile(r"\bbuild\b", re.IGNORECASE),
    re.compile(r"\bedit\b", re.IGNORECASE),
    re.compile(r"\bcreate\s+(a\s+)?file\b", re.IGNORECASE),
    re.compile(r"\brefactor\b", re.IGNORECASE),
    re.compile(r"\bfix\s+(the\s+)?bug\b", re.IGNORECASE),
    re.compile(r"\bgrep\b", re.IGNORECASE),
    re.compile(r"\bsearch\s+(for|the|in)\b", re.IGNORECASE),
    re.compile(r"\bread\s+(the\s+)?file\b", re.IGNORECASE),
]


def route_message(text: str) -> Route:
    """Route a message to either direct Claude chat or Claude Code bridge."""
    if not text.strip():
        return Route.CHAT

    if _CC_PREFIX.match(text):
        return Route.BRIDGE

    if _CHAT_PREFIX.match(text):
        return Route.CHAT

    for pattern in _BRIDGE_PATTERNS:
        if pattern.search(text):
            return Route.BRIDGE

    return Route.CHAT
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_router.py -v
```

Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/slack
git add router.py tests/test_router.py
git commit -m "feat: intent router with explicit prefixes and keyword detection"
```

---

### Task 4: Claude Chat (Vertex AI)

**Files:**
- Create: `/Users/amobrem/ali/slack/chat.py`
- Test: `/Users/amobrem/ali/slack/tests/test_chat.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/amobrem/ali/slack/tests/test_chat.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from chat import ClaudeChat


@pytest.fixture
def mock_anthropic():
    with patch("chat.AnthropicVertex") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello from Claude!")]
        mock_client.messages.create.return_value = mock_response

        yield mock_client


@pytest.fixture
def claude_chat(mock_anthropic):
    return ClaudeChat(
        project_id="test-project",
        region="us-east5",
        model="claude-opus-4-6",
    )


def test_send_message_returns_response(claude_chat, mock_anthropic):
    result = claude_chat.send("thread-1", "hello")
    assert result == "Hello from Claude!"


def test_send_builds_conversation_history(claude_chat, mock_anthropic):
    claude_chat.send("thread-1", "first message")
    claude_chat.send("thread-1", "second message")

    # Second call should include first exchange in messages
    call_args = mock_anthropic.messages.create.call_args
    messages = call_args.kwargs["messages"]
    assert len(messages) == 4  # user, assistant, user, assistant not included — 3 user+asst pairs
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "first message"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Hello from Claude!"
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == "second message"


def test_separate_threads_have_separate_history(claude_chat, mock_anthropic):
    claude_chat.send("thread-1", "msg in thread 1")
    claude_chat.send("thread-2", "msg in thread 2")

    # Thread 2's call should only have 1 message pair
    call_args = mock_anthropic.messages.create.call_args
    messages = call_args.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "msg in thread 2"


def test_uses_correct_model(claude_chat, mock_anthropic):
    claude_chat.send("t1", "test")
    call_args = mock_anthropic.messages.create.call_args
    assert call_args.kwargs["model"] == "claude-opus-4-6"


def test_max_tokens_set(claude_chat, mock_anthropic):
    claude_chat.send("t1", "test")
    call_args = mock_anthropic.messages.create.call_args
    assert call_args.kwargs["max_tokens"] == 4096
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_chat.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'chat'`

- [ ] **Step 3: Write chat.py**

Create `/Users/amobrem/ali/slack/chat.py`:

```python
"""Claude chat via Vertex AI with thread-keyed conversation history."""

from __future__ import annotations

from anthropic import AnthropicVertex


class ClaudeChat:
    def __init__(self, project_id: str, region: str, model: str = "claude-opus-4-6") -> None:
        self._client = AnthropicVertex(project_id=project_id, region=region)
        self._model = model
        self._threads: dict[str, list[dict[str, str]]] = {}

    def send(self, thread_id: str, text: str) -> str:
        """Send a message in a thread, maintaining conversation history."""
        if thread_id not in self._threads:
            self._threads[thread_id] = []

        self._threads[thread_id].append({"role": "user", "content": text})

        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=self._threads[thread_id],
        )

        assistant_text = response.content[0].text
        self._threads[thread_id].append({"role": "assistant", "content": assistant_text})

        return assistant_text
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_chat.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/slack
git add chat.py tests/test_chat.py
git commit -m "feat: Vertex AI Claude chat with thread-keyed history"
```

---

### Task 5: Claude Code Bridge

**Files:**
- Create: `/Users/amobrem/ali/slack/bridge.py`
- Test: `/Users/amobrem/ali/slack/tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/amobrem/ali/slack/tests/test_bridge.py`:

```python
import json
import subprocess
import pytest
from unittest.mock import patch, MagicMock
from bridge import ClaudeCodeBridge


@pytest.fixture
def bridge():
    return ClaudeCodeBridge(
        cwd="/Users/amobrem/ali/pulse-agent",
        timeout=10,
        max_message_length=3500,
    )


def test_run_returns_result_text(bridge):
    mock_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "type": "result",
            "result": "All 1520 tests passed.",
        }),
        stderr="",
    )
    with patch("bridge.subprocess.run", return_value=mock_result):
        result = bridge.run("run the tests")
    assert result == "All 1520 tests passed."


def test_run_handles_error(bridge):
    mock_result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="claude: command not found",
    )
    with patch("bridge.subprocess.run", return_value=mock_result):
        result = bridge.run("do something")
    assert "not available" in result.lower() or "error" in result.lower()


def test_run_handles_timeout(bridge):
    with patch("bridge.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
        result = bridge.run("long running task")
    assert "timed out" in result.lower()


def test_run_handles_invalid_json(bridge):
    mock_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Not JSON output, just plain text response",
        stderr="",
    )
    with patch("bridge.subprocess.run", return_value=mock_result):
        result = bridge.run("do something")
    assert result == "Not JSON output, just plain text response"


def test_chunks_long_responses(bridge):
    long_text = "x" * 8000
    chunks = bridge.chunk_response(long_text)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= 3500
    assert "".join(chunks) == long_text


def test_chunks_short_responses_unchanged(bridge):
    short_text = "hello"
    chunks = bridge.chunk_response(short_text)
    assert chunks == ["hello"]


def test_is_available_true(bridge):
    mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="2.1.107", stderr="")
    with patch("bridge.subprocess.run", return_value=mock_result):
        assert bridge.is_available() is True


def test_is_available_false(bridge):
    with patch("bridge.subprocess.run", side_effect=FileNotFoundError):
        assert bridge.is_available() is False


def test_cwd_is_passed_to_subprocess(bridge):
    mock_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"type": "result", "result": "ok"}),
        stderr="",
    )
    with patch("bridge.subprocess.run", return_value=mock_result) as mock_run:
        bridge.run("test")
    assert mock_run.call_args.kwargs["cwd"] == "/Users/amobrem/ali/pulse-agent"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_bridge.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'bridge'`

- [ ] **Step 3: Write bridge.py**

Create `/Users/amobrem/ali/slack/bridge.py`:

```python
"""Claude Code bridge — runs claude -p via subprocess."""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger("slack_claude_bridge")


class ClaudeCodeBridge:
    def __init__(self, cwd: str, timeout: int = 120, max_message_length: int = 3500) -> None:
        self._cwd = cwd
        self._timeout = timeout
        self._max_message_length = max_message_length

    def is_available(self) -> bool:
        """Check if claude CLI is available."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def run(self, prompt: str) -> str:
        """Run a prompt through Claude Code CLI and return the result."""
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--output-format", "json",
                    "--dangerously-skip-permissions",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {self._timeout}s."
        except FileNotFoundError:
            return "Claude Code is not available — error: CLI not found."

        if result.returncode != 0:
            return f"Claude Code error: {result.stderr or 'unknown error'}"

        stdout = result.stdout.strip()
        if not stdout:
            return "Claude Code returned no output."

        # Try to parse JSON response
        try:
            data = json.loads(stdout)
            return data.get("result", stdout)
        except json.JSONDecodeError:
            # Plain text fallback
            return stdout

    def chunk_response(self, text: str) -> list[str]:
        """Split a long response into Slack-safe chunks."""
        if len(text) <= self._max_message_length:
            return [text]
        chunks = []
        while text:
            chunks.append(text[: self._max_message_length])
            text = text[self._max_message_length :]
        return chunks
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_bridge.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/slack
git add bridge.py tests/test_bridge.py
git commit -m "feat: Claude Code bridge with subprocess, timeout, and chunking"
```

---

### Task 6: Slack App (Main Entry Point)

**Files:**
- Create: `/Users/amobrem/ali/slack/app.py`
- Test: `/Users/amobrem/ali/slack/tests/test_app.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/amobrem/ali/slack/tests/test_app.py`:

```python
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app import handle_message


@pytest.fixture
def settings():
    mock = MagicMock()
    mock.slack_user_id = "U12345"
    mock.session_token = "secret-token"
    mock.vertex_project_id = "test-project"
    mock.vertex_region = "us-east5"
    mock.model = "claude-opus-4-6"
    mock.claude_code_cwd = "/tmp"
    mock.claude_code_timeout = 10
    mock.max_message_length = 3500
    return mock


def test_rejects_wrong_user(settings):
    event = {"user": "U99999", "text": "hello", "ts": "1234.5678"}
    say = MagicMock()

    with patch("app.settings", settings), \
         patch("app.auth_gate") as mock_gate:
        mock_gate.check_user.return_value = False
        handle_message(event, say)

    say.assert_not_called()


def test_rejects_unauthenticated_thread(settings):
    event = {"user": "U12345", "text": "hello no token", "ts": "1234.5678"}
    say = MagicMock()

    with patch("app.settings", settings), \
         patch("app.auth_gate") as mock_gate:
        mock_gate.check_user.return_value = True
        mock_gate.check_thread_auth.return_value = False
        handle_message(event, say)

    say.assert_not_called()


def test_routes_to_chat(settings):
    event = {
        "user": "U12345",
        "text": "secret-token explain kubernetes",
        "ts": "1234.5678",
    }
    say = MagicMock()

    with patch("app.settings", settings), \
         patch("app.auth_gate") as mock_gate, \
         patch("app.route_message") as mock_route, \
         patch("app.claude_chat") as mock_chat:
        mock_gate.check_user.return_value = True
        mock_gate.check_thread_auth.return_value = True
        mock_gate.strip_token.return_value = "explain kubernetes"
        from router import Route
        mock_route.return_value = Route.CHAT
        mock_chat.send.return_value = "Kubernetes is..."
        handle_message(event, say)

    say.assert_called_once()
    assert "Kubernetes" in say.call_args.kwargs.get("text", say.call_args[0][0] if say.call_args[0] else "")


def test_routes_to_bridge(settings):
    event = {
        "user": "U12345",
        "text": "secret-token run the tests",
        "ts": "1234.5678",
    }
    say = MagicMock()

    with patch("app.settings", settings), \
         patch("app.auth_gate") as mock_gate, \
         patch("app.route_message") as mock_route, \
         patch("app.code_bridge") as mock_bridge:
        mock_gate.check_user.return_value = True
        mock_gate.check_thread_auth.return_value = True
        mock_gate.strip_token.return_value = "run the tests"
        from router import Route
        mock_route.return_value = Route.BRIDGE
        mock_bridge.is_available.return_value = True
        mock_bridge.run.return_value = "All tests passed"
        mock_bridge.chunk_response.return_value = ["All tests passed"]
        handle_message(event, say)

    say.assert_called()


def test_bridge_unavailable_falls_back_to_chat(settings):
    event = {
        "user": "U12345",
        "text": "secret-token run tests",
        "ts": "1234.5678",
    }
    say = MagicMock()

    with patch("app.settings", settings), \
         patch("app.auth_gate") as mock_gate, \
         patch("app.route_message") as mock_route, \
         patch("app.code_bridge") as mock_bridge, \
         patch("app.claude_chat") as mock_chat:
        mock_gate.check_user.return_value = True
        mock_gate.check_thread_auth.return_value = True
        mock_gate.strip_token.return_value = "run tests"
        from router import Route
        mock_route.return_value = Route.BRIDGE
        mock_bridge.is_available.return_value = False
        mock_chat.send.return_value = "I can't run tests right now."
        handle_message(event, say)

    # Should have called say with a fallback message
    say.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_app.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Write app.py**

Create `/Users/amobrem/ali/slack/app.py`:

```python
"""Slack Claude Bridge — personal Claude bot with Claude Code remote control."""

from __future__ import annotations

import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from auth import AuthGate
from bridge import ClaudeCodeBridge
from chat import ClaudeChat
from config import SlackBridgeSettings
from router import Route, route_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("slack_claude_bridge")

# Global state — initialized in main()
settings: SlackBridgeSettings = None  # type: ignore[assignment]
auth_gate: AuthGate = None  # type: ignore[assignment]
claude_chat: ClaudeChat = None  # type: ignore[assignment]
code_bridge: ClaudeCodeBridge = None  # type: ignore[assignment]
slack_app: App = None  # type: ignore[assignment]


def handle_message(event: dict, say) -> None:
    """Handle an incoming Slack message."""
    user_id = event.get("user", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts", event.get("ts", ""))

    # Gate 1: user ID
    if not auth_gate.check_user(user_id):
        return

    # Gate 2: thread authentication
    if not auth_gate.check_thread_auth(thread_ts, text):
        return

    # Strip token from message
    clean_text = auth_gate.strip_token(text)
    if not clean_text:
        return

    # Route
    route = route_message(clean_text)

    if route == Route.BRIDGE and code_bridge.is_available():
        response = code_bridge.run(clean_text)
        chunks = code_bridge.chunk_response(response)
        for chunk in chunks:
            say(text=chunk, thread_ts=thread_ts)
    elif route == Route.BRIDGE and not code_bridge.is_available():
        say(text="Claude Code is not available — falling back to direct chat.", thread_ts=thread_ts)
        response = claude_chat.send(thread_ts, clean_text)
        say(text=response, thread_ts=thread_ts)
    else:
        response = claude_chat.send(thread_ts, clean_text)
        say(text=response, thread_ts=thread_ts)


def main() -> None:
    global settings, auth_gate, claude_chat, code_bridge, slack_app

    settings = SlackBridgeSettings()

    auth_gate = AuthGate(
        allowed_user_id=settings.slack_user_id,
        session_token=settings.session_token,
    )

    claude_chat = ClaudeChat(
        project_id=settings.vertex_project_id,
        region=settings.vertex_region,
        model=settings.model,
    )

    code_bridge = ClaudeCodeBridge(
        cwd=settings.claude_code_cwd,
        timeout=settings.claude_code_timeout,
        max_message_length=settings.max_message_length,
    )

    slack_app = App(token=settings.slack_bot_token)
    slack_app.event("message")(handle_message)

    logger.info("Slack Claude Bridge starting (user: %s)", settings.slack_user_id)
    handler = SocketModeHandler(slack_app, settings.slack_app_token)
    handler.start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/test_app.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/amobrem/ali/slack
git add app.py tests/test_app.py
git commit -m "feat: Slack app entry point with auth, routing, and bridge fallback"
```

---

### Task 7: Slack App Setup + launchd

**Files:**
- Create: `/Users/amobrem/ali/slack/com.pulse.slack-claude.plist`
- Modify: `/Users/amobrem/ali/slack/.env.example` (add Slack app setup instructions)

- [ ] **Step 1: Create the Slack app on api.slack.com**

Manual step — document instructions:

1. Go to https://api.slack.com/apps → Create New App → From Scratch
2. Name: "Claude Bridge", Workspace: your workspace
3. **Socket Mode:** Enable, create App-Level Token with `connections:write` scope → save as `SLACK_APP_TOKEN`
4. **OAuth & Permissions:** Add Bot Token Scopes:
   - `chat:write` — send messages
   - `im:history` — read DM messages
   - `im:read` — see DM channels
   - `im:write` — open DMs
5. **Event Subscriptions:** Enable, subscribe to bot events:
   - `message.im` — receive DM messages
6. Install App to Workspace → copy Bot User OAuth Token → save as `SLACK_BOT_TOKEN`
7. Find your Slack user ID: Profile → ⋮ → Copy member ID → save as `SLACK_USER_ID`

- [ ] **Step 2: Write the launchd plist**

Create `/Users/amobrem/ali/slack/com.pulse.slack-claude.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pulse.slack-claude</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>/Users/amobrem/ali/slack/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/amobrem/ali/slack</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/amobrem/ali/slack/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/amobrem/ali/slack/logs/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 3: Create logs directory and install plist**

```bash
mkdir -p /Users/amobrem/ali/slack/logs
cp /Users/amobrem/ali/slack/com.pulse.slack-claude.plist ~/Library/LaunchAgents/
```

- [ ] **Step 4: Commit**

```bash
cd /Users/amobrem/ali/slack
git add com.pulse.slack-claude.plist
git commit -m "feat: launchd plist for auto-start and crash recovery"
```

---

### Task 8: Install, Configure, and Run

- [ ] **Step 1: Install dependencies**

```bash
cd /Users/amobrem/ali/slack && pip3 install -r requirements.txt
```

- [ ] **Step 2: Create .env from example**

```bash
cd /Users/amobrem/ali/slack && cp .env.example .env
```

Then edit `.env` with real values:
- `SLACK_BOT_TOKEN` — from Slack app OAuth page
- `SLACK_APP_TOKEN` — from Slack app Socket Mode page
- `SLACK_USER_ID` — your Slack member ID
- `SESSION_TOKEN` — generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `VERTEX_PROJECT_ID` — your GCP project
- `VERTEX_REGION` — your region (e.g., `us-east5`)
- `CLAUDE_CODE_CWD` — `/Users/amobrem/ali/pulse-agent`

- [ ] **Step 3: Run all tests**

```bash
cd /Users/amobrem/ali/slack && python3 -m pytest tests/ -v
```

Expected: All tests pass

- [ ] **Step 4: Start the bot manually to verify**

```bash
cd /Users/amobrem/ali/slack && python3 app.py
```

Expected: `Slack Claude Bridge starting (user: U...)` log output. Send a DM in Slack with your session token to test.

- [ ] **Step 5: Enable launchd (optional — for auto-start)**

```bash
launchctl load ~/Library/LaunchAgents/com.pulse.slack-claude.plist
```

- [ ] **Step 6: Final commit**

```bash
cd /Users/amobrem/ali/slack
git add -A
git commit -m "feat: Slack Claude Bridge v1.0 — personal Claude bot with Claude Code remote"
```
