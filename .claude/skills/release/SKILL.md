---
name: release
description: |
  Automate the full Pulse release process across pulse-agent, pulse-ui and pulse-operator, which share one version number.
  Use this skill when the user says "release", "cut a release", "bump version", "ship it",
  "prepare release", "release v2.x.x", or anything about creating a new version. Also use
  when they ask to "update version numbers", "tag a release", or "publish a new version".
  This skill coordinates both backend and frontend into a single version number.
---

# Pulse release workflow

Use [RELEASE.md](../../../RELEASE.md) as the maintained procedure. Agent, UI and operator share one version number (from v2.39.0): a release is `vX.Y.Z` in all three repositories. Resolve repository paths from the workspace rather than a developer's absolute home path.

1. Inspect current versions, intended release scope, diff, and clean worktrees.
2. Run complete backend/static tests using a disposable PostgreSQL database and UI checks in the actual pulse-ui checkout.
3. Run `make eval-gate` for actual provider-backed replay; collect its transcript/score/baseline artifacts. Offline fixture-suite scores are informational and cannot establish agent quality.
4. Review source/security changes and update changelogs and affected guides. Record actual test counts and skipped/blocked checks.
5. Bump the agent and UI to the release version, review changes, commit and tag them. `scripts/bump-version.sh` only edits the agent package/README.
   **Always release pulse-operator with the same version** ("Every release covers all three repositories" in RELEASE.md): `OperatorVersion` in `compat.go`, the manager image in `deploy/operator.yaml`, the agent/UI/MCP/Helm-runner digests (resolved from the agent/UI `build-push.yml` run summaries after their tags build), a CHANGELOG section and Pages. Check sre-bench's cited scores too.
   Give the user one tag script that pushes the tags and skips tags that already exist; the operator tag follows once its pin commit has merged.
6. Publish only within user authorization; verify image builds, releases (each marked latest), paired-version checks, the operator's bundle and catalog images, and remote CI.
7. Deployment is a distinct authorized action through pulse-operator. Cluster acceptance is required before declaring deployment healthy.

Do not delete published tags as an application rollback. Restore the previous known-good image pair through the CR and account for forward-only database migrations. Never silently skip a failed prerequisite or label a missing check PASS.
