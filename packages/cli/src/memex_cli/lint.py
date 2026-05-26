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


@app.command('run')
@async_command
async def lint_run_cmd(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault to scan. Omit to scan all vaults.'),
    ] = None,
    no_llm: Annotated[
        bool,
        typer.Option('--no-llm', help='Skip LLM checks (SQL rules only).'),
    ] = False,
):
    """Run all lint checks on one or all vaults.

    SQL rules run first (deterministic, cheap). LLM checks run second
    (needs an LLM API key + embeddings). Pass ``--no-llm`` to skip the
    LLM pass.
    """
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        if vault is not None:
            vault_ids = [str(await api.resolve_vault_identifier(vault))]
        else:
            vaults_payload = await api.list_vaults()
            vault_ids = [str(v.id) for v in vaults_payload]
            console.print(f'[dim]Scanning {len(vault_ids)} vaults…[/dim]')

        for vault_id in vault_ids:
            if len(vault_ids) > 1:
                console.print(f'\n[bold]vault {vault_id[:8]}…[/bold]')
            try:
                sql_payload = await api.run_lint_rules(vault_id)
            except Exception as e:
                handle_api_error(e)
                continue
            sql_total = sql_payload.get('total_findings', 0)
            sql_rules = sql_payload.get('rules', [])
            console.print(
                f'[green]sql rules:[/green] {sql_total} findings across {len(sql_rules)} rules'
            )
            for r in sql_rules:
                emitted = r.get('findings_emitted', 0)
                if emitted:
                    console.print(f'  {r["name"]}: {emitted} findings')

            if no_llm:
                continue
            try:
                llm_payload = await api.run_lint_llm(vault_id)
                llm_findings = llm_payload.get('findings_emitted', 0)
                llm_candidates = llm_payload.get('candidates_evaluated', 0)
                console.print(
                    f'[green]llm checks:[/green] {llm_findings} findings '
                    f'from {llm_candidates} candidates evaluated'
                )
            except Exception as e:
                err_str = str(e)
                if '503' in err_str or 'not_initialized' in err_str.lower():
                    console.print(
                        '[yellow]llm checks:[/yellow] skipped (not enabled or model not loaded)'
                    )
                else:
                    handle_api_error(e)
        if no_llm:
            console.print('[dim]LLM checks skipped (--no-llm).[/dim]')

        # Show total pending so the user knows what's waiting for review,
        # regardless of whether THIS run or the background scheduler created them.
        try:
            status_payload = await api.lint_status(scope='all')
            pending = status_payload.get('pending', 0)
            if pending:
                console.print(
                    f'\n[bold]{pending} pending findings.[/bold] Run `memex lint review` to triage.'
                )
            else:
                console.print('\n[dim]No pending findings.[/dim]')
        except Exception:
            pass


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


# ---------------------------------------------------------------------------
# Auto-learning loop — Layer 2: per-rule telemetry (memex lint stats).
# ---------------------------------------------------------------------------


