"""Inbox router CLI — trigger triage and inspect router status.

``memex inbox triage [--dry-run]`` runs one triage pass over the inbox vault;
``memex inbox status`` shows readiness (warmed-up gate) and pending routing
proposals.
"""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import async_command, emit_json, get_api_context

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
    # The CLI talks to the server over HTTP (RemoteMemexAPI); the inbox_router
    # service lives in-process on the server side. The /inbox/triage endpoint
    # ensures the inbox vault and runs the tick — we just call it and render.
    async with get_api_context(config) as api:
        result = await api.trigger_inbox_triage(dry_run=dry_run)
    if json_output:
        emit_json(result)
        return
    verb = 'Would route' if dry_run else 'Routed'
    # ``blocked_*`` keys are absent on responses from an older server; default to 0.
    blocked_cooldown = result.get('blocked_cooldown', 0)
    blocked_backoff = result.get('blocked_backoff', 0)
    console.print(
        f'[bold]Inbox triage[/bold] ({"dry-run" if dry_run else "live"}): '
        f'scored={result["scored"]}  {verb.lower()}/auto={result["auto_routed"]}  '
        f'proposed={result["proposed"]}  no_fit={result["no_fit"]}  '
        f'skipped_cap={result["skipped_cap"]}  '
        f'blocked_cooldown={blocked_cooldown}  blocked_backoff={blocked_backoff}  '
        f'errors={result["errors"]}'
    )
    if blocked_cooldown:
        console.print(
            f'[dim]{blocked_cooldown} note(s) were not re-proposed because they were '
            'routed/dismissed recently. To re-evaluate them now, set '
            'inbox_router.reproposal_cooldown_days=0.[/dim]'
        )


@app.command('status')
@async_command
async def status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Show inbox-router readiness and pending routing proposals."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        payload = await api.inbox_status()
    if json_output:
        emit_json(payload)
        return
    table = Table(title='Inbox Router Status')
    table.add_column('Field')
    table.add_column('Value')
    for k, v in payload.items():
        table.add_row(k, str(v))
    console.print(table)
