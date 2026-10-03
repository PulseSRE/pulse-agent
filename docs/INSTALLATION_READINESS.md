# Installation readiness observations

`GET /readiness` is an authenticated, read-only installation diagnostic. Through the UI proxy the URL is `/api/agent/readiness`. Use the usual agent bearer token; do not put tokens in URLs. `/healthz` remains process liveness and `/health` retains its existing database/circuit-breaker diagnostic. Neither proves that a user can diagnose and repair an incident.

The response contains `status`, `checked_at` (UTC ISO timestamp), `scope` (`agent installation credentials`), `checks`, and `limitations`. Each check has `id`, `status`, `message`, `remediation`, and `source`; remediation may be empty for healthy observations. Check status is `healthy`, `unhealthy`, or `unknown`. Overall status is `degraded` if any check is unhealthy, otherwise `unknown` if any check is unknown, otherwise `healthy`. Responses are cached for 60 seconds; retries during that window return the original timestamp.

| Check ID | Actual observation | Practical limit |
| --- | --- | --- |
| `provider_configuration` | Complete Vertex project/region or direct Anthropic credential is present, following runtime client selection | Presence does not validate credentials. Partial Vertex configuration is reported unhealthy because runtime falls back to direct Anthropic. Vertex application default credentials are not validated. |
| `provider_connectivity` | Unknown; no provider request is sent | There is no existing nonbillable model/inference probe. Run the provider-backed outcome gate separately to demonstrate deployed model access. |
| `database` | Existing pool `SELECT 1` health check | Does not validate schema, migrations, durable outcome writes, backup or recovery. |
| `kubernetes_pods` | Installation identity lists all-namespace pods, limit 1 | Demonstrates that list request, not each namespace's content or caller access. |
| `kubernetes_deployments` | Installation identity lists all-namespace Deployments, limit 1 | Does not grant mutation permission. |
| `kubernetes_nodes` | Installation identity lists nodes, limit 1 | Does not validate metrics APIs or node operations. |
| `kubernetes_events` | Installation identity lists all-namespace events, limit 1 | Does not validate every scanner or historical event retention. |
| `kubernetes_logs` | SelfSubjectAccessReview for cluster-wide `get pods/log` | Does not retrieve log contents. A namespace-restricted deployment may need different scoped coverage; this diagnostic deliberately measures installation-wide monitoring prerequisites. |
| `monitor` | Background monitor instance reports running | A running loop does not prove recent scans completed or every scanner succeeded. |

Kubernetes requests use installation credentials, not forwarded user tokens. Lists are read-only and permission reviews create no stored cluster object. They have connection/read request timeouts; each probe's response wait is also bounded to six seconds. Python cannot cancel a blocked synchronous database operation: a timed-out thread may finish later, and `unknown` must not be interpreted as connectivity success. A single-flight cache limits repeated requests. The endpoint sends no provider inference, no mutation and no raw API/credential/database error body to the browser.

A successful database/list observation is `healthy` only for that check. Denied Kubernetes credentials/permissions are `unhealthy`; unavailable, inconclusive or timed-out probes are `unknown`. Provider connectivity remains unknown in this endpoint, so a fully configured installation currently returns overall `unknown` until external inference evidence is reviewed. That is intentional, not a request to weaken the classification.

Use the supplied remediation messages to fix prerequisite problems, then verify the three incident paths with the actual browser, configured model/provider and explicit disposable test namespace. Keep the operator's cluster acceptance report, backend outcome gate and UI isolated acceptance results as separate evidence. These observations do not certify incident recovery or any individual user's approval/write authority.

Controlled regression tests can run without database or cluster access:

```sh
python -m unittest tests.test_installation_readiness -q
```

The tests exercise configuration redaction, explicit permission verdicts, installation identity, bounded Kubernetes arguments, unknown/error aggregation, authentication dependency, and cached single flight. They do not establish live installation health.
