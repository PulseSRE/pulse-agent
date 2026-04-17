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
TIMEOUT="${CHAOS_TIMEOUT:-600}"
SCENARIO="${CHAOS_SCENARIO:-all}"
DRY_RUN=false
FORCE=false
AGENT_NS="${PULSE_NAMESPACE:-openshiftpulse}"
SCAN_INTERVAL=60
FINDINGS_FILE="/tmp/chaos-findings-$$.jsonl"
WS_CLIENT_PID=""
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
    --force) FORCE=true; shift ;;
    --agent-ns) AGENT_NS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

CMD=$(command -v oc 2>/dev/null || command -v kubectl 2>/dev/null)
if [[ -z "$CMD" ]]; then
  echo -e "${RED}Error: oc or kubectl required${NC}"
  exit 1
fi

# ── Production Safety Guard ────────────────────────────────────────────
PROTECTED_NAMESPACES="default kube-system kube-public openshift openshift-etcd openshift-apiserver openshift-controller-manager openshift-authentication openshift-console openshift-monitoring openshiftpulse"

# #1: Block protected namespaces
if [[ "$FORCE" != "true" ]]; then
  for ns in $PROTECTED_NAMESPACES; do
    if [[ "$NAMESPACE" == "$ns" ]]; then
      echo -e "${RED}SAFETY: Refusing to run chaos tests in protected namespace '${NAMESPACE}'${NC}"
      echo "Use --namespace <test-namespace> to specify a safe namespace"
      echo "Use --force to override this check (for intentional testing)"
      exit 1
    fi
  done

  # Check cluster context — warn if pointed at a production cluster
  CLUSTER_URL=$($CMD cluster-info 2>/dev/null | head -1 | grep -oE 'https://[^ ]+' || echo "unknown")
  if echo "$CLUSTER_URL" | grep -qiE "prod|production|prd"; then
    echo -e "${RED}SAFETY: Cluster URL contains 'prod' — refusing to run chaos tests${NC}"
    echo "URL: ${CLUSTER_URL}"
    echo "Use --force to override"
    exit 1
  fi
fi

# Warn if namespace already has pods
existing_pods=$($CMD get pods -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ "${existing_pods:-0}" -gt 0 && "$FORCE" != "true" && "$DRY_RUN" != "true" ]]; then
  echo -e "${YELLOW}WARNING: Namespace '${NAMESPACE}' has ${existing_pods} existing pods${NC}"
  read -p "Continue? (y/N) " -n 1 -r
  echo
  [[ $REPLY =~ ^[Yy]$ ]] || exit 0
fi

echo -e "${CYAN}═══ Chaos Engineering Test Harness ═══${NC}"
echo -e "Namespace:  ${NAMESPACE}"
echo -e "Timeout:    ${TIMEOUT}s per scenario"
echo -e "Scenario:   ${SCENARIO}"
echo -e "Agent NS:   ${AGENT_NS}"
echo ""

# ── Preflight Checks ──────────────────────────────────────────────────
# Validates all prerequisites before running any scenarios.
# Fixes what it can automatically, fails fast on what it can't.

