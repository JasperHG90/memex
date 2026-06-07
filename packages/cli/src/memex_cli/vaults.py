"""
Vault Management Commands.
"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import emit_json, get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='vault',
    help='Manage Memex Vaults (scopes).',
    no_args_is_help=True,
)

snapshot_app = typer.Typer(
    name='snapshot',
    help='Vault snapshot export (one-way; downstream consumers).',
    no_args_is_help=True,
)
app.add_typer(snapshot_app, name='snapshot')


@app.command('list')
@async_command
async def list_vaults(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one vault name per line.')
    ] = False,
    compact: Annotated[
        bool,
        typer.Option('--compact', help='Output as a plain markdown table with note counts.'),
    ] = False,
):
    """
    List all available vaults.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        if compact:
            try:
                rows = await api.list_vaults_with_counts()
            except Exception as e:
                handle_api_error(e)

            has_access = any(row['vault'].access is not None for row in rows)
            if has_access:
                lines = [
                    '| Name | Notes | Last Modified | Active | Memory Worth | Access | Description |',
                    '|------|-------|---------------|--------|---------|--------|-------------|',
                ]
            else:
                lines = [
                    '| Name | Notes | Last Modified | Active | Memory Worth | Description |',
                    '|------|-------|---------------|--------|---------|-------------|',
                ]
            for row in rows:
                v = row['vault']
                count = row['note_count']
                last_mod_dt = row.get('last_note_added_at')
                last_mod = last_mod_dt.strftime('%Y-%m-%d') if last_mod_dt else '\u2014'
                active = 'yes' if v.is_active else ''
                mw_mode = v.mw_mode.replace('_', ' ') if v.mw_mode else '—'
                desc = v.description or ''
                if has_access:
                    access = ', '.join(v.access) if v.access else '\u2014'
                    lines.append(
                        f'| {v.name} | {count} | {last_mod} | {active} | {mw_mode} | {access} | {desc} |'
                    )
                else:
                    lines.append(
                        f'| {v.name} | {count} | {last_mod} | {active} | {mw_mode} | {desc} |'
                    )
            print('\n'.join(lines))
            return

        try:
            vaults = await api.list_vaults()
        except Exception as e:
            handle_api_error(e)

    if minimal:
        for v in vaults:
            console.print(v.name)
        return

    if json_output:
        emit_json([v.model_dump() for v in vaults])
        return

    has_access = any(v.access is not None for v in vaults)

    table = Table(title='Available Vaults')
    table.add_column('ID', style='dim')
    table.add_column('Name', style='cyan')
    table.add_column('Memory Worth', style='yellow')
    table.add_column('Description', style='white')
    if has_access:
        table.add_column('Access', style='green')

    if not vaults:
        console.print('[yellow]No vaults found.[/yellow]')
    else:
        for v in vaults:
            row = [str(v.id), v.name, v.mw_mode, v.description or '']
            if has_access:
                row.append(', '.join(v.access) if v.access else '\u2014')
            table.add_row(*row)
        console.print(table)

    # Show active from config
    console.print(f'\n[bold]Active Vault (Write):[/bold] {config.write_vault}')
    console.print(f'[bold]Read Vaults:[/bold] {config.read_vaults}')


@app.command('create')
@async_command
async def create_vault(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help='Name of the new vault.')],
    description: Annotated[
        str | None, typer.Option('--description', '-d', help='Optional description.')
    ] = None,
):
    """
    Create a new vault.
    """
    config: MemexConfig = ctx.obj

    console.print(f'[green]Creating vault:[/green] {name}')

    async with get_api_context(config) as api:
        try:
            vault = await api.create_vault(name, description)
        except Exception as e:
            handle_api_error(e)

    console.print(f'[bold green]Vault created successfully![/bold green] ID: {vault.id}')


