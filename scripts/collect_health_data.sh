#!/usr/bin/env bash
# collect_health_data.sh — Collect Kubernetes health data for IFS Kube Medic
#
# Usage:
#   chmod +x scripts/collect_health_data.sh
#   ./scripts/collect_health_data.sh [NAMESPACE] [LOGS_DIR]
#
# Defaults:
#   NAMESPACE = ifs-production
#   LOGS_DIR  = ./logs/health_check
#
# The files created use the naming convention that IFS Kube Medic expects:
#   kubectl-top-*      → parsed as kubectl_top
#   kubectl-get-*      → parsed as kubectl_get
#   kubectl-describe-* → parsed as kubectl_describe
#   kubectl-events-*   → parsed as kubectl_events

set -euo pipefail

NS="${1:-ifs-production}"
OUT="${2:-./logs/health_check}"

echo "========================================"
echo " IFS Kube Medic — Health Data Collector"
echo " Namespace : $NS"
echo " Output    : $OUT"
echo "========================================"

mkdir -p "$OUT"

# Helper: run a kubectl command, print status, save output
run() {
    local label="$1"
    local file="$2"
    shift 2
    echo -n "  Collecting $label ... "
    if kubectl "$@" > "$OUT/$file" 2>&1; then
        echo "OK  → $file"
    else
        echo "WARN (command failed, partial output saved)"
    fi
}

# ─────────────────────────────────────────────
# 1. Node Utilisation
# ─────────────────────────────────────────────
echo ""
echo "[1/10] Node Utilisation"
run "top nodes"          "kubectl-top-nodes.txt"        top nodes
# Describe every node
kubectl get nodes -o name | sed 's|node/||' | while read -r node; do
    safe="${node//\//_}"
    echo -n "  Collecting describe node $node ... "
    kubectl describe node "$node" > "$OUT/kubectl-describe-node-${safe}.txt" 2>&1 && echo "OK" || echo "WARN"
done

# ─────────────────────────────────────────────
# 2. Pod Utilisation
# ─────────────────────────────────────────────
echo ""
echo "[2/10] Pod Utilisation"
run "top pods (memory)"  "kubectl-top-pods-memory.txt"  top pods -n "$NS" --sort-by=memory
run "top pods (CPU)"     "kubectl-top-pods-cpu.txt"     top pods -n "$NS" --sort-by=cpu

# ─────────────────────────────────────────────
# 3. Requests & Limits
# ─────────────────────────────────────────────
echo ""
echo "[3/10] Requests & Limits"
run "resource requests/limits (jsonpath)" \
    "kubectl-get-resource-requests.txt" \
    get pods -n "$NS" -o custom-columns=\
'NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory'

run "resource quotas" \
    "kubectl-get-resourcequota.txt" \
    get resourcequota -n "$NS" -o wide

run "limit ranges" \
    "kubectl-get-limitrange.txt" \
    get limitrange -n "$NS" -o yaml

# ─────────────────────────────────────────────
# 4. Events
# ─────────────────────────────────────────────
echo ""
echo "[4/10] Events"
run "all events (sorted)"    "kubectl-events-all.txt"      get events -n "$NS" --sort-by='.lastTimestamp'
run "warning events only"    "kubectl-events-warnings.txt" get events -n "$NS" --field-selector=type=Warning --sort-by='.lastTimestamp'

# ─────────────────────────────────────────────
# 5. Linkerd Health
# ─────────────────────────────────────────────
echo ""
echo "[5/10] Linkerd Health"
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

# ─────────────────────────────────────────────
# 6. Redis
# ─────────────────────────────────────────────
echo ""
echo "[6/10] Redis"
for label in "app=redis" "app.kubernetes.io/name=redis"; do
    pod=$(kubectl get pods -n "$NS" -l "$label" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -n "$pod" ]]; then
        echo -n "  Redis pod: $pod ... "
        kubectl logs -n "$NS" "$pod" --tail=200 \
            > "$OUT/redis-${pod}.log" 2>&1 && echo "OK" || echo "WARN"
        break
    fi
done
if [[ -z "${pod:-}" ]]; then
    echo "  Redis pod not found — skipped"
fi

# ─────────────────────────────────────────────
# 7. PVC / Storage
# ─────────────────────────────────────────────
echo ""
echo "[7/10] PVC / Storage"
run "PVCs"             "kubectl-get-pvc.txt"           get pvc -n "$NS" -o wide
run "PVs"              "kubectl-get-pv.txt"            get pv -o wide
run "StorageClasses"   "kubectl-get-storageclass.txt"  get storageclass

# ─────────────────────────────────────────────
# 8. HPA
# ─────────────────────────────────────────────
echo ""
echo "[8/10] HPA"
run "HPAs (wide)" "kubectl-get-hpa.txt" get hpa -n "$NS" -o wide

kubectl get hpa -n "$NS" -o name | sed 's|horizontalpodautoscaler/||' | while read -r hpa; do
    echo -n "  Describe HPA $hpa ... "
    kubectl describe hpa -n "$NS" "$hpa" \
        > "$OUT/kubectl-describe-hpa-${hpa}.txt" 2>&1 && echo "OK" || echo "WARN"
done

# ─────────────────────────────────────────────
# 9. Scheduling Constraints
# ─────────────────────────────────────────────
echo ""
echo "[9/10] Scheduling Constraints"
run "node taints"  "kubectl-get-node-taints.txt" \
    get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints'

kubectl get deployments -n "$NS" -o name | sed 's|deployment.apps/||' | while read -r dep; do
    echo -n "  Affinity/topology: $dep ... "
    kubectl get deployment -n "$NS" "$dep" \
        -o jsonpath='{.metadata.name}{"\n"}Affinity: {.spec.template.spec.affinity}{"\n"}TopologySpread: {.spec.template.spec.topologySpreadConstraints}{"\n"}' \
        >> "$OUT/kubectl-get-scheduling-constraints.txt" 2>&1 && echo "OK" || echo "WARN"
done

# ─────────────────────────────────────────────
# 10. Pod Summary
# ─────────────────────────────────────────────
echo ""
echo "[10/10] Pod Summary"
run "all pods (wide)"       "kubectl-get-pods-all.txt"         get pods -n "$NS" -o wide
run "non-running pods"      "kubectl-get-pods-nonrunning.txt"  get pods -n "$NS" --field-selector='status.phase!=Running' -o wide
run "pod restart counts"    "kubectl-get-pods-restarts.txt" \
    get pods -n "$NS" -o custom-columns='NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,STATUS:.status.phase,NODE:.spec.nodeName'

echo ""
echo "========================================"
echo " Collection complete!"
echo " Output: $OUT"
echo " Files : $(ls "$OUT" | wc -l | tr -d ' ') top-level, $(find "$OUT" -type f | wc -l | tr -d ' ') total"
echo ""
echo " Run health check:"
echo "   python -m src.main --mode health-check --logs-dir $OUT"
echo "========================================"