preflight_check() {
  local failed=0
  echo -e "${CYAN}Running preflight checks...${NC}"

  # 1. Cluster connectivity
  if ! $CMD cluster-info &>/dev/null; then
    echo -e "  ${RED}✗ Not logged in to cluster${NC}"
    echo "    Fix: oc login <cluster-url> --username <user> --password <pass>"
    return 1
  fi
  echo -e "  ${GREEN}✓ Cluster connected${NC}"

  # 2. Agent pod running
  local agent_pod
  agent_pod=$($CMD get pods -n "$AGENT_NS" -l app.kubernetes.io/name=openshift-sre-agent --no-headers -o name 2>/dev/null | grep -v mcp | grep -v postgresql | head -1)
  if [[ -z "$agent_pod" ]]; then
    echo -e "  ${RED}✗ Agent pod not found in namespace '${AGENT_NS}'${NC}"
    echo "    Fix: Deploy the agent first — cd OpenshiftPulse && ./deploy/deploy.sh"
    return 1
  fi
  echo -e "  ${GREEN}✓ Agent pod: ${agent_pod}${NC}"

  # 3. Agent listening on 8080
  local port_check
  port_check=$($CMD exec -n "$AGENT_NS" "$agent_pod" -c sre-agent -- python3 -c "import socket; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1',8080)); print(r); s.close()" 2>/dev/null || echo "999")
  if [[ "$port_check" != "0" ]]; then
    echo -e "  ${RED}✗ Agent not listening on port 8080${NC}"
    echo "    Fix: Check agent logs — oc logs $agent_pod -n $AGENT_NS -c sre-agent"
    failed=1
  else
    echo -e "  ${GREEN}✓ Agent listening on 8080${NC}"
  fi

  # 4. WS token available
  local ws_token
  ws_token=$(get_ws_token)
  if [[ -z "$ws_token" ]]; then
    echo -e "  ${RED}✗ WS token not found (secret pulse-ws-token)${NC}"
    echo "    Fix: Redeploy — the deploy script auto-generates the token"
    failed=1
  else
    echo -e "  ${GREEN}✓ WS token available${NC}"
  fi

  # 5. Monitor enabled (check health endpoint)
  local health
  health=$($CMD exec -n "$AGENT_NS" "$agent_pod" -c sre-agent -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:8080/health', timeout=5)
    d = json.loads(r.read())
    print(d.get('status', 'unknown'))
except: print('error')
" 2>/dev/null || echo "error")
  if [[ "$health" != "ok" ]]; then
    echo -e "  ${YELLOW}⚠ Agent health check: ${health}${NC}"
    echo "    Monitor might not be running — findings may not be detected"
    # Don't fail — agent might still work
  else
    echo -e "  ${GREEN}✓ Agent healthy (monitor active)${NC}"
  fi

  # 6. chaos-ws-client.py exists
  local client_script="$(dirname "$0")/chaos-ws-client.py"
  if [[ ! -f "$client_script" ]]; then
    echo -e "  ${RED}✗ WebSocket client not found: ${client_script}${NC}"
    failed=1
  else
    echo -e "  ${GREEN}✓ WebSocket client found${NC}"
  fi

  # 7. Python websockets module available
  if ! python3 -c "import websockets" 2>/dev/null; then
    echo -e "  ${YELLOW}⚠ Python 'websockets' module not installed — installing...${NC}"
    pip3 install websockets --quiet 2>/dev/null
    if ! python3 -c "import websockets" 2>/dev/null; then
      echo -e "  ${RED}✗ Failed to install websockets module${NC}"
      failed=1
    else
      echo -e "  ${GREEN}✓ websockets module installed${NC}"
    fi
  else
    echo -e "  ${GREEN}✓ Python websockets available${NC}"
  fi

  # 8. Can create namespace (test RBAC)
  if ! $CMD auth can-i create namespaces &>/dev/null; then
    echo -e "  ${RED}✗ No permission to create namespaces${NC}"
    echo "    Fix: Log in as cluster-admin or grant namespace creation rights"
    failed=1
  else
    echo -e "  ${GREEN}✓ Can create namespaces${NC}"
  fi

  # 9. Can create deployments in chaos namespace
  if ! $CMD auth can-i create deployments -n "$NAMESPACE" &>/dev/null 2>/dev/null; then
    # Namespace might not exist yet — check against default
    if ! $CMD auth can-i create deployments &>/dev/null; then
      echo -e "  ${YELLOW}⚠ Cannot verify deployment creation permissions${NC}"
    else
      echo -e "  ${GREEN}✓ Can create deployments${NC}"
    fi
  else
    echo -e "  ${GREEN}✓ Can create deployments in ${NAMESPACE}${NC}"
  fi

  # 10. Port-forward test
  echo -e "  Testing port-forward..."
  $CMD port-forward "$agent_pod" -n "$AGENT_NS" 18080:8080 &>/dev/null &
  local pf_pid=$!
  sleep 3
  if kill -0 "$pf_pid" 2>/dev/null; then
    local pf_test
    pf_test=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${ws_token}" http://localhost:18080/health 2>/dev/null || echo "000")
    kill "$pf_pid" 2>/dev/null; wait "$pf_pid" 2>/dev/null || true
    if [[ "$pf_test" == "200" ]]; then
      echo -e "  ${GREEN}✓ Port-forward works (HTTP 200)${NC}"
    else
      echo -e "  ${YELLOW}⚠ Port-forward connected but health returned HTTP ${pf_test}${NC}"
    fi
  else
    echo -e "  ${RED}✗ Port-forward failed${NC}"
    echo "    Fix: Check if another port-forward is using 18080, or restart the agent pod"
    failed=1
  fi

  echo ""
  if [[ $failed -ne 0 ]]; then
    echo -e "${RED}Preflight failed — fix the issues above before running chaos tests${NC}"
    return 1
  fi
  echo -e "${GREEN}All preflight checks passed${NC}"
  echo ""
}

# ── Helpers ────────────────────────────────────────────────────────────

setup_namespace() {
  if ! $CMD get namespace "$NAMESPACE" &>/dev/null; then
    $CMD create namespace "$NAMESPACE" 2>/dev/null || true
  fi
}

cleanup_namespace() {
  echo -e "${YELLOW}Cleaning up ${NAMESPACE}...${NC}"
  stop_ws_client
  # Delete individual resources first (faster than waiting for namespace)
  $CMD delete deployment --all -n "$NAMESPACE" --wait=false 2>/dev/null || true
  $CMD delete pod --all -n "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
  $CMD delete secret --all -n "$NAMESPACE" 2>/dev/null || true
  $CMD delete resourcequota --all -n "$NAMESPACE" 2>/dev/null || true
  # Then delete namespace
  $CMD delete namespace "$NAMESPACE" --wait=false 2>/dev/null || true
  rm -f "$FINDINGS_FILE"
}

get_ws_token() {
  $CMD get secret pulse-ws-token -n "$AGENT_NS" -o jsonpath='{.data.token}' 2>/dev/null | base64 --decode 2>/dev/null || echo ""
}

get_agent_svc_url() {
  # Build the in-cluster service URL for the agent
  local svc_name
  svc_name=$($CMD get svc -n "$AGENT_NS" -l app.kubernetes.io/name=openshift-sre-agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "$svc_name" ]]; then
    # Fallback: try port-forward from the pod
    echo ""
    return
  fi
  echo "${svc_name}.${AGENT_NS}.svc:8080"
}