@app.command('truncate')
@async_command
async def truncate_vault(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the vault to truncate.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Remove all content from a vault (notes, memories, entities, etc.).

    The vault itself is preserved. This is a destructive operation.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            vault_uuid = await api.resolve_vault_identifier(identifier)
        except Exception as e:
            handle_api_error(e)

        # Show what will be deleted
        try:
            stats = await api.get_stats_counts(vault_id=vault_uuid)
        except Exception as e:
            handle_api_error(e)

        console.print(f'\n[bold]Vault:[/bold] {identifier} ({vault_uuid})')
        console.print('[bold red]The following will be permanently deleted:[/bold red]')

        stat_table = Table(show_header=False, box=None, padding=(0, 2))
        stat_table.add_column(style='dim')
        stat_table.add_column(style='bold')
        stat_table.add_row('Notes', str(stats.notes))
        stat_table.add_row('Memory units', str(stats.memories))
        stat_table.add_row('Entities', str(stats.entities))
        stat_table.add_row('Reflection queue', str(stats.reflection_queue))
        console.print(stat_table)
        console.print()

        total = stats.notes + stats.memories + stats.entities + stats.reflection_queue
        if total == 0:
            console.print('[yellow]Vault is already empty.[/yellow]')
            return

        if not force:
            if not typer.confirm('Are you sure? This cannot be undone'):
                console.print('[yellow]Aborted.[/yellow]')
                return

        console.print(f'[red]Truncating vault:[/red] {identifier}...')
        try:
            counts = await api.truncate_vault(vault_uuid)
        except Exception as e:
            handle_api_error(e)

    console.print('[bold green]Vault truncated.[/bold green]')
    for label, count in counts.items():
        if count > 0:
            console.print(f'  {label}: [dim]{count} removed[/dim]')


@app.command('delete')
@async_command
async def delete_vault(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the vault to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete a vault.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            vault_uuid = await api.resolve_vault_identifier(identifier)
        except Exception as e:
            handle_api_error(e)

        if not force:
            if not typer.confirm(
                f'Are you sure you want to delete vault "{identifier}"? This is destructive.'
            ):
                console.print('[yellow]Aborted.[/yellow]')
                return

        console.print(f'[red]Deleting vault:[/red] {identifier} ({vault_uuid})')
        try:
            success = await api.delete_vault(vault_uuid)
        except Exception as e:
            handle_api_error(e)

    if success:
        console.print(f'[green]Vault "{identifier}" deleted successfully.[/green]')
    else:
        console.print(f'[red]Vault "{identifier}" not found.[/red]')


@app.command('summary')
@async_command
async def vault_summary(
    ctx: typer.Context,
    identifier: Annotated[
        str | None,
        typer.Argument(help='Name or UUID of the vault. Defaults to the active vault.'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    compact: Annotated[bool, typer.Option('--compact', help='Output as plain text.')] = False,
    regenerate: Annotated[
        bool, typer.Option('--regenerate', '-r', help='Regenerate the summary from all notes.')
    ] = False,
):
    """
    View or regenerate the vault summary.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        vault_name = identifier or config.write_vault
        try:
            vault_uuid = await api.resolve_vault_identifier(vault_name)
        except Exception as e:
            handle_api_error(e)

        if regenerate:
            console.print(f'[bold cyan]Regenerating summary for vault:[/bold cyan] {vault_name}')
            try:
                summary = await api.regenerate_vault_summary(vault_uuid)
            except Exception as e:
                handle_api_error(e)
            console.print('[bold green]Summary regenerated.[/bold green]')
        else:
            try:
                summary = await api.get_vault_summary(vault_uuid)
            except Exception as e:
                handle_api_error(e)

        if summary is None:
            console.print('[yellow]No summary exists for this vault yet.[/yellow]')
            console.print('Run with [bold]--regenerate[/bold] to generate one.')
            return

    if json_output:
        emit_json(summary.model_dump(exclude={'embedding'}))
        return

    if compact:
        console.print(summary.narrative)
        if summary.themes:
            console.print(f'\nThemes: {", ".join(t["name"] for t in summary.themes)}')
        return

    # Rich display
    from rich.panel import Panel
    from rich.markdown import Markdown

    console.print(
        Panel(
            Markdown(summary.narrative),
            title=f'Vault Summary — {vault_name} (v{summary.version})',
            subtitle=f'{summary.notes_incorporated} notes incorporated',
            border_style='cyan',
        )
    )

    if summary.themes:
        theme_table = Table(title='Themes')
        theme_table.add_column('Name', style='cyan')
        theme_table.add_column('Notes', style='dim', justify='right')
        theme_table.add_column('Trend', style='yellow')
        theme_table.add_column('Description', style='white')
        for t in summary.themes:
            theme_table.add_row(
                t.get('name', ''),
                str(t.get('note_count', '')),
                t.get('trend', ''),
                t.get('description', ''),
            )
        console.print(theme_table)

    if summary.inventory:
        inv = summary.inventory
        inv_table = Table(title='Inventory', show_header=False, box=None, padding=(0, 2))
        inv_table.add_column(style='dim')
        inv_table.add_column(style='bold')
        inv_table.add_row('Total Notes', str(inv.get('total_notes', 0)))
        inv_table.add_row('Total Entities', str(inv.get('total_entities', 0)))
        date_range = inv.get('date_range', {})
        if date_range.get('earliest'):
            inv_table.add_row(
                'Date Range', f'{date_range["earliest"]} to {date_range.get("latest", "?")}'
            )
        recent = inv.get('recent_activity', {})
        if recent:
            inv_table.add_row(
                'Recent (7d / 30d)', f'{recent.get("7d", 0)} / {recent.get("30d", 0)}'
            )
        last_activity_at = inv.get('last_activity_at')
        if last_activity_at:
            days_since = inv.get('days_since_last_note')
            suffix = f' ({days_since} days ago)' if days_since is not None else ''
            inv_table.add_row('Last Activity', f'{last_activity_at}{suffix}')
        by_template = inv.get('by_template', {})
        if by_template:
            inv_table.add_row(
                'Content Types', ', '.join(f'{v} {k}' for k, v in by_template.items())
            )
        console.print(inv_table)

    if summary.key_entities:
        ent_table = Table(title='Key Entities')
        ent_table.add_column('Name', style='cyan')
        ent_table.add_column('Type', style='dim')
        ent_table.add_column('Mentions', style='bold', justify='right')
        for ent in summary.key_entities[:10]:
            ent_table.add_row(
                ent.get('name', ''), ent.get('type', ''), str(ent.get('mention_count', 0))
            )
        console.print(ent_table)


@snapshot_app.command('export')
@async_command
async def snapshot_export(
    ctx: typer.Context,
    vault: Annotated[
        str,
        typer.Argument(help='Vault name or UUID to export.'),
    ],
    output: Annotated[
        Path,
        typer.Option('--output', '-o', help='Directory to write the snapshot into.'),
    ],
) -> None:
    """Export a vault snapshot to a local directory.

    Produces a self-describing snapshot (manifest.json + per-table JSONL +
    note bodies + assets) ready for downstream consumption (analytics,
    eval suites, ML pipelines). The output directory is created if it
    doesn't exist; existing files inside it may be overwritten.

    Refuses the global vault and any vault named 'global' / 'default'.
    """
    config: MemexConfig = ctx.obj

    try:
        import memex_core  # noqa: F401
    except ImportError:
        console.print(
            "[bold red]Error:[/bold red] Snapshot export requires the 'memex-cli[server]' extra "
            '(needs memex-core).'
        )
        raise typer.Exit(1)

    from memex_common.config import LitellmEmbeddingBackend, OnnxBackend
    from memex_core.memory.sql_models import EMBEDDING_DIMENSION
    from memex_core.services.snapshot import SnapshotExporter
    from memex_core.services.snapshot.exporter import SnapshotExportError
    from memex_core.services.snapshot.manifest import EmbeddingModelIdentity
    from memex_core.storage.filestore import get_filestore
    from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine

    # Resolve the vault selector (UUID or name) — accept either form.
    selector: str | UUID
    try:
        selector = UUID(vault)
    except ValueError:
        selector = vault

    # Path safety: refuse symlinks and verify the resolved path lies
    # outside privileged system directories. The CLI runs with the user's
    # privileges (no service-side trust), but a malicious or careless
    # --output ../../../etc would still corrupt user-writable system
    # files.
    output_path = output.expanduser().resolve()
    if output.is_symlink():
        console.print(f'[bold red]Refusing to export into a symlinked path:[/bold red] {output}')
        raise typer.Exit(1)
    for forbidden in ('/etc', '/usr', '/bin', '/sbin', '/proc', '/sys', '/dev', '/boot'):
        try:
            if output_path.is_relative_to(Path(forbidden)):
                console.print(
                    f'[bold red]Refusing to export into a system directory:[/bold red] {output_path}'
                )
                raise typer.Exit(1)
        except ValueError:
            continue
    output_path.mkdir(parents=True, exist_ok=True)

    # Build the embedding-model identity from the live server config so the
    # manifest reflects what was actually used to extract embeddings in the
    # source DB — not the registry default. V12 import refuses on mismatch.
    embedding_cfg = config.server.embedding_model
    if isinstance(embedding_cfg, OnnxBackend) or embedding_cfg is None:
        from memex_core.memory.models.base import MODEL_REGISTRY

        spec = MODEL_REGISTRY['embedding']
        embedding_identity = EmbeddingModelIdentity(
            name=str(spec.repo_id), dim=EMBEDDING_DIMENSION, hash=str(spec.revision)
        )
    elif isinstance(embedding_cfg, LitellmEmbeddingBackend):
        # The dim is unknown for LiteLLM backends ahead of a probe call;
        # record what we know and let V12 decide whether to refuse.
        embedding_identity = EmbeddingModelIdentity(
            name=f'litellm/{embedding_cfg.model}', dim=EMBEDDING_DIMENSION, hash=''
        )
    else:
        console.print(f'[bold red]Unknown embedding backend type:[/bold red] {type(embedding_cfg)}')
        raise typer.Exit(1)

    engine = AsyncPostgresMetaStoreEngine(config=config.server.meta_store)
    await engine.connect(create_schema=False)
    try:
        filestore = get_filestore(config.server.file_store)
        async with engine.session() as session:
            exporter = SnapshotExporter(
                session=session,
                filestore=filestore,
                vault_id_or_name=selector,
                output_dir=output_path,
                embedding_model=embedding_identity,
            )
            try:
                manifest = await exporter.export()
            except SnapshotExportError as e:
                console.print(f'[bold red]Snapshot export failed:[/bold red] {e}')
                raise typer.Exit(1) from e
        console.print(
            f'[green]Wrote snapshot[/green] (version {manifest.snapshot_version}) '
            f'for vault [bold]{manifest.source_vault_name}[/bold] '
            f'to [cyan]{output_path}[/cyan]'
        )
        for table, count in sorted(manifest.table_counts.items()):
            console.print(f'  {table}: {count}')
    finally:
        await engine.close()
