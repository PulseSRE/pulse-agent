---
name: slo_management
version: 2
description: SLO/SLI tracking, error budget analysis, and burn rate alerting
keywords:
  - slo, service level objective, service level
  - sli, service level indicator
  - error budget, burn rate, budget remaining
  - availability target, reliability target
  - set slo, define slo, create slo, add slo
  - check slo, slo status, slo health
  - nine, nines, 99.9, 99.99
categories:
  - monitoring
  - diagnostics
write_tools: false
priority: 5
trigger_patterns:
  - "slo|service.level|error.budget|burn.rate"
  - "set.*slo|define.*slo|create.*slo"
  - "availability.*target|reliability.*target"
  - "99\\.9|nines"
tool_sequences:
  check_slo: [get_prometheus_query]
  define_slo: [get_prometheus_query]
investigation_framework: |
  1. Identify the service and SLO type (availability, latency, error rate)
  2. Query Prometheus for current metric values
  3. Calculate error budget remaining and burn rate
  4. Determine alert level (ok/warning/critical)
  5. Recommend actions if budget is depleting
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
