# API Type Contract: Agent ↔ UI

## WebSocket Events (Server → Client)

| Event | Agent Source | UI Type | Key Fields |
|-------|-------------|---------|------------|
| `text_delta` | `api.py` on_text | `AgentEvent` | `text` |
| `thinking_delta` | `api.py` on_thinking | `AgentEvent` | `thinking` |
| `tool_use` | `api.py` on_tool_use | `AgentEvent` | `tool` |
| `component` | `api.py` on_component | `AgentEvent` | `spec: ComponentSpec`, `tool` |
| `confirm_request` | `api.py` on_confirm | `AgentEvent` | `tool`, `input`, `nonce` |
| `done` | `api.py` | `AgentEvent` | `full_response` |
| `error` | `api.py` | `AgentEvent` | `message`, `category`, `suggestions` |
| `feedback_ack` | `api.py` | `AgentEvent` | `resolved`, `score`, `runbookExtracted` |
| `view_spec` | `api.py` | `AgentEvent` | `spec: ViewSpec` |
| `cleared` | `api.py` | `AgentEvent` | — |
| `finding` | `monitor.py` | `MonitorEvent` | `Finding` fields + `confidence`, `noiseScore` |
| `action_report` | `monitor.py` | `MonitorEvent` | `ActionReport` fields + `confidence` |
| `prediction` | `monitor.py` | `MonitorEvent` | `Prediction` fields |
| `investigation_report` | `monitor.py` | `MonitorEvent` | `InvestigationReport` fields + `evidence`, `alternativesConsidered` |
| `verification_report` | `monitor.py` | `MonitorEvent` | `VerificationReport` fields |
| `resolution` | `monitor.py` | `MonitorEvent` | `findingId`, `category`, `title`, `resolvedBy` |
| `findings_snapshot` | `monitor.py` | `MonitorEvent` | `activeIds[]` |
| `monitor_status` | `monitor.py` | `MonitorEvent` | `activeWatches`, `lastScan`, `nextScan` |

## WebSocket Messages (Client → Server)

| Message | UI Source | Agent Handler | Key Fields |
|---------|----------|---------------|------------|
| `message` | `agentClient.send()` | `_receive_loop` → queue | `content`, `context?`, `fleet?`, `preferences?` |
| `confirm_response` | `agentClient.confirm()` | `_make_receive_loop` | `approved`, `nonce` |
| `clear` | `agentClient.clear()` | `_make_receive_loop` | — |
| `feedback` | `agentClient.sendFeedback()` | `_make_receive_loop` | `resolved`, `messageId?` |
| `subscribe_monitor` | `monitorClient.connect()` | `/ws/monitor` | `trustLevel`, `autoFixCategories` |
| `action_response` | `monitorClient.approveAction()` | `/ws/monitor` | `actionId`, `approved` |

## REST Endpoints

See `API_CONTRACT.md` for the full 21-endpoint REST API specification.

## Type Sources of Truth

| Type | Agent (Python) | UI (TypeScript) |
|------|---------------|-----------------|
| Finding | `monitor.py:_make_finding()` | `monitorClient.ts:Finding` |
| ActionReport | `monitor.py:_make_action_report()` | `monitorClient.ts:ActionReport` |
| Prediction | `monitor.py:_make_prediction()` | `monitorClient.ts:Prediction` |
| InvestigationReport | `monitor.py` investigation loop | `monitorClient.ts:InvestigationReport` |
| Resolution | `monitor.py` scan loop | `monitorClient.ts:Resolution` |
| ComponentSpec | tool return tuples | `agentComponents.ts:ComponentSpec` |
| ViewSpec | `view_tools.py` | `agentComponents.ts:ViewSpec` |
| BriefingResponse | `monitor.py:get_briefing()` | `fixHistory.ts:BriefingResponse` |
| MemoryStats | `api.py:/memory/stats` | `fixHistory.ts:MemoryStats` |
