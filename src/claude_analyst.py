"""
claude_analyst.py — Sends parsed errors and user context to Claude for structured RCA.
"""

import json
import os
from collections import Counter
from typing import Dict, List

import anthropic
from dotenv import load_dotenv
from rich.console import Console

from src.log_parser import LogError
from src import knowledge_base

load_dotenv()

console = Console()

_SYSTEM_PROMPT = """You are a Senior Site Reliability Engineer (SRE) specialising in
Kubernetes infrastructure and IFS ERP systems. You produce formal Root Cause Analysis
(RCA) reports used by engineering teams, management, and CAB (Change Advisory Board).

DEPLOYMENT ENVIRONMENT — read carefully before analysing:
- Platform  : Ubuntu MicroK8s (lightweight single-node or very-few-node Kubernetes)
- HA model  : Multiple pod REPLICAS per service — NOT multiple nodes
- Consequence: Pod anti-affinity (Gate 6) and topology-spread constraints (Gate 8)
  are the MOST COMMON scheduling failure causes because a second replica cannot be
  placed on the same (only) node when requiredDuringScheduling rules are set.
  ALWAYS check these two gates first on any Pending pod.
- NodeNotReady is a total-outage event (only node) — treat as P1 immediately.
- Resource exhaustion (Gate 3) is still very likely on a single node shared by all
  IFS pods (App Server, BO Server, MWS, OData, IAM, Reporting, etc.).
- Reference docs: https://docs.ifs.com/techdocs/25r2/ and https://community.ifs.com/

You will receive a JSON payload containing:
  - user_context   : incident metadata supplied by the engineer
  - errors         : structured log events extracted from multiple sources
                     (container_log, kubectl_describe, linkerd_log, other)
  - errors_by_type / errors_by_source_type : distribution summaries

Produce a structured RCA report with EXACTLY these nine sections, using the
Markdown headings shown (the document generator depends on them):

## 1. Executive Summary
Plain-English paragraph (3-5 sentences). What happened, when, and what the
immediate business impact was. No jargon.

## 2. Scope & Impact
- Affected users / requests / transactions (estimate from evidence)
- Incident duration
- SLA / SLO breach (if determinable)
- Downstream services impacted

## 3. Timeline of Events
Ordered list of key events with timestamps (extract from log data + user context).
Include: first alert, deploy/config changes, service degradation start, cascading
failures, recovery actions.

## 4. Technical Analysis
Sub-sections:
### 4a. Evidence Set
List the log sources used and what each revealed.
### 4b. Healthy vs Incident State Comparison
What was normal vs what changed.
### 4c. Correlation
How the signals across container logs, kubectl describe, Linkerd proxy, ingress,
and autoscaler corroborate each other.

## 5. Root Cause
One concise statement of the primary root cause, followed by the chain of events
in numbered steps (cause → effect → effect…).

## 6. Corrective Actions
Table with columns: Action | Status (Done/In-Progress/Planned) | Owner | ETA
Include only actions that directly stop the bleeding or restore service.

## 7. Preventive Actions
Table with columns: Action | Owner | ETA | Risk if Skipped | CAB Required (Y/N)
Long-term fixes to prevent recurrence.

## 8. Validation Plan
- KPIs / metrics to monitor post-fix
- Success criteria (e.g. "zero OOMKill events for 48 h")
- Rollback criteria

## 9. Appendix
Key log excerpts (max 5, most diagnostic) formatted as code blocks.

Be precise and evidence-based. Reference specific pod names, timestamps, and error
messages from the supplied data. Do not invent facts not present in the evidence."""


_MAX_PHASES = 2

# ---------------------------------------------------------------------------
# Phase system prompts
# ---------------------------------------------------------------------------

_STATUS_INSTRUCTIONS = """
At the end of your response, always append this exact block (no extra text after it):

---
INVESTIGATION_STATUS: CONFIRMED | NEEDS_FURTHER_INVESTIGATION
NEXT_PHASE: resource_pressure | network_connectivity | scheduling_failure | application_crash | cascade_origin | none
CONFIRMED_ROOT_CAUSE: <one sentence — only when CONFIRMED, else leave blank>
---

Choose CONFIRMED only when you can state the specific, actionable root cause with evidence.
Choose NEEDS_FURTHER_INVESTIGATION when the cause is still unclear and name the best next phase."""

