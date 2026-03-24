"""
main.py — Orchestrates the IFS RCA Agent pipeline.

Run with:
    python -m src.main
"""

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from src import log_reader, log_parser, claude_analyst, rca_generator

_DEFAULT_LOGS_DIR = str(Path(__file__).parent.parent / "logs")

console = Console()


def main(logs_dir: str = _DEFAULT_LOGS_DIR, incident_time: str = "") -> None:
    # ------------------------------------------------------------------
    # Welcome banner
    # ------------------------------------------------------------------
    console.print(
        Panel(
            "[bold white]IFS RCA Agent[/bold white]\n"
            "[dim]Powered by Claude Opus 4.6[/dim]",
            expand=False,
            border_style="cyan",
        )
    )

    # ------------------------------------------------------------------
    # Step 1 — Read logs
    # ------------------------------------------------------------------
    console.print("\n[bold]Step 1: Reading log files...[/bold]")
    raw_lines = log_reader.read_logs(logs_dir)

    if not raw_lines:
        console.print(
            "[bold red]No log files found in the 'logs/' directory. "
            "Please add log files and try again.[/bold red]"
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2 — Parse errors
    # ------------------------------------------------------------------
    console.print("\n[bold]Step 2: Parsing errors from logs...[/bold]")
    errors = log_parser.parse_errors(raw_lines)

    if not errors:
        console.print(
            "[bold yellow]No known error patterns detected in the log files. "
            "Exiting without generating a report.[/bold yellow]"
        )
        sys.exit(0)

    console.print(f"[green]Detected {len(errors)} error event(s).[/green]")

    # ------------------------------------------------------------------
    # Step 3 — Build user context from CLI argument
    # ------------------------------------------------------------------
    console.print("\n[bold]Step 3: Building incident context...[/bold]")
    user_context = {
        "incident_time":     incident_time,
        "affected_services": "auto-detected from logs",
        "recent_changes":    "None",
        "environment":       "production",
        "additional_notes":  "",
    }
    console.print(f"[dim]Incident time: {incident_time}[/dim]")

    # ------------------------------------------------------------------
    # Step 4 — Analyse with Claude
    # ------------------------------------------------------------------
    console.print("\n[bold]Step 4: Analysing with Claude...[/bold]")
    analysis = claude_analyst.analyze_with_claude(errors, user_context)

    # ------------------------------------------------------------------
    # Step 5 — Generate RCA document
    # ------------------------------------------------------------------
    console.print("\n[bold]Step 5: Generating RCA document...[/bold]")
    output_path = rca_generator.generate_rca_document(errors, user_context, analysis)

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    console.print(
        Panel(
            f"[bold green]RCA report generated successfully![/bold green]\n\n"
            f"[white]Output file:[/white] [cyan]{output_path}[/cyan]",
            expand=False,
            border_style="green",
        )
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="IFS RCA Agent")
    parser.add_argument(
        "--logs-dir",
        default=_DEFAULT_LOGS_DIR,
        help=f"Path to logs directory (default: '{_DEFAULT_LOGS_DIR}')",
    )
    parser.add_argument(
        "--incident-time",
        required=True,
        help="Incident start date/time (e.g. '2026-03-20 14:00 UTC')",
    )
    args = parser.parse_args()
    main(logs_dir=args.logs_dir, incident_time=args.incident_time)
