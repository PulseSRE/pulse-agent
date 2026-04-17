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
python3 -m pytest tests/ -v                    # all tests must pass
python3 -m mypy sre_agent/ --no-error-summary  # zero type errors
python3 -m ruff check sre_agent/ tests/        # zero lint errors
python3 -m ruff format --check sre_agent/ tests/  # formatting clean
```

**Frontend (OpenshiftPulse):**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
npm run type-check    # tsc --noEmit
npm test              # vitest --run
```

Report: "Backend: X tests passed. Frontend: Y tests passed. Type-check clean."

### Phase 2: Gate Checks

Run the release gate suites. These are the CI gates -- if they fail, stop.

```bash
python3 -m sre_agent.evals.cli --suite release --fail-on-gate
python3 -m sre_agent.evals.cli --suite view_designer --fail-on-gate
```

Also run the selector routing accuracy check:
```bash
python3 -c "
from sre_agent.evals.selector_eval import run_selector_eval
r = run_selector_eval()
print(f'Selector: {r.passed}/{r.total_scenarios} ({r.passed/r.total_scenarios:.0%})')
if r.failed_scenarios:
    for f in r.failed_scenarios:
        print(f'  FAIL: {f[\"id\"]}: got {f[\"got\"]} expected {f[\"expected\"]}')
"
```

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

**Step 4e: Commit doc updates in both repos**
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

### Phase 8: Deploy (optional)

If the user wants to deploy immediately:

```bash
cd /Users/amobrem/ali/OpenshiftPulse
./deploy/deploy.sh
```

### Phase 9: Post-Release

After a successful release:

1. Save baselines for the new version:
```bash
python3 -m sre_agent.evals.cli --suite release --save-baseline
python3 -m sre_agent.evals.cli --suite view_designer --save-baseline
```

2. Update the umbrella chart subchart version (already done by bump-version.sh)

3. Report the release summary:
```
Release v<version> complete!
- Backend: X tests, Y scenarios, Z% release gate
- Frontend: A tests, type-check clean
- Images: quay.io/amobrem/pulse-agent:<version>
- Tags: pulse-agent v<version>, OpenshiftPulse v<version>
- GitHub releases created
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