stats_app = typer.Typer(
    name='stats',
    help='Per-rule accept / dismiss / no-op telemetry rollups.',
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(stats_app)


def _render_telemetry_table(rows: list[dict[str, Any]]) -> Table:
    table = Table(title=f'lint rule telemetry ({len(rows)} rows)')
    table.add_column('rule_name', style='cyan')
    table.add_column('scope')
    table.add_column('accept', justify='right')
    table.add_column('no_op', justify='right')
    table.add_column('dismiss', justify='right')
    table.add_column('legacy', justify='right')
    table.add_column('accept_rate', justify='right')
    table.add_column('window_end')
    for row in rows:
        rate = row.get('accept_rate')
        rate_text = f'{rate * 100:5.1f}%' if rate is not None else '   —  '
        table.add_row(
            row.get('rule_name', '?'),
            (row.get('vault_id') or 'global')[:8] if row.get('vault_id') else 'global',
            str(row.get('accept_count', 0)),
            str(row.get('no_op_count', 0)),
            str(row.get('dismiss_count', 0)),
            str(row.get('legacy_count', 0)),
            rate_text,
            (row.get('window_end') or '')[:19],
        )
    return table


@stats_app.callback()
@async_command
async def lint_stats_default(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Filter to one vault by name or UUID.'),
    ] = None,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Filter to one rule_name.'),
    ] = None,
    include_global: Annotated[
        bool,
        typer.Option(
            '--include-global/--no-include-global',
            help='When --vault is omitted, include the cross-vault rollup row.',
        ),
    ] = True,
):
    """Render the per-rule telemetry rollup.

    Reads ``lint_rule_telemetry``. The numbers reflect the trailing 30-day
    window the rollup service last wrote — run ``memex lint stats refresh``
    to recompute on demand. ``accept_rate`` is the fraction of LABELLED
    verdicts (accept + no_op + dismiss) where a canned action ran; legacy
    rows are excluded from the denominator.
    """
    if ctx.invoked_subcommand is not None:
        return
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_telemetry(
                rule=rule,
                vault_id=vault_id,
                include_global=include_global,
            )
        except Exception as e:
            handle_api_error(e)
            return
    rows = payload.get('rows') or []
    if not rows:
        console.print(
            '[dim]No telemetry rows. Run `memex lint stats refresh` to compute '
            'a rollup over the trailing 30 days.[/dim]'
        )
        return
    console.print(_render_telemetry_table(rows))


@stats_app.command('refresh')
@async_command
async def lint_stats_refresh(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault to rollup; omit for global-only refresh.'),
    ] = None,
    window_days: Annotated[
        int,
        typer.Option('--window-days', min=1, max=365, help='Rolling window length.'),
    ] = 30,
):
    """Recompute the telemetry rollup over the trailing window."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_telemetry_refresh(
                vault_id=vault_id,
                window_days=window_days,
            )
        except Exception as e:
            handle_api_error(e)
            return
    console.print(
        f'[green]refreshed:[/green] '
        f'{payload.get("rows_written", 0)} rows · '
        f'{payload.get("rules_seen", 0)} rules · '
        f'{payload.get("proposals_aggregated", 0)} proposals · '
        f'window {payload.get("window_start", "")[:10]} → {payload.get("window_end", "")[:10]}'
    )


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


# ---------------------------------------------------------------------------
# Auto-learning loop — Layer 4: DSPy signature optimization (memex lint optimize).
# ---------------------------------------------------------------------------


optimize_app = typer.Typer(
    name='optimize',
    help='Compile and manage optimized LLM lint signatures.',
    no_args_is_help=True,
)
app.add_typer(optimize_app)


_KNOWN_LLM_RULES = [
    'llm_semantic_contradiction',
    'llm_schema_drift',
    'propose_contradiction_winner',
]


@optimize_app.command('run')
@async_command
async def lint_optimize_run_cmd(
    ctx: typer.Context,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Rule to compile. Omit to compile all LLM rules.'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
):
    """Compile optimized LLM lint signatures from your review history.

    Without --rule, compiles all known LLM rules. With --rule, compiles
    just that one.
    """
    rules = [rule] if rule else _KNOWN_LLM_RULES
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        for r in rules:
            console.print(f'[dim]Compiling {r}…[/dim]')
            try:
                payload = await api.lint_optimize_run(rule=r, vault_id=vault_id)
            except Exception as e:
                handle_api_error(e)
                continue

            status = payload.get('status', '?')
            style = {
                'promoted': 'green',
                'rejected': 'yellow',
                'insufficient_data': 'yellow',
                'error': 'red',
            }.get(status, 'white')
            console.print(f'  [{style}]{status}:[/{style}] {payload.get("message", "")}')
            if payload.get('new_version'):
                console.print(f'    version:          {payload["new_version"]}')
            if payload.get('validation_score') is not None:
                console.print(f'    validation_score: {payload["validation_score"]:.3f}')
            if payload.get('champion_score') is not None:
                console.print(f'    champion_score:   {payload["champion_score"]:.3f}')
            console.print(f'    examples_used:    {payload.get("examples_used", 0)}')
            for w in payload.get('warnings') or []:
                console.print(f'    [yellow]warning:[/yellow] {w}')


@optimize_app.command('history')
@async_command
async def lint_optimize_history_cmd(
    ctx: typer.Context,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Filter to one rule_name.'),
    ] = None,
):
    """List compiled signature versions with validation scores."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            payload = await api.lint_optimize_history(rule=rule)
        except Exception as e:
            handle_api_error(e)
            return
    sigs = payload.get('signatures') or []
    if not sigs:
        console.print('[dim]No compiled signatures found.[/dim]')
        return
    table = Table(title=f'compiled signatures ({len(sigs)})')
    table.add_column('rule_name', style='cyan')
    table.add_column('v', justify='right')
    table.add_column('val_score', justify='right')
    table.add_column('val_examples', justify='right')
    table.add_column('promoted_at')
    table.add_column('promoted_by')
    table.add_column('superseded')
    for s in sigs:
        vs = s.get('validation_score')
        table.add_row(
            s.get('rule_name', '?'),
            str(s.get('version', '?')),
            f'{vs:.3f}' if vs is not None else '—',
            str(s.get('validation_examples', '—')),
            (s.get('promoted_at') or '')[:19],
            s.get('promoted_by') or '',
            'yes' if s.get('superseded') else '',
        )
    console.print(table)


