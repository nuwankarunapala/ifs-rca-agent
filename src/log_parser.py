"""
log_parser.py — Parses raw log lines into structured LogError objects.

Handles two distinct log formats:
  1. container_log / linkerd_log  — plain kubectl logs output
  2. kubectl_describe             — kubectl describe pod/deployment/job output

The file_type hint (from log_reader) is used to choose the right extraction
strategy for pod name, namespace, and severity.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LogError:
    timestamp: str
    source_file: str        # relative path, e.g. "IFS_Cloud/pods/logs/ifs-app.log"
    file_type: str          # container_log | linkerd_log | kubectl_describe | other
    error_type: str
    message: str            # capped at 300 chars
    pod_name: str
    namespace: str
    severity: str


# ---------------------------------------------------------------------------
# Error patterns  (order matters — first match wins per line)
# ---------------------------------------------------------------------------

PATTERNS: Dict[str, re.Pattern] = {
    # Kubernetes lifecycle
    "CrashLoopBackOff":  re.compile(r"CrashLoopBackOff", re.IGNORECASE),
    "OOMKilled":         re.compile(r"OOMKilled|Out of memory|memory limit exceeded|Reason:\s+OOMKilled", re.IGNORECASE),
    "ImagePullError":    re.compile(r"ImagePullBackOff|ErrImagePull|Failed to pull image|Back-off pulling image", re.IGNORECASE),
    "NodeNotReady":      re.compile(r"NodeNotReady|node.*not ready|node condition.*False|KubeletNotReady", re.IGNORECASE),
    "Evicted":           re.compile(r"\bEvicted\b|eviction threshold|low on resource", re.IGNORECASE),
    "LivenessFail":      re.compile(r"Liveness probe failed|liveness.*probe.*fail", re.IGNORECASE),
    "ReadinessFail":     re.compile(r"Readiness probe failed|readiness.*probe.*fail", re.IGNORECASE),
    # Application errors
    "PodRestart":        re.compile(r"Restarting container|restart count|Back-off restarting|restarted \d+ time", re.IGNORECASE),
    "ConnectionError":   re.compile(r"Connection refused|connection reset|ECONNREFUSED|dial tcp.*refused|FATAL.*starting up", re.IGNORECASE),
    "Exception":         re.compile(r"\bException\b|Traceback|panic:|fatal error|NullPointerException|OutOfMemoryError", re.IGNORECASE),
    "BackOff":           re.compile(r"Back-off|backoff", re.IGNORECASE),
    # IFS / application specific
    "IFSError":          re.compile(r"IFS.*error|ifs.*exception|oracle.*error|ORA-\d+", re.IGNORECASE),
    # Linkerd / service mesh
    "LinkerdError":      re.compile(r"linkerd.*error|proxy.*error|inbound.*connection.*reset|outbound.*refused", re.IGNORECASE),
    # Ingress / autoscaler
    "IngressError":      re.compile(r"upstream.*timed out|upstream.*unavailable|502 Bad Gateway|503 Service", re.IGNORECASE),
    "ScalingError":      re.compile(r"failed to scale|unable to scale|HPA.*error|autoscaler.*failed", re.IGNORECASE),
}


# ---------------------------------------------------------------------------
# Timestamp extraction
# ---------------------------------------------------------------------------

_TIMESTAMP_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"),       # 2026-03-20T14:03:22Z
    re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?"),          # 2026-03-20 14:03:22
    re.compile(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"),            # Mar 20 14:03:22
    re.compile(r"\d{10,13}"),                                                  # epoch ms
]

def _extract_timestamp(line: str) -> str:
    for pat in _TIMESTAMP_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group(0)
    return "unknown"


# ---------------------------------------------------------------------------
# Severity extraction
# ---------------------------------------------------------------------------

_SEVERITY_RE = re.compile(
    r"\b(error|err|warn(?:ing)?|fatal|crit(?:ical)?|info|Warning|Normal)\b",
    re.IGNORECASE,
)
_SEVERITY_MAP = {
    "error": "ERROR",   "err": "ERROR",
    "warn": "WARNING",  "warning": "WARNING",
    "fatal": "CRITICAL", "crit": "CRITICAL", "critical": "CRITICAL",
    "info": "INFO",
    "normal": "INFO",
}

def _extract_severity(line: str) -> str:
    m = _SEVERITY_RE.search(line)
    if m:
        return _SEVERITY_MAP.get(m.group(1).lower(), "INFO")
    return "INFO"


# ---------------------------------------------------------------------------
# Pod / namespace extraction — two strategies
# ---------------------------------------------------------------------------

# Strategy A: container logs  "namespace/ifs-production pod/ifs-app-xxx"
_POD_LOG_RE  = re.compile(r"pod[/\s\"']+([a-zA-Z0-9_.\-]+)", re.IGNORECASE)
_NS_LOG_RE   = re.compile(r"namespace[/\s\"']+([a-zA-Z0-9_\-]+)", re.IGNORECASE)

# Strategy B: kubectl describe  "Name:  ifs-app-7d9f4b-xk2p" / "Namespace: ifs-production"
_POD_DESC_RE = re.compile(r"^Name:\s+([a-zA-Z0-9_.\-]+)", re.IGNORECASE)
_NS_DESC_RE  = re.compile(r"^Namespace:\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)

# Fallback: extract from the file path itself  e.g. "IFS_Cloud/pods/logs/ifs-app-xxx.log"
_POD_PATH_RE = re.compile(r"(?:pods|logs|linkerd_logs)/([a-zA-Z0-9_.\-]+)(?:\.log|\.txt|\.gz)?$", re.IGNORECASE)
_NS_PATH_RE  = re.compile(r"namespace[_\-]([a-zA-Z0-9_\-]+)", re.IGNORECASE)


def _extract_pod_ns(line: str, file_type: str, source_file: str) -> Tuple[str, str]:
    """
    Return (pod_name, namespace) using the most appropriate strategy.
    Falls back through log-line → file-path → 'unknown'.
    """
    if file_type == "kubectl_describe":
        pod = (_match(line, _POD_DESC_RE) or
               _match(line, _POD_LOG_RE)  or
               _match(source_file, _POD_PATH_RE) or "unknown")
        ns  = (_match(line, _NS_DESC_RE)  or
               _match(line, _NS_LOG_RE)   or
               _match(source_file, _NS_PATH_RE) or "unknown")
    else:
        pod = (_match(line, _POD_LOG_RE)  or
               _match(source_file, _POD_PATH_RE) or "unknown")
        ns  = (_match(line, _NS_LOG_RE)   or
               _match(source_file, _NS_PATH_RE) or "unknown")
    return pod, ns


def _match(text: str, pattern: re.Pattern) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# State carried across lines for kubectl describe blocks
# ---------------------------------------------------------------------------

class _DescribeState:
    """Tracks the current pod/namespace while walking a describe file."""
    def __init__(self):
        self.pod: str = "unknown"
        self.ns:  str = "unknown"

    def update(self, line: str) -> None:
        m = _POD_DESC_RE.match(line)
        if m:
            self.pod = m.group(1)
            return
        m = _NS_DESC_RE.match(line)
        if m:
            self.ns = m.group(1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_errors(raw_lines: List[Dict[str, str]]) -> List[LogError]:
    """
    Parse a list of raw log line dicts into LogError objects.

    Args:
        raw_lines: Output from log_reader.read_logs()
                   Each dict has keys: source_file, line, file_type.

    Returns:
        List of LogError (one per matched line, first pattern wins).
    """
    errors: List[LogError] = []

    # Per-file describe state (pod/ns persist across lines in a describe file)
    describe_states: Dict[str, _DescribeState] = {}

    for entry in raw_lines:
        line        = entry.get("line", "")
        source      = entry.get("source_file", "unknown")
        file_type   = entry.get("file_type", "other")

        # Update running pod/ns state for describe files
        if file_type == "kubectl_describe":
            if source not in describe_states:
                describe_states[source] = _DescribeState()
            describe_states[source].update(line)

        for error_type, pattern in PATTERNS.items():
            if not pattern.search(line):
                continue

            # Derive pod + namespace
            if file_type == "kubectl_describe" and source in describe_states:
                st = describe_states[source]
                pod_name  = st.pod
                namespace = st.ns
                # Override if the line itself has explicit values
                inline_pod, inline_ns = _extract_pod_ns(line, file_type, source)
                if inline_pod != "unknown":
                    pod_name = inline_pod
                if inline_ns != "unknown":
                    namespace = inline_ns
            else:
                pod_name, namespace = _extract_pod_ns(line, file_type, source)

            errors.append(LogError(
                timestamp=_extract_timestamp(line),
                source_file=source,
                file_type=file_type,
                error_type=error_type,
                message=line[:300],
                pod_name=pod_name,
                namespace=namespace,
                severity=_extract_severity(line),
            ))
            break  # one error type per line

    return errors
