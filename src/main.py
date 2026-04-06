"""
main.py — Orchestrates the IFS RCA Agent pipeline.

Run with:
    python -m src.main
"""

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from src import log_reader, log_parser, claude_analyst, rca_generator, knowledge_base

_DEFAULT_LOGS_DIR = str(Path(__file__).parent.parent / "logs")

console = Console()


def main(
    logs_dir: str = _DEFAULT_LOGS_DIR,
    incident_time: str = "",
    mode: str = "incident",
) -> None:
    # ------------------------------------------------------------------
    # Welcome banner
    # ------------------------------------------------------------------
    mode_label = "Health Check" if mode == "health-check" else "Incident RCA"
    console.print(
        Panel(
            f"[bold white]IFS RCA Agent — {mode_label}[/bold white]\n"
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

    if mode == "health-check":
        # Filter to the last 7 days — gives a rolling week view without noise from old data
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        errors = log_parser.parse_errors(raw_lines, incident_time=now_utc, window_hours=168)
    else:
        errors = log_parser.parse_errors(raw_lines, incident_time=incident_time, window_hours=48)

    if not errors:
        if mode == "health-check":
            console.print(
                "[bold yellow]No known event patterns detected in the logs. "
                "The cluster appears event-free — generating a clean health report.[/bold yellow]"
            )
        else:
            console.print(
                "[bold yellow]No known error patterns detected in the log files. "
                "Exiting without generating a report.[/bold yellow]"
            )
            sys.exit(0)
    else:
        console.print(f"[green]Detected {len(errors)} event(s).[/green]")

    # ------------------------------------------------------------------
    # Step 2b — Extract ticket / kubectl command context
    # ------------------------------------------------------------------
    extra_context = log_parser.extract_context(raw_lines)
    if extra_context:
        found = ", ".join(extra_context.keys())
        console.print(f"[dim]Extra context found: {found}[/dim]")

    # ------------------------------------------------------------------
    # Health-check mode — separate pipeline
    # ------------------------------------------------------------------
    if mode == "health-check":
        console.print("\n[bold]Step 3: Analysing cluster health with Claude...[/bold]")
        analysis = claude_analyst.analyze_health(errors, extra_context=extra_context)

        console.print("\n[bold]Step 4: Generating health report...[/bold]")
        output_path = rca_generator.generate_health_report(errors, analysis, logs_dir=logs_dir)

        console.print(
            Panel(
                f"[bold green]Health report generated successfully![/bold green]\n\n"
                f"[white]Output file:[/white] [cyan]{output_path}[/cyan]",
                expand=False,
                border_style="green",
            )
        )
        return

    # ------------------------------------------------------------------
    # Incident RCA mode (original pipeline)
    # ------------------------------------------------------------------

    # Step 3 — Build user context from CLI argument
    console.print("\n[bold]Step 3: Building incident context...[/bold]")
    user_context = {
        "incident_time":     incident_time,
        "affected_services": "auto-detected from logs",
        "recent_changes":    "None",
        "environment":       "production",
        "additional_notes":  "",
    }
    console.print(f"[dim]Incident time: {incident_time}[/dim]")

    # Step 4 — Analyse with Claude
    console.print("\n[bold]Step 4: Analysing with Claude...[/bold]")
    analysis = claude_analyst.analyze_with_claude(errors, user_context, extra_context=extra_context)

    # Step 5 — Generate RCA document
    console.print("\n[bold]Step 5: Generating RCA document...[/bold]")
    output_path = rca_generator.generate_rca_document(errors, user_context, analysis)

    # Step 6 — Save to knowledge base
    console.print("\n[bold]Step 6: Saving incident to knowledge base...[/bold]")
    knowledge_base.save_incident(errors, user_context, analysis)

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
    parser = argparse.ArgumentParser(
        description="IFS RCA Agent — Incident RCA or proactive Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Investigate a specific incident (requires --incident-time):
  python -m src.main --mode incident --incident-time "2026-03-20 14:00 UTC"

  # Proactive health check — just point at logs, no incident needed:
  python -m src.main --mode health-check --logs-dir ./logs
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["incident", "health-check"],
        default="incident",
        help="'incident' = RCA for a specific outage (default); 'health-check' = proactive cluster health assessment",
    )
    parser.add_argument(
        "--logs-dir",
        default=_DEFAULT_LOGS_DIR,
        help=f"Path to logs directory (default: '{_DEFAULT_LOGS_DIR}')",
    )
    parser.add_argument(
        "--incident-time",
        default="",
        help="Incident start date/time — required for 'incident' mode (e.g. '2026-03-20 14:00 UTC')",
    )
    args = parser.parse_args()

    if args.mode == "incident" and not args.incident_time:
        parser.error("--incident-time is required when --mode is 'incident'")

    main(logs_dir=args.logs_dir, incident_time=args.incident_time, mode=args.mode)