_PHASE_SYSTEM_PROMPTS = {

    "resource_pressure": f"""You are a Senior SRE specialising in Kubernetes and IFS ERP systems.
A previous investigation phase identified RESOURCE PRESSURE as a factor.
Your task: determine EXACTLY what caused the resource pressure.

Investigate and confirm or rule out each with evidence from the log data:
1. Memory leak — gradual RSS growth before OOMKill
2. Traffic spike — sudden request surge in ingress/app logs
3. Limits set too low — pod spec limits below normal workload requirements
4. Runaway HPA — autoscaler created too many replicas for available node capacity
5. Noisy neighbour — another workload in the namespace consuming quota
6. ResourceQuota too tight — namespace quota near-full before incident
7. Missing resource requests — pod spec had no resources block, LimitRange defaulted low
8. Node pool undersized — all nodes already at capacity, no burst headroom

Produce these sections:
## Resource Pressure Investigation
### Cause (confirmed or most likely)
### Evidence (bullet list with log references)
### What changed vs normal
### Immediate remediation
### Long-term prevention
{_STATUS_INSTRUCTIONS}""",

    "network_connectivity": f"""You are a Senior SRE specialising in Kubernetes and IFS ERP systems.
A previous phase identified NETWORK CONNECTIVITY failures.
Your task: find the exact cause of the connectivity breakdown.

Investigate:
1. DNS resolution failure — CoreDNS issues, service name typo
2. Service endpoint not ready — all pods behind Service are unready
3. NetworkPolicy blocking traffic — ingress/egress rules too restrictive
4. Linkerd/service mesh misconfiguration — mTLS rejection, proxy crash
5. Ingress controller failure — upstream 502/503 from NGINX/Traefik
6. Database port unreachable — DB pod restarted, port binding failed
7. External dependency down — third-party API, LDAP, Oracle listener

Produce these sections:
## Network Connectivity Investigation
### Cause (confirmed or most likely)
### Evidence (bullet list with log references)
### Affected communication paths
### Immediate remediation
### Long-term prevention
{_STATUS_INSTRUCTIONS}""",

    "scheduling_failure": f"""You are a Senior SRE specialising in Kubernetes and IFS ERP systems.
A previous phase identified SCHEDULING FAILURES (pod stuck Pending).
Your task: identify which scheduling gate is blocking the pod.

ENVIRONMENT: Ubuntu MicroK8s — typically SINGLE NODE with multiple pod replicas for HA.
Gates 6 and 8 are the most likely culprits — check them FIRST.

Check each gate with evidence (priority order for MicroK8s single-node):
Gate 6 — Pod anti-affinity unsatisfiable ★ MOST LIKELY on single-node MicroK8s
          (requiredDuringScheduling prevents 2nd replica landing on same node)
Gate 8 — Topology spread constraint violated ★ VERY LIKELY on single-node
          (maxSkew + DoNotSchedule with only 1 node = 2nd pod stays Pending forever)
Gate 3 — No allocatable CPU/memory — single node shared by ALL IFS pods, easy to exhaust
Gate 1 — ResourceQuota exceeded in namespace
Gate 2 — LimitRange violation (no resources.requests defined in IFS Helm chart)
Gate 7 — PVC not bound (MicroK8s uses hostpath-storage by default — check StorageClass)
Gate 4 — Taint not tolerated
Gate 5 — nodeSelector/nodeAffinity label mismatch

Produce these sections:
## Scheduling Failure Investigation
### Blocked gate(s) identified
### Evidence per gate (confirm or rule out)
### Root cause of the gate failure
### Immediate remediation steps
### Prevention
{_STATUS_INSTRUCTIONS}""",

    "application_crash": f"""You are a Senior SRE specialising in Kubernetes and IFS ERP systems.
A previous phase identified APPLICATION-LEVEL CRASHES or exceptions.
Your task: find the exact cause of the crash.

Investigate:
1. Startup failure — misconfigured environment variable, missing secret/ConfigMap
2. Database connection pool exhausted — too many concurrent connections to Oracle/DB
3. Out of heap / thread starvation — JVM OOM, thread pool maxed out
4. Deadlock or long-running transaction — database lock contention in IFS
5. IFS license failure — license server unreachable or expired
6. Dependency service not ready — IFS component started before its dependency
7. Config change side-effect — recent values.yaml / ConfigMap change broke app

Produce these sections:
## Application Crash Investigation
### Crash cause (confirmed or most likely)
### Evidence (log excerpts, error messages)
### Dependency chain that failed
### Immediate remediation
### Prevention
{_STATUS_INSTRUCTIONS}""",

    "cascade_origin": f"""You are a Senior SRE specialising in Kubernetes and IFS ERP systems.
A previous phase identified a CASCADING FAILURE.
Your task: trace the cascade back to the single origin event.

Reconstruct the failure chain:
- What was the FIRST thing to fail? (timestamp + pod)
- What did that failure cause? (second event)
- How did it propagate to other services?
- Which IFS components amplified the failure?
- Was there a circuit breaker or retry storm that worsened it?

Produce these sections:
## Cascade Origin Investigation
### Origin event (first failure — timestamp, pod, reason)
### Propagation chain (numbered steps with timestamps)
### Amplification factors
### Where the cascade could have been stopped
### Immediate remediation
### Prevention (circuit breakers, timeouts, PDBs)
{_STATUS_INSTRUCTIONS}""",
}

