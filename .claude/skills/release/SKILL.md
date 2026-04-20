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

**Key rule:** Capture ALL scores and counts into a structured summary. Every phase appends
to a running report that becomes the GitHub release body.

## Pre-flight: Determine Version

Ask the user for the version if not provided. Use semver (MAJOR.MINOR.PATCH).
Check the current version:

```bash
grep '^version' pyproject.toml | head -1
```

## Release Checklist

Execute each phase in order. Stop on any failure. Track all results for the summary.

---

### Phase 1: Verify (both repos)

Run in parallel where possible:

**Backend (pulse-agent):**
```bash
# Lint + format + type check
ruff check sre_agent/ tests/ 2>&1 | tail -1
ruff format --check sre_agent/ tests/ 2>&1 | tail -1
mypy sre_agent/ --ignore-missing-imports 2>&1 | tail -1

# Tests -- capture count
python3 -m pytest tests/ --tb=no -q 2>&1 | tail -1
```

**Frontend (OpenshiftPulse):**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
npx tsc --noEmit 2>&1 | tail -1
npx vitest run 2>&1 | grep -E "(Test Files|Tests)" | head -2
```

**Record:** backend_tests=N, frontend_tests=N, lint=PASS, typecheck=PASS

---

### Phase 2: Full Suite + Gate Checks

Run ALL suites and capture scores in a structured table.

**2a. Selector routing (deterministic, GATING):**
```bash
python3 -c "
import sre_agent.skill_loader as sl
sl._skills = {}; sl._keyword_index = []; sl._selector = None; sl._HARD_PRE_ROUTE.clear()
from sre_agent.evals.selector_eval import run_selector_eval
r = run_selector_eval()
print(f'SELECTOR_RESULT: {r.passed}/{r.total_scenarios} ({r.passed/r.total_scenarios:.0%})')
if r.failed_scenarios:
    for f in r.failed_scenarios:
        print(f'  FAIL: {f[\"id\"]}: got {f[\"got\"]} expected {f[\"expected\"]}')
"
```
**Record:** selector_passed=N, selector_total=N, selector_pct=N%

**2b. Gating suites (needs API key):**

Run each and capture scores as JSON:
```bash
python3 -m sre_agent.evals.cli --suite release --fail-on-gate --format json --output /tmp/pulse-release.json
python3 -m sre_agent.evals.cli --suite view_designer --fail-on-gate --format json --output /tmp/pulse-view-designer.json
```

Extract scores:
```bash
python3 -c "
import json
for suite in ['release', 'view_designer']:
    with open(f'/tmp/pulse-{suite}.json') as f:
        data = json.load(f)
    avg = data['average_overall']
    gate = 'PASS' if data['gate_passed'] else 'FAIL'
    dims = data['dimension_averages']
    blockers = data['blocker_counts']
    print(f'{suite.upper()}: {gate} (avg={avg:.3f})')
    print(f'  resolution={dims.get(\"resolution\",0):.2f} efficiency={dims.get(\"efficiency\",0):.2f} safety={dims.get(\"safety\",0):.2f} speed={dims.get(\"speed\",0):.2f}')
    if any(v > 0 for v in blockers.values()):
        print(f'  blockers: {blockers}')
"
```

**Record:** release_gate, release_avg, vd_gate, vd_avg, plus per-dimension scores

**2c. Baseline regression check:**
```bash
python3 -m sre_agent.evals.cli --suite release --compare-baseline 2>&1
python3 -m sre_agent.evals.cli --suite view_designer --compare-baseline 2>&1
```
**Record:** regressions=N (any regression blocks release unless intentional)

**2d. Non-gating suites (informational):**
```bash
for suite in core safety integration adversarial; do
    python3 -m sre_agent.evals.cli --suite $suite --format json --output /tmp/pulse-$suite.json 2>&1 | tail -3
done
```

Extract all scores:
```bash
python3 -c "
import json, os
for suite in ['core', 'safety', 'integration', 'adversarial']:
    path = f'/tmp/pulse-{suite}.json'
    if not os.path.exists(path): continue
    with open(path) as f:
        data = json.load(f)
    avg = data['average_overall']
    passed = data['passed_count']
    total = data['scenario_count']
    print(f'{suite}: {passed}/{total} passed (avg={avg:.3f})')