@optimize_app.command('rollback')
@async_command
async def lint_optimize_rollback_cmd(
    ctx: typer.Context,
    rule: Annotated[str, typer.Option('--rule', help='Rule to rollback.')],
    version: Annotated[int, typer.Option('--version', help='Version to rollback to.')],
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
):
    """Rollback a rule's DSPy signature to a specific version."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_optimize_rollback(
                rule=rule, version=version, vault_id=vault_id
            )
        except Exception as e:
            handle_api_error(e)
            return
    ok = payload.get('rolled_back', False)
    if ok:
        console.print(f'[green]rolled back:[/green] {rule} → v{version}')
    else:
        console.print(f'[red]rollback failed:[/red] version {version} not found for {rule}')


# ---------------------------------------------------------------------------
# Auto-learning loop — Layer 3: threshold calibration (memex lint calibration).
# ---------------------------------------------------------------------------


calibration_app = typer.Typer(
    name='calibration',
    help='Per-rule emission threshold calibration.',
    no_args_is_help=True,
)
app.add_typer(calibration_app)


@calibration_app.command('list')
@async_command
async def lint_calibration_list(
    ctx: typer.Context,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Filter to one rule_name.'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
):
    """List calibration rows — versioned per-rule thresholds."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_calibration_list(rule=rule, vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return
    rows = payload.get('rows') or []
    if not rows:
        console.print('[dim]No calibration rows. Run `memex lint calibration run` first.[/dim]')
        return
    table = Table(title=f'lint calibrations ({len(rows)} rows)')
    table.add_column('rule', style='cyan')
    table.add_column('v', justify='right')
    table.add_column('threshold', justify='right')
    table.add_column('frozen')
    table.add_column('superseded')
    table.add_column('learned_at')
    for row in rows:
        th = row.get('surprise_threshold')
        table.add_row(
            row.get('rule_name', '?'),
            str(row.get('version', '?')),
            f'{th:.3f}' if th is not None else '—',
            'yes' if row.get('frozen') else '',
            str(row.get('superseded_by_version', '')) if row.get('superseded_by_version') else '',
            (row.get('learned_at') or '')[:19],
        )
    console.print(table)