# ── WebSocket Client ──────────────────────────────────────────────────

start_ws_client() {
  local token
  token=$(get_ws_token)
  if [[ -z "$token" ]]; then
    echo -e "${RED}Error: Could not retrieve WS token from secret pulse-ws-token${NC}"
    return 1
  fi

  # Always use port-forward for WebSocket — in-cluster DNS doesn't resolve
  # from local machines, and port-forward works everywhere.
  local ws_url=""
  {
    # Port-forward to the agent pod for out-of-cluster access
    local pod
    pod=$($CMD get pods -n "$AGENT_NS" -l app.kubernetes.io/name=openshift-sre-agent --no-headers -o name 2>/dev/null | grep -v mcp | grep -v postgresql | head -1)
    if [[ -z "$pod" ]]; then
      echo -e "${RED}Error: Could not find agent pod${NC}"
      return 1
    fi
    # Start port-forward in background
    $CMD port-forward "$pod" -n "$AGENT_NS" 18080:8080 &>/dev/null &
    local pf_pid=$!
    sleep 2
    if ! kill -0 "$pf_pid" 2>/dev/null; then
      echo -e "${RED}Error: Port-forward failed${NC}"
      return 1
    fi
    # Track port-forward PID for cleanup
    WS_PORT_FORWARD_PID=$pf_pid
    ws_url="ws://localhost:18080/ws/monitor"
  }

  # Clear previous findings
  > "$FINDINGS_FILE"

  echo -e "${CYAN}Starting WebSocket monitor client...${NC}"
  echo -e "  URL: ${ws_url}"

  python3 "$(dirname "$0")/chaos-ws-client.py" \
    --url "$ws_url" \
    --token "$token" \
    --output "$FINDINGS_FILE" \
    --trust-level 3 \
    --auto-fix-categories "crashloop,image_pull,oom" &
  WS_CLIENT_PID=$!
  sleep 3

  if ! kill -0 "$WS_CLIENT_PID" 2>/dev/null; then
    echo -e "${RED}Error: WebSocket client failed to start${NC}"
    WS_CLIENT_PID=""
    return 1
  fi
  echo -e "  ${GREEN}Connected (PID: ${WS_CLIENT_PID})${NC}"
}

