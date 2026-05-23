"""Maintenance ledger / linter CLI.

Subcommands:

* ``memex lint status [--vault X | --global | --all]`` — pending counts.
* ``memex lint findings [--type ...]`` — list findings.
* ``memex lint dismiss <finding_id>`` — flip to dismissed.
* ``memex lint resolve <finding_id>`` — flip to resolved.
* ``memex lint apply <finding_id>`` — apply a winner-proposal action.
* ``memex lint reverse <finding_id>`` — reverse an applied winner-proposal.
* ``memex lint review [--vault X | --global | --all] [--apply]`` —
  interactive triage.

The maintenance ledger is read-only from the agent surface; this CLI is
for human inspection and reconciliation.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.lint_review import render_summary, run_review_loop
from memex_cli.utils import async_command, get_api_context, handle_api_error, parse_uuid

console = Console()

app = typer.Typer(
    name='lint',
    help=(
        'Maintenance ledger: rule-based finding scan over the vault. '
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
        typer.Option('--vault', '-v', help='Filter to one vault by name or UUID.'),
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
        typer.Option('--vault', '-v', help='Filter to one vault by name or UUID.'),
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
    winner: Annotated[
        str | None,
        typer.Option(
            '--winner',
            '-w',
            help=(
                'For entity_collapse_cluster findings, override the suggested winner. '
                'Accepts either a UUID (must match a cluster member) or a canonical '
                'name (case-sensitive match against the cluster). The collapse '
                'affects every vault in evidence.vaults_affected.'
            ),
        ),
    ] = None,
):
    """Flip a pending finding to ``resolved``.

    For ``entity_collapse_cluster`` findings, optionally override the
    suggested cluster winner with ``--winner / -w``. Without the flag,
    the server applies the suggested winner recorded in the finding.
    """
    parse_uuid(finding_id, 'finding_id')
    config: MemexConfig = ctx.obj
    legacy_params: dict[str, Any] | None = None
    if winner is not None:
        try:
            UUID(winner)
            legacy_params = {'winner_id': winner}
        except ValueError:
            legacy_params = {'winner_canonical_name': winner}
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_resolve(finding_id, legacy_params=legacy_params)
        except Exception as e:
            handle_api_error(e)
            return
    console.print(f'[green]resolved:[/green] {payload["finding_id"]}')


@app.command('apply')
@async_command
async def lint_apply_cmd(
    ctx: typer.Context,
    finding_id: Annotated[
        str,
        typer.Argument(help='Finding UUID (winner-proposal) to apply.'),
    ],
):
    """Apply a winner-proposal finding's recorded action.

    The finding's ``evidence.action`` drives the mutation: mark a unit
    stale, mark a note superseded, rewrite a contradicts link as refines,
    or a no-op write when inconclusive. Captures ``prior_state`` so the
    change can be reversed with ``memex lint reverse``.
    """
    parse_uuid(finding_id, 'finding_id')
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_apply_winner(finding_id)
        except Exception as e:
            handle_api_error(e)
            return
    effective = payload.get('effective_action', 'unknown')
    console.print(
        f'[green]applied:[/green] {payload["finding_id"]} [dim](action={effective})[/dim]'
    )
    fallback = payload.get('fallback_reason')
    if fallback:
        console.print(f'[yellow]fallback:[/yellow] {fallback}')


@app.command('reverse')
@async_command
async def lint_reverse_cmd(
    ctx: typer.Context,
    finding_id: Annotated[
        str,
        typer.Argument(help='Finding UUID (winner-proposal) to reverse.'),
    ],
):
    """Reverse a previously applied winner-proposal.

    Restores the row(s) recorded under ``evidence.resolution.prior_state``
    and writes a paired audit row. The original finding stays resolved.
    """
    parse_uuid(finding_id, 'finding_id')
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_reverse_winner(finding_id)
        except Exception as e:
            handle_api_error(e)
            return
    effective = payload.get('effective_action', 'unknown')
    console.print(
        f'[green]reversed:[/green] {payload["finding_id"]} [dim](action={effective})[/dim]'
    )


@app.command('review')
@async_command
async def lint_review_cmd(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Filter to one vault by name or UUID.'),
    ] = None,
    is_global: Annotated[
        bool,
        typer.Option('--global/--no-global', help='Review only global (vault_id NULL) findings.'),
    ] = False,
    is_all: Annotated[
        bool,
        typer.Option('--all/--no-all', help='Review pending findings across every scope.'),
    ] = False,
    lint_type: Annotated[
        str | None,
        typer.Option(
            '--type',
            help='Filter by lint_type: structural, quality, governance, schema.',
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            '--limit',
            min=1,
            max=500,
            help='Max findings to load into the cockpit at once.',
        ),
    ] = 50,
    use_tui: Annotated[
        bool,
        typer.Option(
            '--tui/--no-tui',
            help=(
                'Launch the Textual TUI cockpit (default). Pass --no-tui to '
                'fall back to the legacy prompt-loop reviewer (useful for '
                'headless/CI runs that cannot drive a terminal app).'
            ),
        ),
    ] = True,
    apply: Annotated[
        bool,
        typer.Option(
            '--apply',
            help=(
                'Legacy prompt mode only: actually write resolutions instead '
                'of running dry. Ignored under --tui (the TUI always commits).'
            ),
        ),
    ] = False,
):
    """Walk pending proposals interactively.

    Default launches the Textual TUI cockpit — a two-pane interface with the
    proposal queue on the left and a detail card + numbered remediation menu
    on the right. The cockpit mirrors AskUserQuestion's pattern (canned
    options + free-form Other + reviewer note). All verdicts go through the
    generalised ``/lint/findings/{id}/resolve|dismiss|reverse`` endpoints.

    Pass ``--no-tui`` to fall back to the legacy prompt-loop reviewer for
    headless/CI use; in that mode ``--apply`` switches dry-run off.
    """
    scope = _resolve_scope(vault, is_global, is_all)
    if lint_type is not None and lint_type not in {
        'structural',
        'quality',
        'governance',
        'schema',
    }:
        console.print(f'[red]Unknown --type: {lint_type!r}[/red]')
        raise typer.Exit(2)

    config: MemexConfig = ctx.obj
    if use_tui:
        from memex_cli.cockpit.app import ProposalCockpitApp
        from memex_cli.cockpit.controller import CockpitController

        async with get_api_context(config) as api:
            vault_id: str | None = None
            if scope == 'vault' and vault is not None:
                vault_id = str(await api.resolve_vault_identifier(vault))
            controller = CockpitController(api, vault_id=vault_id)
            cockpit = ProposalCockpitApp(controller, limit=limit)
            await cockpit.run_async()
        return

    # Legacy prompt-loop path — kept for headless invocations.
    async with get_api_context(config) as api:
        try:
            vault_id_legacy: str | None = None
            if scope == 'vault' and vault is not None:
                vault_id_legacy = str(await api.resolve_vault_identifier(vault))
            payload = await api.lint_findings(
                vault_id=vault_id_legacy,
                lint_type=lint_type,
                status='pending',
                limit=limit,
            )
        except Exception as e:
            handle_api_error(e)
            return

        findings = payload.get('findings', [])
        if scope == 'global':
            findings = [f for f in findings if f.get('vault_id') in (None, '')]

        if not apply:
            console.print(
                '[dim]Dry-run mode — no resolutions will be written. '
                'Pass --apply to persist verdicts.[/dim]'
            )

        summary = await run_review_loop(
            findings,
            apply=apply,
            api=api,
            console=console,
        )

    render_summary(console, summary, apply=apply)