@calibration_app.command('run')
@async_command
async def lint_calibration_run(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
):
    """Run threshold calibration now — adjust per-rule emission thresholds from telemetry."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_calibration_run(vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return
    console.print(
        f'[green]calibrated:[/green] '
        f'{payload.get("rules_calibrated", 0)} adjusted · '
        f'{payload.get("rules_unchanged", 0)} unchanged · '
        f'{payload.get("rules_skipped_frozen", 0)} frozen · '
        f'{payload.get("rules_skipped_insufficient_data", 0)} insufficient data'
    )
    for d in payload.get('details') or []:
        status = d.get('status', '?')
        rule = d.get('rule', '?')
        if status == 'calibrated':
            console.print(
                f'  {rule}: {d.get("old_threshold", "?")} → {d.get("new_threshold", "?")} '
                f'({d.get("reason", "")})'
            )


@calibration_app.command('freeze')
@async_command
async def lint_calibration_freeze(
    ctx: typer.Context,
    rule: Annotated[str, typer.Argument(help='Rule to freeze.')],
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
    unfreeze: Annotated[
        bool,
        typer.Option('--unfreeze', help='Unfreeze instead of freezing.'),
    ] = False,
):
    """Freeze (or unfreeze) auto-calibration for a rule."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            await api.lint_calibration_freeze(rule=rule, vault_id=vault_id, frozen=not unfreeze)
        except Exception as e:
            handle_api_error(e)
            return
    verb = 'unfrozen' if unfreeze else 'frozen'
    console.print(f'[green]{verb}:[/green] {rule}')


@calibration_app.command('rollback')
@async_command
async def lint_calibration_rollback(
    ctx: typer.Context,
    rule: Annotated[str, typer.Argument(help='Rule to rollback.')],
    version: Annotated[int, typer.Argument(help='Version to rollback to.')],
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope.'),
    ] = None,
):
    """Rollback a rule to a specific calibration version."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_calibration_rollback(
                rule=rule, version=version, vault_id=vault_id
            )
        except Exception as e:
            handle_api_error(e)
            return
    ok = payload.get('rolled_back', False)
    if ok:
        console.print(f'[green]rolled back:[/green] {rule} → v{version}')
    else:
        console.print(f'[red]rollback failed:[/red] version {version} not found for {rule}')


# ---------------------------------------------------------------------------
# Compiled LLM lint signatures -- browse, inspect, compare.
# ---------------------------------------------------------------------------

from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

signatures_app = typer.Typer(
    name='signatures',
    help='Browse and inspect compiled LLM lint signatures.',
    no_args_is_help=True,
)
app.add_typer(signatures_app)


def _sig_status_text(sig: dict[str, Any]) -> Text:
    """Render the status column for a signature row."""
    superseded = sig.get('superseded_by_version')
    if superseded is None:
        return Text('● active', style='bold green')
    if superseded == -1:
        return Text('rolled back', style='yellow')
    return Text(f'→ v{superseded}', style='dim')


def _truncate(s: str, length: int = 45) -> str:
    """Truncate ``s`` to ``length`` chars, appending an ellipsis if needed."""
    if len(s) <= length:
        return s
    return s[: length - 1] + '…'


def _verdict_text(verdict: str) -> Text:
    """Color-code a verdict string."""
    if verdict == 'accept':
        return Text('accept', style='green')
    if verdict == 'dismiss':
        return Text('dismiss', style='red')
    return Text(verdict, style='dim')


def _score_delta_text(delta: float) -> Text:
    """Render a signed score delta with color."""
    if delta > 0:
        return Text(f'(+{delta:.3f})', style='green')
    if delta < 0:
        return Text(f'({delta:.3f})', style='red')
    return Text('(+0.000)', style='dim')


def _bar_chart(value: int, total: int, width: int = 20) -> Text:
    """Render a block-char bar chart."""
    if total == 0:
        return Text('░' * width, style='dim')
    filled = round(value / total * width)
    empty = width - filled
    bar = Text()
    bar.append('█' * filled, style='green')
    bar.append('░' * empty, style='dim')
    return bar


@signatures_app.command('list')
@async_command
async def signatures_list_cmd(
    ctx: typer.Context,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Filter to one rule_name.'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope (name or UUID).'),
    ] = None,
):
    """List compiled signature versions with status indicators."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            payload = await api.lint_optimize_history(rule=rule)
        except Exception as e:
            handle_api_error(e)
            return
    sigs = payload.get('signatures') or []
    if vault_id is not None:
        sigs = [s for s in sigs if s.get('vault_id') == vault_id]
    if not sigs:
        console.print('[dim]No compiled signatures found.[/dim]')
        return

    table = Table(title=f'signatures ({len(sigs)})')
    table.add_column('rule_name', style='cyan')
    table.add_column('v', justify='right')
    table.add_column('score', justify='right')
    table.add_column('examples', justify='right')
    table.add_column('promoted_at')
    table.add_column('promoted_by')
    table.add_column('status')
    for s in sigs:
        vs = s.get('validation_score')
        status_text = _sig_status_text(s)
        score_str = f'{vs:.3f}' if vs is not None else '—'
        examples_str = str(s.get('validation_examples', '—'))
        promoted_at = (s.get('promoted_at') or '')[:19]
        promoted_by = s.get('promoted_by') or ''

        is_active = s.get('superseded_by_version') is None
        table.add_row(
            s.get('rule_name', '?'),
            str(s.get('version', '?')),
            score_str,
            examples_str,
            promoted_at,
            promoted_by,
            status_text,
            style='' if is_active else 'dim',
        )
    console.print(table)


