---
name: release
description: |
  Automate the full Pulse release process across both repos (pulse-agent + OpenshiftPulse).
  Use this skill when the user says "release", "cut a release", "bump version", "ship it",
  "prepare release", "release v2.x.x", or anything about creating a new version. Also use
  when they ask to "update version numbers", "tag a release", or "publish a new version".
  This skill coordinates both backend and frontend into a single version number.
---

# Pulse Release

Coordinates a release across both repos (pulse-agent + OpenshiftPulse) with a single
version number. Every release must pass all gates before shipping.

## Pre-flight: Determine Version

Ask the user for the version if not provided. Use semver (MAJOR.MINOR.PATCH).
Check the current version:

```bash
grep '^version' pyproject.toml | head -1
```

## Release Checklist

Execute each phase in order. Stop on any failure.

### Phase 1: Verify (both repos)

Run in parallel where possible:

**Backend (pulse-agent):**
```bash
make test-everything   # lint + type-check + pytest + ALL eval suites (deterministic + LLM-judged)
```

This runs `make verify` (ruff lint, ruff format, mypy, pytest) followed by `make evals-full`
(core, safety, sysadmin, integration, adversarial, errors suites + prompt audit).

**Frontend (OpenshiftPulse):**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
npm run type-check    # tsc --noEmit
npm test              # vitest --run
```

Report: "Backend: X tests passed. Frontend: Y tests passed. Type-check clean. Evals: all passed."

**Post-release CI check** -- after pushing, always verify CI passes:
```bash
gh run list --limit 3          # check backend CI
cd /Users/amobrem/ali/OpenshiftPulse && gh run list --limit 3  # check frontend CI
```
If CI fails, fix immediately and push before proceeding.

### Phase 2: Full Eval Suite + Gate Checks

Run ALL evaluation suites. Gate suites MUST pass -- non-gating suites are informational
but failures should be investigated before releasing.

**2a. Deterministic evals (no API key needed):**
```bash
# Selector routing accuracy -- MUST be 100%
python3 -c "
import sre_agent.skill_loader as sl
sl._skills = {}; sl._keyword_index = []; sl._selector = None; sl._HARD_PRE_ROUTE.clear()
from sre_agent.evals.selector_eval import run_selector_eval
r = run_selector_eval()
print(f'Selector: {r.passed}/{r.total_scenarios} ({r.passed/r.total_scenarios:.0%})')
if r.failed_scenarios:
    for f in r.failed_scenarios:
        print(f'  FAIL: {f[\"id\"]}: got {f[\"got\"]} expected {f[\"expected\"]}')
"

