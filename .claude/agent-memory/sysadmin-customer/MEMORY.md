# Sysadmin Customer Review Memory

## Project: OpenShift Pulse (Dashboard) v4.2.0
- See previous entries for dashboard project details

## Project: Pulse Agent (CLI/AI)
- Path: /Users/amobrem/ali/open
- 5 Python files, Helm chart, no tests
- AI-powered SRE + Security agent using Claude API + K8s Python client
- @beta_tool decorator for tool schema generation
- Dual mode: SRE (20 tools) + Security Scanner (9 tools)
- Vertex AI or direct Anthropic API
- See `pulse-agent-review.md` for full review details

## Architecture Patterns (Confirmed)
- Shell: CommandBar + TabBar + Outlet + Dock + StatusBar (Dashboard)
- Navigation via `useNavigateTab()` hook (Dashboard)
- Agent: manual agentic loop with streaming, @beta_tool decorators
- Agent RBAC: read-only default, write ops behind Helm flag

## Key Review Findings (Pulse Agent, 2026-03-24)
- BLOCKER: Zero tests
- BLOCKER: No audit logging for write operations
- BLOCKER: No confirmation gate server-side (relies on LLM prompt)
- HIGH: Hardcoded model, no retry logic, no cost controls
- HIGH: Duplicate code between agent.py and security_agent.py
- See `pulse-agent-review.md` for complete list