# Keywords that trigger each phase (used as fallback if Claude doesn't specify NEXT_PHASE)
_PHASE_TRIGGERS = {
    "resource_pressure":   {"oomkill", "memory limit", "resource quota", "insufficient cpu",
                            "insufficient memory", "evict", "out of memory", "gate1", "gate2",
                            "gate3", "resourcequota", "limitrange", "memory pressure"},
    "network_connectivity":{"connection refused", "connection reset", "econnrefused", "502",
                            "503", "upstream", "dns", "linkerd", "service unreachable",
                            "dial tcp", "no route"},
    "scheduling_failure":  {"pending", "gate4", "gate5", "gate6", "gate7", "gate8",
                            "taint", "affinity", "nodeselector", "pvc", "topology spread",
                            "0/", "nodes available"},
    "application_crash":   {"exception", "traceback", "panic:", "nullpointer", "ora-",
                            "ifs.*error", "deadlock", "heap", "out of heap", "jvm",
                            "license", "startup fail"},
    "cascade_origin":      {"cascad", "propagat", "chain of failure", "downstream",
                            "retry storm", "multiple services"},
}

# Error types fed to each phase
_PHASE_ERROR_FILTER = {
    "resource_pressure":   {"Gate1_ResourceQuota","Gate2_LimitRange","Gate3_InsufficientResources",
                            "OOMKilled","Evicted","ScalingError","IFSError"},
    "network_connectivity":{"ConnectionError","LinkerdError","IngressError","LivenessFail",
                            "ReadinessFail"},
    "scheduling_failure":  {"Gate4_TaintToleration","Gate5_AffinityMismatch","Gate6_AntiAffinity",
                            "Gate7_PVCPending","Gate8_TopologySpread",
                            "Gate1_ResourceQuota","Gate2_LimitRange","Gate3_InsufficientResources"},
    "application_crash":   {"Exception","CrashLoopBackOff","IFSError","PodRestart","BackOff"},
    "cascade_origin":      None,   # all errors
}


def _parse_phase_status(text: str):
    """
    Parse the INVESTIGATION_STATUS block Claude appends.
    Returns (confirmed: bool, next_phase: str | None).
    """
    confirmed = False
    next_phase = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("INVESTIGATION_STATUS:"):
            if "CONFIRMED" in line and "NEEDS" not in line:
                confirmed = True
        elif line.startswith("NEXT_PHASE:"):
            val = line.split(":", 1)[1].strip().lower()
            if val and val != "none":
                next_phase = val
    return confirmed, next_phase


def _keyword_next_phase(text: str, completed: set) -> str | None:
    """Fallback: pick next phase by keyword scan if Claude didn't specify."""
    lower = text.lower()
    for phase, keywords in _PHASE_TRIGGERS.items():
        if phase not in completed and any(kw in lower for kw in keywords):
            return phase
    return None


def _filter_errors_for_phase(errors: List[LogError], phase: str) -> List[LogError]:
    allowed = _PHASE_ERROR_FILTER.get(phase)
    if allowed is None:
        return errors[:15]
    filtered = [e for e in errors if e.error_type in allowed]
    return (filtered if filtered else errors)[:15]


# ---------------------------------------------------------------------------
# Shared priority tables — used by both analyze_with_claude and analyze_health
# ---------------------------------------------------------------------------

