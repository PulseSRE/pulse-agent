---
name: performance-analyzer
description: Profile and optimize hot paths in the agent loop, API endpoints, and frontend rendering
---

# Performance Analyzer

Identify bottlenecks and optimize performance across the Pulse Agent stack.

## What to analyze

### Backend
- Agent loop latency (tool execution, LLM calls, prompt assembly)
- API endpoint response times (FastAPI endpoints in sre_agent/api/)
- Database query performance (PostgreSQL queries in db.py)
- Tool execution time (k8s_tools/, security_tools.py)
- Memory usage during monitor scanning
- Prompt token costs (system prompt size, tool definitions)

### Frontend
- Component render cycles (React profiler)
- Bundle size (recharts lazy loading, code splitting)
- WebSocket message handling latency
- TanStack Query cache efficiency
- Virtualization performance (TableView with 1000+ rows)

## How to profile

### Backend timing
```python
# Check tool execution stats
from sre_agent.tool_usage import get_tool_stats
stats = get_tool_stats()
# Sort by avg duration to find slow tools
```

### Prompt audit
```bash
python3 -m sre_agent.evals.cli --audit-prompt --mode sre
python3 -m sre_agent.evals.cli --audit-prompt --mode security
```

### Frontend bundle
```bash
cd ../pulse-ui
npx rspack build --mode production --profile
```

## Output

Report findings as a prioritized list:
1. What's slow (with measurements)
2. Why it's slow (root cause)
3. How to fix it (specific code changes)
4. Expected improvement (estimated)
