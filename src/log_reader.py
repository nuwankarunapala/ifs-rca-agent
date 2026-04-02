"""
log_reader.py — Reads all log/description files from the structured logs directory.

Expected input layout (any depth):
    logs/
    ├── ifs-autoscaler/
    │   ├── ifs-autoscaler_configs/   ← SKIPPED (config, not logs)
    │   └── logs/
    ├── ifs-ingress/
    │   ├── ifs-ingress_configs/      ← SKIPPED
    │   └── logs/
    └── IFS_Cloud/
        ├── deployments/
        │   └── descriptions/         ← kubectl describe deployment output
        ├── jobs/
        │   └── descriptions/         ← kubectl describe job output
        └── pods/
            ├── descriptions/         ← kubectl describe pod output
            ├── linkerd_logs/         ← Linkerd sidecar container logs
            └── logs/                 ← main container logs

Each returned dict has:
    source_file : relative path from logs_dir root  (e.g. "IFS_Cloud/pods/logs/ifs-app.log")
    line        : a single non-empty line of text
    file_type   : one of  "container_log" | "linkerd_log" | "kubectl_describe" | "other"
"""

import gzip
from pathlib import Path
from typing import List, Dict

from rich.console import Console
from rich.table import Table

console = Console()

# Directories whose names end with _configs are skipped entirely.
_SKIP_SUFFIX = "_configs"

# Supported file extensions
_SUPPORTED_EXT = {".log", ".txt", ".gz"}


def _classify(relative_parts: tuple) -> str:
    """
    Derive a file_type string from the relative path parts.

    Filename-prefix convention (takes priority — works from any subdirectory):
        ticket-*          →  ticket          (Jira/ServiceNow/Slack/email thread)
        kubectl-events-*  →  kubectl_events  (kubectl get events -n <ns>)
        kubectl-top-*     →  kubectl_top     (kubectl top pods / nodes)
        kubectl-get-*     →  kubectl_get     (kubectl get pods -o wide, etc.)
        kubectl-describe-*→  kubectl_describe (kubectl describe pod/deploy)

    Directory-based convention (fallback):
        …/linkerd_logs/…  →  linkerd_log
        …/descriptions/…  →  kubectl_describe
        …/logs/…          →  container_log
    """
    filename = relative_parts[-1].lower() if relative_parts else ""
    parts_lower = [p.lower() for p in relative_parts]

    # Filename-prefix detection (highest priority)
    if filename.startswith("ticket-") or filename.startswith("incident-"):
        return "ticket"
    if filename.startswith("kubectl-events"):
        return "kubectl_events"
    if filename.startswith("kubectl-top"):
        return "kubectl_top"
    if filename.startswith("kubectl-get"):
        return "kubectl_get"
    if filename.startswith("kubectl-describe"):
        return "kubectl_describe"

    # Directory-based detection (fallback)
    if "linkerd_logs" in parts_lower:
        return "linkerd_log"
    if "descriptions" in parts_lower:
        return "kubectl_describe"
    if "logs" in parts_lower:
        return "container_log"
    return "other"


def _should_skip(path: Path, logs_root: Path) -> bool:
    """Return True if any directory component in the path ends with _configs."""
    try:
        relative = path.relative_to(logs_root)
    except ValueError:
        return False
    return any(part.endswith(_SKIP_SUFFIX) for part in relative.parts)


def read_logs(logs_dir: str) -> List[Dict[str, str]]:
    """
    Recursively read all supported log/description files.

    Args:
        logs_dir: Path to the top-level logs directory.

    Returns:
        List of dicts with keys: source_file, line, file_type.
    """
    logs_path = Path(logs_dir)

    if not logs_path.exists():
        console.print(f"[bold red]Logs directory not found:[/bold red] {logs_path.resolve()}")
        return []

    all_files = [
        f for f in logs_path.rglob("*")
        if f.is_file()
        and f.suffix.lower() in _SUPPORTED_EXT
        and not _should_skip(f, logs_path)
    ]

    if not all_files:
        console.print(f"[bold yellow]No readable log files found in:[/bold yellow] {logs_path}")
        return []

    # Summary table
    console.print(f"\n[bold cyan]Found {len(all_files)} file(s) to process[/bold cyan]\n")

    summary: Dict[str, int] = {}   # file_type → file count
    raw_lines: List[Dict[str, str]] = []

    for file_path in sorted(all_files):
        relative = file_path.relative_to(logs_path)
        file_type = _classify(relative.parts)
        summary[file_type] = summary.get(file_type, 0) + 1

        try:
            lines = (
                _read_gz_file(file_path)
                if file_path.suffix.lower() == ".gz"
                else _read_text_file(file_path)
            )

            file_lines = 0
            for line in lines:
                stripped = line.rstrip("\n")
                if stripped.strip():           # skip blank lines
                    raw_lines.append({
                        "source_file": str(relative),
                        "line": stripped,
                        "file_type": file_type,
                    })
                    file_lines += 1

            console.print(
                f"  [green]✓[/green] [dim]{relative}[/dim]"
                f" [yellow]({file_type})[/yellow]"
                f" — {file_lines} lines"
            )

        except Exception as exc:
            console.print(
                f"  [yellow]⚠ Could not read '{relative}': {exc}[/yellow]"
            )

    # Print breakdown
    _print_summary(summary, len(raw_lines))
    return raw_lines


def _print_summary(summary: Dict[str, int], total_lines: int) -> None:
    tbl = Table(title="File Breakdown", show_header=True, header_style="bold magenta")
    tbl.add_column("File Type", style="cyan")
    tbl.add_column("Files", justify="right")
    for ft, count in sorted(summary.items()):
        tbl.add_row(ft, str(count))
    console.print(tbl)
    console.print(f"[bold green]Total lines read:[/bold green] {total_lines}\n")


def _read_text_file(file_path: Path) -> List[str]:
    with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
        return fh.readlines()


def _read_gz_file(file_path: Path) -> List[str]:
    with gzip.open(file_path, "rt", encoding="utf-8", errors="ignore") as fh:
        return fh.readlines()
