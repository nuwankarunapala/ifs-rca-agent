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


def analyze_with_claude(errors: List[LogError], user_context: Dict[str, str]) -> str:
    """
    Send errors and context to Claude Opus 4.6 and return the structured RCA text.

    Args:
        errors:       List of LogError objects from the parser.
        user_context: Dict from user_interaction.gather_user_context().

    Returns:
        Claude's RCA as a Markdown string with the 9-section structure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.example to .env and add your key."
        )

    client = anthropic.Anthropic(api_key=api_key)

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
        for e in errors
    ]

    payload = {
        "total_errors_found":    len(errors),
        "errors_by_type":        dict(Counter(e.error_type for e in errors)),
        "errors_by_source_type": dict(Counter(e.file_type  for e in errors)),
        "user_context":          user_context,
        "errors":                error_list,
    }

    prompt_text = (
        "Please produce a full Root Cause Analysis report following the 9-section "
        "structure defined in the system prompt.\n\n"
        "Source type key: container_log=kubectl-logs output; "
        "linkerd_log=Linkerd sidecar proxy; kubectl_describe=kubectl describe output; "
        "other=miscellaneous.\n\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )

    console.print("\n[bold cyan]Sending data to Claude Opus 4.6 for RCA analysis...[/bold cyan]")

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_text}],
    )

    for block in response.content:
        if block.type == "text":
            return block.text

    return "No analysis returned."
