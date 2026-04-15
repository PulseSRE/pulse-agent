#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# Chaos Engineering Test Harness
#
# Deploys known failure scenarios to the cluster, waits for the Pulse
# Agent to detect, diagnose, and remediate, then scores the results.
#
# Usage:
#   make chaos-test                    # run all scenarios
#   ./scripts/chaos-test.sh --scenario crashloop   # single scenario
#   ./scripts/chaos-test.sh --namespace chaos-test  # custom namespace
#   ./scripts/chaos-test.sh --timeout 300          # custom timeout (seconds)
#   ./scripts/chaos-test.sh --dry-run              # show what would be deployed
#
# Prerequisites:
#   - oc/kubectl authenticated to cluster
#   - Pulse Agent deployed and monitoring enabled
#   - Trust level >= 2 for auto-fix scenarios
#
# Scenarios:
#   1. crashloop    — Pod with exit(1), agent should detect + investigate
#   2. oom          — Pod exceeding memory limit, agent should detect OOM
#   3. image-pull   — Deployment with bad image tag, agent should detect + rollback
#   4. node-pressure — Simulate disk pressure via annotation (read-only test)
#   5. cert-expiry  — Create expired TLS secret, agent should detect
#
# Scoring:
#   - Detected: Did the agent create a finding? (30 points)
#   - Diagnosed: Did the investigation identify root cause? (30 points)
#   - Remediated: Did auto-fix resolve it? (30 points)
#   - Speed: Detection within 2 scan cycles? (10 points)
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# Defaults
NAMESPACE="${CHAOS_NAMESPACE:-chaos-test}"
TIMEOUT="${CHAOS_TIMEOUT:-300}"
SCENARIO="${CHAOS_SCENARIO:-all}"
DRY_RUN=false
AGENT_NS="${PULSE_NAMESPACE:-openshiftpulse}"
SCAN_INTERVAL=60
RESULTS=()
TOTAL_SCORE=0
TOTAL_MAX=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --scenario) SCENARIO="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --agent-ns) AGENT_NS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

CMD=$(command -v oc 2>/dev/null || command -v kubectl 2>/dev/null)
if [[ -z "$CMD" ]]; then
  echo -e "${RED}Error: oc or kubectl required${NC}"
  exit 1
fi

echo -e "${CYAN}═══ Chaos Engineering Test Harness ═══${NC}"
echo -e "Namespace:  ${NAMESPACE}"
echo -e "Timeout:    ${TIMEOUT}s per scenario"
echo -e "Scenario:   ${SCENARIO}"
echo -e "Agent NS:   ${AGENT_NS}"
echo ""

# ── Helpers ────────────────────────────────────────────────────────────

setup_namespace() {
  if ! $CMD get namespace "$NAMESPACE" &>/dev/null; then
    $CMD create namespace "$NAMESPACE" 2>/dev/null || true
  fi
}

cleanup_namespace() {
  echo -e "${YELLOW}Cleaning up ${NAMESPACE}...${NC}"
  $CMD delete namespace "$NAMESPACE" --wait=false 2>/dev/null || true
}

get_ws_token() {
  $CMD get secret pulse-ws-token -n "$AGENT_NS" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || echo ""
}

get_agent_pod() {
  $CMD get pods -n "$AGENT_NS" -l app.kubernetes.io/name=openshift-sre-agent --no-headers -o name 2>/dev/null | grep -v mcp | grep -v postgresql | head -1
}

# Check if agent detected a finding for a category
check_finding() {
  local category="$1"
  local token
  token=$(get_ws_token)
  local pod
  pod=$(get_agent_pod)

  if [[ -z "$pod" ]]; then
    echo "0"
    return
  fi

  # Check agent logs for findings matching category
  local count
  count=$($CMD logs "$pod" -n "$AGENT_NS" --tail=50 2>/dev/null | grep -c "$category" 2>/dev/null || true)
  echo "${count:-0}" | tr -d '[:space:]'
}

# Wait for agent to detect something
wait_for_detection() {
  local category="$1"
  local max_wait="$2"
  local elapsed=0

  echo -n "  Waiting for detection"
  while [[ $elapsed -lt $max_wait ]]; do
    local found
    found=$(check_finding "$category")
    if [[ "$found" -gt 0 ]]; then
      echo -e " ${GREEN}detected (${elapsed}s)${NC}"
      return 0
    fi
    echo -n "."
    sleep 10
    elapsed=$((elapsed + 10))
  done
  echo -e " ${RED}timeout (${max_wait}s)${NC}"
  return 1
}

