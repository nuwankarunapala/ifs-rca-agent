#!/usr/bin/env bash
# collect_health_data.sh -- Collect Kubernetes health data for IFS Kube Medic
#
# Usage:
#   chmod +x scripts/collect_health_data.sh
#   ./scripts/collect_health_data.sh [NAMESPACE] [LOGS_DIR] [NAMESPACE2]
#
# Defaults:
#   NAMESPACE  = ifs-production
#   LOGS_DIR   = ./logs/health_check
#   NAMESPACE2 = (empty - optional second namespace e.g. ifs-staging)
#
# File naming conventions (IFS Kube Medic expects):
#   kubectl-top-*      -> parsed as kubectl_top
#   kubectl-get-*      -> parsed as kubectl_get
#   kubectl-describe-* -> parsed as kubectl_describe
#   kubectl-events-*   -> parsed as kubectl_events

set -euo pipefail

NS="${1:-ifs-production}"
OUT="${2:-./logs/health_check}"
NS2="${3:-}"

echo "========================================"
echo " IFS Kube Medic - Health Data Collector"
echo " Namespace  : $NS"
if [[ -n "$NS2" ]]; then
echo " Namespace2 : $NS2"
fi
echo " Output     : $OUT"
echo "========================================"

mkdir -p "$OUT"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

run() {
    local label="$1"
    local file="$2"
    shift 2
    echo -n "  Collecting $label ... "
    if kubectl "$@" > "$OUT/$file" 2>&1; then
        echo "OK  -> $file"
    else
        echo "WARN (command failed, partial output saved)"
    fi
}

run_ns2() {
    # Run a command for NS2 only if NS2 is set
    [[ -z "$NS2" ]] && return
    local label="$1"
    local file="$2"
    shift 2
    echo -n "  Collecting $label ($NS2) ... "
    if kubectl "$@" -n "$NS2" > "$OUT/$file" 2>&1; then
        echo "OK  -> $file"
    else
        echo "WARN"
    fi
}

# ---------------------------------------------------------------------------
# Metrics-server availability check
# Gate all kubectl top commands behind this flag
# ---------------------------------------------------------------------------
echo ""
echo "[CHECK] Metrics-server availability ..."
METRICS_OK=false
if kubectl top nodes > /dev/null 2>&1; then
    METRICS_OK=true
    echo "  metrics-server: OK"
else
    echo "  metrics-server: NOT available -- top sections will be skipped"
fi

# ---------------------------------------------------------------------------
# 1. Node Utilisation
# ---------------------------------------------------------------------------
echo ""
echo "[1/15] Node Utilisation"

if $METRICS_OK; then
    run "top nodes (cpu)"    "kubectl-top-nodes-cpu.txt"    top nodes --sort-by=cpu
    run "top nodes (memory)" "kubectl-top-nodes-memory.txt" top nodes --sort-by=memory
else
    echo "  SKIPPED (metrics-server unavailable)"
fi

kubectl get nodes -o name | sed 's|node/||' | while read -r node; do
    safe="${node//\//_}"
    echo -n "  Describe node $node ... "
    kubectl describe node "$node" > "$OUT/kubectl-describe-node-${safe}.txt" 2>&1 && echo "OK" || echo "WARN"
done

# ---------------------------------------------------------------------------
# 2. Pod Utilisation
# ---------------------------------------------------------------------------
echo ""
echo "[2/15] Pod Utilisation"

