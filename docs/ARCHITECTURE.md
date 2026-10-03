# Pulse Agent architecture

Current code map for the Python agent, Protocol v2. Operational settings come from `sre_agent/config.py`; route definitions and tests take precedence over examples. See the [documentation index](README.md).

## Deployment boundary

[pulse-operator](https://github.com/PulseSRE/pulse-operator) manages the deployed OpenShift product: agent, UI/OAuth proxy, PostgreSQL, MCP, optional Temporal, Secrets, RBAC, Routes, and NetworkPolicies. This repository contains no Helm chart or umbrella deployment script. The standalone CLI can use a Kubernetes kubeconfig; the deployed product requires OpenShift APIs.

The UI reverse proxy supplies the shared agent credential and forwards authenticated identity/access-token headers. A shared service credential alone is not a user's authorization. Network policy and correct proxy configuration are part of that boundary; do not expose the agent publicly as an unauthenticated direct API.

## Entry points and agent loop

- `sre_agent/main.py`: interactive CLI (`sre-agent` / `python -m sre_agent.main`).
- `sre_agent/serve.py` and `sre_agent/api/`: FastAPI (`pulse-agent-api`), REST and WebSocket endpoints, lifespan tasks.
- `agent.py`: shared asynchronous streaming loop using `AsyncAnthropic` / `AsyncAnthropicVertex`. Synchronous Kubernetes tools run in an executor; callbacks are mediated by `event_bus.py`.
- `config.py`: Pydantic configuration; Python 3.11+ and package dependencies are declared in `pyproject.toml`.

The orchestration path classifies/routs prompts to skill configurations. `skill_loader.py`, `skill_router.py`, `skill_selector.py`, `selector_learning.py`, and `synthesis.py` contain loading, routing, selection/learning, and multi-skill result synthesis. Skill routing features are configuration-dependent; static benchmark counts are not a runtime invariant.

## Tools and skills

`tool_registry.py`, the decorators, native `k8s_tools/` modules, and MCP discovery define available tools. Actual tool counts depend on loaded skills and MCP servers; use `/version`, `/tools`, and `/agents` to inspect a running instance. Seven built-in skill packages live in `sre_agent/skills/`; additional skills can be installed/authored at runtime. See [skill development](SKILL_DEVELOPER_GUIDE.md).

Native and MCP registry write classifications trigger the interactive confirmation path; callers cannot remove them with an empty write set. Unknown MCP tools default to writes, while trusted read-only annotations can exempt them. Registry collisions cannot override native or other-server tools. `policy.py` checks protected-namespace pod deletion and node-operation rules on the chat and auto-fix paths. This is a scoped policy, not a generic denial of all production changes. `tool_contracts.py` attaches precondition/snapshot/postcondition handling to five tools: restart, scale, pod deletion, deployment rollback, and node cordon. Other tools and execution paths must be assessed separately; there is no universal contract covering every mutation.

## Views and protocol

`view_tools.py`, the view REST/WS modules, `quality_engine.py`, layout helpers, and component catalogs handle generated views, validation, versioning, claims, and lifecycle changes. The UI must validate/render the matching `ComponentSpec` contract; a tool returning structured data does not establish that every UI component exists.

The streaming endpoints are `/ws/agent` and `/ws/monitor`, both authenticated by the configured shared token. Interactive turns stream text, thinking, tool activity, components, and confirmation events. Monitor connections receive findings, investigations, action/verification reports, snapshots, and scan status. [API_CONTRACT](../API_CONTRACT.md) documents messages and routes; dynamically generated OpenAPI shows REST schemas on a running server.

## Monitor and trust

`monitor/cluster_monitor.py` owns the server monitor and scan loop, scanner instances, subscriber state, investigation scheduling, and auto-fix dispatch. It starts from the API lifespan, independent of a browser connection. Scanner protocols and registry modules provide metadata and implementations; `/monitor/scanners` exposes current coverage.

The server setting `PULSE_AGENT_MAX_TRUST_LEVEL` defaults to **2**; `PULSE_AGENT_TRUST_LEVEL` is an accepted alias used by the operator. The canonical name wins when both are present. WebSocket requests are clamped to that setting, while effective trust is the maximum of the configured setting and subscribers. Consequently browser settings cannot lower the server's effective level. Set observe-only behavior on the server/CR when validating without writes.

Levels 0/1 do not enter remediation; level 2 proposes actions for approval; level 3 automatically handles the effective categories; level 4 handles all fixable findings. **Current category limitation:** effective categories start with every registered auto-fix handler and union subscriber selections. Browser subsets therefore cannot restrict level-3 categories. Do not use that UI control as a safety boundary.

Safety controls include a per-scan cap, per-resource cooldown/attempt tracking, controller-owned pod checks, deny policy, and the persisted pause flag. Verification reads live state and may return unverifiable; disappearance of a finding alone is insufficient evidence of resolution. `monitor/recurrence.py` revises a previously verified outcome when the condition returns within the configured horizon. Approval, verification, concurrency conflicts, and denied operations need real cluster testing as well as mocked tests.

## Persistence and learning

`db.py`, `db_schema.py`, and `db_migrations.py` provide PostgreSQL persistence, currently through migration **036**. Connections use configured pooling; see [DATABASE](../DATABASE.md). Memory, findings/actions/investigations, views/chat, analytics, episodes, learned candidates, and runtime artifacts are persisted in distinct tables.

`artifact_store.py` writes runtime-authored skill/plan/eval documents and their versions to the database and restores them at startup. `trajectory.py` and related learning modules gate promotion on verified outcomes. A preserved artifact or score is not by itself proof of a safe or useful learned procedure.

## Plans and Temporal

`plan_runtime.py` is the in-process plan engine; `skill_plan.py` and plan templates describe phase contracts and dependencies. The `temporal/` package supplies durable workflow/activity paths when `PULSE_AGENT_TEMPORAL_HOST` is set. The operator can provision the service using `spec.temporal`. The worker is a lifespan task in the agent process; orchestration state is maintained by Temporal.

Durable plan and incident-remediation flows have different routing and configuration. Monitor plans still use the in-process engine unless their path explicitly selects durable execution; `PULSE_AGENT_DURABLE_AUTOFIX` is off by default. See [TEMPORAL](TEMPORAL.md) for settings and limitations. PlanRuntime preserves write classifications, supports optional caller-token/confirmation propagation, and serializes write-enabled phases within a run. Without confirmation, writes are denied. Durable start/approve/cancel endpoints require configured administrator authorization; worker writes require an explicit approved phase plus server trust >=2 and use service credentials. Durability does not automatically provide caller authorization; see the security guide for remaining scope limits.

## Security, validation, and history

[SECURITY](../SECURITY.md) describes credential/identity distinctions, caller-token forwarding, scoped policy, and known coverage limitations. [TESTING](../TESTING.md) distinguishes test success, offline harness checks, provider-backed replay, and cluster verification. No source-only audit proves deployment readiness.

Historical specifications/plans under `superpowers/` and [JOURNEY](JOURNEY.md) preserve past design decisions and proposed tasks. They are not current deployment instructions. Runtime behavior must be verified against the code and current tests.
