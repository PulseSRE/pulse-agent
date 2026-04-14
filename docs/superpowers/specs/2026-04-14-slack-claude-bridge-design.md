# Slack Claude Bridge — Design Spec

**Date:** 2026-04-14
**Location:** `/Users/amobrem/ali/slack/`
**Purpose:** Personal Slack bot that provides Claude chat (via Vertex AI) and remote control of Claude Code running on the user's Mac, accessible from mobile.

## Problem

Claude Desktop and the Claude mobile app use Anthropic account billing, not Vertex AI credentials. The user hits rate limits on their Anthropic subscription but has Vertex AI capacity available. They need a way to access Claude from their phone using Vertex billing, with the ability to interact with their local Claude Code instance.

## Requirements

- Personal bot — single user only
- Multi-turn conversation (thread-based history)
- Claude chat via Vertex AI (bypasses Anthropic billing limits)
- Remote control of Claude Code on the user's Mac (read files, run commands, edit code)
- Context sharing — see what Claude Code is working on
- Dual auth: Slack user ID + session token
- Runs locally on the Mac (not deployed to cluster)

## Architecture

Single Python process on macOS with three layers:

1. **Slack layer** — `slack_bolt` in socket mode (no public URL needed)
2. **Claude layer** — Anthropic SDK with Vertex AI for general chat
3. **Claude Code bridge** — SDK for structured operations, CLI fallback

### Data Flow

```
Phone -> Slack DM -> Socket Mode -> Auth gate (user ID + token)
                                        |
                                    Router: chat or code?
                                   /              \
                             Claude (Vertex)    Claude Code bridge
                             multi-turn chat    SDK -> CLI fallback
                                   \              /
                                Slack reply (in thread)
```

## Components

### `app.py` — Entry point
- Slack bolt app in socket mode
- Event handlers for messages and mentions
- Starts the socket mode handler

### `auth.py` — Two-gate authentication
- Gate 1: Slack user ID must match configured ID
- Gate 2: First message in a thread must include the session token (e.g., `pulse: what's the git status`)
- After first auth in a thread, subsequent messages in that thread are trusted
- Unauthorized users receive no response (silent ignore)

### `chat.py` — Vertex AI Claude conversations
- Anthropic SDK configured for Vertex AI
- Thread-keyed conversation history (dict of thread_id -> messages list)
- In-memory only — no database, cleared on restart
- Multi-turn within a thread, fresh context per new thread

### `bridge.py` — Claude Code bridge
- Primary: Claude Code CLI via `claude -p "..." --output-format stream-json` for structured responses
- Fallback: plain `claude -p "..."` text mode
- Capabilities:
  - Run commands (tests, git operations, shell commands)
  - Read and edit files
  - Search the codebase
  - Ask Claude Code questions about current work
- Long responses chunked at 3500 chars for Slack's 4k message limit

### `router.py` — Intent routing
- Decides per-message: direct Claude chat vs Claude Code bridge
- Keyword/intent detection: mentions of files, commands, code, tests, git -> bridge
- Everything else -> direct Claude chat
- Explicit prefix support: `cc:` forces Claude Code, `chat:` forces direct Claude

### `config.py` — Settings
- Pydantic settings with env var support
- Fields: Slack bot token, Slack app token, Slack user ID, session token, Vertex project ID, Vertex region, Claude Code working directory

## Authentication

Two-gate auth protects the Mac since the bot has shell access:

1. **Slack user ID** — configured in `.env`, checked on every message
2. **Session token** — must appear in the first message of each thread
3. **Thread trust** — once a thread is authenticated, all subsequent messages in it are trusted

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Claude Code not running | Reply: "Claude Code not available, falling back to direct chat" |
| Command timeout (>120s) | "Still working..." update at 30s, hard timeout at 120s |
| Auth failure | Silent ignore |
| Slack rate limits | Handled by `slack_bolt` |
| Process crash | Auto-restart via macOS `launchd` plist |

## Process Management

- `launchd` plist for auto-start on login and auto-restart on crash
- Installed at `~/Library/LaunchAgents/com.pulse.slack-claude.plist`

## Dependencies

- `slack-bolt` — Slack SDK with socket mode
- `anthropic` — Anthropic SDK (Vertex AI support)
- `pydantic-settings` — typed config
- Claude Code CLI (`claude -p` with `--output-format stream-json`) — no separate SDK package; bridge uses subprocess

## File Structure

```
/Users/amobrem/ali/slack/
  app.py          # entry point
  auth.py         # two-gate authentication
  chat.py         # Vertex AI Claude conversations
  bridge.py       # Claude Code bridge (SDK + CLI)
  router.py       # intent routing
  config.py       # Pydantic settings
  .env            # secrets (Slack tokens, user ID, session token)
  requirements.txt
  com.pulse.slack-claude.plist  # launchd auto-restart
```
