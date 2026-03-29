#!/bin/bash
# Quick deploy — local build + push (~30-45s total)
# Uses Podman with local layer cache to avoid OpenShift's cold Docker builds.
# Falls back to oc start-build if Podman or registry route is unavailable.
# Usage: ./deploy/quick-deploy.sh [--skip-tests] [namespace]
set -e

SKIP_TESTS=false
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=true; shift ;;
    esac
done

# Prerequisites
command -v oc &>/dev/null || { echo "ERROR: 'oc' not found. Install the OpenShift CLI."; exit 1; }
oc whoami &>/dev/null || { echo "ERROR: Not logged in. Run 'oc login' first."; exit 1; }

NS="${1:-openshiftpulse}"
DEPLOY="pulse-agent-openshift-sre-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Clean up old build pods to free quota
oc delete pod -n "$NS" -l openshift.io/build.name --field-selector=status.phase!=Running 2>/dev/null || true

# Auto-detect Dockerfile: use Dockerfile.full if deps image doesn't exist
DOCKERFILE="Dockerfile"
if ! oc get istag pulse-agent-deps:latest -n "$NS" &>/dev/null; then
    echo "==> No deps image found, using full build..."
    DOCKERFILE="Dockerfile.full"
fi

# Run tests before building (fail early)
if [[ "$SKIP_TESTS" == "false" ]]; then
    echo "==> Running tests..."
    cd "$SCRIPT_DIR"
    python3 -m pytest tests/ -q || { echo "ERROR: Tests failed. Use --skip-tests to bypass."; exit 1; }
fi

# Try to get external registry route
REGISTRY=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}' 2>/dev/null || echo "")

if command -v podman &>/dev/null && podman info &>/dev/null && [[ -n "$REGISTRY" ]]; then
    # === FAST PATH: Local Podman build + direct push ===
    IMAGE="$REGISTRY/$NS/pulse-agent:latest"

    echo "==> Building locally with Podman (cached layers)..."
    cd "$SCRIPT_DIR"
    podman build --platform linux/amd64 -t "$IMAGE" -f "$DOCKERFILE" . 2>&1 | tail -5

    echo "==> Logging into registry..."
    SA_TOKEN=$(oc create token builder -n "$NS" 2>/dev/null || oc whoami -t)
    podman login "$REGISTRY" -u unused -p "$SA_TOKEN" --tls-verify=false 2>&1 | tail -1  # Internal registry only — TLS not required for pod-to-registry traffic

    echo "==> Pushing image..."
    podman push "$IMAGE" --tls-verify=false 2>&1 | tail -5  # Internal registry only — TLS not required for pod-to-registry traffic

    echo "==> Pinning image digest..."
    DIGEST=$(oc get istag pulse-agent:latest -n "$NS" -o jsonpath='{.image.dockerImageReference}')
    oc set image "deployment/$DEPLOY" "sre-agent=$DIGEST" -n "$NS"
else
    # === FALLBACK: OpenShift binary build ===
    echo "==> No Podman or registry route — using oc start-build..."
    cd "$SCRIPT_DIR"
    if ! oc start-build pulse-agent --from-dir=. --follow -n "$NS"; then
        echo "ERROR: Code build failed."
        if ! oc get istag pulse-agent-deps:latest -n "$NS" &>/dev/null; then
            echo "Deps image missing. Falling back to full build..."
            oc patch bc pulse-agent -n "$NS" --type=json \
              -p='[{"op":"replace","path":"/spec/strategy/dockerStrategy","value":{"dockerfilePath":"Dockerfile.full"}}]'
            oc start-build pulse-agent --from-dir=. --follow -n "$NS"
            oc patch bc pulse-agent -n "$NS" --type=json \
              -p='[{"op":"replace","path":"/spec/strategy/dockerStrategy","value":{"from":{"kind":"ImageStreamTag","name":"pulse-agent-deps:latest"}}}]'
        else
            exit 1
        fi
    fi

    echo "==> Pinning image digest..."
    DIGEST=$(oc get istag pulse-agent:latest -n "$NS" -o jsonpath='{.image.dockerImageReference}')
    oc set image "deployment/$DEPLOY" "sre-agent=$DIGEST" -n "$NS"
fi

echo "==> Restarting deployment..."
oc rollout restart "deployment/$DEPLOY" -n "$NS"
oc rollout status "deployment/$DEPLOY" -n "$NS" --timeout=60s

# === WS Token Sync for UI compatibility ===
echo "==> Syncing WS auth token for UI..."
SECRET_NAME=$(oc get deployment "$DEPLOY" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PULSE_AGENT_WS_TOKEN")].valueFrom.secretKeyRef.name}' 2>/dev/null || echo "${DEPLOY}-ws-token")

if oc get secret "$SECRET_NAME" -n "$NS" &>/dev/null; then
    TOKEN=$(oc get secret "$SECRET_NAME" -n "$NS" -o jsonpath='{.data.token}' | base64 -d 2>/dev/null || echo "")
    if [[ -n "$TOKEN" ]]; then
        echo "    Token secret: $SECRET_NAME"
        echo "    Token (first 12 chars): ${TOKEN:0:12}..."
        oc set env deployment/"$DEPLOY" PULSE_AGENT_WS_TOKEN="$TOKEN" -n "$NS" --overwrite
        echo "    ✓ WS token synced to deployment"
    fi
else
    echo "    ⚠️ WS token secret not found"
fi

echo "==> Verifying health..."
for i in 1 2 3; do
    sleep 5
    AGENT_POD=$(oc get pod -l app.kubernetes.io/instance=pulse-agent -n "$NS" --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
    if [[ -n "$AGENT_POD" ]]; then
        HEALTH=$(oc exec "$AGENT_POD" -n "$NS" -- curl -sf localhost:8080/healthz 2>/dev/null || echo "")
        if [[ "$HEALTH" == *"ok"* ]]; then
            echo "==> Agent is healthy!"
            break
        fi
    fi
    [[ $i -eq 3 ]] && echo "WARNING: Agent health check failed after 3 attempts"
done

echo "==> Done!"
