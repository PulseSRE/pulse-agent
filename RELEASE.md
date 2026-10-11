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

`make eval-gate` matches the checked-in replay gate: model `claude-sonnet-5`, concurrency 4, minimum judge score 60, three judge samples, and `sre_agent/evals/baselines/replay.json`. These are repository settings; provider availability must be verified by the run.

## Release evidence beyond replay

The [eval workflow](.github/workflows/evals.yml) also runs the production agent through SRE-Bench's simulation and independently enforces artifact validity, mandatory safety/outcome checks, and the core/error score gates. Preserve both `srebench-sim.json` and `live_judge.json`; run the artifact acceptance command in [TESTING](TESTING.md). At this revision the workflow installs SRE-Bench from its `main` branch, so record the installed benchmark revision as well as the agent commit. A mutable dependency name is not an exact benchmark version.

[Container Build](.github/workflows/container-build.yml) builds the production Dockerfile on PR/main and checks packaged entry points without registry publication. Its success is packaging evidence, not model, provider, Kubernetes or incident-recovery evidence. Real-cluster acceptance must still use the operator guide and a disposable namespace, preserving approval, exact target identity and sustained recovery observations.

## Version and publish

`make release VERSION=X.Y.Z` is a convenience target. It runs live judged replay, bumps the agent version/badge, saves a fixture-suite baseline, commits those two version files, and creates a local tag. It **does not** run the whole verification suite, update the UI, or publish anything. Its replay command differs from `make eval-gate` (no explicit minimum judge score or baseline), so run the gate above first.

Alternatively run `scripts/bump-version.sh X.Y.Z`, review and commit the intended changes, then create `vX.Y.Z`. Pushing tags is the publication step and triggers [build-push.yml](.github/workflows/build-push.yml) and [release.yml](.github/workflows/release.yml). Release notes are taken from the matching changelog section; missing notes generate a warning and fallback text. [version-sync.yml](.github/workflows/version-sync.yml) compares the latest non-draft, non-prerelease agent/UI releases, rather than package fields on main.

Verify the image build and actual GitHub release in both repos before deployment. Keep eval artifacts with the release evidence; do not treat a saved baseline as proof the agent passed a live gate.

## Deploy and recover

Install/configure through the operator and an `OpenShiftPulse` resource. Follow its current README and CRD; update agent/UI image references as a tested pair, then verify pod health, `/version`, interactive responses, read-only cluster access, and approval/rollback behavior on a disposable namespace.

For an application rollback, restore the previous known-good image pair through the CR. Preserve published tags/releases for traceability. Database migrations are forward-only; rolling an image back does not revert the schema, so confirm compatibility and backups first. Do not delete release tags as a deployment recovery procedure.
