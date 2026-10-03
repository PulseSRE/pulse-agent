---
name: deploy
description: |
  Deploy Pulse to an OpenShift cluster with pre-flight checks and health verification.
  Use when the user says "deploy", "push to cluster", "roll out", "ship to prod",
  "deploy to openshift", or wants to update the running application. Handles cluster
  selection, dry-run, rollback, and post-deploy verification.
---

# Pulse deployment workflow

The deployed product is managed by [pulse-operator](https://github.com/PulseSRE/pulse-operator), installed via OLM. There is no local umbrella Helm deploy script. Read the operator README/CRD and [agent README](../../../README.md) before changing a cluster.

1. Confirm the requested context/namespace/CR and inspect `oc whoami`, `oc whoami --show-server`, and `oc get openshiftpulse -A` without exposing credentials.
2. Use tested published agent/UI images as a matched pair. Inspect the existing CR and propose the exact image/config change; do not assume historical Helm resource names.
3. Follow user authorization for deployment. Preview the CR diff and retain previous image/config references for recovery.
4. Apply only the intended CR fields through the operator, then verify rollout, CR conditions, pod readiness, `/version`, authenticated API/UI behavior, and read-only cluster access.
5. Test approved writes/rollback only in a disposable namespace. Browser trust/category controls cannot lower current server autonomy; configure the server/CR accordingly.

Rollback uses the prior known-good image/config pair. Database migrations are forward-only: check compatibility/backups before downgrading. Report checks that actually ran and remaining cluster validation. Never claim success from pod existence alone.