"
```

**Record:** core_avg, safety_avg, integration_avg, adversarial_avg

**2e. Prompt token audit:**
```bash
python3 -m sre_agent.evals.cli --audit-prompt --mode sre 2>&1 | grep -E "(Total|Section|tokens)" | head -20
python3 -m sre_agent.evals.cli --audit-prompt --mode security 2>&1 | grep -E "(Total|Section|tokens)" | head -20
```
**Record:** sre_prompt_tokens=N, security_prompt_tokens=N

**2f. Chaos tests (if live cluster available):**
```bash
make chaos-test 2>&1 | tail -5
```
**Record:** chaos_score=N% (skip if no cluster)

---

### Phase 3: Code Review + Security Review + Simplify

**3a. Change summary** since last release tag:
```bash
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "HEAD~20")
echo "=== Commits since $LAST_TAG ==="
git log --oneline $LAST_TAG..HEAD
echo ""
echo "=== Files changed ==="
git diff $LAST_TAG..HEAD --stat | tail -1
```

**3b. Code review** -- use pre-commit-reviewer agent on the diff.

**3c. Security review** -- flag HIGH severity findings (blocks release).

**3d. Simplify** -- run `/simplify` on recently changed files. Fix issues.

---

### Phase 4: Update All Documentation

Compute current counts dynamically and update every doc that references them.

**Step 4a: Compute counts**
```bash
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
scenarios = sum(len(json.load(open(f)).get('scenarios',[])) for f in glob.glob('sre_agent/evals/scenarios_data/*.json'))
print(f'Total scenarios: {scenarios}')
suites = len(glob.glob('sre_agent/evals/scenarios_data/*.json'))
print(f'Suites: {suites}')
"
```

**Step 4b: Update each doc file.** Read, check for stale counts/versions, update.

| File | What to update |
|------|----------------|
| `CLAUDE.md` | Tool count, module count, scenarios, version string, new key files |
| `README.md` | Version badge, feature list |
| `API_CONTRACT.md` | New REST/WS message types, component specs |
| `TESTING.md` | Backend test count, frontend test count, suite count |
| `CHANGELOG.md` | New version section from git log (see 4c) |
| `SECURITY.md` | New security controls, RBAC changes |
| `DATABASE.md` | New migrations, tables, schema changes |
| `docs/ARCHITECTURE.md` | New modules, tools, architectural changes |
| `sre_agent/evals/README.md` | Updated suite list, scenario counts |

**Step 4c: Generate CHANGELOG entry:**
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
echo ""
echo "### Docs"
git log --oneline $LAST_TAG..HEAD --grep="docs:" --format="- %s"
```

Prepend this to `CHANGELOG.md`.

**Step 4d: Frontend docs (OpenshiftPulse)** -- same process.

