"""F6 maintenance ledger / linter CLI.

Subcommands:

* ``memex lint status [--vault X | --global | --all]`` — pending counts.
* ``memex lint findings [--type ...]`` — list findings.
* ``memex lint dismiss <finding_id>`` — flip to dismissed.
* ``memex lint resolve <finding_id>`` — flip to resolved.

The maintenance ledger is read-only from the agent surface (F8 ships the
MCP tool); this CLI is for human inspection and reconciliation.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import async_command, get_api_context, handle_api_error, parse_uuid

console = Console()

app = typer.Typer(
    name='lint',
    help=(
        'F6 maintenance ledger: rule-based finding scan over the vault. '
        'Findings are advisory; nothing is auto-applied.'
    ),
    no_args_is_help=True,
)


def _resolve_scope(
    vault: str | None,
    is_global: bool,
    is_all: bool,
) -> str:
    """Reduce the three flag-shaped vault scoping args to a scope string.

    Vault identifier resolution happens in the command body via
    ``api.resolve_vault_identifier`` (parity with ``memex consolidate``)
    so vault names — not just UUIDs — are accepted.
    """
    chosen = sum(1 for x in (vault, is_global, is_all) if x)
    if chosen > 1:
        console.print('[red]Pass at most one of --vault / --global / --all.[/red]')
        raise typer.Exit(2)
    if is_all:
        return 'all'
    if is_global:
        return 'global'
    if vault is not None:
        return 'vault'
    return 'all'  # Default — show every pending finding.


@app.command('status')
@async_command
async def lint_status(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', help='Vault UUID to scope to.'),
    ] = None,
    is_global: Annotated[
        bool,
        typer.Option('--global/--no-global', help='Show only global (vault_id NULL) findings.'),
    ] = False,
    is_all: Annotated[
        bool,
        typer.Option('--all/--no-all', help='Show total pending across every vault and global.'),
    ] = False,
):
    """Pending finding counts. Default behaviour is ``--all``."""
    scope = _resolve_scope(vault, is_global, is_all)

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = (
                str(await api.resolve_vault_identifier(vault))
                if scope == 'vault' and vault is not None
                else None
            )
            payload = await api.lint_status(scope=scope, vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return

    pending = payload.get('pending', 0)
    if scope == 'vault':
        console.print(f'[bold]vault {payload["vault_id"]}:[/bold] {pending} pending findings')
    elif scope == 'global':
        console.print(f'[bold]global:[/bold] {pending} pending findings')
    else:
        console.print(f'[bold]all scopes:[/bold] {pending} pending findings')


@app.command('findings')
@async_command
async def lint_findings(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', help='Filter to one vault.'),
    ] = None,
    lint_type: Annotated[
        str | None,
        typer.Option(
            '--type',
            help='Filter by lint_type: structural, quality, governance, schema.',
        ),
    ] = None,
    status: Annotated[
        str,
        typer.Option(
            '--status',
            help='Lifecycle filter: pending, resolved, dismissed.',
        ),
    ] = 'pending',
    limit: Annotated[int, typer.Option('--limit', min=1, max=500)] = 50,
):
    """List maintenance findings."""
    if lint_type is not None and lint_type not in {'structural', 'quality', 'governance', 'schema'}:
        console.print(f'[red]Unknown --type: {lint_type!r}[/red]')
        raise typer.Exit(2)

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = str(await api.resolve_vault_identifier(vault)) if vault is not None else None
            payload = await api.lint_findings(
                vault_id=vault_id,
                lint_type=lint_type,
                status=status,
                limit=limit,
            )
        except Exception as e:
            handle_api_error(e)
            return

    findings = payload.get('findings', [])
    if not findings:
        console.print(f'[dim]No {status} findings.[/dim]')
        return

    table = Table(title=f'{status} findings ({len(findings)})')
    table.add_column('id', style='cyan', no_wrap=False)
    table.add_column('lint_type')
    table.add_column('rule_name')
    table.add_column('target_type')
    table.add_column('target_id', max_width=36)
    table.add_column('vault_id', max_width=36)
    for f in findings:
        table.add_row(
            f['id'][:8] + '…',
            f['lint_type'],
            f['rule_name'],
            f['target_type'],
            f['target_id'],
            f['vault_id'] or '(global)',
        )
    console.print(table)


@app.command('dismiss')
@async_command
async def lint_dismiss_cmd(
    ctx: typer.Context,
    finding_id: Annotated[str, typer.Argument(help='Finding UUID to dismiss.')],
):
    """Flip a pending finding to ``dismissed``."""
    parse_uuid(finding_id, 'finding_id')
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_dismiss(finding_id)
        except Exception as e:
            handle_api_error(e)
            return
    console.print(f'[green]dismissed:[/green] {payload["finding_id"]}')


@app.command('resolve')
@async_command
async def lint_resolve_cmd(
    ctx: typer.Context,
    finding_id: Annotated[str, typer.Argument(help='Finding UUID to mark resolved.')],
):
    """Flip a pending finding to ``resolved``."""
    parse_uuid(finding_id, 'finding_id')
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_resolve(finding_id)
        except Exception as e:
            handle_api_error(e)
            return
    console.print(f'[green]resolved:[/green] {payload["finding_id"]}')