_PRIORITY = {
    "Gate6_AntiAffinity": 1, "Gate8_TopologySpread": 1,
    "Gate3_InsufficientResources": 1,
    "Gate1_ResourceQuota": 2, "Gate2_LimitRange": 2, "Gate7_PVCPending": 2,
    "Gate4_TaintToleration": 3, "Gate5_AffinityMismatch": 3,
    "CrashLoopBackOff": 2, "OOMKilled": 2, "NodeNotReady": 2, "Evicted": 2,
    "ImagePullError": 3, "LivenessFail": 3, "ReadinessFail": 3, "ConnectionError": 3,
    "PodRestart": 4, "Exception": 4, "BackOff": 4,
    "IFSError": 4, "LinkerdError": 4, "IngressError": 4, "ScalingError": 4,
}

_SEV_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}


def _pod_score(pod: str) -> int:
    """Lower score = higher priority. ifsapp-odata is always first."""
    p = pod.lower()
    if "odata" in p:               return 0   # ifsapp-odata       ★ highest
    if "iam" in p:                 return 1   # ifsapp-iam
    if "client-services" in p:     return 2   # ifsapp-client-services
    if "proxy" in p:               return 3   # ifsapp-proxy
    if "client-notification" in p: return 4   # ifsapp-client-notification
    if "client" in p:              return 5   # ifsapp-client
    if "connect" in p:             return 6   # ifsapp-connect
    if "application-svc" in p:     return 7   # ifsapp-application-svc
    if "db-init" in p:             return 8   # ifs-db-init
    if "docman" in p or "esign" in p: return 9
    if "doc" in p:                 return 10
    if "reporting" in p or "crystal" in p: return 11
    return 99


def _call_claude(client, system: str, user: str, max_tokens: int = 2048) -> str:
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "text":
            return block.text
    return ""


_MOCK_RESPONSE = """\
PRIMARY ROOT CAUSE:
The IFS application experienced a cascading failure initiated by
an OOMKilled event on the database pod ifs-db-primary-0. The pod
exceeded its 2Gi memory limit causing it to be killed by the
Kubernetes OOM controller.

CHAIN OF EVENTS:
1. 14:01:45 - ifs-db-primary-0 exceeded memory limit and was OOMKilled
2. 14:02:10 - Application pods lost database connectivity
3. 14:03:22 - ifs-app-7d9f4b-xk2p entered CrashLoopBackOff due to
              failed database connections
4. 14:05:00 - Liveness probes began failing across all app pods
5. 14:07:30 - Full service unavailability reached

BLAST RADIUS:
- IFS Core application completely unavailable
- All user sessions terminated
- Payment module affected due to shared database dependency

RECOMMENDATIONS:
1. Increase database pod memory limit from 2Gi to 4Gi
2. Add memory usage alerting at 80% threshold
3. Implement database connection retry logic in application
4. Add Pod Disruption Budget to prevent simultaneous pod failures
5. Schedule regular memory profiling of database workloads
"""


