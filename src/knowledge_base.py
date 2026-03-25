"""
knowledge_base.py — Local incident memory for the IFS RCA Agent.

Each completed RCA run is saved as a record. On the next run, similar past
incidents are retrieved (Jaccard similarity on error-type + pod-name pairs)
and injected into Claude's prompt so it can correlate patterns immediately.

Storage: knowledge/incidents.json  (grows over time, human-readable)
"""

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console

from src.log_parser import LogError

console = Console()

_KB_PATH = Path(__file__).parent.parent / "knowledge" / "incidents.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class IncidentRecord:
    id: str
    saved_at: str                    # ISO UTC datetime
    incident_time: str               # from user context
    environment: str
    error_types: List[str]           # unique error types seen
    gate_hits: List[str]             # scheduling Gates detected
    affected_pods: List[str]         # unique pod names
    error_signature: List[str]       # "ErrorType::pod_name" pairs (for similarity)
    root_cause_summary: str          # first 600 chars of Claude's analysis
    recommendations: List[str]       # up to 5 bullet points extracted from analysis
    total_errors: int


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_raw() -> List[Dict]:
    if not _KB_PATH.exists():
        return []
    try:
        with _KB_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(records: List[Dict]) -> None:
    _KB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _KB_PATH.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)


def load_all() -> List[IncidentRecord]:
    return [IncidentRecord(**r) for r in _load_raw()]


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def find_similar(
    errors: List[LogError],
    top_n: int = 3,
    min_score: float = 0.25,
) -> List[tuple]:  # List of (score, IncidentRecord)
    """
    Return up to `top_n` past incidents whose error signature is similar
    to the current run's signature (Jaccard ≥ min_score).
    """
    current_sig = _build_signature(errors)
    if not current_sig:
        return []

    results = []
    for rec in load_all():
        score = _jaccard(current_sig, rec.error_signature)
        if score >= min_score:
            results.append((score, rec))

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:top_n]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def _build_signature(errors: List[LogError]) -> List[str]:
    """Unique "ErrorType::pod" pairs — the fingerprint of this incident."""
    return list({f"{e.error_type}::{e.pod_name}" for e in errors})


def _extract_recommendations(analysis: str) -> List[str]:
    """Pull up to 5 lines that look like recommendations."""
    recs = []
    for line in analysis.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Lines starting with a number, dash, or bullet inside recommendation sections
        if any(stripped.startswith(p) for p in ("1.", "2.", "3.", "4.", "5.",
                                                  "- ", "* ", "• ")):
            recs.append(stripped.lstrip("0123456789.-*• ").strip())
        if len(recs) >= 5:
            break
    return recs


def save_incident(
    errors: List[LogError],
    user_context: Dict,
    analysis: str,
) -> None:
    """Append a new incident record to the knowledge base."""
    gate_hits = sorted({e.error_type for e in errors if e.error_type.startswith("Gate")})
    error_types = sorted({e.error_type for e in errors})
    affected_pods = sorted({e.pod_name for e in errors if e.pod_name != "unknown"})

    record = IncidentRecord(
        id=str(uuid.uuid4())[:8],
        saved_at=datetime.now(timezone.utc).isoformat(),
        incident_time=user_context.get("incident_time", ""),
        environment=user_context.get("environment", "production"),
        error_types=error_types,
        gate_hits=gate_hits,
        affected_pods=affected_pods,
        error_signature=_build_signature(errors),
        root_cause_summary=analysis[:600],
        recommendations=_extract_recommendations(analysis),
        total_errors=len(errors),
    )

    records = _load_raw()
    records.append(asdict(record))
    _save_raw(records)

    console.print(
        f"[dim]Knowledge base updated — {len(records)} incident(s) stored.[/dim]"
    )


# ---------------------------------------------------------------------------
# Format for prompt injection
# ---------------------------------------------------------------------------

def format_for_prompt(similar: List[tuple]) -> str:
    """
    Render similar past incidents as a compact text block for Claude's prompt.
    """
    if not similar:
        return ""

    lines = ["SIMILAR PAST INCIDENTS (from knowledge base — use for pattern correlation):"]
    for rank, (score, rec) in enumerate(similar, 1):
        lines.append(
            f"\n--- Past Incident #{rank} "
            f"(similarity {score:.0%}, recorded {rec.saved_at[:10]}) ---"
        )
        lines.append(f"Incident time : {rec.incident_time}")
        lines.append(f"Environment   : {rec.environment}")
        lines.append(f"Error types   : {', '.join(rec.error_types)}")
        if rec.gate_hits:
            lines.append(f"Gates hit     : {', '.join(rec.gate_hits)}")
        lines.append(f"Affected pods : {', '.join(rec.affected_pods) or 'unknown'}")
        lines.append(f"Root cause    : {rec.root_cause_summary[:300]}")
        if rec.recommendations:
            lines.append("Recommendations from that incident:")
            for r in rec.recommendations:
                lines.append(f"  - {r}")

    return "\n".join(lines)
