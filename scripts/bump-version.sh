#!/bin/bash
# Bump version in all locations: pyproject.toml, chart/Chart.yaml
# Usage: ./scripts/bump-version.sh <version>
# Example: ./scripts/bump-version.sh 1.6.0
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 1.6.0"
    exit 1
fi

# Validate semver format
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must be semver (e.g. 1.6.0), got: $VERSION"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Update pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/pyproject.toml"
rm -f "$REPO_ROOT/pyproject.toml.bak"

# Update chart/Chart.yaml
sed -i.bak "s/^version: .*/version: $VERSION/" "$REPO_ROOT/chart/Chart.yaml"
sed -i.bak "s/^appVersion: .*/appVersion: \"$VERSION\"/" "$REPO_ROOT/chart/Chart.yaml"
rm -f "$REPO_ROOT/chart/Chart.yaml.bak"

# Verify
PY_VER=$(grep '^version = ' "$REPO_ROOT/pyproject.toml" | sed 's/version = "\(.*\)"/\1/')
CHART_VER=$(grep '^version: ' "$REPO_ROOT/chart/Chart.yaml" | awk '{print $2}')
APP_VER=$(grep '^appVersion: ' "$REPO_ROOT/chart/Chart.yaml" | sed 's/appVersion: "\(.*\)"/\1/')

if [[ "$PY_VER" != "$VERSION" || "$CHART_VER" != "$VERSION" || "$APP_VER" != "$VERSION" ]]; then
    echo "Error: version sync failed!"
    echo "  pyproject.toml: $PY_VER"
    echo "  Chart.yaml version: $CHART_VER"
    echo "  Chart.yaml appVersion: $APP_VER"
    exit 1
fi

# Update umbrella chart subchart dependency in OpenshiftPulse (if repo exists)
UI_REPO="${REPO_ROOT}/../OpenshiftPulse"
UMBRELLA_CHART="$UI_REPO/deploy/helm/pulse/Chart.yaml"
if [[ -f "$UMBRELLA_CHART" ]]; then
    sed -i.bak "/name: openshift-sre-agent/{n;s/version: \".*\"/version: \"$VERSION\"/;}" "$UMBRELLA_CHART"
    rm -f "$UMBRELLA_CHART.bak"
    UMBRELLA_VER=$(grep -A1 'name: openshift-sre-agent' "$UMBRELLA_CHART" | grep version | sed 's/.*"\(.*\)"/\1/')
    if [[ "$UMBRELLA_VER" == "$VERSION" ]]; then
        echo "  OpenshiftPulse umbrella chart subchart → $VERSION"
    else
        echo "  ⚠️ Failed to update umbrella chart (got $UMBRELLA_VER)"
    fi
else
    echo "  ⚠️ OpenshiftPulse repo not found at $UI_REPO — update umbrella chart manually"
fi

echo "Version bumped to $VERSION in:"
echo "  pyproject.toml"
echo "  chart/Chart.yaml (version + appVersion)"
