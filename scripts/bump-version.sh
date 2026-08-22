#!/bin/bash
# Bump the agent version in pyproject.toml and the README release badge.
# Usage: ./scripts/bump-version.sh <version>
# Example: ./scripts/bump-version.sh 2.18.0
#
# The Helm charts this used to maintain are gone — the operator
# (github.com/PulseSRE/pulse-operator, installed via OLM) owns deployment now,
# and the agent version it runs is a field on the OpenShiftPulse CR:
#
#   oc patch openshiftpulse pulse -n openshiftpulse --type=merge \
#     -p '{"spec":{"agent":{"image":"quay.io/amobrem/pulse-agent:vX.Y.Z"}}}'
#
# This script also used to reach into the pulse-ui checkout beside this one and
# edit its umbrella chart. That is why an agent release kept leaving uncommitted
# changes in a different repository.
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 2.18.0"
    exit 1
fi

# Validate semver format
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must be semver (e.g. 2.18.0), got: $VERSION"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Update pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/pyproject.toml"
rm -f "$REPO_ROOT/pyproject.toml.bak"

# Update the README release badge. Missed on the 2.12.0 bump and caught only
# by the CI docs-consistency check, which asserts the README mentions the
# packaged version. Doing it here means the check has nothing left to catch.
sed -i.bak -E "s|releases/tag/v[0-9]+\.[0-9]+\.[0-9]+|releases/tag/v$VERSION|; s|badge/release-v[0-9]+\.[0-9]+\.[0-9]+|badge/release-v$VERSION|" "$REPO_ROOT/README.md"
rm -f "$REPO_ROOT/README.md.bak"

# Verify
PY_VER=$(grep '^version = ' "$REPO_ROOT/pyproject.toml" | sed 's/version = "\(.*\)"/\1/')

if ! grep -q "release-v$VERSION" "$REPO_ROOT/README.md"; then
    echo "Error: README release badge was not updated to $VERSION"
    exit 1
fi

if [[ "$PY_VER" != "$VERSION" ]]; then
    echo "Error: pyproject.toml is $PY_VER, expected $VERSION"
    exit 1
fi

echo "Version bumped to $VERSION in:"
echo "  pyproject.toml"
echo "  README.md (release badge)"
echo ""
echo "Next: commit, tag v$VERSION, and once the image builds, roll the cluster:"
echo "  oc patch openshiftpulse pulse -n openshiftpulse --type=merge \\"
echo "    -p '{\"spec\":{\"agent\":{\"image\":\"quay.io/amobrem/pulse-agent:v$VERSION\"}}}'"
