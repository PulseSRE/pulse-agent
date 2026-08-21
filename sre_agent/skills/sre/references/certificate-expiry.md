# Certificate expiry on OpenShift

Load this when `get_tls_certificates` reports EXPIRING/EXPIRED, when clients report
TLS handshake failures, or when a route stops being admitted.

## Establish which certificate is actually failing

Three different certificates get blamed for each other:

| Certificate | Owner | Symptom when expired |
|---|---|---|
| Router default (`router-certs-default` in `openshift-ingress`) | ingress operator | every route on the default domain fails at once |
| Per-route custom cert (secret referenced by the Route) | the app team | one hostname fails, others fine |
| Service serving cert (`service.beta.openshift.io/serving-cert-secret-name`) | service CA operator | in-cluster callers fail, external clients unaffected |

Check `days_left` in `get_tls_certificates` before anything else. A cluster-wide
outage with one expiring cert in `openshift-ingress` is the router cert; a single
failing hostname with a healthy router cert is a per-route secret.

## Order of operations for renewal

Renewal is not a single step, and the rollout is the part that gets missed.

1. **Confirm the source.** Service serving certs rotate automatically — if one has
   expired, the service CA operator is degraded and renewing by hand fixes the
   symptom while leaving the cause. Check `get_cluster_operators` first.
2. **Replace the secret.** For a router cert this is a TLS secret in
   `openshift-ingress`; for a route it is the secret the Route references.
3. **Wait for propagation.** The ingress operator reconciles the secret into the
   router pods. This is not instant and does not always restart them.
4. **Verify the router actually picked it up.** A replaced secret with stale router
   pods is the most common "I renewed it and it still fails" case. Confirm the
   rollout completed rather than assuming reconciliation implies restart.
5. **Re-check the route is admitted.** `list_routes` shows `Admitted`; a route can
   be admitted with a bad certificate, so check both.

## Common wrong turns

- **Renewing the wrong layer.** Replacing a route's certificate when the router's
  default cert is what expired changes nothing.
- **Treating a service serving cert as an operator problem.** These rotate on their
  own; expiry means look at the service CA operator, not the certificate.
- **Deleting router pods to force a reload.** It usually works and it is a cluster
  ingress outage while they come back. Prefer a rollout.

## What to report

Name the specific secret and namespace, the days remaining, the blast radius (one
hostname vs the whole apps domain), and whether renewal requires a rollout. If the
certificate is auto-managed, say so — the fix is the operator, not the cert.