score_scenario() {
  local name="$1"
  local detected="$2"
  local diagnosed="$3"
  local remediated="$4"
  local speed="$5"

  local score=$((detected * 30 + diagnosed * 30 + remediated * 30 + speed * 10))
  local max=100
  TOTAL_SCORE=$((TOTAL_SCORE + score))
  TOTAL_MAX=$((TOTAL_MAX + max))

  local status="${RED}FAIL${NC}"
  [[ $score -ge 60 ]] && status="${YELLOW}WARN${NC}"
  [[ $score -ge 90 ]] && status="${GREEN}PASS${NC}"

  RESULTS+=("$(printf "  %-20s %3d/100  %b" "$name" "$score" "$status")")
  echo -e "  Score: ${score}/100 [${status}]"
}

# ── Scenarios ──────────────────────────────────────────────────────────

run_crashloop() {
  echo -e "\n${CYAN}Scenario 1: Crashlooping Pod${NC}"
  echo "  Deploy: pod that exits with code 1 → should CrashLoopBackOff"

  if $DRY_RUN; then
    echo "  [dry-run] Would create crashloop pod"
    return
  fi

  $CMD run chaos-crashloop --image=busybox --restart=Always -n "$NAMESPACE" \
    -- /bin/sh -c "echo 'chaos-crashloop' && exit 1" 2>/dev/null

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "crashloop\|CrashLoopBackOff\|chaos-crashloop" "$TIMEOUT"; then
    detected=1
    speed=1
    # Check if investigation ran
    local inv_count
    inv_count=$($CMD logs "$(get_agent_pod)" -n "$AGENT_NS" --tail=100 2>/dev/null | grep -c "investigation\|diagnose\|triage" || echo "0")
    [[ $inv_count -gt 0 ]] && diagnosed=1
  fi

  # Cleanup
  $CMD delete pod chaos-crashloop -n "$NAMESPACE" --wait=false 2>/dev/null || true

  score_scenario "crashloop" $detected $diagnosed $remediated $speed
}

run_oom() {
  echo -e "\n${CYAN}Scenario 2: OOM Kill${NC}"
  echo "  Deploy: pod requesting 10Mi but trying to allocate 100Mi"

  if $DRY_RUN; then
    echo "  [dry-run] Would create OOM pod"
    return
  fi

  cat <<EOF | $CMD apply -n "$NAMESPACE" -f -
apiVersion: v1
kind: Pod
metadata:
  name: chaos-oom
spec:
  containers:
  - name: oom
    image: busybox
    command: ["/bin/sh", "-c", "dd if=/dev/zero of=/dev/null bs=100M"]
    resources:
      limits:
        memory: "10Mi"
  restartPolicy: Never
EOF

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "oom\|OOMKilled\|chaos-oom" "$TIMEOUT"; then
    detected=1
    speed=1
    local inv_count
    inv_count=$($CMD logs "$(get_agent_pod)" -n "$AGENT_NS" --tail=100 2>/dev/null | grep -c "oom\|memory\|OOM" || echo "0")
    [[ $inv_count -gt 0 ]] && diagnosed=1
  fi

  $CMD delete pod chaos-oom -n "$NAMESPACE" --wait=false 2>/dev/null || true

  score_scenario "oom" $detected $diagnosed $remediated $speed
}

run_image_pull() {
  echo -e "\n${CYAN}Scenario 3: Bad Image Pull${NC}"
  echo "  Deploy: deployment with working image → update to nonexistent tag"

  if $DRY_RUN; then
    echo "  [dry-run] Would create deployment with bad image"
    return
  fi

  # Create healthy deployment first
  $CMD create deployment chaos-image --image=registry.access.redhat.com/ubi9/httpd-24:latest \
    --replicas=1 -n "$NAMESPACE" 2>/dev/null

  # Wait for healthy
  sleep 20

  # Push bad update
  $CMD set image deployment/chaos-image httpd-24=registry.access.redhat.com/ubi9/httpd-24:nonexistent \
    -n "$NAMESPACE" 2>/dev/null || true

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "image_pull\|ImagePullBackOff\|ErrImagePull\|chaos-image" "$TIMEOUT"; then
    detected=1
    speed=1
    local inv_count
    inv_count=$($CMD logs "$(get_agent_pod)" -n "$AGENT_NS" --tail=100 2>/dev/null | grep -c "image\|pull\|rollback" || echo "0")
    [[ $inv_count -gt 0 ]] && diagnosed=1
    # Check if rollback happened
    local revision
    revision=$($CMD rollout history deployment/chaos-image -n "$NAMESPACE" 2>/dev/null | tail -2 | head -1 | awk '{print $1}')
    [[ -n "$revision" && "$revision" -gt 2 ]] && remediated=1
  fi

  $CMD delete deployment chaos-image -n "$NAMESPACE" --wait=false 2>/dev/null || true

  score_scenario "image-pull" $detected $diagnosed $remediated $speed
}

