# collect_health_data.ps1 — Collect Kubernetes health data for IFS Kube Medic (Windows)
#
# Usage:
#   .\scripts\collect_health_data.ps1 [Namespace] [LogsDir]
#
# Defaults:
#   Namespace = ifs-production
#   LogsDir   = .\logs\health_check
#
# The files created use the naming convention that IFS Kube Medic expects:
#   kubectl-top-*      -> parsed as kubectl_top
#   kubectl-get-*      -> parsed as kubectl_get
#   kubectl-describe-* -> parsed as kubectl_describe
#   kubectl-events-*   -> parsed as kubectl_events

param(
    [string]$Namespace = "ifs-production",
    [string]$LogsDir   = ".\logs\health_check"
)

$ErrorActionPreference = "Continue"

Write-Host "========================================"
Write-Host " IFS Kube Medic - Health Data Collector"
Write-Host " Namespace : $Namespace"
Write-Host " Output    : $LogsDir"
Write-Host "========================================"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# Helper: run kubectl, print status, save output
function Run-Kubectl {
    param(
        [string]$Label,
        [string]$File,
        [string[]]$Args
    )
    $outFile = Join-Path $LogsDir $File
    Write-Host -NoNewline "  Collecting $Label ... "
    try {
        $result = kubectl @Args 2>&1
        $result | Out-File -FilePath $outFile -Encoding utf8
        Write-Host "OK  -> $File"
    }
    catch {
        Write-Host "WARN (command failed)"
    }
}

# ─────────────────────────────────────────────
# 1. Node Utilisation
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[1/10] Node Utilisation"
Run-Kubectl "top nodes" "kubectl-top-nodes.txt" @("top", "nodes")

