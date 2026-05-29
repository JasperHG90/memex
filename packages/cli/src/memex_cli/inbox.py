"""Inbox router CLI — trigger triage and inspect router status.

``memex inbox triage [--dry-run]`` runs one triage pass over the inbox vault;
``memex inbox status`` shows readiness (warmed-up gate) and pending routing
proposals.
"""

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import async_command, get_api_context

console = Console()

app = typer.Typer(
    name='inbox',
    help='Inbox router: triage notes in the inbox vault and route them to vaults.',
    no_args_is_help=True,
)


@app.command('triage')
@async_command
async def triage(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option('--dry-run', help='Score + decide without mutating anything.')
    ] = False,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Run one inbox-router triage tick."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        await api.inbox_router.ensure_inbox_vault()
        result = await api.inbox_router.triage_tick(dry_run=dry_run)
    payload = {'dry_run': dry_run, **result.as_dict()}
    if json_output:
        console.print_json(json.dumps(payload))
        return
    verb = 'Would route' if dry_run else 'Routed'
    console.print(
        f'[bold]Inbox triage[/bold] ({"dry-run" if dry_run else "live"}): '
        f'scored={result.scored}  {verb.lower()}/auto={result.auto_routed}  '
        f'proposed={result.proposed}  no_fit={result.no_fit}  '
        f'skipped_cap={result.skipped_cap}  errors={result.errors}'
    )


@app.command('status')
@async_command
async def status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Show inbox-router readiness and pending routing proposals."""
    config: MemexConfig = ctx.obj
    cfg = config.server.memory.inbox_router
    from sqlalchemy import text

    async with get_api_context(config) as api:
        async with api.metastore.session() as session:
            match_n = (
                await session.execute(
                    text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1')
                )
            ).scalar()
            rows = (
                await session.execute(
                    text(
                        'SELECT rule_name, COUNT(*) FROM maintenance_proposals '
                        "WHERE lint_type = 'routing' AND status = 'pending' GROUP BY rule_name"
                    )
                )
            ).all()
    pending = {r[0]: int(r[1]) for r in rows}
    match_count = float(match_n or 0.0)
    payload = {
        'enabled': cfg.enabled,
        'auto_apply_enabled': cfg.auto_apply_enabled,
        'warmed_up': match_count >= cfg.min_decisions_before_auto_apply,
        'match_observations': match_count,
        'pending_route': pending.get('inbox_vault_route', 0),
        'pending_no_fit': pending.get('inbox_vault_no_fit', 0),
    }
    if json_output:
        console.print_json(json.dumps(payload))
        return
    table = Table(title='Inbox Router Status')
    table.add_column('Field')
    table.add_column('Value')
    for k, v in payload.items():
        table.add_row(k, str(v))
    console.print(table)
