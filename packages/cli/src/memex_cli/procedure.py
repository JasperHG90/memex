"""Procedure KV namespace commands (F14).

Thin CLI wrapper over the procedure-key surface:

* ``memex procedure list`` — top procedure outcomes for a vault, ranked
  by Memory Worth.
* ``memex procedure show`` — read a single procedure key (active value
  by default, or the full envelope with ``--history``).
* ``memex procedure add`` — write a new procedure value (creates v=1 or
  bumps the version with capped history per RFC-007 §63-112).

Refer to the agent-facing tools (`memex_kv_*`, `memex_record_outcome`)
for in-session use; this CLI is for human inspection and seeding.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import async_command, get_api_context, handle_api_error

console = Console()

app = typer.Typer(
    name='procedure',
    help=(
        'Procedure KV namespace (F14): compact, learned how-tos owned by '
        'the agent. Each key is procedure:<verb>:<context-tag> with a '
        'versioned envelope and capped history (5 prior versions). '
        'Per-(vault, key) Memory Worth counters track success/failure.'
    ),
    no_args_is_help=True,
)


@app.command('list')
@async_command
async def procedure_list(
    ctx: typer.Context,
    vault: Annotated[
        str,
        typer.Option(
            '--vault',
            help='Vault UUID to scope observations to.',
        ),
    ],
    context: Annotated[
        str | None,
        typer.Option(
            '--context',
            help='Optional substring filter on the procedure key context-tag.',
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            '--limit',
            help='Max observations to return (1-20, default 5).',
            min=1,
            max=20,
        ),
    ] = 5,
    json_output: Annotated[
        bool,
        typer.Option('--json', help='Emit JSON instead of a table.'),
    ] = False,
):
    """List top procedure outcomes for a vault, ranked by Memory Worth."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault)
            rows = await api.list_top_procedure_outcomes(
                vault_id=vault_id, context=context, limit=limit
            )
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(data=[r.model_dump(mode='json') for r in rows])
        return

    if not rows:
        console.print('[dim]No procedure outcomes recorded for this vault.[/dim]')
        return

    table = Table(title='Top procedure outcomes (Memory Worth ↓)')
    table.add_column('kv_key', style='cyan')
    table.add_column('success', justify='right')
    table.add_column('failure', justify='right')
    table.add_column('mw_score', justify='right')
    table.add_column('last_outcome_at')
    for r in rows:
        last = r.last_outcome_at.isoformat() if r.last_outcome_at else '—'
        table.add_row(
            r.kv_key,
            str(r.success_co_count),
            str(r.failure_co_count),
            f'{r.mw_score:.3f}',
            last,
        )
    console.print(table)


@app.command('show')
@async_command
async def procedure_show(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help='Procedure key (procedure:<verb>:<context-tag>).')],
    history: Annotated[
        bool,
        typer.Option(
            '--history',
            help='Include the full envelope (active value, version, capped 5-version history).',
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option('--json', help='Emit JSON instead of formatted output.'),
    ] = False,
):
    """Read a procedure key. Default returns active value; ``--history`` exposes the envelope."""
    if not key.startswith('procedure:'):
        console.print(
            f'[red]Not a procedure key: {key!r}. Expected procedure:<verb>:<context-tag>.[/red]'
        )
        raise typer.Exit(2)

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            entry = await api.kv_get(key=key, include_history=history)
        except Exception as e:
            handle_api_error(e)
            return

    if entry is None:
        console.print(f'[yellow]Key not found: {key}[/yellow]')
        raise typer.Exit(1)

    if json_output:
        console.print_json(data=entry.model_dump(mode='json'))
        return

    if history and isinstance(entry.value, dict):
        console.print(f'[bold cyan]{entry.key}[/bold cyan]')
        console.print(f'[bold]version:[/bold] {entry.value["version"]}')
        console.print(f'[bold]active value:[/bold] {entry.value["value"]}')
        prior = entry.value.get('history') or []
        if prior:
            console.print(f'[bold]history ({len(prior)}):[/bold]')
            for h in prior:
                console.print(f'  v{h["v"]} ({h.get("superseded_at", "?")}): {h["value"]}')
        else:
            console.print('[dim]history: empty[/dim]')
    else:
        console.print(f'[bold cyan]{entry.key}[/bold cyan]')
        console.print(entry.value)


@app.command('add')
@async_command
async def procedure_add(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help='Procedure key (procedure:<verb>:<context-tag>).')],
    value: Annotated[
        str,
        typer.Argument(help='Procedure body — the learned how-to text.'),
    ],
):
    """Write or update a procedure value (versioned + capped history per RFC-007)."""
    if not key.startswith('procedure:'):
        console.print(
            f'[red]Not a procedure key: {key!r}. Expected procedure:<verb>:<context-tag>.[/red]'
        )
        raise typer.Exit(2)

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            entry = await api.kv_put(value=value, key=key)
        except Exception as e:
            handle_api_error(e)
            return

    console.print(f'[green]Wrote:[/green] {entry.key}')
    # Server stores the JSON envelope; show only the active value for legibility.
    try:
        payload = json.loads(entry.value) if isinstance(entry.value, str) else entry.value
        if isinstance(payload, dict) and 'value' in payload:
            console.print(f'[bold]version:[/bold] {payload.get("v", "?")}')
            console.print(f'[bold]active value:[/bold] {payload["value"]}')
            return
    except (ValueError, TypeError):
        pass
    console.print(entry.value)