@signatures_app.command('show')
@async_command
async def signatures_show_cmd(
    ctx: typer.Context,
    rule: Annotated[str, typer.Argument(help='Rule name.')],
    version: Annotated[int, typer.Argument(help='Signature version number.')],
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope (name or UUID).'),
    ] = None,
):
    """Show full detail for a specific signature version, including demos."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            detail = await api.lint_signature_detail(rule, version, vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return

    if detail is None:
        console.print(f'[red]Signature not found:[/red] {rule} v{version}')
        raise typer.Exit(1)

    # Header info
    vs = detail.get('validation_score')
    score_str = f'{vs:.3f}' if vs is not None else '—'
    examples = detail.get('validation_examples', '—')
    base_model = detail.get('base_model') or '—'
    promoted_at = (str(detail.get('promoted_at') or ''))[:19]
    promoted_by = detail.get('promoted_by') or '—'
    program = detail.get('compiled_program') or {}
    program_type = program.get('type', '—') if isinstance(program, dict) else '—'
    superseded = detail.get('superseded_by_version')
    if superseded is None:
        status_line = '[bold green]● active[/bold green]'
    elif superseded == -1:
        status_line = '[yellow]rolled back[/yellow]'
    else:
        status_line = f'[dim]→ v{superseded}[/dim]'

    header_lines = [
        f'[bold]{rule}[/bold] v{version}  {status_line}',
        f'score: {score_str}  examples: {examples}  base_model: {base_model}',
        f'promoted_at: {promoted_at}  promoted_by: {promoted_by}',
        f'program: {program_type}',
    ]

    # Demos table
    demos = detail.get('demos') or []
    demo_table: Table | None = None
    if demos:
        demo_table = Table(show_header=True, expand=True)
        demo_table.add_column('#', justify='right', style='dim', width=3)
        demo_table.add_column('target_text', ratio=3)
        demo_table.add_column('verdict', justify='center')
        demo_table.add_column('surprise', justify='right')
        for i, d in enumerate(demos, 1):
            target = _truncate(str(d.get('target_text', '')), 45)
            verdict = _verdict_text(str(d.get('verdict', '')))
            surprise = d.get('surprise_score')
            surprise_str = f'{surprise:.2f}' if surprise is not None else '—'
            demo_table.add_row(str(i), target, verdict, surprise_str)

    panel_content = Text()
    for line in header_lines:
        panel_content.append_text(Text.from_markup(line))
        panel_content.append('\n')
    console.print(Panel(panel_content, title=f'{rule} v{version}', border_style='cyan'))
    if demo_table is not None:
        console.print(demo_table)
    else:
        console.print('[dim]No demos in this signature.[/dim]')


@signatures_app.command('diff')
@async_command
async def signatures_diff_cmd(
    ctx: typer.Context,
    rule: Annotated[str, typer.Argument(help='Rule name.')],
    v1: Annotated[int, typer.Argument(help='First version to compare.')],
    v2: Annotated[int, typer.Argument(help='Second version to compare.')],
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope (name or UUID).'),
    ] = None,
):
    """Compare two signature versions side by side."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            d1 = await api.lint_signature_detail(rule, v1, vault_id=vault_id)
            d2 = await api.lint_signature_detail(rule, v2, vault_id=vault_id)
        except Exception as e:
            handle_api_error(e)
            return

    if d1 is None:
        console.print(f'[red]Signature not found:[/red] {rule} v{v1}')
        raise typer.Exit(1)
    if d2 is None:
        console.print(f'[red]Signature not found:[/red] {rule} v{v2}')
        raise typer.Exit(1)

    # Metadata comparison
    s1 = d1.get('validation_score')
    s2 = d2.get('validation_score')
    e1 = d1.get('validation_examples')
    e2 = d2.get('validation_examples')

    meta_table = Table(title=f'{rule}: v{v1} vs v{v2}', show_header=True)
    meta_table.add_column('field', style='cyan')
    meta_table.add_column(f'v{v1}', justify='right')
    meta_table.add_column(f'v{v2}', justify='right')
    meta_table.add_column('delta', justify='right')

    score_delta = (s2 or 0) - (s1 or 0)
    meta_table.add_row(
        'score',
        f'{s1:.3f}' if s1 is not None else '—',
        f'{s2:.3f}' if s2 is not None else '—',
        _score_delta_text(score_delta),
    )
    examples_delta = (e2 or 0) - (e1 or 0)
    meta_table.add_row(
        'examples',
        str(e1) if e1 is not None else '—',
        str(e2) if e2 is not None else '—',
        str(examples_delta) if examples_delta else '',
    )
    console.print(meta_table)

    # Demos comparison -- match by target_text content
    demos1 = d1.get('demos') or []
    demos2 = d2.get('demos') or []

    map1: dict[str, dict[str, Any]] = {}
    for d in demos1:
        key = str(d.get('target_text', ''))[:200]
        map1[key] = d
    map2: dict[str, dict[str, Any]] = {}
    for d in demos2:
        key = str(d.get('target_text', ''))[:200]
        map2[key] = d

    all_keys: list[str] = []
    seen: set[str] = set()
    for k in list(map1.keys()) + list(map2.keys()):
        if k not in seen:
            all_keys.append(k)
            seen.add(k)

    if all_keys:
        diff_demo_table = Table(title='demos', show_header=True, expand=True)
        diff_demo_table.add_column('', width=1)
        diff_demo_table.add_column('target_text', ratio=3)
        diff_demo_table.add_column(f'v{v1}', justify='center')
        diff_demo_table.add_column(f'v{v2}', justify='center')
        for key in all_keys:
            in1 = key in map1
            in2 = key in map2
            v1_verdict = str(map1[key].get('verdict', '')) if in1 else ''
            v2_verdict = str(map2[key].get('verdict', '')) if in2 else ''
            if in1 and in2:
                marker = ' ' if v1_verdict == v2_verdict else '*'
            elif in1:
                marker = '-'
            else:
                marker = '+'
            marker_style = {'*': 'yellow', '+': 'green', '-': 'red'}.get(marker, 'dim')
            diff_demo_table.add_row(
                Text(marker, style=marker_style),
                _truncate(key, 45),
                _verdict_text(v1_verdict) if v1_verdict else Text('', style='dim'),
                _verdict_text(v2_verdict) if v2_verdict else Text('', style='dim'),
            )
        console.print(diff_demo_table)
    else:
        console.print('[dim]No demos in either version.[/dim]')


