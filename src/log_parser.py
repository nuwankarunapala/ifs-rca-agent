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
from datetime import datetime, timezone, timedelta
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
# IFS pod whitelist — only errors from these pods are kept.
# Pod names in logs carry a random suffix (e.g. ifsapp-odata-7d9f4b-xk2p),
# so we match by prefix/substring.
# ---------------------------------------------------------------------------

IFS_POD_PREFIXES = (
    "ifs-db-init",
    "ifsapp-application-svc",
    "ifsapp-client-notification",
    "ifsapp-client-services",
    "ifsapp-client",
    "ifsapp-connect",
    "ifsapp-docman-esign",
    "ifsapp-doc",
    "ifsapp-iam",
    "ifsapp-odata",
    "ifsapp-proxy",
    "ifsapp-reporting-br",
    "ifsapp-reporting-cr",
    "ifsapp-reporting-ren",
    "ifsapp-reporting",
)


def _strip_linkerd_suffix(name: str) -> str:
    """Remove Linkerd container suffixes so the underlying IFS pod name is exposed."""
    for suffix in ("-linkerd-proxy", "-linkerd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _is_ifs_pod(pod_name: str, source_file: str = "", file_type: str = "") -> bool:
    """
    Return True if the pod belongs to the IFS whitelist.

    For Linkerd sidecar logs the extracted pod_name may be just 'linkerd-proxy'
    (the container name) rather than the IFS pod name, so we also fall back to
    checking the source file path which carries the full pod name.
    """
    if pod_name == "unknown":
        return True

    # Strip Linkerd container suffix before matching
    cleaned = _strip_linkerd_suffix(pod_name.lower())
    if any(cleaned.startswith(p) or p in cleaned for p in IFS_POD_PREFIXES):
        return True

    # Fallback for linkerd_log: check the file path (contains full pod name)
    if file_type == "linkerd_log" and source_file:
        sf = source_file.lower()
        if any(p in sf for p in IFS_POD_PREFIXES):
            return True

    return False


# ---------------------------------------------------------------------------
# Error patterns  (order matters — first match wins per line)
# ---------------------------------------------------------------------------

PATTERNS: Dict[str, re.Pattern] = {
    # -----------------------------------------------------------------------
    # IFS Scheduling Gates (highest priority — pod never starts)
    # -----------------------------------------------------------------------
    "Gate1_ResourceQuota":        re.compile(
        r"exceeded quota|resource quota|quota.*exceeded|ResourceQuota|"
        r"forbidden.*exceeded.*quota",
        re.IGNORECASE),
    "Gate2_LimitRange":           re.compile(
        r"LimitRange|limit range.*violation|"
        r"maximum (cpu|memory).*exceed|pods.*forbidden.*limit",
        re.IGNORECASE),
    "Gate3_InsufficientResources":re.compile(
        r"Insufficient (cpu|memory)|0/\d+ nodes.*available|"
        r"no nodes are available|insufficient resources|"
        r"nodes.*had.*insufficient",
        re.IGNORECASE),
    "Gate4_TaintToleration":      re.compile(
        r"taint.*NoSchedule|had taint.*NoSchedule|"
        r"didn.t tolerate|untolerated taint|"
        r"nodes.*taint.*not tolerated",
        re.IGNORECASE),
    "Gate5_AffinityMismatch":     re.compile(
        r"didn.t match.*node selector|nodeAffinity|"
        r"MatchNodeSelector|node\(s\) didn.t match|"
        r"node selector.*not match",
        re.IGNORECASE),
    "Gate6_AntiAffinity":         re.compile(
        r"anti.affinity|AntiAffinity|pod anti affinity|"
        r"didn.t match.*anti.affinity",
        re.IGNORECASE),
    "Gate7_PVCPending":           re.compile(
        r"PVC.*Pending|persistentvolumeclaim.*pending|"
        r"unbound.*PVC|no persistent volumes available|"
        r"waiting.*volume.*bind",
        re.IGNORECASE),
    "Gate8_TopologySpread":       re.compile(
        r"topology spread|topologySpreadConstraints|"
        r"ErrTopologySpread|maxSkew",
        re.IGNORECASE),
    # -----------------------------------------------------------------------
    # Kubernetes lifecycle
    # -----------------------------------------------------------------------
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
# Time window helpers
# ---------------------------------------------------------------------------

_TS_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


def _parse_dt(ts: str) -> Optional[datetime]:
    """Parse a timestamp string to an aware UTC datetime, or None."""
    if not ts or ts == "unknown":
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Syslog format (no year): "Mar 20 14:03:22"
    try:
        dt = datetime.strptime(ts, "%b %d %H:%M:%S")
        return dt.replace(year=datetime.now(timezone.utc).year, tzinfo=timezone.utc)
    except ValueError:
        pass
    # Epoch (ms or s)
    try:
        epoch = int(ts)
        if epoch > 1e10:
            epoch /= 1000
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OSError):
        pass
    return None


def _parse_incident_dt(incident_time: str) -> Optional[datetime]:
    """Parse the user-supplied incident time string to UTC datetime."""
    clean = incident_time.upper().replace("UTC", "").replace("Z", "").strip()
    for fmt in _TS_FORMATS + ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"]:
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


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

def extract_context(raw_lines: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Extract non-log contextual content from specially named files:

        ticket / incident files  → free-text incident communications
        kubectl-events files     → cluster event output
        kubectl-top files        → resource usage snapshot (node & pod utilisation)
        kubectl-get files        → pod/resource listing (requests, limits, PVCs, HPAs, pods)
        kubectl-describe files   → full describe output (nodes, HPAs, scheduling constraints)

    Returns a dict with keys: ticket, kubectl_events, kubectl_top, kubectl_get, kubectl_describe.
    Each value is the concatenated text content (capped at 4000 chars each).
    Only populated keys are included.
    """
    buckets: Dict[str, list] = {
        "ticket":           [],
        "kubectl_events":   [],
        "kubectl_top":      [],
        "kubectl_get":      [],
        "kubectl_describe": [],
        "health_report":    [],   # full combined output from health_info.ps1
    }

    for entry in raw_lines:
        ft = entry.get("file_type", "")
        if ft in buckets:
            buckets[ft].append(entry.get("line", ""))

    result: Dict[str, str] = {}
    for key, lines in buckets.items():
        if lines:
            # health_report is a large combined file — allow a bigger slice
            cap = 12000 if key == "health_report" else 4000
            result[key] = "\n".join(lines)[:cap]

    return result


def parse_errors(
    raw_lines: List[Dict[str, str]],
    incident_time: str = "",
    window_hours: int = 48,
) -> List[LogError]:
    """
    Parse a list of raw log line dicts into LogError objects.

    Args:
        raw_lines:      Output from log_reader.read_logs()
        incident_time:  Incident start time string (e.g. "2026-03-20 14:00 UTC").
                        When provided, only errors within `window_hours` before
                        (and 2 hours after) this time are kept.
        window_hours:   Look-back window in hours (default 48).

    Returns:
        List of LogError (one per matched line, first pattern wins).
    """
    # Build time window bounds
    incident_dt = _parse_incident_dt(incident_time) if incident_time else None
    window_start = incident_dt - timedelta(hours=window_hours) if incident_dt else None
    window_end   = incident_dt + timedelta(hours=2)            if incident_dt else None

    errors: List[LogError] = []
    skipped_by_time = 0

    # Per-file describe state (pod/ns persist across lines in a describe file)
    describe_states: Dict[str, _DescribeState] = {}

    for entry in raw_lines:
        line      = entry.get("line", "")
        source    = entry.get("source_file", "unknown")
        file_type = entry.get("file_type", "other")

        # Update running pod/ns state for describe files
        if file_type == "kubectl_describe":
            if source not in describe_states:
                describe_states[source] = _DescribeState()
            describe_states[source].update(line)

        for error_type, pattern in PATTERNS.items():
            if not pattern.search(line):
                continue

            ts_str = _extract_timestamp(line)

            # Time-window filter (skip only when we can parse the timestamp)
            if window_start and ts_str != "unknown":
                line_dt = _parse_dt(ts_str)
                if line_dt and not (window_start <= line_dt <= window_end):
                    skipped_by_time += 1
                    break

            # Derive pod + namespace
            if file_type == "kubectl_describe" and source in describe_states:
                st = describe_states[source]
                pod_name  = st.pod
                namespace = st.ns
                inline_pod, inline_ns = _extract_pod_ns(line, file_type, source)
                if inline_pod != "unknown":
                    pod_name = inline_pod
                if inline_ns != "unknown":
                    namespace = inline_ns
            else:
                pod_name, namespace = _extract_pod_ns(line, file_type, source)

            # Drop errors from pods outside the IFS whitelist
            if not _is_ifs_pod(pod_name, source_file=source, file_type=file_type):
                break

            # Store clean pod name (strip Linkerd container suffix)
            clean_pod = _strip_linkerd_suffix(pod_name)

            errors.append(LogError(
                timestamp=ts_str,
                source_file=source,
                file_type=file_type,
                error_type=error_type,
                message=line[:300],
                pod_name=clean_pod,
                namespace=namespace,
                severity=_extract_severity(line),
            ))
            break  # one error type per line

    if skipped_by_time:
        from rich.console import Console as _C
        _C().print(
            f"[dim]Time-window filter: skipped {skipped_by_time} error(s) "
            f"outside the {window_hours}h window.[/dim]"
        )

    return errors