_HEALTH_SYSTEM_PROMPT = """You are a Senior Site Reliability Engineer (SRE) specialising in
Kubernetes infrastructure and IFS ERP systems. You are performing a PROACTIVE HEALTH ASSESSMENT
— not investigating a specific incident. Your goal is to identify current health status, risks,
and potential issues before they become incidents.

DEPLOYMENT ENVIRONMENT — read carefully before analysing:
- Platform  : Ubuntu MicroK8s (lightweight single-node or very-few-node Kubernetes)
- HA model  : Multiple pod REPLICAS per service — NOT multiple nodes
- Consequence: Pod anti-affinity (Gate 6) and topology-spread constraints (Gate 8) are the MOST
  COMMON scheduling risks because a second replica cannot be placed on the same (only) node when
  requiredDuringScheduling rules are set.
- Resource exhaustion is a constant risk on a single node shared by all IFS pods.
- Reference docs: https://docs.ifs.com/techdocs/25r2/ and https://community.ifs.com/

You will receive a JSON payload that may include:
  - events         : detected log error patterns
  - kubectl_top    : kubectl top nodes / top pods output
  - kubectl_get    : kubectl get pods, PVCs, HPAs, resource quotas, scheduling constraints
  - kubectl_describe : kubectl describe node, HPA, deployment output
  - kubectl_events : kubectl get events output

Produce a HEALTH REPORT with EXACTLY these seven sections using the Markdown headings shown
(the document generator depends on them):

## 1. Cluster Health Overview
Overall cluster status: Healthy / Warning / Critical.
2-3 sentence summary of what the data reveals about current cluster state.
Health score table per area:

| Area | Status | Summary |
|------|--------|---------|
| Node Utilisation | Healthy/Warning/Critical | one-line finding |
| Pod Utilisation | Healthy/Warning/Critical | one-line finding |
| Requests & Limits | Healthy/Warning/Critical | one-line finding |
| Events | Healthy/Warning/Critical | one-line finding |
| Linkerd / Service Mesh | Healthy/Warning/Critical/Unknown | one-line finding |
| Redis | Healthy/Warning/Critical/Unknown | one-line finding |
| PVC / Storage | Healthy/Warning/Critical/Unknown | one-line finding |
| HPA / Autoscaling | Healthy/Warning/Critical/Unknown | one-line finding |
| Scheduling Constraints | Healthy/Warning/Critical | one-line finding |
| Pod Summary | Healthy/Warning/Critical | one-line finding |

## 2. Problems Found
List EVERY identified problem clearly. For each problem:
- **[SEVERITY] Problem title** — one-line description
  - Evidence: quote the specific metric, pod name, or log line that proves the problem
  - Impact: what breaks or degrades if this is not fixed

Severity levels: CRITICAL (service down/imminent), WARNING (degradation likely), INFO (hygiene).
Order: CRITICAL first, then WARNING, then INFO.
If no problems are found in an area, state "No issues detected."

## 3. How to Fix — Step-by-Step Resolutions
For EVERY problem listed in Section 2, provide a numbered resolution block:

### Fix N: [Problem title from Section 2]
**Root cause:** one sentence
**Steps:**
1. Exact kubectl command or config change (copy-paste ready)
2. Next step
3. Verify with: `kubectl command to confirm fix worked`
**Expected outcome:** what should be true after the fix
**Risk if skipped:** what happens if left unfixed

## 4. Resource & Capacity Analysis
- Node CPU and memory: current usage vs allocatable (from kubectl_top / describe node)
- Pods closest to their memory/CPU limits (list top 5 by utilisation %)
- ResourceQuota consumed vs total (exact numbers if available)
- HPA current replicas vs min/max, and whether autoscaling is healthy
- Capacity headroom: can the node absorb a pod restart or traffic spike?

## 5. Service Health Status
Per-component health table: Component | Status | Key Observations
Components to cover: ifsapp-odata, ifsapp-iam, ifsapp-client, ifsapp-proxy, ifsapp-connect,
ifsapp-reporting, ifsapp-docman, ifsapp-application-svc, ifs-db-init, Redis, Linkerd
Status: Healthy | Degraded | At Risk | Critical | Unknown (no data)

## 6. Missing Information
Data that was NOT available in this assessment but would improve accuracy:

| Missing Data | Why It Matters | Collect With |
|--------------|----------------|--------------|
| ... | ... | `kubectl command` |

## 7. Priority Action List
Consolidated list of all actions from Section 3, ordered by urgency:

| Priority | Action | Component | Effort | Risk if Skipped |
|----------|--------|-----------|--------|-----------------|
| P1 | ... | ... | Low/Med/High | ... |

Be evidence-based. Reference specific pod names, exact numbers, and timestamps from the data.
If a data source (e.g. kubectl_top) was not provided, note it as Unknown and explain what to run."""


