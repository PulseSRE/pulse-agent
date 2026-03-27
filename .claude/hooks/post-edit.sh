#!/bin/bash
# PostToolUse hook: Route file edits to the appropriate auditor agent.
# Fires after Edit/Write operations and injects context based on which files were changed.

set -euo pipefail
INPUT=$(cat)

FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE" ] && exit 0

# Tool files → tool-auditor
if echo "$FILE" | grep -qE '(k8s_tools|security_tools|fleet_tools|gitops_tools|timeline_tools|predict_tools|git_tools)\.py$'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "TOOL AUDITOR: A tool file was modified. Verify: (1) inputs validated with _validate_k8s_name/_validate_k8s_namespace, (2) K8s API calls wrapped in safe(), (3) write tools added to WRITE_TOOLS set, (4) no secrets leaked in output, (5) tool added to ALL_TOOLS. Reference: .claude/agents/tool-auditor.md"
    }
  }'
  exit 0
fi

# API/protocol files → protocol-checker
if echo "$FILE" | grep -qE '(api\.py|API_CONTRACT\.md|serve\.py|monitor\.py)$'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "PROTOCOL CHECKER: A protocol-related file was modified. Verify: (1) all message types in API_CONTRACT.md are implemented, (2) WebSocket events match the contract, (3) REST endpoints return correct schemas, (4) rate limiting and auth are intact. Reference: .claude/agents/protocol-checker.md"
    }
  }'
  exit 0
fi

# Security-relevant files → security-hardener
if echo "$FILE" | grep -qE '(security_|Dockerfile|values\.yaml|networkpolicy|clusterrole|secret|SECURITY\.md|config\.py)'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "SECURITY HARDENER: A security-relevant file was modified. Verify: (1) no hardcoded credentials, (2) RBAC is least-privilege, (3) container runs as non-root, (4) input validation intact, (5) prompt injection defenses preserved. Reference: .claude/agents/security-hardener.md"
    }
  }'
  exit 0
fi

# Memory files → memory-auditor
if echo "$FILE" | grep -qE 'memory/(store|patterns|retrieval|runbooks|evaluation|memory_tools)\.py$'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "MEMORY AUDITOR: A memory system file was modified. Verify: (1) no SQL injection — use parameterized queries, (2) no secrets stored in memory DB, (3) concurrent access handled, (4) data bounded with pruning, (5) graceful degradation when DB unavailable. Reference: .claude/agents/memory-auditor.md"
    }
  }'
  exit 0
fi

# Runbook files → runbook-writer context
if echo "$FILE" | grep -qE 'runbooks\.py$'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "RUNBOOK WRITER: A runbook file was modified. Verify: (1) all referenced tool names exist in the codebase, (2) diagnostic steps are ordered broad→specific, (3) exit codes are mapped correctly, (4) remediation suggestions are actionable, (5) runbook is concise (5-10 lines max). Reference: .claude/agents/runbook-writer.md"
    }
  }'
  exit 0
fi

exit 0
