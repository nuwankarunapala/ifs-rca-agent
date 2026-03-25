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


def analyze_with_claude(errors: List[LogError], user_context: Dict[str, str]) -> str:
    """
    Send errors and context to Claude Opus 4.6 and return the structured RCA text.

    Args:
        errors:       List of LogError objects from the parser.
        user_context: Dict from user_interaction.gather_user_context().

    Returns:
        Claude's RCA as a Markdown string with the 9-section structure.
    """
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

    # Error type priority — lower = more important
    _PRIORITY = {
        # Scheduling gates (pod never starts — investigate first)
        "Gate1_ResourceQuota": 1, "Gate2_LimitRange": 1,
        "Gate3_InsufficientResources": 1, "Gate4_TaintToleration": 1,
        "Gate5_AffinityMismatch": 1, "Gate6_AntiAffinity": 1,
        "Gate7_PVCPending": 1, "Gate8_TopologySpread": 1,
        # Critical runtime failures
        "CrashLoopBackOff": 2, "OOMKilled": 2, "NodeNotReady": 2, "Evicted": 2,
        # Probe / image / connectivity
        "ImagePullError": 3, "LivenessFail": 3, "ReadinessFail": 3, "ConnectionError": 3,
        # Lower signal
        "PodRestart": 4, "Exception": 4, "BackOff": 4,
        "IFSError": 4, "LinkerdError": 4, "IngressError": 4, "ScalingError": 4,
    }
    _SEV_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}

    # Pod priority — IFS critical services first
    def _pod_score(pod: str) -> int:
        p = pod.lower()
        if any(k in p for k in ("odata",)):
            return 0
        if any(k in p for k in ("iam",)):
            return 1
        if any(k in p for k in ("client",)):
            return 2
        if any(k in p for k in ("report", "crystal")):
            return 3
        return 4

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

    # Step 2: fill remaining slots (up to 50) with highest-priority unseen (pod, type) pairs
    _CAP = 50
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

    console.print("\n[bold cyan]Sending data to Claude Opus 4.6 for RCA analysis...[/bold cyan]")

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_text}],
    )

    for block in response.content:
        if block.type == "text":
            return block.text

    return "No analysis returned."