@signatures_app.command('status')
@async_command
async def signatures_status_cmd(
    ctx: typer.Context,
    rule: Annotated[
        str | None,
        typer.Option('--rule', help='Filter to one rule_name.'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault scope (name or UUID).'),
    ] = None,
):
    """Composite status: signature + calibration + telemetry + optimizer readiness per rule."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        vault_id: str | None = None
        if vault is not None:
            vault_id = str(await api.resolve_vault_identifier(vault))
        try:
            sig_payload = await api.lint_optimize_history(rule=rule)
            cal_payload = await api.lint_calibration_list(rule=rule, vault_id=vault_id)
            tel_payload = await api.lint_telemetry(
                rule=rule, vault_id=vault_id, include_global=vault_id is None
            )
        except Exception as e:
            handle_api_error(e)
            return

    sigs = sig_payload.get('signatures') or []
    cals = cal_payload.get('rows') or []
    tels = tel_payload.get('rows') or []

    rule_names: set[str] = set()
    for s in sigs:
        rule_names.add(s.get('rule_name', ''))
    for c in cals:
        rule_names.add(c.get('rule_name', ''))
    for t in tels:
        rule_names.add(t.get('rule_name', ''))

    if not rule_names:
        console.print('[dim]No data found. Run lint checks and resolve findings first.[/dim]')
        return

    for rn in sorted(rule_names):
        rule_sigs = [s for s in sigs if s.get('rule_name') == rn]
        active_sig = next(
            (s for s in rule_sigs if s.get('superseded_by_version') is None),
            None,
        )
        rule_cals = [c for c in cals if c.get('rule_name') == rn]
        active_cal = next(
            (c for c in rule_cals if not c.get('superseded_by_version')),
            None,
        )
        rule_tel = next((t for t in tels if t.get('rule_name') == rn), None)

        lines: list[str] = []

        # Signature section
        if active_sig:
            vs = active_sig.get('validation_score')
            demo_count = active_sig.get('validation_examples', 0)
            score_fmt = f'{vs:.3f}' if vs is not None else '—'
            lines.append(
                f'  [bold]signature:[/bold] v{active_sig.get("version", "?")} '
                f'score={score_fmt} demos={demo_count}'
            )
        else:
            lines.append('  [bold]signature:[/bold] [dim]none compiled[/dim]')

        # Calibration section
        if active_cal:
            th = active_cal.get('surprise_threshold')
            frozen = active_cal.get('frozen', False)
            frozen_tag = ' [yellow](frozen)[/yellow]' if frozen else ''
            th_fmt = f'{th:.3f}' if th is not None else '—'
            lines.append(
                f'  [bold]calibration:[/bold] v{active_cal.get("version", "?")} '
                f'threshold={th_fmt}{frozen_tag}'
            )
        else:
            lines.append('  [bold]calibration:[/bold] [dim]none[/dim]')

        # Telemetry section
        if rule_tel:
            accept = int(rule_tel.get('accept_count', 0))
            no_op = int(rule_tel.get('no_op_count', 0))
            dismiss = int(rule_tel.get('dismiss_count', 0))
            total = accept + no_op + dismiss
            rate = rule_tel.get('accept_rate')
            bar = _bar_chart(accept, total)
            rate_str = f'{rate * 100:.1f}%' if rate is not None else '—'
            lines.append(
                f'  [bold]telemetry:[/bold] accept={accept} no_op={no_op} dismiss={dismiss}'
            )
            bar_line = Text('  ')
            bar_line.append_text(bar)
            bar_line.append(f'  accept_rate={rate_str}')
            lines.append(str(bar_line))
        else:
            lines.append('  [bold]telemetry:[/bold] [dim]no data[/dim]')

        # Optimizer readiness
        labelled = int(rule_tel.get('labelled_count', 0)) if rule_tel else 0
        min_examples = 50
        rate = rule_tel.get('accept_rate') if rule_tel else None
        ready = labelled >= min_examples
        rate_ok = rate is not None and 0.3 <= rate <= 0.8
        lines.append(
            f'  [bold]optimizer:[/bold] '
            f'labelled={labelled}/{min_examples} '
            f'{"[green]ready[/green]" if ready else "[yellow]not ready[/yellow]"}'
            f'  accept_rate={"in range" if rate_ok else "out of range"} '
            f'{"[green]ok[/green]" if rate_ok else "[yellow]needs review[/yellow]"}'
        )

        panel_text = '\n'.join(lines)
        console.print(
            Panel(
                panel_text,
                title=f'[bold cyan]{rn}[/bold cyan]',
                border_style='cyan',
            )
        )
