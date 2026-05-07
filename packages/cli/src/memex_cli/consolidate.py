"""`memex consolidate` CLI subgroup.

Operator-facing surface for the consolidation orchestrator. There is
intentionally no MCP / Hermes / Claude Code tool — this CLI is the only
first-class human surface.

Subcommands:
- ``memex consolidate tick [--vault X] [--dry-run] [--budget N]``
- ``memex consolidate status [--vault X]``
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_cli.utils import async_command, get_api_context, handle_api_error
from memex_common.config import MemexConfig

console = Console()

app = typer.Typer(
    name='consolidate',
    help='Per-vault consolidation tick (contradiction → reflection → prune-stale-only).',
    no_args_is_help=True,
)


@app.command('tick')
@async_command
async def tick_cmd(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault name or UUID. Omit to tick every vault.'),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option('--dry-run', help='Skip writes; report per-step counts only.'),
    ] = False,
    budget: Annotated[
        int | None,
        typer.Option(
            '--budget',
            help='Override per-tick units budget. Omit to use config default (500).',
            min=1,
            max=10_000,
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option('--json', help='Emit JSON instead of a table.')
    ] = False,
):
    """Run consolidation tick(s) immediately."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault) if vault else None
            payload = await api.consolidation_tick(
                vault_id=vault_id, dry_run=dry_run, budget=budget
            )
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return

    ticks = payload.get('ticks', [])
    if not ticks:
        console.print('[dim]No vaults to consolidate.[/dim]')
        return

    title = 'Consolidation tick (dry-run)' if dry_run else 'Consolidation tick'
    table = Table(title=title)
    table.add_column('vault_id', style='cyan')
    table.add_column('units', justify='right')
    table.add_column('entities', justify='right')
    table.add_column('contradictions', justify='right')
    table.add_column('stale_pruned', justify='right')
    table.add_column('error', style='red')
    for row in ticks:
        table.add_row(
            row.get('vault_id', '?'),
            str(row.get('units_processed', 0)),
            str(row.get('entities_reflected', 0)),
            str(row.get('contradictions_run', 0)),
            str(row.get('stale_pruned', 0)),
            row.get('error') or '',
        )
    console.print(table)


@app.command('status')
@async_command
async def status_cmd(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault name or UUID. Omit to list every vault.'),
    ] = None,
    json_output: Annotated[
        bool, typer.Option('--json', help='Emit JSON instead of a table.')
    ] = False,
):
    """Show the most recent consolidation tick per vault."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault) if vault else None
            payload = await api.consolidation_status(vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return

    rows = payload.get('ticks', [])
    if not rows:
        console.print('[dim]No consolidation ticks recorded.[/dim]')
        return

    table = Table(title='Last consolidation tick per vault')
    table.add_column('vault_id', style='cyan')
    table.add_column('completed_at')
    table.add_column('units', justify='right')
    table.add_column('entities', justify='right')
    table.add_column('contradictions', justify='right')
    table.add_column('stale_pruned', justify='right')
    table.add_column('error', style='red')
    for r in rows:
        table.add_row(
            r.get('vault_id', '?'),
            r.get('completed_at') or 'in-progress',
            str(r.get('units_processed', 0)),
            str(r.get('entities_reflected', 0)),
            str(r.get('contradictions_run', 0)),
            str(r.get('stale_pruned', 0)),
            r.get('error') or '',
        )
    console.print(table)