if $METRICS_OK; then
    # Primary namespace -- sorted by memory and CPU
    run "top pods memory ($NS)"  "kubectl-top-pods-memory.txt"      top pods -n "$NS" --sort-by=memory
    run "top pods cpu ($NS)"     "kubectl-top-pods-cpu.txt"         top pods -n "$NS" --sort-by=cpu
    # Primary namespace -- container breakdown (exposes linkerd-proxy / fluent-bit sidecar usage)
    run "top pods containers ($NS)" "kubectl-top-pods-containers.txt" top pods -n "$NS" --containers --sort-by=memory
    # Second namespace if provided
    if [[ -n "$NS2" ]]; then
        run "top pods memory ($NS2)" "kubectl-top-pods-memory-ns2.txt"      top pods -n "$NS2" --sort-by=memory
        run "top pods cpu ($NS2)"    "kubectl-top-pods-cpu-ns2.txt"         top pods -n "$NS2" --sort-by=cpu
        run "top pods containers ($NS2)" "kubectl-top-pods-containers-ns2.txt" top pods -n "$NS2" --containers --sort-by=memory
    fi
    # Cluster-wide -- catches rogue pods in any namespace
    run "top pods cluster-wide memory" "kubectl-top-pods-all-memory.txt" top pods -A --sort-by=memory
    run "top pods cluster-wide cpu"    "kubectl-top-pods-all-cpu.txt"    top pods -A --sort-by=cpu
    run "top pods cluster-wide containers" "kubectl-top-pods-all-containers.txt" top pods -A --containers --sort-by=memory
else
    echo "  SKIPPED (metrics-server unavailable)"
fi

# ---------------------------------------------------------------------------
# 3. Requests & Limits
# ---------------------------------------------------------------------------
echo ""
echo "[3/15] Requests & Limits"

run "resource requests/limits" \
    "kubectl-get-resource-requests.txt" \
    get pods -n "$NS" -o custom-columns=\
'NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory'

run "resource quotas" "kubectl-get-resourcequota.txt" get resourcequota -n "$NS" -o wide
run "limit ranges"    "kubectl-get-limitrange.txt"    get limitrange    -n "$NS" -o yaml

if [[ -n "$NS2" ]]; then
    run "resource requests/limits ($NS2)" \
        "kubectl-get-resource-requests-ns2.txt" \
        get pods -n "$NS2" -o custom-columns=\
'NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory'
fi

# ---------------------------------------------------------------------------
# 4. Events
# ---------------------------------------------------------------------------
echo ""
echo "[4/15] Events"
run "all events ($NS)"      "kubectl-events-all.txt"          get events -n "$NS"  --sort-by='.lastTimestamp'
run "warning events ($NS)"  "kubectl-events-warnings.txt"     get events -n "$NS"  --field-selector=type=Warning --sort-by='.lastTimestamp'
if [[ -n "$NS2" ]]; then
    run "all events ($NS2)"     "kubectl-events-all-ns2.txt"  get events -n "$NS2" --sort-by='.lastTimestamp'
    run "warning events ($NS2)" "kubectl-events-warnings-ns2.txt" get events -n "$NS2" --field-selector=type=Warning --sort-by='.lastTimestamp'
fi

# ---------------------------------------------------------------------------
# 5. Linkerd Health
# ---------------------------------------------------------------------------
echo ""
echo "[5/15] Linkerd Health"
mkdir -p "$OUT/linkerd_logs"
for component in identity destination proxy-injector; do
    echo -n "  Logs: linkerd $component ... "
    kubectl logs -n linkerd "deploy/linkerd-$component" --tail=200 \
        > "$OUT/linkerd_logs/kubectl-linkerd-${component}.log" 2>&1 && echo "OK" || echo "WARN (skipped)"
done
echo -n "  Linkerd cert check ... "
if command -v linkerd &>/dev/null; then
    linkerd check --proxy > "$OUT/linkerd_logs/kubectl-linkerd-check.txt" 2>&1 && echo "OK" || echo "WARN"
else
    echo "SKIP (linkerd CLI not found)"
fi

# ---------------------------------------------------------------------------
# 6. Redis
# ---------------------------------------------------------------------------
echo ""
echo "[6/15] Redis"
redis_pod=""
for label in "app=redis" "app.kubernetes.io/name=redis"; do
    redis_pod=$(kubectl get pods -n "$NS" -l "$label" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -n "$redis_pod" ]]; then
        echo -n "  Redis pod: $redis_pod ... "
        kubectl logs -n "$NS" "$redis_pod" --tail=200 \
            > "$OUT/redis-${redis_pod}.log" 2>&1 && echo "OK" || echo "WARN"
        break
    fi
