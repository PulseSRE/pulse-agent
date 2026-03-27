#!/bin/bash
# Stop hook: After Claude finishes responding, check if tests should be written.
# Inspects git diff to see if source files changed without corresponding test updates.

set -euo pipefail

# Check if source files were modified but tests weren't
CHANGED_SRC=$(git diff --name-only HEAD 2>/dev/null | grep -E '^sre_agent/.*\.py$' | grep -v '__pycache__' || true)
CHANGED_TESTS=$(git diff --name-only HEAD 2>/dev/null | grep -E '^tests/.*\.py$' || true)

# Also check unstaged changes
UNSTAGED_SRC=$(git diff --name-only 2>/dev/null | grep -E '^sre_agent/.*\.py$' | grep -v '__pycache__' || true)
UNSTAGED_TESTS=$(git diff --name-only 2>/dev/null | grep -E '^tests/.*\.py$' || true)

ALL_SRC="$CHANGED_SRC"$'\n'"$UNSTAGED_SRC"
ALL_TESTS="$CHANGED_TESTS"$'\n'"$UNSTAGED_TESTS"

# Remove empty lines
ALL_SRC=$(echo "$ALL_SRC" | grep -v '^$' | sort -u || true)
ALL_TESTS=$(echo "$ALL_TESTS" | grep -v '^$' | sort -u || true)

if [ -n "$ALL_SRC" ] && [ -z "$ALL_TESTS" ]; then
  FILELIST=$(echo "$ALL_SRC" | head -5 | tr '\n' ', ' | sed 's/,$//')
  jq -n --arg files "$FILELIST" '{
    "hookSpecificOutput": {
      "hookEventName": "Stop",
      "additionalContext": ("TEST WRITER: Source files were modified without corresponding test updates: " + $files + ". Consider writing tests for the changes. Use the test patterns from tests/conftest.py (mock_k8s fixture, _make_pod/_make_node helpers, _text() wrapper). Reference: .claude/agents/test-writer.md")
    }
  }'
else
  exit 0
fi
