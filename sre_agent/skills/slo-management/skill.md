---
name: slo_management
version: 1
description: SLO/SLI tracking, error budget analysis, and burn rate alerting
keywords:
  - slo
  - sli
  - error budget
  - burn rate
  - availability
  - reliability target
  - service level
categories:
  - monitoring
  - diagnostics
write_tools: false
priority: 5
---

## SLO Management

Help users define, monitor, and analyze Service Level Objectives.

**Security**: This skill monitors SLO compliance but does not execute changes. All analysis is read-only.

### Capabilities
- Define SLOs for services (availability, latency, error_rate)
- Check current burn rate against error budget
- Alert when error budget is depleting (fast burn = P1, slow burn = P2)
- Recommend actions when SLOs are at risk

### SLO Types
- **Availability**: Percentage of successful requests (e.g., 99.9%)
- **Latency**: P99 response time target (e.g., < 500ms)
- **Error Rate**: Maximum acceptable error percentage (e.g., < 1%)

### When to escalate
- Error budget < 10% remaining → P1 incident
- Error budget < 30% remaining → P2 investigation
- Burn rate > 10x normal → immediate action needed
