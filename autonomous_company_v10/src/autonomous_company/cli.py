from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.panel import Panel

from .config import init_settings, Settings
from .telemetry import configure_logging, configure_tracing, get_cost_collector
from .factory import mission_context

app = typer.Typer(name="autonomous-company", help="Run autonomous multi-agent missions powered by Claude.", add_completion=False)
console = Console()
err_console = Console(stderr=True)


@app.command()
def run(
    mission: str = typer.Argument(..., help="The mission to execute"),
    name: str = typer.Option(None, "--name", "-n"),
    budget: float = typer.Option(5.0, "--budget", "-b"),
    max_roles: int = typer.Option(10, "--max-roles"),
    company_id: str = typer.Option(None, "--company-id", "-c"),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    env_file: Path = typer.Option(Path(".env"), "--env-file"),
) -> None:
    """Run a mission end-to-end."""
    settings = init_settings()
    configure_logging(settings)
    configure_tracing(settings)
    console.print(Panel.fit(
        f"[bold blue]autonomous-company v10[/bold blue]\nMission: {mission[:80]}{'...' if len(mission) > 80 else ''}\nBudget: ${budget:.2f} | Max roles: {max_roles}",
        title="[bold]Starting Mission[/bold]",
    ))

    async def _run() -> str:
        async with mission_context(mission=mission, settings=settings, name=name, total_budget_usd=budget, max_roles=max_roles, company_id=company_id) as (storage, bus, ceo):
            plan = await ceo.plan()
            console.print(f"\n[bold green]Plan created:[/bold green] {len(plan.roles)} roles, {len(plan.steps)} steps")
            for role in plan.roles:
                console.print(f"  - {role.role_name} ({role.model}, ${role.budget_cap_usd:.2f})")
            return await ceo.orchestrate(plan)

    try:
        result = asyncio.run(_run())
        console.print("\n")
        console.print(Panel(result, title="[bold green]Mission Complete[/bold green]"))
        collector = get_cost_collector()
        console.print(f"\n[dim]Total cost: ${collector.total_cost_usd():.4f}[/dim]")
        if output:
            output.write_text(result, encoding="utf-8")
            console.print(f"[dim]Output saved to: {output}[/dim]")
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Mission interrupted by user[/yellow]")
        sys.exit(1)
    except Exception as e:
        err_console.print(f"\n[bold red]Mission failed:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command()
def status(company_id: str = typer.Argument(..., help="Company ID to check")) -> None:
    """Show the status of a mission run."""
    settings = init_settings()
    configure_logging(settings)

    async def _status() -> None:
        from .storage import Storage
        storage = Storage(settings)
        await storage.connect()
        try:
            company = await storage.get_company(company_id)
            if not company:
                err_console.print(f"[red]Company {company_id} not found[/red]")
                raise typer.Exit(1)
            checkpoints = await storage.get_checkpoints(company_id)
            console.print(Panel.fit(f"Company: {company.name}\nStatus: {company.status}\nSpent: ${company.spent_usd:.4f} / ${company.total_budget_usd:.2f}\nSteps: {len(checkpoints)} checkpoints", title=f"[bold]Mission {company_id[:8]}...[/bold]"))
            for cp in checkpoints:
                icon = {"completed": "checkmark", "failed": "X", "running": "~", "pending": "o"}.get(cp.status, "?")
                console.print(f"  {icon} Step {cp.step_index}: {cp.status} (${cp.cost_usd:.4f})")
        finally:
            await storage.close()

    asyncio.run(_status())


def main() -> None:
    app()
