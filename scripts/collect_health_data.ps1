# collect_health_data.ps1 -- Collect Kubernetes health data for IFS Kube Medic (Windows)
#
# Usage:
#   .\scripts\collect_health_data.ps1 [-Namespace ifs-production] [-LogsDir .\logs\health_check] [-Namespace2 ifs-staging]
#
# File naming conventions (IFS Kube Medic expects):
#   kubectl-top-*      -> parsed as kubectl_top
#   kubectl-get-*      -> parsed as kubectl_get
#   kubectl-describe-* -> parsed as kubectl_describe
#   kubectl-events-*   -> parsed as kubectl_events

param(
    [string]$Namespace  = "ifs-production",
    [string]$LogsDir    = ".\logs\health_check",
    [string]$Namespace2 = ""
)

$ErrorActionPreference = "Continue"

Write-Host "========================================"
Write-Host " IFS Kube Medic - Health Data Collector"
Write-Host " Namespace  : $Namespace"
if ($Namespace2) { Write-Host " Namespace2 : $Namespace2" }
Write-Host " Output     : $LogsDir"
Write-Host "========================================"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Run-Kubectl {
    param([string]$Label, [string]$File, [string[]]$KArgs)
    $outFile = Join-Path $LogsDir $File
    Write-Host -NoNewline "  Collecting $Label ... "
    $result = kubectl @KArgs 2>&1
    $result | Out-File -FilePath $outFile -Encoding utf8
    if ($LASTEXITCODE -eq 0) { Write-Host "OK  -> $File" } else { Write-Host "WARN (exit $LASTEXITCODE)" }
}

function Append-Text {
    param([string]$File, [string]$Text)
    Add-Content -Path (Join-Path $LogsDir $File) -Value $Text -Encoding utf8
}

# ---------------------------------------------------------------------------
# Metrics-server availability check
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[CHECK] Metrics-server availability ..."
$MetricsOK = $false
kubectl top nodes 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    $MetricsOK = $true
    Write-Host "  metrics-server: OK"
} else {
    Write-Host "  metrics-server: NOT available -- top sections will be skipped"
}

# ---------------------------------------------------------------------------
# 1. Node Utilisation
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[1/15] Node Utilisation"

if ($MetricsOK) {
    Run-Kubectl "top nodes (cpu)"    "kubectl-top-nodes-cpu.txt"    @("top", "nodes", "--sort-by=cpu")
    Run-Kubectl "top nodes (memory)" "kubectl-top-nodes-memory.txt" @("top", "nodes", "--sort-by=memory")
} else {
    Write-Host "  SKIPPED (metrics-server unavailable)"
}