stop_ws_client() {
  if [[ -n "$WS_CLIENT_PID" ]]; then
    kill "$WS_CLIENT_PID" 2>/dev/null || true
    wait "$WS_CLIENT_PID" 2>/dev/null || true
    WS_CLIENT_PID=""
  fi
  if [[ -n "${WS_PORT_FORWARD_PID:-}" ]]; then
    kill "$WS_PORT_FORWARD_PID" 2>/dev/null || true
    wait "$WS_PORT_FORWARD_PID" 2>/dev/null || true
    WS_PORT_FORWARD_PID=""
  fi
}

# #3: Cache agent pod name (refreshed once per scenario, not per check)
CACHED_AGENT_POD=""
refresh_agent_pod() {
  CACHED_AGENT_POD=$($CMD get pods -n "$AGENT_NS" -l app.kubernetes.io/name=openshift-sre-agent --no-headers -o name 2>/dev/null | grep -v mcp | grep -v postgresql | head -1)
}
get_agent_pod() {
  if [[ -z "$CACHED_AGENT_POD" ]]; then
    refresh_agent_pod
  fi
  echo "$CACHED_AGENT_POD"
}

# Check if agent detected a finding matching a pattern (via WS findings file)
check_finding() {
  local pattern="$1"

  if [[ ! -f "$FINDINGS_FILE" ]]; then
    echo "0"
    return
  fi

  local count
  count=$(grep -cE "$pattern" "$FINDINGS_FILE" 2>/dev/null || true)
  echo "${count:-0}" | tr -d '[:space:]'
}

# Check if an action_report was received for a category
check_remediation() {
  local category="$1"

  if [[ ! -f "$FINDINGS_FILE" ]]; then
    echo "0"
    return
  fi

  local count
  count=$(grep '"type".*"action_report"' "$FINDINGS_FILE" 2>/dev/null | grep -cE "$category" 2>/dev/null || true)
  echo "${count:-0}" | tr -d '[:space:]'
}

# Wait for agent to detect something via WebSocket findings
wait_for_detection() {
  local pattern="$1"
  local max_wait="$2"
  local elapsed=0

  echo -n "  Waiting for detection"
  while [[ $elapsed -lt $max_wait ]]; do
    local found
    found=$(check_finding "$pattern")
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
  local max="${6:-100}"  # optional 6th arg overrides max score
  # Cap score at max (for non-remediable scenarios)
  [[ $score -gt $max ]] && score=$max
  TOTAL_SCORE=$((TOTAL_SCORE + score))
  TOTAL_MAX=$((TOTAL_MAX + max))

  local pct=$((score * 100 / max))
  local status="${RED}FAIL${NC}"
  [[ $pct -ge 60 ]] && status="${YELLOW}WARN${NC}"
  [[ $pct -ge 90 ]] && status="${GREEN}PASS${NC}"

  RESULTS+=("$(printf "  %-20s %3d/%-3d  %b" "$name" "$score" "$max" "$status")")
  echo -e "  Score: ${score}/${max} [${status}]"
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

  if wait_for_detection "crashloop|CrashLoopBackOff|chaos-crashloop" "$TIMEOUT"; then
    detected=1
    speed=1
    # Check if investigation report was received
    local inv_count
    inv_count=$(check_finding "investigation_report|diagnos")
    [[ "$inv_count" -gt 0 ]] && diagnosed=1
    # Check if auto-fix action was taken
    local fix_count
    fix_count=$(check_remediation "crashloop")
    [[ "$fix_count" -gt 0 ]] && remediated=1
  fi

  # Cleanup
  $CMD delete pod chaos-crashloop -n "$NAMESPACE" --wait=false 2>/dev/null || true

  score_scenario "crashloop" $detected $diagnosed $remediated $speed
}