_MOCK_HEALTH_RESPONSE = """\
## 1. Cluster Health Overview

Overall status: **Warning** — several scheduling and resource pressure signals detected.

The logs show recurring Gate 6 (anti-affinity) and Gate 8 (topology spread) violations
indicating that HA replica placement is failing on this single-node MicroK8s cluster.
Memory pressure on the ifsapp-odata pod is approaching its configured limit.

| Subsystem    | Status  |
|--------------|---------|
| Scheduling   | Warning |
| Resources    | Warning |
| Networking   | Healthy |
| Application  | Healthy |
| Storage      | Unknown |

## 2. Risk Assessment

| Risk | Severity | Component | Evidence | Likelihood |
|------|----------|-----------|----------|------------|
| Second replica of ifsapp-odata cannot be scheduled | P1 | ifsapp-odata | Gate6_AntiAffinity events | High |
| Memory OOMKill if traffic spikes | P2 | ifsapp-odata | RSS 1.8Gi / 2Gi limit | Medium |
| Topology spread blocking iam replica | P2 | ifsapp-iam | Gate8_TopologySpread events | High |

## 3. Resource & Capacity Analysis

- ifsapp-odata: memory at ~90% of 2Gi limit — OOMKill risk under load
- Single node: all IFS pods sharing node resources; no burst headroom observed
- No kubectl top data available — recommend running `kubectl top nodes` and `kubectl top pods`

## 4. Service Health Status

| Component | Status | Observations |
|-----------|--------|--------------|
| ifsapp-odata | At Risk | Memory pressure, Gate6 scheduling failures |
| ifsapp-iam | Degraded | Gate8 topology spread violations |
| ifsapp-client | Healthy | No errors detected |
| ifsapp-proxy | Unknown | No log data available |

## 5. Configuration & Scheduling Concerns

- Anti-affinity rules set to `requiredDuringScheduling` — this prevents 2nd replica on single-node MicroK8s
- Topology spread `maxSkew: 1` with `DoNotSchedule` — same issue as above
- Recommend switching to `preferredDuringScheduling` for non-production or adding a second node

## 6. Missing Information

| Missing Data | Why It Matters | How to Collect |
|--------------|----------------|----------------|
| Node resource usage | Confirm capacity headroom | `kubectl top nodes` |
| Pod resource usage | Identify memory/CPU hotspots | `kubectl top pods -A` |
| PVC status | Storage health | `kubectl get pvc -A` |
| Recent events | Scheduling/probe failures | `kubectl get events -A --sort-by=.lastTimestamp` |

## 7. Recommendations

| Action | Priority | Component | Expected Benefit |
|--------|----------|-----------|-----------------|
| Change anti-affinity to `preferredDuringScheduling` | P1 | ifsapp-odata, ifsapp-iam | Allow HA replicas on single node |
| Increase ifsapp-odata memory limit to 3Gi | P1 | ifsapp-odata | Prevent OOMKill under load |
| Add `kubectl top` outputs to log collection | P2 | All | Better capacity analysis |
| Review topology spread constraints across all deployments | P2 | All | Prevent future Gate8 blocks |
"""