$nodes = kubectl get nodes -o name 2>&1 | ForEach-Object { $_ -replace "^node/", "" }
foreach ($node in $nodes) {
    $safeNode = $node -replace "[/\\]", "_"
    $outFile = Join-Path $LogsDir "kubectl-describe-node-${safeNode}.txt"
    Write-Host -NoNewline "  Describe node $node ... "
    kubectl describe node $node 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

# ─────────────────────────────────────────────
# 2. Pod Utilisation
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[2/10] Pod Utilisation"
Run-Kubectl "top pods (memory)" "kubectl-top-pods-memory.txt" @("top", "pods", "-n", $Namespace, "--sort-by=memory")
Run-Kubectl "top pods (CPU)"    "kubectl-top-pods-cpu.txt"    @("top", "pods", "-n", $Namespace, "--sort-by=cpu")

# ─────────────────────────────────────────────
# 3. Requests & Limits
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[3/10] Requests & Limits"
Run-Kubectl "resource requests/limits" "kubectl-get-resource-requests.txt" @(
    "get", "pods", "-n", $Namespace,
    "-o", "custom-columns=NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory"
)
Run-Kubectl "resource quotas" "kubectl-get-resourcequota.txt" @("get", "resourcequota", "-n", $Namespace, "-o", "wide")
Run-Kubectl "limit ranges"    "kubectl-get-limitrange.txt"    @("get", "limitrange",    "-n", $Namespace, "-o", "yaml")

# ─────────────────────────────────────────────
# 4. Events
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[4/10] Events"
Run-Kubectl "all events (sorted)"  "kubectl-events-all.txt"      @("get", "events", "-n", $Namespace, "--sort-by=.lastTimestamp")
Run-Kubectl "warning events only"  "kubectl-events-warnings.txt" @("get", "events", "-n", $Namespace, "--field-selector=type=Warning", "--sort-by=.lastTimestamp")

# ─────────────────────────────────────────────
# 5. Linkerd Health
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[5/10] Linkerd Health"
$linkerdDir = Join-Path $LogsDir "linkerd_logs"
New-Item -ItemType Directory -Force -Path $linkerdDir | Out-Null

foreach ($component in @("identity", "destination", "proxy-injector")) {
    $outFile = Join-Path $linkerdDir "kubectl-linkerd-${component}.log"
    Write-Host -NoNewline "  Logs: linkerd $component ... "
    kubectl logs -n linkerd "deploy/linkerd-$component" --tail=200 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

Write-Host -NoNewline "  Linkerd cert check ... "
if (Get-Command linkerd -ErrorAction SilentlyContinue) {
    $outFile = Join-Path $linkerdDir "kubectl-linkerd-check.txt"
    linkerd check --proxy 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
} else {
    Write-Host "SKIP (linkerd CLI not found)"
}

# ─────────────────────────────────────────────
# 6. Redis
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[6/10] Redis"
$redisPod = $null
foreach ($label in @("app=redis", "app.kubernetes.io/name=redis")) {
    $pod = kubectl get pods -n $Namespace -l $label -o "jsonpath={.items[0].metadata.name}" 2>$null
    if ($pod) { $redisPod = $pod; break }
}
if ($redisPod) {
    $outFile = Join-Path $LogsDir "redis-${redisPod}.log"
    Write-Host -NoNewline "  Redis pod: $redisPod ... "
    kubectl logs -n $Namespace $redisPod --tail=200 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
} else {
    Write-Host "  Redis pod not found - skipped"
}

# ─────────────────────────────────────────────
# 7. PVC / Storage
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[7/10] PVC / Storage"
Run-Kubectl "PVCs"           "kubectl-get-pvc.txt"          @("get", "pvc",          "-n", $Namespace, "-o", "wide")
Run-Kubectl "PVs"            "kubectl-get-pv.txt"           @("get", "pv",           "-o", "wide")
Run-Kubectl "StorageClasses" "kubectl-get-storageclass.txt" @("get", "storageclass")

# ─────────────────────────────────────────────
# 8. HPA
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[8/10] HPA"
Run-Kubectl "HPAs (wide)" "kubectl-get-hpa.txt" @("get", "hpa", "-n", $Namespace, "-o", "wide")

$hpas = kubectl get hpa -n $Namespace -o name 2>&1 | ForEach-Object { $_ -replace "^horizontalpodautoscaler/", "" }
foreach ($hpa in $hpas) {
    $outFile = Join-Path $LogsDir "kubectl-describe-hpa-${hpa}.txt"
    Write-Host -NoNewline "  Describe HPA $hpa ... "
    kubectl describe hpa -n $Namespace $hpa 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

# ─────────────────────────────────────────────
# 9. Scheduling Constraints
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[9/10] Scheduling Constraints"
Run-Kubectl "node taints" "kubectl-get-node-taints.txt" @(
    "get", "nodes", "-o",
    "custom-columns=NAME:.metadata.name,TAINTS:.spec.taints"
)

$constraintsFile = Join-Path $LogsDir "kubectl-get-scheduling-constraints.txt"
$deployments = kubectl get deployments -n $Namespace -o name 2>&1 | ForEach-Object { $_ -replace "^deployment.apps/", "" }
foreach ($dep in $deployments) {
    Write-Host -NoNewline "  Affinity/topology: $dep ... "
    $output = kubectl get deployment -n $Namespace $dep `
        -o jsonpath="{.metadata.name}{`"\n`"}Affinity: {.spec.template.spec.affinity}{`"\n`"}TopologySpread: {.spec.template.spec.topologySpreadConstraints}{`"\n`"}" 2>&1
    Add-Content -Path $constraintsFile -Value $output -Encoding utf8
    Write-Host "OK"
}

# ─────────────────────────────────────────────
# 10. Pod Summary
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "[10/10] Pod Summary"
Run-Kubectl "all pods (wide)"    "kubectl-get-pods-all.txt"        @("get", "pods", "-n", $Namespace, "-o", "wide")
Run-Kubectl "non-running pods"   "kubectl-get-pods-nonrunning.txt" @("get", "pods", "-n", $Namespace, "--field-selector=status.phase!=Running", "-o", "wide")
Run-Kubectl "pod restart counts" "kubectl-get-pods-restarts.txt"   @(
    "get", "pods", "-n", $Namespace,
    "-o", "custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,STATUS:.status.phase,NODE:.spec.nodeName"
)

# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────
$totalFiles = (Get-ChildItem -Path $LogsDir -Recurse -File).Count

Write-Host ""
Write-Host "========================================"
Write-Host " Collection complete!"
Write-Host " Output : $LogsDir"
Write-Host " Files  : $totalFiles total"
Write-Host ""
Write-Host " Run health check:"
Write-Host "   python -m src.main --mode health-check --logs-dir $LogsDir"
Write-Host "========================================"
