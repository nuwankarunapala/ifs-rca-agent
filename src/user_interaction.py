"""
user_interaction.py — Gather incident context from the user via CLI prompts.
"""

from typing import Dict

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.rule import Rule

console = Console()


def gather_user_context() -> Dict[str, str]:
    """
    Ask the user a series of questions about the incident.

    Returns:
        Dict with keys: incident_time, affected_services, recent_changes,
        environment, additional_notes.
    """
    console.print()
    console.print(Rule("[bold cyan]Incident Context[/bold cyan]"))
    console.print(
        "[dim]Please answer the following questions to provide context for the RCA.[/dim]\n"
    )

    incident_time = Prompt.ask(
        "[bold]1. When did the incident start?[/bold] (e.g. 2026-03-20 14:00 UTC)"
    )

    affected_services = Prompt.ask(
        "[bold]2. Which services were affected?[/bold] (e.g. ifs-app, ifs-db)"
    )

    recent_changes = Prompt.ask(
        "[bold]3. Any recent deployments or changes?[/bold]",
        default="None",
    )

    environment = Prompt.ask(
        "[bold]4. Environment[/bold]",
        choices=["production", "staging", "dev"],
        default="production",
    )

    additional_notes = ""
    wants_notes = Confirm.ask(
        "\n[bold]5. Would you like to add any additional notes?[/bold]",
        default=False,
    )
    if wants_notes:
        additional_notes = Prompt.ask("   Please enter your notes")

    console.print()

    return {
        "incident_time": incident_time,
        "affected_services": affected_services,
        "recent_changes": recent_changes,
        "environment": environment,
        "additional_notes": additional_notes,
    }