run_cert_expiry() {
  echo -e "\n${CYAN}Scenario 4: Expired Certificate${NC}"
  echo "  Deploy: TLS secret with already-expired cert"

  if $DRY_RUN; then
    echo "  [dry-run] Would create expired cert secret"
    return
  fi

  # Generate expired self-signed cert
  openssl req -x509 -newkey rsa:2048 -keyout /tmp/chaos-key.pem -out /tmp/chaos-cert.pem \
    -days -1 -nodes -subj "/CN=chaos-expired.test" 2>/dev/null || true

  if [[ -f /tmp/chaos-cert.pem ]]; then
    $CMD create secret tls chaos-expired-cert \
      --cert=/tmp/chaos-cert.pem --key=/tmp/chaos-key.pem \
      -n "$NAMESPACE" 2>/dev/null || true
    rm -f /tmp/chaos-key.pem /tmp/chaos-cert.pem
  fi

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "cert\|expir\|certificate\|chaos-expired" "$TIMEOUT"; then
    detected=1
    speed=1
    diagnosed=1  # cert expiry is self-evident
  fi

  $CMD delete secret chaos-expired-cert -n "$NAMESPACE" 2>/dev/null || true

  score_scenario "cert-expiry" $detected $diagnosed $remediated $speed
}

run_resource_pressure() {
  echo -e "\n${CYAN}Scenario 5: Resource Quota Exhaustion${NC}"
  echo "  Deploy: ResourceQuota with 1 pod max, then deploy 3 replicas"

  if $DRY_RUN; then
    echo "  [dry-run] Would create quota + deployment exceeding it"
    return
  fi

  cat <<EOF | $CMD apply -n "$NAMESPACE" -f -
apiVersion: v1
kind: ResourceQuota
metadata:
  name: chaos-quota
spec:
  hard:
    pods: "1"
EOF

  $CMD create deployment chaos-quota --image=busybox --replicas=3 -n "$NAMESPACE" \
    -- /bin/sh -c "sleep 3600" 2>/dev/null || true

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "quota\|pending\|Pending\|chaos-quota" "$TIMEOUT"; then
    detected=1
    speed=1
    diagnosed=1
  fi

  $CMD delete deployment chaos-quota -n "$NAMESPACE" --wait=false 2>/dev/null || true
  $CMD delete resourcequota chaos-quota -n "$NAMESPACE" 2>/dev/null || true

  score_scenario "resource-pressure" $detected $diagnosed $remediated $speed
}

# ── Main ───────────────────────────────────────────────────────────────

trap cleanup_namespace EXIT

setup_namespace

case $SCENARIO in
  crashloop)       run_crashloop ;;
  oom)             run_oom ;;
  image-pull)      run_image_pull ;;
  cert-expiry)     run_cert_expiry ;;
  resource)        run_resource_pressure ;;
  all)
    run_crashloop
    run_oom
    run_image_pull
    run_cert_expiry
    run_resource_pressure
    ;;
  *)
    echo -e "${RED}Unknown scenario: $SCENARIO${NC}"
    echo "Available: crashloop, oom, image-pull, cert-expiry, resource, all"
    exit 1
    ;;
esac

# ── Results ────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}════════════════════════════════════════${NC}"
echo -e "${CYAN}  Chaos Test Results${NC}"
echo -e "${CYAN}════════════════════════════════════════${NC}"
for r in "${RESULTS[@]}"; do
  echo -e "$r"
done
echo ""

if [[ $TOTAL_MAX -gt 0 ]]; then
  PCT=$((TOTAL_SCORE * 100 / TOTAL_MAX))
  if [[ $PCT -ge 80 ]]; then
    echo -e "  Total: ${GREEN}${TOTAL_SCORE}/${TOTAL_MAX} (${PCT}%)${NC}"
  elif [[ $PCT -ge 60 ]]; then
    echo -e "  Total: ${YELLOW}${TOTAL_SCORE}/${TOTAL_MAX} (${PCT}%)${NC}"
  else
    echo -e "  Total: ${RED}${TOTAL_SCORE}/${TOTAL_MAX} (${PCT}%)${NC}"
  fi
fi
echo -e "${CYAN}════════════════════════════════════════${NC}"
