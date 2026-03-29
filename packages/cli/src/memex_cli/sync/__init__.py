from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import structlog
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import get_api_context, async_command

from .config import DEFAULT_CONFIG_TOML, CONFIG_FILENAME, WatchMode, load_config
from .scanner import scan_vault
from .state import SyncStateDB, diff
from .engine import sync_vault
from .watcher import run_watcher

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

console = Console()

app = typer.Typer(
    name='sync',
    help='Sync a folder of Markdown notes to Memex.',
    no_args_is_help=True,
)


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn('[bold]{task.description}'),
        BarColumn(),
        TextColumn('{task.completed}/{task.total}'),
        TimeElapsedColumn(),
        TextColumn('{task.fields[detail]}'),
        console=console,
        transient=False,
    )


@app.command('run', no_args_is_help=True)
@async_command
async def run_sync(
    ctx: typer.Context,
    vault_path: Path = typer.Argument(..., help='Path to the notes folder'),
    config: Path | None = typer.Option(None, '--config', '-c', help='Path to config TOML'),
    full: bool = typer.Option(False, '--full', help='Ignore last sync, re-sync all'),
    dry_run: bool = typer.Option(False, '--dry-run', help='Show what would be synced'),
    background: bool = typer.Option(
        False,
        '--background',
        '-b',
        help='Submit batch job and return immediately without waiting',
    ),
    no_handle_deletes: bool = typer.Option(
        False,
        '--no-handle-deletes',
        help='Do not archive or delete notes in Memex when local files are removed. '
        'By default, deleted files are archived (marked stale, excluded from retrieval).',
    ),
    hard_delete: bool = typer.Option(
        False,
        '--hard-delete',
        help='Permanently delete notes from Memex when local files are removed, '
        'instead of archiving them. Use with caution — this is irreversible.',
    ),
) -> None:
    """Sync changed notes to Memex.

    By default, when a file is deleted from the local folder, its corresponding
    note in Memex is archived: the note is marked with status 'archived' and all
    its memory units become stale (excluded from retrieval). The data is preserved
    and can be restored by setting the note status back to 'active'.

    Use --no-handle-deletes to skip this behavior entirely, or --hard-delete to
    permanently remove the note and all associated data from Memex.
    """
    vault_path = vault_path.resolve()
    if not vault_path.is_dir():
        console.print(f'[red]Path does not exist: {vault_path}[/red]')
        raise typer.Exit(1)

    cfg = load_config(vault_path, config)
    memex_config: MemexConfig = ctx.obj
    vault_id = cfg.vault_id or memex_config.write_vault

    progress = _make_progress()
    task_ids: dict[str, int] = {}

    def on_progress(phase: str, current: int, total: int, detail: str) -> None:
        if phase not in task_ids:
            desc = {
                'scanning': 'Scanning',
                'preparing': 'Preparing',
                'ingesting': 'Ingesting',
                'archiving': 'Archiving',
                'deleting': 'Deleting',
                'done': 'Done',
            }.get(phase, phase)
            task_ids[phase] = progress.add_task(desc, total=total or None, detail=detail)
        tid = task_ids[phase]
        progress.update(tid, completed=current, total=total or None, detail=detail)

    async with get_api_context(memex_config) as api:
        if dry_run:
            result = await sync_vault(
                vault_path,
                api,
                cfg.sync,
                vault_id=vault_id,
                dry_run=True,
                full=full,
            )
            console.print(f'[bold]Scanned:[/bold] {result.total_scanned} notes')
            console.print(f'[bold]Changed:[/bold] {result.changed} notes would be synced')
            if result.deleted_detected:
                console.print(
                    f'[yellow]Deleted from folder:[/yellow] {len(result.deleted_detected)} notes'
                )
                if no_handle_deletes:
                    console.print('  (--no-handle-deletes: no action will be taken)')
                elif hard_delete:
                    console.print('  (--hard-delete: would be permanently deleted from Memex)')
                else:
                    console.print('  (would be archived in Memex)')
            return

        with progress:
            result = await sync_vault(
                vault_path,
                api,
                cfg.sync,
                vault_id=vault_id,
                full=full,
                background=background,
                handle_deletes=not no_handle_deletes,
                hard_delete=hard_delete,
                on_progress=on_progress,
            )

    if background and result.job_id:
        console.print(f'[green]Batch job submitted:[/green] {result.job_id}')
        console.print(f'  {result.changed} note(s) queued for ingestion')
        console.print(f'  Check status: memex note sync job {result.job_id}')
        return

    if result.changed == 0 and not result.deleted_detected:
        console.print('[green]Everything is up to date.[/green]')
        return

    if result.ingested:
        console.print(f'[green]Ingested:[/green] {result.ingested}')
    if result.skipped:
        console.print(f'[yellow]Skipped:[/yellow] {result.skipped}')
    if result.failed:
        console.print(f'[red]Failed:[/red] {result.failed}')
    if result.archived:
        console.print(f'[yellow]Archived:[/yellow] {result.archived} (notes marked stale in Memex)')
    if result.hard_deleted:
        console.print(f'[red]Deleted:[/red] {result.hard_deleted} (permanently removed from Memex)')
    if result.deleted_detected and no_handle_deletes:
        console.print(
            f'[yellow]{len(result.deleted_detected)} file(s) deleted from folder '
            f'(no action taken, use without --no-handle-deletes to archive)[/yellow]'
        )
    for err in result.errors:
        console.print(f'  [red]{err}[/red]')