# Replay dry-run (offline fixture tests)
python3 -m sre_agent.evals.cli --suite release --replay-only
```

**2b. Live judge evals (needs API key -- GATING):**
```bash
# These call Claude to judge agent responses. MUST pass for release.
python3 -m sre_agent.evals.cli --suite release --fail-on-gate
python3 -m sre_agent.evals.cli --suite view_designer --fail-on-gate
```

If API key is not available, note this in the release summary and ensure
CI runs them on tag push (build-push.yml triggers evals.yml).

**2c. Compare against baseline (regression check):**
```bash
python3 -m sre_agent.evals.cli --suite release --compare-baseline
python3 -m sre_agent.evals.cli --suite view_designer --compare-baseline
```

Any regression blocks the release unless intentional (e.g., prompt change).

**2d. Non-gating suites (informational -- run all):**
```bash
python3 -m sre_agent.evals.cli --suite core
python3 -m sre_agent.evals.cli --suite safety
python3 -m sre_agent.evals.cli --suite integration
python3 -m sre_agent.evals.cli --suite adversarial
```

**2e. Prompt token audit:**
```bash
python3 -m sre_agent.evals.cli --audit-prompt --mode sre
python3 -m sre_agent.evals.cli --audit-prompt --mode security
```

Report token counts and flag if prompts grew significantly since last release.

**2f. Chaos tests (needs live cluster):**
```bash
make chaos-test
```

Runs 5 failure injection scenarios. Score must be >= 60%.

### Phase 3: Code Review + Security Review + Simplify

**3a. Code review** -- review changes since the last release tag:
```bash
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "HEAD~20")
git log --oneline $LAST_TAG..HEAD
git diff $LAST_TAG..HEAD --stat
```

Use the pre-commit-reviewer agent to review the diff. Flag concerns.

**3b. Security review** -- run `/security-review` on the changes since the last tag.
Focus on new endpoints, auth changes, input validation, and injection risks.
Any HIGH severity finding blocks the release.

**3c. Simplify** -- run `/simplify` on recently changed files to catch:
- Unused imports, dead code
- Duplicated logic that should be shared
- Efficiency issues (memory leaks, O(n) lookups, unnecessary re-renders)
- Fix all issues before proceeding.

### Phase 4: Update All Documentation

Compute current counts dynamically and update every doc that references them.

**Step 4a: Compute counts**
```bash
# Import all tool modules to populate registry
python3 -c "
from sre_agent import k8s_tools, security_tools, fleet_tools, gitops_tools, predict_tools, timeline_tools, git_tools, handoff_tools, view_tools, self_tools
from sre_agent.tool_registry import TOOL_REGISTRY
from tests.eval_prompts import EVAL_PROMPTS
import json, glob
print(f'Native tools: {len(TOOL_REGISTRY)}')
print(f'Eval prompts: {len(EVAL_PROMPTS)}')
print(f'k8s_tools modules: {len(glob.glob(\"sre_agent/k8s_tools/*.py\"))}')
print(f'monitor modules: {len(glob.glob(\"sre_agent/monitor/*.py\"))}')
print(f'api modules: {len(glob.glob(\"sre_agent/api/*.py\"))}')
scenarios = sum(len(json.load(open(f)).get(\"scenarios\",[])) for f in glob.glob('sre_agent/evals/scenarios_data/*.json'))
print(f'Total eval scenarios: {scenarios}')
suites = len(glob.glob('sre_agent/evals/scenarios_data/*.json'))
print(f'Eval suites: {suites}')
selector = json.load(open('sre_agent/evals/scenarios_data/selector.json'))
print(f'Selector scenarios: {len(selector[\"scenarios\"])}')
"
```

**Step 4b: Update each doc file.** For every file below, read it, check for stale
counts/versions, and update. Only edit files that actually need changes.

| File | What to update |
|------|----------------|
| `CLAUDE.md` | Tool count (native + MCP), module count (k8s_tools/N, monitor/N, api/N), eval prompts, scenarios, version string, new key files |
| `README.md` | Version badge (`badge/release-vX.Y.Z`), feature list (new features since last release), install instructions if deps changed |
| `API_CONTRACT.md` | New REST endpoints, new WebSocket message types, updated component specs (e.g., datasources on data_table) |
| `TESTING.md` | Backend test count, frontend test count, new test files, eval suite count, CI pipeline changes |
| `CHANGELOG.md` | New version section with categorized changes (Features, Fixes, Breaking Changes) generated from git log since last tag |
| `SECURITY.md` | New security controls, RBAC changes, auth changes |
| `DATABASE.md` | New migrations, new tables, schema changes |
| `CONTRIBUTING.md` | Workflow changes (new skills, new tools pattern) |
| `DESIGN_PRINCIPLES.md` | Only if principles evolved (rare) |
| `docs/ARCHITECTURE.md` | New modules, new tools, architectural changes, updated diagrams |
| `docs/SKILL_DEVELOPER_GUIDE.md` | New skill patterns (e.g., trigger_patterns field) |
| `sre_agent/evals/README.md` | Updated suite list, scenario counts, gate thresholds |

**Step 4c: Generate CHANGELOG entry** from git log:
```bash
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "HEAD~30")
echo "## v<version> ($(date +%Y-%m-%d))"
echo ""
echo "### Features"
git log --oneline $LAST_TAG..HEAD --grep="feat:" --format="- %s"
echo ""
echo "### Fixes"  
git log --oneline $LAST_TAG..HEAD --grep="fix:" --format="- %s"
echo ""
echo "### Tests"
git log --oneline $LAST_TAG..HEAD --grep="test:" --format="- %s"
```

Prepend this to `CHANGELOG.md`.

**Step 4d: Frontend docs (OpenshiftPulse)**

Do the same for the frontend repo:
```bash
cd /Users/amobrem/ali/OpenshiftPulse
```

| File | What to update |
|------|----------------|
| `CLAUDE.md` | Component count, test count, version, new views/hooks/components |
| `README.md` | Version badge, feature list, screenshots |
| `CHANGELOG.md` | New version entry matching the backend |
| `API_CONTRACT.md` | Frontend API contracts, WebSocket protocol |
| `CONTRIBUTING.md` | New patterns (ResourceTable, useMultiSourceTable, etc.) |
| `SECURITY.md` | CSP changes, auth changes, proxy config |

Generate the frontend CHANGELOG the same way (git log since last tag).

**Step 4e: Update GitHub Pages (both repos)**

Both repos have docs sites served from `/docs` on main branch:
- https://alimobrem.github.io/pulse-agent/ (backend)
- https://alimobrem.github.io/OpenshiftPulse/ (frontend)

**Backend** (`/Users/amobrem/ali/pulse-agent/docs/index.html`):
- Update version strings (v2.X.X → v<version>)
- Update tool count, scenario count in meta descriptions
- Verify by reading the file

**Frontend** (`/Users/amobrem/ali/OpenshiftPulse/docs/index.html`):
- Update version in the hero section (`.version` div)
- Update agent version reference and tool count in features section
- Verify by reading the file

Both pages auto-deploy when pushed to main — no manual publish needed.

**Step 4f: Commit doc updates in both repos**
```bash
# Backend
cd /Users/amobrem/ali/pulse-agent
git add -A *.md docs/ sre_agent/evals/README.md
git commit -m "docs: update all documentation for v<version>"

# Frontend
cd /Users/amobrem/ali/OpenshiftPulse
git add -A *.md
git commit -m "docs: update all documentation for v<version>"
```

### Phase 5: Version Bump (both repos)

**Backend:**
```bash
make release VERSION=<version>
# This runs bump-version.sh which updates:
# - pyproject.toml
# - chart/Chart.yaml (version + appVersion)
# - OpenshiftPulse/deploy/helm/pulse/Chart.yaml (subchart version)
```

**Frontend -- bump UI version to match:**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
npm version <version> --no-git-tag-version
git add package.json
git commit -m "chore: bump UI version to <version>"
```

### Phase 6: Push and Tag

**Backend:**
```bash
git push && git push --tags
# Triggers .github/workflows/build-push.yml
# Builds and pushes quay.io/amobrem/pulse-agent:<version>
```

**Frontend:**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
git tag "v<version>"
git push && git push --tags
```

### Phase 7: GitHub Release

Create a GitHub release with auto-generated changelog:

```bash
# Backend
gh release create "v<version>" --generate-notes --title "Pulse Agent v<version>"

# Frontend  
cd /Users/amobrem/ali/OpenshiftPulse
gh release create "v<version>" --generate-notes --title "OpenShift Pulse v<version>"
```

### Phase 8: Deploy + E2E Integration Tests

**8a. Deploy:**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
./deploy/deploy.sh
```

Verify agent reports the new version in the health check output.

**8b. Integration tests (against live cluster):**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
./deploy/integration-test.sh --namespace openshiftpulse
```

Tests: WebSocket connectivity, agent health endpoint, tool execution,
monitor scanning, view CRUD. All must pass.

**8c. Smoke test:** Open the deployed app in a browser and verify:
- Welcome page loads with correct cluster info
- Agent chat responds to a simple query
- Resource browser shows pods
- A custom view renders (if any saved views exist)

### Phase 9: Post-Release

**9a. Save eval baselines** for the new version:
```bash
python3 -m sre_agent.evals.cli --suite release --save-baseline
python3 -m sre_agent.evals.cli --suite view_designer --save-baseline
```

**9b. Publish eval results** to the GitHub release. Edit the release to append:
```bash
gh release edit "v<version>" --notes-file - << 'EVAL'
## Eval Results
- Selector routing: X/Y (Z%)
- Release gate: PASS/FAIL (score%)
- View designer gate: PASS/FAIL (score%)
- Backend tests: N passed
- Frontend tests: N passed
- Chaos tests: score%
- Integration tests: PASS/FAIL
EVAL
```

Do this for both repos' releases.

**9c. Update the umbrella chart** subchart version (already done by bump-version.sh)

**9d. Verify CI** -- check that the tag push triggered build-push.yml:
```bash
gh run list --limit 3
```

Confirm the container image was built and pushed to quay.io.

**9e. Report the release summary:**
```
Release v<version> complete!
- Backend: X tests, Y eval scenarios, Z% release gate
- Frontend: A tests, type-check clean
- Selector: N/N routing accuracy
- Integration: PASS/FAIL
- Images: quay.io/amobrem/pulse-agent:<version>
- Tags: pulse-agent v<version>, OpenshiftPulse v<version>
- GitHub releases: <urls>
- Pages: https://alimobrem.github.io/pulse-agent/
         https://alimobrem.github.io/OpenshiftPulse/
```

## Rollback

If something goes wrong after push:

```bash
# Delete the tag locally and remotely
git tag -d v<version>
git push origin :refs/tags/v<version>

# Revert the version commit
git revert HEAD
git push
```

## Quick Reference

```bash
# Full release (interactive)
/release 2.4.0

# Just verify (no changes)
make verify && cd /Users/amobrem/ali/OpenshiftPulse && npm run type-check && npm test

# Just gate checks
python3 -m sre_agent.evals.cli --suite release --fail-on-gate
```