$nodes = kubectl get nodes -o name 2>&1 | ForEach-Object { $_ -replace "^node/", "" }
foreach ($node in $nodes) {
    $safeNode = $node -replace "[/\\]", "_"
    $outFile = Join-Path $LogsDir "kubectl-describe-node-${safeNode}.txt"
    Write-Host -NoNewline "  Describe node $node ... "
    kubectl describe node $node 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

# ---------------------------------------------------------------------------
# 2. Pod Utilisation
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[2/15] Pod Utilisation"

if ($MetricsOK) {
    # Primary namespace
    Run-Kubectl "top pods memory ($Namespace)"      "kubectl-top-pods-memory.txt"      @("top", "pods", "-n", $Namespace, "--sort-by=memory")
    Run-Kubectl "top pods cpu ($Namespace)"         "kubectl-top-pods-cpu.txt"         @("top", "pods", "-n", $Namespace, "--sort-by=cpu")
    Run-Kubectl "top pods containers ($Namespace)"  "kubectl-top-pods-containers.txt"  @("top", "pods", "-n", $Namespace, "--containers", "--sort-by=memory")
    # Second namespace
    if ($Namespace2) {
        Run-Kubectl "top pods memory ($Namespace2)"     "kubectl-top-pods-memory-ns2.txt"     @("top", "pods", "-n", $Namespace2, "--sort-by=memory")
        Run-Kubectl "top pods cpu ($Namespace2)"        "kubectl-top-pods-cpu-ns2.txt"        @("top", "pods", "-n", $Namespace2, "--sort-by=cpu")
        Run-Kubectl "top pods containers ($Namespace2)" "kubectl-top-pods-containers-ns2.txt" @("top", "pods", "-n", $Namespace2, "--containers", "--sort-by=memory")
    }
    # Cluster-wide
    Run-Kubectl "top pods cluster-wide memory"     "kubectl-top-pods-all-memory.txt"     @("top", "pods", "-A", "--sort-by=memory")
    Run-Kubectl "top pods cluster-wide cpu"        "kubectl-top-pods-all-cpu.txt"        @("top", "pods", "-A", "--sort-by=cpu")
    Run-Kubectl "top pods cluster-wide containers" "kubectl-top-pods-all-containers.txt" @("top", "pods", "-A", "--containers", "--sort-by=memory")
} else {
    Write-Host "  SKIPPED (metrics-server unavailable)"
}

# ---------------------------------------------------------------------------
# 3. Requests & Limits
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[3/15] Requests & Limits"

Run-Kubectl "resource requests/limits ($Namespace)" "kubectl-get-resource-requests.txt" @(
    "get", "pods", "-n", $Namespace, "-o",
    "custom-columns=NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory"
)
Run-Kubectl "resource quotas" "kubectl-get-resourcequota.txt" @("get", "resourcequota", "-n", $Namespace, "-o", "wide")
Run-Kubectl "limit ranges"    "kubectl-get-limitrange.txt"    @("get", "limitrange",    "-n", $Namespace, "-o", "yaml")

if ($Namespace2) {
    Run-Kubectl "resource requests/limits ($Namespace2)" "kubectl-get-resource-requests-ns2.txt" @(
        "get", "pods", "-n", $Namespace2, "-o",
        "custom-columns=NAME:.metadata.name,CPU-REQ:.spec.containers[*].resources.requests.cpu,CPU-LIM:.spec.containers[*].resources.limits.cpu,MEM-REQ:.spec.containers[*].resources.requests.memory,MEM-LIM:.spec.containers[*].resources.limits.memory"
    )
}

# ---------------------------------------------------------------------------
# 4. Events
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[4/15] Events"
Run-Kubectl "all events ($Namespace)"     "kubectl-events-all.txt"          @("get", "events", "-n", $Namespace,  "--sort-by=.lastTimestamp")
Run-Kubectl "warning events ($Namespace)" "kubectl-events-warnings.txt"     @("get", "events", "-n", $Namespace,  "--field-selector=type=Warning", "--sort-by=.lastTimestamp")
if ($Namespace2) {
    Run-Kubectl "all events ($Namespace2)"     "kubectl-events-all-ns2.txt"      @("get", "events", "-n", $Namespace2, "--sort-by=.lastTimestamp")
    Run-Kubectl "warning events ($Namespace2)" "kubectl-events-warnings-ns2.txt" @("get", "events", "-n", $Namespace2, "--field-selector=type=Warning", "--sort-by=.lastTimestamp")
}

# ---------------------------------------------------------------------------
# 5. Linkerd Health
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[5/15] Linkerd Health"
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

# ---------------------------------------------------------------------------
# 6. Redis
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[6/15] Redis"
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

# ---------------------------------------------------------------------------
# 7. PVC / Storage
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[7/15] PVC / Storage"
Run-Kubectl "PVCs"           "kubectl-get-pvc.txt"          @("get", "pvc",          "-n", $Namespace, "-o", "wide")
Run-Kubectl "PVs"            "kubectl-get-pv.txt"           @("get", "pv",           "-o", "wide")
Run-Kubectl "StorageClasses" "kubectl-get-storageclass.txt" @("get", "storageclass")

# ---------------------------------------------------------------------------
# 8. HPA
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[8/15] HPA"
Run-Kubectl "HPAs ($Namespace)" "kubectl-get-hpa.txt" @("get", "hpa", "-n", $Namespace, "-o", "wide")

$hpas = kubectl get hpa -n $Namespace -o name 2>&1 | ForEach-Object { $_ -replace "^horizontalpodautoscaler/", "" }
foreach ($hpa in $hpas) {
    $outFile = Join-Path $LogsDir "kubectl-describe-hpa-${hpa}.txt"
    Write-Host -NoNewline "  Describe HPA $hpa ... "
    kubectl describe hpa -n $Namespace $hpa 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

if ($Namespace2) {
    Run-Kubectl "HPAs ($Namespace2)" "kubectl-get-hpa-ns2.txt" @("get", "hpa", "-n", $Namespace2, "-o", "wide")
}

# ---------------------------------------------------------------------------
# 9. Scheduling Constraints
# Use ConvertFrom-Json instead of jsonpath curly-brace expressions (PowerShell compat)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[9/15] Scheduling Constraints"
Run-Kubectl "node taints" "kubectl-get-node-taints.txt" @(
    "get", "nodes", "-o",
    "custom-columns=NAME:.metadata.name,TAINTS:.spec.taints"
)

$constraintsFile = Join-Path $LogsDir "kubectl-get-scheduling-constraints.txt"
"" | Out-File -FilePath $constraintsFile -Encoding utf8  # create/truncate

foreach ($ns_loop in @($Namespace) + @($Namespace2 | Where-Object { $_ })) {
    $depJson = kubectl get deployments -n $ns_loop -o json 2>&1 | ConvertFrom-Json
    foreach ($dep in $depJson.items) {
        $name   = $dep.metadata.name
        $aff    = $dep.spec.template.spec.affinity | ConvertTo-Json -Depth 5 -Compress
        $topo   = $dep.spec.template.spec.topologySpreadConstraints | ConvertTo-Json -Depth 5 -Compress
        Write-Host -NoNewline "  Affinity/topology: $name ($ns_loop) ... "
        $block = "=== $ns_loop / $name ===`nAffinity: $aff`nTopologySpread: $topo`n"
        Add-Content -Path $constraintsFile -Value $block -Encoding utf8
        Write-Host "OK"
    }
}

# ---------------------------------------------------------------------------
# 10. Pod Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[10/15] Pod Summary"
Run-Kubectl "all pods ($Namespace)"          "kubectl-get-pods-all.txt"         @("get", "pods", "-n", $Namespace, "-o", "wide")
Run-Kubectl "non-running pods ($Namespace)"  "kubectl-get-pods-nonrunning.txt"  @("get", "pods", "-n", $Namespace, "--field-selector=status.phase!=Running", "-o", "wide")
Run-Kubectl "pod restart counts ($Namespace)" "kubectl-get-pods-restarts.txt"   @(
    "get", "pods", "-n", $Namespace, "-o",
    "custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,STATUS:.status.phase,NODE:.spec.nodeName"
)
if ($Namespace2) {
    Run-Kubectl "all pods ($Namespace2)"         "kubectl-get-pods-all-ns2.txt"        @("get", "pods", "-n", $Namespace2, "-o", "wide")
    Run-Kubectl "non-running pods ($Namespace2)" "kubectl-get-pods-nonrunning-ns2.txt" @("get", "pods", "-n", $Namespace2, "--field-selector=status.phase!=Running", "-o", "wide")
}
Run-Kubectl "non-running pods (cluster-wide)" "kubectl-get-pods-nonrunning-all.txt" @("get", "pods", "-A", "--field-selector=status.phase!=Running", "-o", "wide")

# ---------------------------------------------------------------------------
# 11. IFS Application Health
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[11/15] IFS Application Health"

# Deployment image tags -- confirms Helm upgrade landed
Run-Kubectl "deployment images ($Namespace)" "kubectl-get-deployment-images.txt" @(
    "get", "deployments", "-n", $Namespace, "-o",
    "custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas,DESIRED:.spec.replicas"
)
if ($Namespace2) {
    Run-Kubectl "deployment images ($Namespace2)" "kubectl-get-deployment-images-ns2.txt" @(
        "get", "deployments", "-n", $Namespace2, "-o",
        "custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image,READY:.status.readyReplicas,DESIRED:.spec.replicas"
    )
}

# IFS-specific pods by label
$ifsPodsFile = Join-Path $LogsDir "kubectl-get-ifs-app-pods.txt"
foreach ($selector in @("app=ifs-main", "app=ifs-enums", "app=mws", "app.kubernetes.io/name=mws")) {
    Write-Host -NoNewline "  IFS pods selector $selector ... "
    $result = kubectl get pods -n $Namespace -l $selector `
        -o "custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[*].ready" 2>&1
    if ($LASTEXITCODE -eq 0 -and $result -notmatch "No resources found") {
        "=== $selector ===" | Add-Content -Path $ifsPodsFile -Encoding utf8
        $result             | Add-Content -Path $ifsPodsFile -Encoding utf8
        ""                  | Add-Content -Path $ifsPodsFile -Encoding utf8
        Write-Host "OK"
    } else {
        Write-Host "not found"
    }
}

# Pod labels in use
Run-Kubectl "pod labels ($Namespace)" "kubectl-get-pod-labels.txt" @("get", "pods", "-n", $Namespace, "--show-labels", "-o", "wide")

# Describe pods where Ready = False -- use ConvertFrom-Json to avoid jsonpath curly braces
Write-Host -NoNewline "  Describe not-ready pods ... "
$podJson = kubectl get pods -n $Namespace -o json 2>&1 | ConvertFrom-Json
foreach ($pod in $podJson.items) {
    $podName = $pod.metadata.name
    foreach ($cond in $pod.status.conditions) {
        if ($cond.type -eq "Ready" -and $cond.status -eq "False") {
            $outFile = Join-Path $LogsDir "kubectl-describe-pod-notready-${podName}.txt"
            kubectl describe pod -n $Namespace $podName 2>&1 | Out-File -FilePath $outFile -Encoding utf8
        }
    }
}
Write-Host "OK"

# ---------------------------------------------------------------------------
# 12. StatefulSets
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[12/15] StatefulSets"
Run-Kubectl "StatefulSets ($Namespace)" "kubectl-get-statefulsets.txt" @("get", "statefulsets", "-n", $Namespace, "-o", "wide")

$stsList = kubectl get statefulsets -n $Namespace -o name 2>&1 | ForEach-Object { $_ -replace "^statefulset.apps/", "" }
foreach ($sts in $stsList) {
    $outFile = Join-Path $LogsDir "kubectl-describe-statefulset-${sts}.txt"
    Write-Host -NoNewline "  Describe StatefulSet $sts ... "
    kubectl describe statefulset -n $Namespace $sts 2>&1 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "OK"
}

if ($Namespace2) {
    Run-Kubectl "StatefulSets ($Namespace2)" "kubectl-get-statefulsets-ns2.txt" @("get", "statefulsets", "-n", $Namespace2, "-o", "wide")
    $stsList2 = kubectl get statefulsets -n $Namespace2 -o name 2>&1 | ForEach-Object { $_ -replace "^statefulset.apps/", "" }
    foreach ($sts in $stsList2) {
        $outFile = Join-Path $LogsDir "kubectl-describe-statefulset-${sts}-ns2.txt"
        Write-Host -NoNewline "  Describe StatefulSet $sts ($Namespace2) ... "
        kubectl describe statefulset -n $Namespace2 $sts 2>&1 | Out-File -FilePath $outFile -Encoding utf8
        Write-Host "OK"
    }
}

# ---------------------------------------------------------------------------
# 13. Node Conditions and Pressure
# Use ConvertFrom-Json -- no jsonpath curly braces needed
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[13/15] Node Conditions and Pressure"
Write-Host -NoNewline "  Parsing node conditions and capacity ... "

$nodeJson = kubectl get nodes -o json 2>&1 | ConvertFrom-Json
$nodeCondFile = Join-Path $LogsDir "kubectl-get-node-conditions.txt"
$lines = @()

foreach ($node in $nodeJson.items) {
    $name = $node.metadata.name
    $conditions = @{}
    foreach ($c in $node.status.conditions) { $conditions[$c.type] = $c.status }
    $alloc = $node.status.allocatable
    $cap   = $node.status.capacity

    $lines += "=== Node: $name ==="
    foreach ($cond in @("MemoryPressure", "DiskPressure", "PIDPressure")) {
        $status = if ($conditions.ContainsKey($cond)) { $conditions[$cond] } else { "Unknown" }
        $flag   = if ($status -eq "True") { "ALERT" } else { "OK" }
        $lines += "  ${cond}: $status [$flag]"
    }
    $lines += "  Ready: $($conditions['Ready'])"
    $lines += "  CPU       -- Allocatable: $($alloc.cpu)  Capacity: $($cap.cpu)"
    $lines += "  Memory    -- Allocatable: $($alloc.memory)  Capacity: $($cap.memory)"
    $lines += "  Max Pods  -- Allocatable: $($alloc.pods)  Capacity: $($cap.pods)"
    $lines += ""
}

$lines | Out-File -FilePath $nodeCondFile -Encoding utf8
Write-Host "OK"

# ---------------------------------------------------------------------------
# 14. API Server and Control Plane Health
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[14/15] API Server and Control Plane Health"

Run-Kubectl "kube-system pods"           "kubectl-get-kubesystem-pods.txt" @("get", "pods", "-n", "kube-system", "-o", "wide")
Run-Kubectl "kube-system warning events" "kubectl-events-kubesystem.txt"   @("get", "events", "-n", "kube-system", "--field-selector=type=Warning", "--sort-by=.lastTimestamp")
Run-Kubectl "APIService status"          "kubectl-get-apiservices.txt"     @("get", "apiservices")
Run-Kubectl "metrics-server pod status"  "kubectl-get-metrics-server.txt"  @("get", "pods", "-n", "kube-system", "-l", "k8s-app=metrics-server", "-o", "wide")

if ($MetricsOK) {
    Run-Kubectl "kube-system top pods" "kubectl-top-kubesystem-pods.txt" @("top", "pods", "-n", "kube-system", "--sort-by=memory")
}

# APIServices with non-True Available condition -- use ConvertFrom-Json
Write-Host -NoNewline "  APIServices not available ... "
$apiSvcJson = kubectl get apiservices -o json 2>&1 | ConvertFrom-Json
$apiProblems = @()
foreach ($svc in $apiSvcJson.items) {
    $svcName = $svc.metadata.name
    foreach ($cond in $svc.status.conditions) {
        if ($cond.type -eq "Available" -and $cond.status -ne "True") {
            $apiProblems += "NOT AVAILABLE: $svcName  Reason: $($cond.reason)  Message: $($cond.message)"
        }
    }
}
$apiProbFile = Join-Path $LogsDir "kubectl-get-apiservices-problems.txt"
if ($apiProblems.Count -gt 0) {
    $apiProblems | Out-File -FilePath $apiProbFile -Encoding utf8
} else {
    "All APIServices available." | Out-File -FilePath $apiProbFile -Encoding utf8
}
Write-Host "OK"

# ---------------------------------------------------------------------------
# 15. Rollout and Deployment History
# --timeout=10s prevents hanging on stuck rollouts
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[15/15] Rollout and Deployment History"
$rolloutFile = Join-Path $LogsDir "kubectl-get-rollout-status.txt"
$historyFile = Join-Path $LogsDir "kubectl-get-rollout-history.txt"
"" | Out-File -FilePath $rolloutFile -Encoding utf8
"" | Out-File -FilePath $historyFile -Encoding utf8

foreach ($ns_loop in @($Namespace) + @($Namespace2 | Where-Object { $_ })) {
    $deps = kubectl get deployments -n $ns_loop -o name 2>&1 | ForEach-Object { $_ -replace "^deployment.apps/", "" }
    foreach ($dep in $deps) {
        Write-Host -NoNewline "  Rollout status: $dep ($ns_loop) ... "
        $statusOut = kubectl rollout status "deployment/$dep" -n $ns_loop --timeout=10s 2>&1
        "=== $ns_loop / $dep ===" | Add-Content -Path $rolloutFile -Encoding utf8
        $statusOut                 | Add-Content -Path $rolloutFile -Encoding utf8
        ""                         | Add-Content -Path $rolloutFile -Encoding utf8
        Write-Host "OK"

        Write-Host -NoNewline "  Rollout history: $dep ($ns_loop) ... "
        $histOut = kubectl rollout history "deployment/$dep" -n $ns_loop 2>&1
        "=== $ns_loop / $dep ===" | Add-Content -Path $historyFile -Encoding utf8
        $histOut                   | Add-Content -Path $historyFile -Encoding utf8
        ""                         | Add-Content -Path $historyFile -Encoding utf8
        Write-Host "OK"
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
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
