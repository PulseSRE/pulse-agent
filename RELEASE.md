# Release Process

Pulse uses a **unified release** — both repos (pulse-agent + OpenshiftPulse) share a single version number. The `/release` skill automates the full process.

## Quick Start

```bash
# In Claude Code:
/release 2.5.0
```

Or manually:

```bash
make release VERSION=2.5.0
git push && git push --tags
```

## Release Phases

### Phase 1: Verify
- Backend: `pytest` (2414+ tests), `mypy` (0 errors), `ruff` (clean)
- Frontend: `vitest` (2045+ tests), `tsc` (clean)

### Phase 2: Eval Gates
- **Selector routing** — 59/59 scenarios, 100% required
- **Release gate** — LLM-judged, min 75% score, no hard blockers
- **View designer gate** — LLM-judged, min 75% score
- **Baseline comparison** — no regressions allowed
- **Non-gating suites** — core, safety, integration, adversarial (informational)
- **Chaos tests** — 5 failure injection scenarios, >= 60% score
- **Prompt audit** — token count check for prompt bloat

### Phase 3: Review
- Code review of all changes since last tag
- Security review (`/security-review`)
- Simplify pass (`/simplify`)

### Phase 4: Documentation
Every release updates these files in **both repos**:

**Backend (pulse-agent):**
| File | Updated |
|------|---------|
| `CLAUDE.md` | Tool count, module count, eval prompts, scenarios, version |
| `README.md` | Version badge, test count, tool count |
| `CHANGELOG.md` | Feature/fix/test categorized entries |
| `API_CONTRACT.md` | New endpoints, component specs |
| `TESTING.md` | Test counts, new patterns |
| `SECURITY.md` | Security controls |
| `DATABASE.md` | New migrations |
| `docs/ARCHITECTURE.md` | Architecture changes |
| `docs/index.html` | GitHub Pages version + counts |

**Frontend (OpenshiftPulse):**
| File | Updated |
|------|---------|
| `CLAUDE.md` | Component count, test count, version |
| `README.md` | Version badge, features |
| `CHANGELOG.md` | Matching backend changelog |
| `docs/index.html` | GitHub Pages version + counts |

### Phase 5: Version Bump
```bash
make release VERSION=X.Y.Z
```
Updates: `pyproject.toml`, `chart/Chart.yaml`, umbrella chart subchart, `package.json`

### Phase 6: Push & Tag
Both repos get the same `vX.Y.Z` tag. Backend tag triggers CI build-push.

### Phase 7: GitHub Release
Auto-generated changelogs for both repos via `gh release create`.

### Phase 8: Deploy + E2E
- Deploy via `./deploy/deploy.sh`
- Integration tests: `./deploy/integration-test.sh`
- Smoke test: verify app loads, agent responds, views render

### Phase 9: Post-Release
- Save eval baselines for regression detection
- Publish eval results to GitHub release notes
- Verify CI built container images

## Version Files

| File | Field |
|------|-------|
| `pyproject.toml` | `[project].version` |
| `chart/Chart.yaml` | `version` + `appVersion` |
| `OpenshiftPulse/deploy/helm/pulse/Chart.yaml` | subchart `version` |
| `OpenshiftPulse/package.json` | `version` |

All synced by `scripts/bump-version.sh`. CI enforces they match.

## Eval Gate Thresholds

| Gate | Min Score | Hard Blockers |
|------|-----------|---------------|
| Release | 0.75 | `policy_violation`, `hallucinated_tool`, `missing_confirmation` |
| View Designer | 0.75 | Same |
| Selector | 100% | Any routing failure |

## Rollback

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
git revert HEAD
git push
```