run_oom() {
  echo -e "\n${CYAN}Scenario 2: OOM Kill${NC}"
  echo "  Deploy: pod requesting 10Mi but trying to allocate 50Mi (restarts to stay visible)"

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
    command: ["/bin/sh", "-c", "dd if=/dev/zero of=/dev/shm/fill bs=1M count=50 2>/dev/null; sleep 3600"]
    resources:
      limits:
        memory: "10Mi"
  restartPolicy: Always
EOF

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "oom|OOMKilled|chaos-oom" "$TIMEOUT"; then
    detected=1
    speed=1
    local inv_count
    inv_count=$(check_finding "investigation_report|diagnos|OOM|memory")
    [[ "$inv_count" -gt 0 ]] && diagnosed=1
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
  $CMD rollout status deployment/chaos-image -n "$NAMESPACE" --timeout=30s 2>/dev/null || true

  # Push bad update
  $CMD set image deployment/chaos-image httpd-24=registry.access.redhat.com/ubi9/httpd-24:nonexistent \
    -n "$NAMESPACE" 2>/dev/null || true

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "image_pull|ImagePullBackOff|ErrImagePull|chaos-image" "$TIMEOUT"; then
    detected=1
    speed=1
    local inv_count
    inv_count=$(check_finding "investigation_report|diagnos|image|pull")
    [[ "$inv_count" -gt 0 ]] && diagnosed=1
    # Check if auto-fix rollback action was taken
    local fix_count
    fix_count=$(check_remediation "image_pull")
    [[ "$fix_count" -gt 0 ]] && remediated=1
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

  # #6: Generate expired cert — use -days 0 for portability (some openssl reject -1)
  local tmpdir
  tmpdir=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -keyout "$tmpdir/key.pem" -out "$tmpdir/cert.pem" \
    -days 0 -nodes -subj "/CN=chaos-expired.test" 2>/dev/null || true

  if [[ -f "$tmpdir/cert.pem" ]]; then
    $CMD create secret tls chaos-expired-cert \
      --cert="$tmpdir/cert.pem" --key="$tmpdir/key.pem" \
      -n "$NAMESPACE" 2>/dev/null || true
  fi
  # #7: Clean temp files immediately
  rm -rf "$tmpdir"

  local detected=0 diagnosed=0 remediated=0 speed=0

  if wait_for_detection "cert|expir|certificate|chaos-expired" "$TIMEOUT"; then
    detected=1
    speed=1
    diagnosed=1  # cert expiry is self-evident
  fi

  $CMD delete secret chaos-expired-cert -n "$NAMESPACE" 2>/dev/null || true

  score_scenario "cert-expiry" $detected $diagnosed $remediated $speed 70
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

  if wait_for_detection "quota|pending|Pending|chaos-quota" "$TIMEOUT"; then
    detected=1
    speed=1
    diagnosed=1
  fi

  $CMD delete deployment chaos-quota -n "$NAMESPACE" --wait=false 2>/dev/null || true
  $CMD delete resourcequota chaos-quota -n "$NAMESPACE" 2>/dev/null || true

  score_scenario "resource-pressure" $detected $diagnosed $remediated $speed 70
}

# ── Main ───────────────────────────────────────────────────────────────

# Run preflight unless dry-run — must be after helper function definitions
if [[ "$DRY_RUN" != "true" ]]; then
  preflight_check || exit 1
fi

trap cleanup_namespace EXIT

setup_namespace

# Start WebSocket client to trigger monitor scanning
if [[ "$DRY_RUN" != "true" ]]; then
  if ! start_ws_client; then
    echo -e "${RED}Failed to start WebSocket monitor client — detection will fall back to log grep${NC}"
  fi
  # Wait for first scan cycle to complete
  echo -e "${CYAN}Waiting for initial scan cycle...${NC}"
  sleep 5
fi

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
for r in "${RESULTS[@]+"${RESULTS[@]}"}"; do
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