done
if [[ -z "$redis_pod" ]]; then
    echo "  Redis pod not found - skipped"
fi

# ---------------------------------------------------------------------------
# 7. PVC / Storage
# ---------------------------------------------------------------------------
echo ""
echo "[7/15] PVC / Storage"
run "PVCs"           "kubectl-get-pvc.txt"          get pvc -n "$NS" -o wide
run "PVs"            "kubectl-get-pv.txt"           get pv -o wide
run "StorageClasses" "kubectl-get-storageclass.txt" get storageclass

# ---------------------------------------------------------------------------
# 8. HPA
# ---------------------------------------------------------------------------
echo ""
echo "[8/15] HPA"
run "HPAs ($NS)" "kubectl-get-hpa.txt" get hpa -n "$NS" -o wide

kubectl get hpa -n "$NS" -o name 2>/dev/null | sed 's|horizontalpodautoscaler/||' | while read -r hpa; do
    echo -n "  Describe HPA $hpa ... "
    kubectl describe hpa -n "$NS" "$hpa" \
        > "$OUT/kubectl-describe-hpa-${hpa}.txt" 2>&1 && echo "OK" || echo "WARN"
done

if [[ -n "$NS2" ]]; then
    run "HPAs ($NS2)" "kubectl-get-hpa-ns2.txt" get hpa -n "$NS2" -o wide
fi

# ---------------------------------------------------------------------------
# 9. Scheduling Constraints
# ---------------------------------------------------------------------------
echo ""
echo "[9/15] Scheduling Constraints"
run "node taints" "kubectl-get-node-taints.txt" \
    get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints'

constraints_file="$OUT/kubectl-get-scheduling-constraints.txt"
> "$constraints_file"   # truncate

for ns_loop in "$NS" ${NS2:+"$NS2"}; do
    kubectl get deployments -n "$ns_loop" -o name 2>/dev/null | sed 's|deployment.apps/||' | while read -r dep; do
        echo -n "  Affinity/topology: $dep ($ns_loop) ... "
        {
            echo "=== $ns_loop / $dep ==="
            kubectl get deployment -n "$ns_loop" "$dep" \
                -o jsonpath='{.metadata.name}{"\n"}Affinity: {.spec.template.spec.affinity}{"\n"}TopologySpread: {.spec.template.spec.topologySpreadConstraints}{"\n"}' \
                2>&1
            echo ""
        } >> "$constraints_file"
        echo "OK"
    done
done

# ---------------------------------------------------------------------------
# 10. Pod Summary
# ---------------------------------------------------------------------------
echo ""
echo "[10/15] Pod Summary"
run "all pods ($NS)"          "kubectl-get-pods-all.txt"         get pods -n "$NS"  -o wide
run "non-running pods ($NS)"  "kubectl-get-pods-nonrunning.txt"  get pods -n "$NS"  --field-selector='status.phase!=Running' -o wide
run "pod restart counts ($NS)" "kubectl-get-pods-restarts.txt" \
    get pods -n "$NS" -o custom-columns='NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,STATUS:.status.phase,NODE:.spec.nodeName'
if [[ -n "$NS2" ]]; then
    run "all pods ($NS2)"         "kubectl-get-pods-all-ns2.txt"         get pods -n "$NS2" -o wide
    run "non-running pods ($NS2)" "kubectl-get-pods-nonrunning-ns2.txt"  get pods -n "$NS2" --field-selector='status.phase!=Running' -o wide
fi
# Cluster-wide non-running (catches issues in any namespace)
run "non-running pods (cluster-wide)" "kubectl-get-pods-nonrunning-all.txt" \
    get pods -A --field-selector='status.phase!=Running' -o wide

# ---------------------------------------------------------------------------
# 11. IFS Application Health
# ---------------------------------------------------------------------------
echo ""
echo "[11/15] IFS Application Health"

# Image tags / versions per deployment (confirms Helm upgrade landed)
run "deployment image tags ($NS)" "kubectl-get-deployment-images.txt" \
    get deployments -n "$NS" -o custom-columns=\
'NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas,DESIRED:.spec.replicas'

if [[ -n "$NS2" ]]; then
    run "deployment image tags ($NS2)" "kubectl-get-deployment-images-ns2.txt" \
        get deployments -n "$NS2" -o custom-columns=\
'NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas,DESIRED:.spec.replicas'
fi

# IFS-specific pods by label (ifs-main, ifs-enums, MWS)
for selector in "app=ifs-main" "app=ifs-enums" "app=mws" "app.kubernetes.io/name=mws"; do
    pods_found=$(kubectl get pods -n "$NS" -l "$selector" \
        -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[*].ready' \
        2>/dev/null || true)
    if [[ -n "$pods_found" ]]; then
        echo "  Found pods for selector $selector"
        echo "=== $selector ===" >> "$OUT/kubectl-get-ifs-app-pods.txt"
        echo "$pods_found"       >> "$OUT/kubectl-get-ifs-app-pods.txt"
        echo ""                  >> "$OUT/kubectl-get-ifs-app-pods.txt"
    fi
done

# Pod labels in use (helps identify correct selectors if labels differ between envs)
run "pod labels ($NS)" "kubectl-get-pod-labels.txt" \
    get pods -n "$NS" --show-labels -o wide

# Describe pods where Ready condition is False
echo -n "  Describe not-ready pods ... "
not_ready_count=0
kubectl get pods -n "$NS" -o json 2>/dev/null | \
    python3 -c "
import json, sys
data = json.load(sys.stdin)
for pod in data.get('items', []):
    name = pod['metadata']['name']
    for cond in pod.get('status', {}).get('conditions', []):
        if cond.get('type') == 'Ready' and cond.get('status') == 'False':
            print(name)
" | while read -r pod_name; do
    kubectl describe pod -n "$NS" "$pod_name" \
        > "$OUT/kubectl-describe-pod-notready-${pod_name}.txt" 2>&1
    not_ready_count=$((not_ready_count + 1))
done
echo "OK (not-ready pods described)"

# ---------------------------------------------------------------------------
# 12. StatefulSets
# ---------------------------------------------------------------------------
echo ""
echo "[12/15] StatefulSets"
run "StatefulSets ($NS)" "kubectl-get-statefulsets.txt" get statefulsets -n "$NS" -o wide

kubectl get statefulsets -n "$NS" -o name 2>/dev/null | sed 's|statefulset.apps/||' | while read -r sts; do
    echo -n "  Describe StatefulSet $sts ... "
    kubectl describe statefulset -n "$NS" "$sts" \
        > "$OUT/kubectl-describe-statefulset-${sts}.txt" 2>&1 && echo "OK" || echo "WARN"
done

if [[ -n "$NS2" ]]; then
    run "StatefulSets ($NS2)" "kubectl-get-statefulsets-ns2.txt" get statefulsets -n "$NS2" -o wide
    kubectl get statefulsets -n "$NS2" -o name 2>/dev/null | sed 's|statefulset.apps/||' | while read -r sts; do
        echo -n "  Describe StatefulSet $sts ($NS2) ... "
        kubectl describe statefulset -n "$NS2" "$sts" \
            > "$OUT/kubectl-describe-statefulset-${sts}-ns2.txt" 2>&1 && echo "OK" || echo "WARN"
    done
fi

# ---------------------------------------------------------------------------
# 13. Node Conditions and Pressure
# ---------------------------------------------------------------------------
echo ""
echo "[13/15] Node Conditions and Pressure"
echo -n "  Parsing node conditions and capacity ... "
kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys

data = json.load(sys.stdin)
lines = []
for node in data.get('items', []):
    name = node['metadata']['name']
    conditions = {c['type']: c['status'] for c in node['status'].get('conditions', [])}
    alloc  = node['status'].get('allocatable', {})
    cap    = node['status'].get('capacity', {})
    lines.append('=== Node: {} ==='.format(name))
    for cond in ['MemoryPressure', 'DiskPressure', 'PIDPressure']:
        status = conditions.get(cond, 'Unknown')
        flag = 'ALERT' if status == 'True' else 'OK'
        lines.append('  {}: {} [{}]'.format(cond, status, flag))
    ready_status = conditions.get('Ready', 'Unknown')
    lines.append('  Ready: {}'.format(ready_status))
    lines.append('  CPU       -- Allocatable: {}  Capacity: {}'.format(alloc.get('cpu','?'), cap.get('cpu','?')))
    lines.append('  Memory    -- Allocatable: {}  Capacity: {}'.format(alloc.get('memory','?'), cap.get('memory','?')))
    lines.append('  Max Pods  -- Allocatable: {}  Capacity: {}'.format(alloc.get('pods','?'), cap.get('pods','?')))
    lines.append('')
print('\n'.join(lines))
" > "$OUT/kubectl-get-node-conditions.txt" 2>&1 && echo "OK" || echo "WARN"

# ---------------------------------------------------------------------------
# 14. API Server and Control Plane Health
# ---------------------------------------------------------------------------
echo ""
echo "[14/15] API Server and Control Plane Health"

run "kube-system pods"           "kubectl-get-kubesystem-pods.txt"   get pods -n kube-system -o wide
run "kube-system warning events" "kubectl-events-kubesystem.txt"     get events -n kube-system --field-selector=type=Warning --sort-by='.lastTimestamp'
run "APIService status"          "kubectl-get-apiservices.txt"       get apiservices

if $METRICS_OK; then
    run "kube-system top pods" "kubectl-top-kubesystem-pods.txt" top pods -n kube-system --sort-by=memory
fi

# Metrics-server pod health
echo -n "  Metrics-server pod status ... "
kubectl get pods -n kube-system -l k8s-app=metrics-server -o wide \
    > "$OUT/kubectl-get-metrics-server.txt" 2>&1 && echo "OK" || echo "WARN"

# APIServices with non-True status (catches broken API extensions)
echo -n "  APIServices not available ... "
kubectl get apiservices -o json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
problems = []
for svc in data.get('items', []):
    name = svc['metadata']['name']
    for cond in svc.get('status', {}).get('conditions', []):
        if cond.get('type') == 'Available' and cond.get('status') != 'True':
            problems.append('NOT AVAILABLE: {}  Reason: {}  Message: {}'.format(
                name, cond.get('reason','?'), cond.get('message','?')))
if problems:
    print('\n'.join(problems))
else:
    print('All APIServices available.')
" > "$OUT/kubectl-get-apiservices-problems.txt" 2>&1 && echo "OK" || echo "WARN"

# ---------------------------------------------------------------------------
# 15. Rollout and Deployment History
# ---------------------------------------------------------------------------
echo ""
echo "[15/15] Rollout and Deployment History"
rollout_file="$OUT/kubectl-get-rollout-status.txt"
history_file="$OUT/kubectl-get-rollout-history.txt"
> "$rollout_file"
> "$history_file"

for ns_loop in "$NS" ${NS2:+"$NS2"}; do
    kubectl get deployments -n "$ns_loop" -o name 2>/dev/null | sed 's|deployment.apps/||' | while read -r dep; do
        echo -n "  Rollout status: $dep ($ns_loop) ... "
        {
            echo "=== $ns_loop / $dep ==="
            kubectl rollout status deployment/"$dep" -n "$ns_loop" --timeout=10s 2>&1
            echo ""
        } >> "$rollout_file"
        echo "OK"

        echo -n "  Rollout history: $dep ($ns_loop) ... "
        {
            echo "=== $ns_loop / $dep ==="
            kubectl rollout history deployment/"$dep" -n "$ns_loop" 2>&1
            echo ""
        } >> "$history_file"
        echo "OK"
    done
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo " Collection complete!"
echo " Output : $OUT"
echo " Files  : $(find "$OUT" -type f | wc -l | tr -d ' ') total"
echo ""
echo " Run health check:"
echo "   python -m src.main --mode health-check --logs-dir $OUT"
echo "========================================"