@app.command(no_args_is_help=True)
def status(
    ctx: typer.Context,
    vault_path: Path = typer.Argument(..., help='Path to the notes folder'),
    config: Path | None = typer.Option(None, '--config', '-c', help='Path to config TOML'),
) -> None:
    """Show sync state and pending changes."""
    vault_path = vault_path.resolve()
    cfg = load_config(vault_path, config)
    memex_config: MemexConfig = ctx.obj

    db_path = vault_path / cfg.sync.state_file
    state = SyncStateDB(db_path)
    try:
        all_notes = scan_vault(vault_path, cfg.sync.exclude, cfg.sync.assets)
        tracked = state.get_all_files()
        changed, deleted, returning = diff(state, all_notes)

        table = Table(title='Sync Status')
        table.add_column('Metric', style='bold')
        table.add_column('Value')

        table.add_row('Path', str(vault_path))
        table.add_row('Last sync', state.last_sync or 'never')
        table.add_row('Total notes', str(len(all_notes)))
        table.add_row('Tracked', str(len(tracked)))
        table.add_row('Changed / new', str(len(changed)))
        table.add_row('Deleted from folder', str(len(deleted)))
        table.add_row('Returning (unarchive)', str(len(returning)))
        table.add_row(
            'Memex vault',
            state.vault_id or cfg.vault_id or memex_config.write_vault or '(default)',
        )

        console.print(table)

        if changed:
            console.print('\n[bold]Pending changes:[/bold]')
            for note in changed[:20]:
                status_label = 'new' if note.relative_path not in tracked else 'modified'
                asset_count = len(note.assets)
                suffix = f' (+{asset_count} assets)' if asset_count else ''
                console.print(f'  [{status_label}] {note.relative_path}{suffix}')
            if len(changed) > 20:
                console.print(f'  ... and {len(changed) - 20} more')

        if deleted:
            console.print(f'\n[yellow]Files deleted since last sync ({len(deleted)}):[/yellow]')
            for path in deleted[:10]:
                console.print(f'  {path}')
            if len(deleted) > 10:
                console.print(f'  ... and {len(deleted) - 10} more')
    finally:
        state.close()


@app.command('job', no_args_is_help=True)
@async_command
async def job_status(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help='Batch job ID returned by sync --background'),
) -> None:
    """Check the status of a background batch ingestion job."""
    memex_config: MemexConfig = ctx.obj
    async with get_api_context(memex_config) as api:
        job = await api.get_job_status(UUID(job_id))

    table = Table(title='Batch Job Status')
    table.add_column('Field', style='bold')
    table.add_column('Value')

    table.add_row('Job ID', str(job.job_id))
    table.add_row('Status', job.status)
    if job.progress:
        table.add_row('Progress', job.progress)
    if job.result:
        table.add_row('Processed', str(job.result.processed_count))
        table.add_row('Skipped', str(job.result.skipped_count))
        table.add_row('Failed', str(job.result.failed_count))
        if job.result.errors:
            table.add_row('Errors', str(len(job.result.errors)))

    console.print(table)


@app.command(no_args_is_help=True)
@async_command
async def watch(
    ctx: typer.Context,
    vault_path: Path = typer.Argument(..., help='Path to the notes folder'),
    config: Path | None = typer.Option(None, '--config', '-c', help='Path to config TOML'),
    mode: str | None = typer.Option(None, help='Override watch mode: events|poll'),
) -> None:
    """Watch folder for changes and sync continuously."""
    vault_path = vault_path.resolve()
    if not vault_path.is_dir():
        console.print(f'[red]Path does not exist: {vault_path}[/red]')
        raise typer.Exit(1)

    cfg = load_config(vault_path, config)
    if mode:
        cfg.watch.mode = WatchMode(mode)

    memex_config: MemexConfig = ctx.obj
    vault_id = cfg.vault_id or memex_config.write_vault
    api_key_str = memex_config.api_key.get_secret_value() if memex_config.api_key else None

    console.print(f'Watch mode: [cyan]{cfg.watch.mode.value}[/cyan]')
    await run_watcher(
        vault_path,
        sync_config=cfg.sync,
        watch_config=cfg.watch,
        server_url=memex_config.server_url,
        api_key=api_key_str,
        vault_id=vault_id,
    )


@app.command(no_args_is_help=True)
def init(
    vault_path: Path = typer.Argument(..., help='Path to the notes folder'),
) -> None:
    """Create a default note-sync.toml in the folder."""
    vault_path = vault_path.resolve()
    config_path = vault_path / CONFIG_FILENAME
    if config_path.exists():
        console.print(f'[yellow]Config already exists: {config_path}[/yellow]')
        raise typer.Exit(1)

    config_path.write_text(DEFAULT_CONFIG_TOML)
    console.print(f'[green]Created {config_path}[/green]')
    console.print('Edit the file to configure your sync settings.')
