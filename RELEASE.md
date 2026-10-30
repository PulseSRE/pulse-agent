# Release process

Agent and [pulse-ui](https://github.com/PulseSRE/pulse-ui) are released as a matched version pair. The [operator](https://github.com/PulseSRE/pulse-operator) has its own version and controls the deployed image references. There is no Helm chart or `deploy/deploy.sh` in this repository.

## Prepare and validate

1. Run the checks in [TESTING](TESTING.md), including the actual provider-backed gate when releasing model/prompt changes.
2. Review changes and update `CHANGELOG.md`, API/security documentation, and affected operational guides. Test counts and benchmark scores are results of a specific run, not permanent release guarantees.
3. Choose the same agent/UI version and prepare each repository independently. `scripts/bump-version.sh X.Y.Z` updates **only** this repository's `pyproject.toml` and README badge; it does not edit the UI.
4. Review the final diff, clean worktree, and corresponding UI version before committing and tagging.

```bash
make verify
python -m ruff format --check sre_agent/ tests/
python scripts/check_discipline.py
make eval-gate  # provider calls; requires credentials
```

`make eval-gate` matches the checked-in replay gate: model `claude-sonnet-5`, concurrency 4, minimum judge score 60, three judge samples, and `sre_agent/evals/baselines/replay.json`. It also runs the live monitor-investigation replay (11 scenarios, 3 attempts each, judged against the pass rates in `sre_agent/evals/investigation_baseline.json`: right root cause, evidence citing tools actually called, no writes on any attempt, injected instructions not followed, Brier ≤ 0.25). These are repository settings; provider availability must be verified by the run.

## Release evidence beyond replay

The [eval workflow](.github/workflows/evals.yml) also runs the production agent through SRE-Bench's simulation and independently enforces artifact validity, mandatory safety/outcome checks, and the core/error score gates. Preserve `srebench-sim.json`, `live_judge.json` and `investigation_replay.json`; run the artifact acceptance command in [TESTING](TESTING.md). The workflow installs SRE-Bench at a pinned commit (see the step's comment for how to move the pin), so a change in that repository cannot move this gate without a commit here.

[Container Build](.github/workflows/container-build.yml) builds the production Dockerfile on PR/main and checks packaged entry points without registry publication. Its success is packaging evidence, not model, provider, Kubernetes or incident-recovery evidence. Real-cluster acceptance must still use the operator guide and a disposable namespace, preserving approval, exact target identity and sustained recovery observations.

## Version and publish

`make release VERSION=X.Y.Z` is a convenience target. It runs live judged replay, bumps the agent version/badge, saves a fixture-suite baseline, commits those two version files, and creates a local tag. It **does not** run the whole verification suite, update the UI, or publish anything. Its replay command differs from `make eval-gate` (no explicit minimum judge score or baseline), so run the gate above first.

Alternatively run `scripts/bump-version.sh X.Y.Z`, review and commit the intended changes, then create `vX.Y.Z`. Pushing tags is the publication step and triggers [build-push.yml](.github/workflows/build-push.yml) and [release.yml](.github/workflows/release.yml). Release notes are taken from the matching changelog section; missing notes generate a warning and fallback text. [version-sync.yml](.github/workflows/version-sync.yml) compares the latest non-draft, non-prerelease agent/UI releases, rather than package fields on main.

Verify the image build and actual GitHub release in both repos before deployment. Keep eval artifacts with the release evidence; do not treat a saved baseline as proof the agent passed a live gate.

## MCP server image

The MCP server sidecar is built from [openshift/openshift-mcp-server](https://github.com/openshift/openshift-mcp-server) at the commit pinned in `build-push.yml` (`MCP_SERVER_REF`), not from upstream `main`. A release tag pushes `quay.io/amobrem/pulse-agent:mcp-server-vX.Y.Z` (the operator, from v0.13.0, runs the one matching `spec.agent.image`) and `mcp-server-<commit>`, and still moves the old `mcp-server` tag for operators up to v0.12.0. The build's summary records the commit and tags.

Moving the pin is a change like any other: a PR that sets `MCP_SERVER_REF` to the new commit, with what changed upstream since the old one (`git log OLD..NEW` in that repo) in the description. To build an MCP image without a release, run `build-push.yml` manually with `mcp_only` (and `mcp_tag` to name it); a manual build never moves `mcp-server`.

## Every release covers all three repositories

A release of the agent/UI pair also checks the operator: what shipped in it since its last tag, and whether its docs name the new pair. When pulse-operator `main` has commits since its last tag, it is released in the same pass (it was not, from v0.11.0 to v0.12.0, so the self-heal fix sat merged and unshipped while v2.36.0 went out).

For pulse-operator (its own version line, e.g. v0.12.0):

1. Bump `OperatorVersion` in `internal/controller/compat.go` to the tag without `v`. `release.yml` refuses a tag that does not match.
2. Cut a `## vX.Y.Z (date)` section in its `CHANGELOG.md` from `Unreleased`, saying which agent/UI pair it pairs with and any behaviour change on upgrade, and add the release to Pages (`docs/index.html`: header version, "What's new", footer, example CR images).
3. Tag it. `release.yml` builds the operator, bundle and catalog images (the CSV version and `replaces` are set from the tag in CI) and `github-release.yml` publishes the GitHub Release from the changelog section.
4. When the operator does not change, still update its docs to say which agent/UI versions run on the current operator unchanged, and note any agent migrations that run on start.

Also check sre-bench: its README and Pages cite Pulse's simulation scores by version.

**Tags.** Tag pushes are made from the maintainer's machine, with their GitHub credentials. Keep one script that pushes every tag of a release, agent, UI and operator together, and skips any tag already on GitHub so it can be re-run. After the push, verify in each repository: the image build, the GitHub Release (and that it is marked latest), version-sync for the agent/UI pair, and for the operator the bundle and catalog images.

## Deploy and recover

Install/configure through the operator and an `OpenShiftPulse` resource. Follow its current README and CRD; update agent/UI image references as a tested pair, then verify pod health, `/version`, interactive responses, read-only cluster access, and approval/rollback behavior on a disposable namespace.

For an application rollback, restore the previous known-good image pair through the CR. Preserve published tags/releases for traceability. Database migrations are forward-only; rolling an image back does not revert the schema, so confirm compatibility and backups first. Do not delete release tags as a deployment recovery procedure.
