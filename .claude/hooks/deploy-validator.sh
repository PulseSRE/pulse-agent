#!/bin/bash
# PreToolUse hook: Validate deploy/helm commands before execution.
# Triggers the deploy-validator agent context when running deploy scripts or helm commands.

set -euo pipefail
INPUT=$(cat)

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

# Match deploy scripts, helm install/upgrade, or oc start-build
if echo "$COMMAND" | grep -qE '(deploy/|helm (install|upgrade|template)|oc start-build|oc rollout)'; then
  # Inject deploy-validator context
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "additionalContext": "DEPLOY VALIDATOR: You are about to run a deploy command. Before proceeding, verify: (1) helm lint chart/ passes, (2) no hardcoded secrets in values.yaml, (3) image tags are correct, (4) RBAC and security context are properly configured. Check chart/values.yaml and Dockerfile if you have not already. Reference: .claude/agents/deploy-validator.md"
    }
  }'
else
  exit 0
fi