**Step 4e: Update GitHub Pages** (both repos' `docs/index.html`).

**Step 4f: Commit doc updates in both repos.**

---

### Phase 5: Version Bump (both repos)

**Backend:**
```bash
make release VERSION=<version>
```

**Frontend:**
```bash
cd /Users/amobrem/ali/OpenshiftPulse
npm version <version> --no-git-tag-version
git add package.json
git commit -m "chore: bump UI version to <version>"
```

---

### Phase 6: Push and Tag

```bash
git push && git push --tags
cd /Users/amobrem/ali/OpenshiftPulse && git tag "v<version>" && git push && git push --tags
```

---

### Phase 7: Wait for CI + Verify

**CRITICAL: Do not proceed until CI passes on both repos.**

```bash
# Wait for backend CI to complete
gh run list --limit 1 --json status,conclusion,name | python3 -c "
import json, sys
runs = json.load(sys.stdin)
if runs:
    r = runs[0]
    print(f'Backend CI: {r[\"name\"]} -- {r[\"status\"]} ({r.get(\"conclusion\", \"pending\")})')
    if r['status'] != 'completed' or r.get('conclusion') != 'success':
        print('WARNING: CI not yet passed')
else:
    print('No CI runs found')
"

# Wait for frontend CI
cd /Users/amobrem/ali/OpenshiftPulse
gh run list --limit 1 --json status,conclusion,name | python3 -c "
import json, sys
runs = json.load(sys.stdin)
if runs:
    r = runs[0]
    print(f'Frontend CI: {r[\"name\"]} -- {r[\"status\"]} ({r.get(\"conclusion\", \"pending\")})')
"
```

If CI is still running, wait and re-check. If CI fails, fix and re-push before proceeding.

**Record:** backend_ci=PASS/FAIL, frontend_ci=PASS/FAIL

---

### Phase 8: GitHub Release with Full Summary

Build the release body from ALL captured values:

```markdown
## Release Summary

### Test Results
| Suite | Result |
|-------|--------|
| Backend unit tests | BACKEND_TESTS passed |
| Frontend unit tests | FRONTEND_TESTS passed |
| Lint + Type check | Clean |
| Backend CI | PASS/FAIL |
| Frontend CI | PASS/FAIL |

### Gate Scores
| Suite | Gate | Avg Score | Resolution | Efficiency | Safety | Speed |
|-------|------|-----------|------------|------------|--------|-------|
| **Selector** | SELECTOR_PCT | — | — | — | — | — |
| **Release** | RELEASE_GATE | RELEASE_AVG | RES_R | EFF_R | SAF_R | SPD_R |
| **View Designer** | VD_GATE | VD_AVG | RES_V | EFF_V | SAF_V | SPD_V |

### Informational Scores
| Suite | Avg Score | Scenarios |
|-------|-----------|-----------|
| Core | CORE_AVG | N |
| Safety | SAFETY_AVG | N |
| Integration | INTEG_AVG | N |
| Adversarial | ADVER_AVG | N |

### Prompt Token Budget
| Mode | Tokens |
|------|--------|
| SRE | SRE_TOKENS |
| Security | SEC_TOKENS |

### Baseline Regressions
REGRESSIONS (or "None detected")

### Changes
CHANGELOG_CONTENT
```

Replace ALL placeholders with actual captured values, then create releases:

```bash
gh release create "v<version>" --title "Pulse Agent v<version>" --notes-file /tmp/release-notes.md
cd /Users/amobrem/ali/OpenshiftPulse
gh release create "v<version>" --title "OpenShift Pulse v<version>" --notes-file /tmp/release-notes.md
```

---

### Phase 9: Deploy + E2E

```bash
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/deploy.sh
```

Verify agent reports the new version. Run integration tests if available:
```bash
cd /Users/amobrem/ali/OpenshiftPulse && ./deploy/integration-test.sh --namespace openshiftpulse
```

Smoke test: load app, chat response, resource browser, custom views.

**Record:** deploy=PASS/FAIL, integration=PASS/FAIL, smoke=PASS/FAIL

---

### Phase 10: Post-Release

**10a. Save baselines:**
```bash
python3 -m sre_agent.evals.cli --suite release --save-baseline
python3 -m sre_agent.evals.cli --suite view_designer --save-baseline
```

**10b. Print the final release report:**

```
================================================================
  RELEASE v<version> COMPLETE
================================================================

  Tests
  ──────────────────────────────────────────────────────────
  Backend:          BACKEND_TESTS passed
  Frontend:         FRONTEND_TESTS passed
  Lint/Format/Mypy: Clean
  Backend CI:       PASS/FAIL
  Frontend CI:      PASS/FAIL

  Gate Scores (must pass)
  ──────────────────────────────────────────────────────────
  Selector:         SELECTOR_PASSED/SELECTOR_TOTAL (SELECTOR_PCT)
  Release:          RELEASE_GATE (avg=RELEASE_AVG)
    resolution=RES_R efficiency=EFF_R safety=SAF_R speed=SPD_R
  View Designer:    VD_GATE (avg=VD_AVG)
    resolution=RES_V efficiency=EFF_V safety=SAF_V speed=SPD_V

  Informational Scores
  ──────────────────────────────────────────────────────────
  Core:             CORE_AVG
  Safety:           SAFETY_AVG
  Integration:      INTEG_AVG
  Adversarial:      ADVER_AVG

  Prompt Budget
  ──────────────────────────────────────────────────────────
  SRE mode:         SRE_TOKENS tokens
  Security mode:    SEC_TOKENS tokens

  Baseline Regressions: REGRESSIONS
  Chaos Score:          CHAOS_SCORE

  Artifacts
  ──────────────────────────────────────────────────────────
  Agent image:      quay.io/amobrem/pulse-agent:<version>
  UI image:         quay.io/amobrem/openshiftpulse:<ui-tag>
  Backend tag:      v<version>
  Frontend tag:     v<version>
  GitHub releases:  <urls>

  Deploy
  ──────────────────────────────────────────────────────────
  Cluster:          CLUSTER_URL
  Status:           DEPLOY_STATUS
  Integration:      INTEG_STATUS
  Smoke test:       SMOKE_STATUS

================================================================
```

Replace ALL placeholders with actual values captured during the release.

---

## Rollback

If something goes wrong after push:

```bash
git tag -d v<version>
git push origin :refs/tags/v<version>
git revert HEAD
git push
```

## Quick Reference

```bash
/release 2.5.0       # Full 10-phase release
/release --dry-run    # Verify + evals only, no version bump
```
