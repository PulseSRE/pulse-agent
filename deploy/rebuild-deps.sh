#!/bin/bash
# Rebuild the deps base image.
# Run when: pyproject.toml changes, or periodically for security patches.
# Usage: ./deploy/rebuild-deps.sh [namespace]
set -e

# Prerequisites
command -v oc &>/dev/null || { echo "ERROR: 'oc' not found. Install the OpenShift CLI."; exit 1; }
oc whoami &>/dev/null || { echo "ERROR: Not logged in. Run 'oc login' first."; exit 1; }

NS="${1:-openshiftpulse}"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Compute deps hash for cache invalidation label
DEPS_HASH=$(shasum -a 256 "$SCRIPT_DIR/pyproject.toml" | cut -d' ' -f1)

# Ensure ImageStream exists
oc create imagestream pulse-agent-deps -n "$NS" 2>/dev/null || true

# Ensure BuildConfig exists
if ! oc get bc pulse-agent-deps -n "$NS" &>/dev/null; then
    cat <<EOF | oc apply -f - -n "$NS"
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: pulse-agent-deps
spec:
  output:
    to:
      kind: ImageStreamTag
      name: "pulse-agent-deps:latest"
  source:
    type: Binary
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Dockerfile.deps
      buildArgs:
        - name: DEPS_HASH
          value: "$DEPS_HASH"
EOF
fi

echo "==> Building deps image (hash: ${DEPS_HASH:0:8})..."
oc start-build pulse-agent-deps --from-dir="$SCRIPT_DIR" --build-arg="DEPS_HASH=$DEPS_HASH" --follow -n "$NS"

# Ensure code BC references deps image
oc patch bc pulse-agent -n "$NS" --type=json \
  -p='[{"op":"replace","path":"/spec/strategy/dockerStrategy","value":{"from":{"kind":"ImageStreamTag","name":"pulse-agent-deps:latest"}}}]' \
  2>/dev/null || true

echo "==> Deps image updated. Next code deploy will use the new base."