def analyze_health(
    errors: List[LogError],
    extra_context: Dict[str, str] = None,
) -> str:
    """
    Perform a proactive health assessment of the cluster based on log data.

    Unlike analyze_with_claude(), this does not require an incident — it assesses
    overall cluster health, identifies risks, and flags missing information.

    Args:
        errors:        All LogError objects detected in the logs.
        extra_context: Optional dict from log_parser.extract_context().

    Returns:
        Claude's 7-section health report as a Markdown string.
    """
    extra_context = extra_context or {}

    if os.getenv("MOCK_MODE", "false").strip().lower() == "true":
        console.print(
            "\n[bold yellow][MOCK MODE] Skipping real API call - using mock health response[/bold yellow]"
        )
        return _MOCK_HEALTH_RESPONSE

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.example to .env and add your key."
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Sample errors — guarantee one per type, up to 25 total
    # Same pod priority as RCA: ifsapp-odata first, then iam, client-services, etc.
    sorted_errors = sorted(
        errors,
        key=lambda e: (
            _PRIORITY.get(e.error_type, 9),
            _pod_score(e.pod_name),
            _SEV_ORDER.get(e.severity, 9),
        ),
    )

    sampled: List[LogError] = []
    seen_types: set = set()
    for e in sorted_errors:
        if e.error_type not in seen_types:
            seen_types.add(e.error_type)
            sampled.append(e)

    _CAP = 25
    seen_pairs: set = {(e.error_type, e.pod_name) for e in sampled}
    for e in sorted_errors:
        if len(sampled) >= _CAP:
            break
        key = (e.error_type, e.pod_name)
        if key not in seen_pairs:
            seen_pairs.add(key)
            sampled.append(e)

    if len(sampled) < len(errors):
        console.print(
            f"[dim]Sending {len(sampled)} representative errors to Claude "
            f"(selected from {len(errors)} total by priority)[/dim]"
        )

    error_list = [
        {
            "timestamp":  e.timestamp,
            "error_type": e.error_type,
            "pod":        e.pod_name,
            "namespace":  e.namespace,
            "message":    e.message,
            "source":     e.source_file,
            "file_type":  e.file_type,
            "severity":   e.severity,
        }
        for e in sampled
    ]

    payload = {
        "analysis_type":         "proactive_health_assessment",
        "total_events_found":    len(errors),
        "errors_by_type":        dict(Counter(e.error_type for e in errors)),
        "errors_by_source_type": dict(Counter(e.file_type  for e in errors)),
        "affected_pods":         list({e.pod_name for e in errors if e.pod_name != "unknown"}),
        "events":                error_list,
    }

    if extra_context.get("ticket"):
        payload["incident_communications"] = extra_context["ticket"]
        console.print("[dim]Ticket/communications context found — included.[/dim]")
    if extra_context.get("kubectl_events"):
        payload["kubectl_events"] = extra_context["kubectl_events"]
        console.print("[dim]kubectl events output found — included.[/dim]")
    if extra_context.get("kubectl_top"):
        payload["kubectl_top"] = extra_context["kubectl_top"]
        console.print("[dim]kubectl top output found — included.[/dim]")
    if extra_context.get("kubectl_get"):
        payload["kubectl_get"] = extra_context["kubectl_get"]
        console.print("[dim]kubectl get output found — included.[/dim]")
    if extra_context.get("kubectl_describe"):
        payload["kubectl_describe"] = extra_context["kubectl_describe"]
        console.print("[dim]kubectl describe output found — included.[/dim]")

    prompt_text = (
        "Please produce a full Cluster Health Report following the 7-section structure "
        "defined in the system prompt.\n\n"
        "Section 2 must list EVERY problem found with evidence.\n"
        "Section 3 must provide a copy-paste-ready fix for EVERY problem in Section 2.\n\n"
        "Source type key: container_log=kubectl-logs; linkerd_log=Linkerd sidecar; "
        "kubectl_describe=kubectl describe output; kubectl_top=kubectl top; "
        "kubectl_get=kubectl get; kubectl_events=kubectl get events.\n\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )

    console.print("\n[bold cyan]Analysing cluster health with Claude...[/bold cyan]")
    result = _call_claude(client, _HEALTH_SYSTEM_PROMPT, prompt_text, max_tokens=4096)

    if not result:
        return "No health analysis returned."

    console.print("\n[bold green]Health assessment complete.[/bold green]")
    return result


def analyze_with_claude(
    errors: List[LogError],
    user_context: Dict[str, str],
    extra_context: Dict[str, str] = None,
) -> str:
    """
    Send errors and context to Claude Opus 4.6 and return the structured RCA text.

    Args:
        errors:        List of LogError objects from the parser.
        user_context:  Dict from user_interaction.gather_user_context().
        extra_context: Optional dict from log_parser.extract_context() containing
                       ticket, kubectl_events, kubectl_top, kubectl_get content.

    Returns:
        Claude's RCA as a Markdown string with the 9-section structure.
    """
    extra_context = extra_context or {}
    if os.getenv("MOCK_MODE", "false").strip().lower() == "true":
        console.print(
            "\n[bold yellow][MOCK MODE] Skipping real API call - using mock response[/bold yellow]"
        )
        return _MOCK_RESPONSE

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.example to .env and add your key."
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Uses module-level _PRIORITY, _SEV_ORDER, _pod_score — same as analyze_health
    sorted_errors = sorted(
        errors,
        key=lambda e: (
            _PRIORITY.get(e.error_type, 9),
            _pod_score(e.pod_name),
            _SEV_ORDER.get(e.severity, 9),
        ),
    )

    # Step 1: guarantee one representative per error type
    sampled: List[LogError] = []
    seen_types: set = set()
    for e in sorted_errors:
        if e.error_type not in seen_types:
            seen_types.add(e.error_type)
            sampled.append(e)

    # Step 2: fill remaining slots (up to 20) with highest-priority unseen (pod, type) pairs
    _CAP = 20
    seen_pairs: set = {(e.error_type, e.pod_name) for e in sampled}
    for e in sorted_errors:
        if len(sampled) >= _CAP:
            break
        key = (e.error_type, e.pod_name)
        if key not in seen_pairs:
            seen_pairs.add(key)
            sampled.append(e)

    error_list = [
        {
            "timestamp":  e.timestamp,
            "error_type": e.error_type,
            "pod":        e.pod_name,
            "namespace":  e.namespace,
            "message":    e.message,
            "source":     e.source_file,
            "file_type":  e.file_type,
            "severity":   e.severity,
        }
        for e in sampled
    ]

    if len(sampled) < len(errors):
        console.print(
            f"[dim]Sending {len(sampled)} representative errors to Claude "
            f"(selected from {len(errors)} total by priority)[/dim]"
        )

    payload = {
        "total_errors_found":    len(errors),
        "errors_by_type":        dict(Counter(e.error_type for e in errors)),
        "errors_by_source_type": dict(Counter(e.file_type  for e in errors)),
        "user_context":          user_context,
        "errors":                error_list,
    }

    # Inject ticket / kubectl command context if present
    if extra_context.get("ticket"):
        payload["incident_communications"] = extra_context["ticket"]
        console.print("[dim]Incident ticket/communications found — included in analysis.[/dim]")
    if extra_context.get("kubectl_events"):
        payload["kubectl_events"] = extra_context["kubectl_events"]
        console.print("[dim]kubectl events output found — included in analysis.[/dim]")
    if extra_context.get("kubectl_top"):
        payload["kubectl_top"] = extra_context["kubectl_top"]
        console.print("[dim]kubectl top output found — included in analysis.[/dim]")
    if extra_context.get("kubectl_get"):
        payload["kubectl_get"] = extra_context["kubectl_get"]
        console.print("[dim]kubectl get output found — included in analysis.[/dim]")

    # Knowledge base — find similar past incidents
    similar = knowledge_base.find_similar(errors, top_n=3, min_score=0.25)
    kb_block = knowledge_base.format_for_prompt(similar)
    if similar:
        console.print(
            f"[bold green]Knowledge base:[/bold green] found {len(similar)} similar "
            f"past incident(s) — injecting into prompt."
        )

    prompt_text = (
        "Please produce a full Root Cause Analysis report following the 9-section "
        "structure defined in the system prompt.\n\n"
        "Source type key: container_log=kubectl-logs output; "
        "linkerd_log=Linkerd sidecar proxy; kubectl_describe=kubectl describe output; "
        "other=miscellaneous.\n\n"
        + (f"{kb_block}\n\n" if kb_block else "")
        + f"```json\n{json.dumps(payload, indent=2)}\n```"
    )

    # ------------------------------------------------------------------
    # Phase 1 — Initial broad RCA
    # ------------------------------------------------------------------
    console.print("\n[bold cyan]Phase 1 — Initial RCA...[/bold cyan]")
    phase1 = _call_claude(client, _SYSTEM_PROMPT, prompt_text, max_tokens=4096)
    if not phase1:
        return "No analysis returned."

    phases_output = [f"# Phase 1 — Initial RCA\n\n{phase1}"]
    completed_phases: set = {"initial"}

    confirmed, next_phase = _parse_phase_status(phase1)

    # Fallback: keyword detection if Claude didn't emit the status block
    if not confirmed and not next_phase:
        next_phase = _keyword_next_phase(phase1, completed_phases)

    # ------------------------------------------------------------------
    # Phase 2 … N — focused deep dives until confirmed or cap reached
    # ------------------------------------------------------------------
    phase_num = 2
    accumulated_findings = phase1[:1200]   # rolling summary fed to each phase

    while not confirmed and next_phase and phase_num <= _MAX_PHASES:
        if next_phase not in _PHASE_SYSTEM_PROMPTS:
            break

        phase_errors = _filter_errors_for_phase(errors, next_phase)
        phase_payload = {
            "phase": phase_num,
            "investigation_focus": next_phase,
            "user_context": user_context,
            "accumulated_findings_summary": accumulated_findings,
            "relevant_errors": [
                {
                    "timestamp":  e.timestamp,
                    "error_type": e.error_type,
                    "pod":        e.pod_name,
                    "namespace":  e.namespace,
                    "message":    e.message,
                    "source":     e.source_file,
                }
                for e in phase_errors
            ],
        }

        phase_prompt = (
            f"This is investigation phase {phase_num}.\n"
            f"Focus: {next_phase.replace('_', ' ').upper()}\n\n"
            f"```json\n{json.dumps(phase_payload, indent=2)}\n```"
        )

        label = next_phase.replace("_", " ").title()
        console.print(f"\n[bold yellow]Phase {phase_num} — {label}...[/bold yellow]")

        phase_result = _call_claude(
            client,
            _PHASE_SYSTEM_PROMPTS[next_phase],
            phase_prompt,
            max_tokens=2048,
        )

        if not phase_result:
            break

        phases_output.append(f"# Phase {phase_num} — {label}\n\n{phase_result}")
        completed_phases.add(next_phase)
        accumulated_findings += f"\n\n[Phase {phase_num} — {label}]:\n{phase_result[:600]}"

        confirmed, next_phase = _parse_phase_status(phase_result)
        if not confirmed and not next_phase:
            next_phase = _keyword_next_phase(phase_result, completed_phases)

        phase_num += 1

    total = len(phases_output)
    console.print(
        f"\n[bold green]Investigation complete — {total} phase(s) run"
        + (" — Root cause confirmed." if confirmed else " — Max depth reached.")
        + "[/bold green]"
    )

    return "\n\n---\n\n".join(phases_output)
