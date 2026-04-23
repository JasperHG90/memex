"""
Note Management Commands.
"""

import asyncio
import base64
import json
import mimetypes
import pathlib
from typing import Annotated, Any
import aiofiles
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.tree import Tree

from memex_common.config import MemexConfig
from memex_common.client import RemoteMemexAPI
from memex_common.templates import TemplateRegistry, BUILTIN_PROMPTS_DIR
from memex_common.schemas import (
    BatchJobStatus,
    IngestResponse,
    NoteCreateDTO,
    NoteDTO,
    IngestURLRequest,
)
import httpx

from memex_cli.utils import get_api_context, async_command, handle_api_error, parse_uuid

console = Console()

from memex_cli.assets import app as assets_app

app = typer.Typer(
    name='note',
    help='Manage and view source notes.',
    no_args_is_help=True,
)
app.add_typer(assets_app)

try:
    from memex_cli.sync import app as sync_app

    app.add_typer(sync_app)
except ImportError:
    _sync_stub = typer.Typer(
        name='sync',
        help='Sync a folder of Markdown notes to Memex. (requires: pip install memex-cli[sync])',
        invoke_without_command=True,
    )

    @_sync_stub.callback(invoke_without_command=True)
    def _sync_not_installed(ctx: typer.Context) -> None:
        console.print(
            '[bold red]Error:[/bold red] Missing dependencies for note sync.\n'
            'Install with: [cyan]pip install memex-cli\\[sync][/cyan]'
        )
        raise typer.Exit(1)

    app.add_typer(_sync_stub)


@app.command('add')
@async_command
async def add_note(
    ctx: typer.Context,
    content: Annotated[str | None, typer.Argument(help='The content of the note to add.')] = None,
    file: Annotated[
        pathlib.Path | None,
        typer.Option('--file', '-f', help='Path to a file or directory to ingest.', dir_okay=True),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option('--url', '-u', help='URL to scrape and ingest.'),
    ] = None,
    asset: Annotated[
        list[pathlib.Path] | None,
        typer.Option('--asset', '-a', help='Path to an asset file to attach to the note.'),
    ] = None,
    vault: Annotated[
        str | None, typer.Option('--vault', '-v', help='Target vault (write).')
    ] = None,
    key: Annotated[
        str | None, typer.Option('--key', '-k', help='Unique stable key for the note.')
    ] = None,
    background: Annotated[
        bool, typer.Option('--background', '-b', help='Queue as background job.')
    ] = False,
    user_notes: Annotated[
        str | None,
        typer.Option('--user-notes', '-n', help='Your own context or commentary about this note.'),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option('--title', '-t', help='Note title (default: "Quick Note" for inline).'),
    ] = None,
    description_opt: Annotated[
        str | None,
        typer.Option('--description', help='Note description/summary.'),
    ] = None,
    author: Annotated[
        str | None,
        typer.Option('--author', help='Author name.'),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option('--tag', help='Tag for the note. Can be repeated.'),
    ] = None,
    date: Annotated[
        str | None,
        typer.Option('--date', '-d', help='Note date in ISO 8601 format (e.g. 2026-03-15).'),
    ] = None,
    template: Annotated[
        str | None,
        typer.Option('--template', help='Template slug used to create this note.'),
    ] = None,
):
    """
    Add a new note to Memex.
    You can provide text directly, use --file to load from disk, or --url to scrape a website.
    Use --asset to attach auxiliary files (images, PDFs) to a note.
    """
    config: MemexConfig = ctx.obj
    # Override active vault if specified
    if vault:
        config.vault.active = vault

    # Determine input source
    if file:
        file_path = file
        if not file_path.exists():
            console.print(f'[red]Error: Path does not exist: {file_path}[/red]')
            raise typer.Exit(1)
    elif url:
        pass  # Valid input
    elif content:
        pass
    else:
        console.print('[red]Error: Must provide content, --file, or --url.[/red]')
        raise typer.Exit(1)

    console.print('[bold green]Adding Note[/bold green]')

    async with get_api_context(config) as api:
        result: IngestResponse | BatchJobStatus | dict[str, str]
        if url:
            try:
                # Load assets if provided
                assets_dict = {}
                if asset:
                    console.print(f'[cyan]Loading {len(asset)} asset(s)...[/cyan]')
                    for asset_path in asset:
                        if not asset_path.exists():
                            console.print(f'[red]Warning: Asset not found: {asset_path}[/red]')
                            continue

                        async with aiofiles.open(asset_path, 'rb') as f:
                            asset_data = await f.read()

                        assets_dict[asset_path.name] = base64.b64encode(asset_data)

                console.print(f'[cyan]Fetching and summarizing {url}...[/cyan]')
                req = IngestURLRequest(
                    url=url,
                    assets=assets_dict,
                    vault_id=config.write_vault,
                    user_notes=user_notes,
                )
                result = await api.ingest_url(req, background=background)
            except Exception as e:
                handle_api_error(e)
        elif file and not asset:
            # Multi-part upload using aiofiles (Traditional path)
            try:
                files_to_upload = []
                if file.is_dir():
                    console.print(f'[cyan]Scanning directory {file.name}...[/cyan]')
                    # Recursively find all files
                    for p in file.rglob('*'):
                        if p.is_file() and not p.name.startswith('.'):
                            async with aiofiles.open(p, 'rb') as f:
                                data = await f.read()

                            mime_type, _ = mimetypes.guess_type(p)
                            mime_type = mime_type or 'application/octet-stream'
                            # Use relative path as filename to preserve structure
                            rel_path = str(p.relative_to(file))
                            files_to_upload.append(('files', (rel_path, data, mime_type)))
                else:
                    console.print(f'[cyan]Reading file {file.name}...[/cyan]')
                    async with aiofiles.open(file, 'rb') as f:
                        data = await f.read()
                    mime_type, _ = mimetypes.guess_type(file)
                    mime_type = mime_type or 'application/octet-stream'
                    files_to_upload.append(('files', (file.name, data, mime_type)))

                if not files_to_upload:
                    console.print('[red]Error: No files found to upload.[/red]')
                    raise typer.Exit(1)

                console.print(
                    f'[cyan]Uploading and summarizing {len(files_to_upload)} file(s)...[/cyan]'
                )
                metadata = {}
                if config.write_vault:
                    metadata['vault_id'] = str(config.write_vault)
                if user_notes:
                    metadata['user_notes'] = user_notes

                result = await api.ingest_upload(
                    files=files_to_upload, metadata=metadata, background=background
                )
            except Exception as e:
                handle_api_error(e)
        else:
            # Handle NoteDTO path (content + assets or file + assets)
            try:
                note_content = ''
                note_name = title or 'Quick Note'
                note_description = description_opt or 'Added via CLI'

                if file:
                    if file.is_dir():
                        console.print(
                            '[red]Error: --asset cannot be used with a directory --file. Point --file to a markdown file instead.[/red]'
                        )
                        raise typer.Exit(1)

                    console.print(f'[cyan]Reading main note file {file.name}...[/cyan]')
                    async with aiofiles.open(file, 'r', encoding='utf-8') as f:
                        note_content = await f.read()
                    if not title:
                        note_name = file.stem
                else:
                    note_content = content or ''

                # Build frontmatter for inline content when metadata is provided
                fm_data: dict[str, Any] = {}
                if title:
                    fm_data['title'] = note_name
                if date:
                    fm_data['date'] = date
                if author:
                    fm_data['author'] = author
                if description_opt:
                    fm_data['description'] = note_description

                default_tags = ['cli', 'note-with-assets'] if asset else ['cli', 'quick-note']
                effective_tags = list(tag or []) + default_tags
                if tag:
                    fm_data['tags'] = effective_tags

                if fm_data:
                    import yaml

                    fm_yaml = yaml.safe_dump(fm_data, sort_keys=False).strip()
                    note_content = f'---\n{fm_yaml}\n---\n\n{note_content}'

                # Load assets
                assets_dict = {}
                if asset:
                    console.print(f'[cyan]Loading {len(asset)} asset(s)...[/cyan]')
                    for asset_path in asset:
                        if not asset_path.exists():
                            console.print(f'[red]Warning: Asset not found: {asset_path}[/red]')
                            continue

                        async with aiofiles.open(asset_path, 'rb') as f:
                            asset_data = await f.read()

                        assets_dict[asset_path.name] = base64.b64encode(asset_data)

                note = NoteCreateDTO(
                    name=note_name,
                    description=note_description,
                    content=base64.b64encode(note_content.encode('utf-8')),
                    files=assets_dict,
                    tags=effective_tags,
                    note_key=key,
                    vault_id=config.write_vault,
                    user_notes=user_notes,
                    author=author,
                    template=template,
                )

                result = await api.ingest(note, background=background)
            except Exception as e:
                handle_api_error(e)

        # 4. Show Result
        if isinstance(result, BatchJobStatus):
            console.print(f'[bold green]Queued.[/bold green] Job ID: [cyan]{result.job_id}[/cyan]')
            console.print(f'[dim]Poll: GET /api/v1/ingestions/{result.job_id}[/dim]')
        elif isinstance(result, dict):
            # Fire-and-forget background (url/upload): server accepted but no job ID
            console.print('[bold green]Accepted.[/bold green] Ingestion running in background.')
        elif result.status == 'skipped':
            console.print(f'[yellow]Note skipped: {result.reason}[/yellow]')
        else:
            console.print(f'[green]Note added successfully![/green] UUID: {result.note_id}')
            if result.unit_ids:
                console.print(f'Extracted {len(result.unit_ids)} memory units.')


@app.command('list')
@async_command
async def list_notes(
    ctx: typer.Context,
    limit: int = 50,
    offset: int = 0,
    vault: Annotated[
        list[str],
        typer.Option('--vault', '-v', help='Vault(s) to filter by. Use "*" for all vaults.'),
    ] = [],
    after: Annotated[
        str | None,
        typer.Option('--after', help='Only notes on/after this date (ISO 8601).'),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option('--before', help='Only notes on/before this date (ISO 8601).'),
    ] = None,
    date_by: Annotated[
        str,
        typer.Option(
            '--date-by',
            help=(
                "Which date column --after/--before filter on: 'created_at' "
                "(ingest time, default), 'publish_date' (authored date), or "
                "'coalesce' (publish_date if set, else created_at)."
            ),
        ),
    ] = 'created_at',
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one note ID per line.')
    ] = False,
    compact: Annotated[
        bool, typer.Option('--compact', help='One line per note: title, date, description.')
    ] = False,
    template: Annotated[
        str | None,
        typer.Option('--template', help='Filter by template slug (e.g. "general_note").'),
    ] = None,
):
    """
    List all notes.
    """
    from datetime import datetime

    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    config: MemexConfig = ctx.obj

    if date_by not in ('coalesce', 'created_at', 'publish_date'):
        console.print(
            f'[red]Invalid --date-by {date_by!r}. Expected one of: '
            "'coalesce', 'created_at', 'publish_date'[/red]"
        )
        raise typer.Exit(code=1)

    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            console.print(f'[red]Invalid --after date: {after}[/red]')
            raise typer.Exit(code=1)
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            console.print(f'[red]Invalid --before date: {before}[/red]')
            raise typer.Exit(code=1)

    if vault and ALL_VAULTS_WILDCARD in vault:
        vault_ids = None
    else:
        vault_ids = vault if vault else config.read_vaults

    async with get_api_context(config) as api:
        try:
            notes = await api.list_notes(
                limit=limit,
                offset=offset,
                vault_ids=vault_ids,
                after=parsed_after,
                before=parsed_before,
                template=template,
                date_field=date_by,
            )
        except Exception as e:
            handle_api_error(e)

    if minimal:
        for d in notes:
            console.print(str(d.id))
        return

    if compact:
        for d in notes:
            _print_compact_note(d)
        return

    if json_output:
        console.print_json(json.dumps([d.model_dump() for d in notes], default=str))
        return

    table = Table(title='Notes')
    table.add_column('Title', style='cyan')
    table.add_column('Vault', style='yellow')
    table.add_column('Publish Date', style='green')
    table.add_column('Created At', style='dim')
    table.add_column('ID', style='dim')

    for d in notes:
        pub_date = ''
        if hasattr(d, 'publish_date') and d.publish_date:
            pub_date = str(d.publish_date.date())
        vault_name = getattr(d, 'vault_name', '') or ''
        table.add_row(d.name or 'Untitled', vault_name, pub_date, str(d.created_at), str(d.id))

    console.print(table)


@app.command('recent')
@async_command
async def list_recent(
    ctx: typer.Context,
    limit: int = 10,
    vault: Annotated[
        list[str],
        typer.Option('--vault', '-v', help='Vault(s) to filter by. Use "*" for all vaults.'),
    ] = [],
    after: Annotated[
        str | None,
        typer.Option('--after', help='Only notes on/after this date (ISO 8601).'),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option('--before', help='Only notes on/before this date (ISO 8601).'),
    ] = None,
    date_by: Annotated[
        str,
        typer.Option(
            '--date-by',
            help=(
                "Which date column --after/--before filter on: 'created_at' "
                "(ingest time, default), 'publish_date' (authored date), or "
                "'coalesce' (publish_date if set, else created_at)."
            ),
        ),
    ] = 'created_at',
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one note ID per line.')
    ] = False,
    compact: Annotated[
        bool, typer.Option('--compact', help='One line per note: title, date, description.')
    ] = False,
):
    """
    Show most recent notes.
    """
    from datetime import datetime

    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    config: MemexConfig = ctx.obj

    if date_by not in ('coalesce', 'created_at', 'publish_date'):
        console.print(
            f'[red]Invalid --date-by {date_by!r}. Expected one of: '
            "'coalesce', 'created_at', 'publish_date'[/red]"
        )
        raise typer.Exit(code=1)

    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            console.print(f'[red]Invalid --after date: {after}[/red]')
            raise typer.Exit(code=1)
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            console.print(f'[red]Invalid --before date: {before}[/red]')
            raise typer.Exit(code=1)

    if vault and ALL_VAULTS_WILDCARD in vault:
        vault_ids = None
    else:
        vault_ids = vault if vault else None

    async with get_api_context(config) as api:
        try:
            notes = await api.get_recent_notes(
                limit=limit,
                vault_ids=vault_ids,
                after=parsed_after,
                before=parsed_before,
                date_field=date_by,
            )
        except Exception as e:
            handle_api_error(e)

    if minimal:
        for d in notes:
            console.print(str(d.id))
        return

    if compact:
        for d in notes:
            _print_compact_note(d)
        return

    if json_output:
        console.print_json(json.dumps([d.model_dump() for d in notes], default=str))
        return

    table = Table(title='Recent Notes')
    table.add_column('Title', style='cyan')
    table.add_column('Vault', style='yellow')
    table.add_column('Publish Date', style='green')
    table.add_column('Created At', style='dim')
    table.add_column('ID', style='dim')

    for d in notes:
        pub_date = ''
        if hasattr(d, 'publish_date') and d.publish_date:
            pub_date = str(d.publish_date.date())
        vault_name = getattr(d, 'vault_name', '') or ''
        table.add_row(d.name or 'Untitled', vault_name, pub_date, str(d.created_at), str(d.id))

    console.print(table)


def _print_compact_note(d: Any) -> None:
    """Print a single note in compact one-line format."""
    title = d.title or d.name or 'Untitled'
    note_id = str(d.id) if d.id else ''
    date = str(d.created_at.date()) if d.created_at else 'unknown'
    vault_name = getattr(d, 'vault_name', '') or ''
    vault_tag = f' @{vault_name}' if vault_name else ''
    desc = getattr(d, 'description', '') or ''
    if len(desc) > 120:
        desc = desc[:117] + '...'
    suffix = f': {desc}' if desc else ''
    console.print(f'- **{title}**{vault_tag} ({date}) [{note_id}]{suffix}')


@app.command('find')
@async_command
async def find_note(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Approximate title to search for.')],
    limit: int = 5,
    vault: Annotated[
        list[str],
        typer.Option('--vault', '-v', help='Vault(s) to filter by. Use "*" for all vaults.'),
    ] = [],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Find notes by approximate title match (trigram similarity).
    """
    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    config: MemexConfig = ctx.obj
    vault_ids = None if (vault and ALL_VAULTS_WILDCARD in vault) else (vault or None)
    async with get_api_context(config) as api:
        try:
            results = await api.find_notes_by_title(
                query=query,
                vault_ids=vault_ids,
                limit=limit,
            )
        except Exception as e:
            handle_api_error(e)

    if not results:
        console.print('[dim]No matching notes found.[/dim]')
        return

    if json_output:
        console.print_json(json.dumps([r.model_dump() for r in results], default=str))
        return

    table = Table(title=f'Notes matching "{query}"')
    table.add_column('Title', style='cyan')
    table.add_column('Score', style='yellow', justify='right')
    table.add_column('Date', style='green')
    table.add_column('Status', style='dim')
    table.add_column('Note ID', style='dim')

    for r in results:
        date = r.publish_date or r.created_at
        if hasattr(date, 'date'):
            date = str(date.date())
        else:
            date = str(date)[:10] if date else ''
        table.add_row(
            r.title or 'Untitled',
            f'{r.score:.2f}',
            date,
            r.status or '',
            str(r.note_id),
        )

    console.print(table)


@app.command('links')
@async_command
async def note_links(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of the note.')],
    link_type: Annotated[
        str | None,
        typer.Option('--type', '-t', help='Filter by link type (e.g. contradicts).'),
    ] = None,
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max links to return.')] = 20,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View relationship links for a note (aggregated from its memory units).
    Shows temporal, semantic, causal, contradiction, and other typed links.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            links = await api.get_note_links(uuid_obj, link_type=link_type, limit=limit)
        except Exception as e:
            handle_api_error(e)
            return

    if not links:
        console.print('[dim]No links found.[/dim]')
        return

    if json_output:
        console.print_json(json.dumps([lnk.model_dump() for lnk in links], default=str))
        return

    table = Table(title=f'Links for note {note_id[:8]}...')
    table.add_column('Relation', style='cyan')
    table.add_column('Target Unit', style='dim')
    table.add_column('Note Title', style='white')
    table.add_column('Weight', style='magenta', justify='right')
    table.add_column('Time', style='dim')

    for lnk in links:
        table.add_row(
            lnk.relation,
            str(lnk.unit_id)[:8] + '...',
            lnk.note_title or '-',
            f'{lnk.weight:.2f}',
            str(lnk.time)[:10] if lnk.time else '-',
        )

    console.print(table)


@app.command('delete')
@async_command
async def delete_note(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete a note and all associated data (memory units, chunks, links, assets).
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    if not force:
        if not typer.confirm(
            f'Are you sure you want to delete note {note_id}? This is destructive.'
        ):
            console.print('[yellow]Aborted.[/yellow]')
            return

    async with get_api_context(config) as api:
        try:
            success = await api.delete_note(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if success:
        console.print(f'[green]Note {note_id} deleted successfully.[/green]')
    else:
        console.print(f'[red]Note {note_id} not found.[/red]')


@app.command('update-date')
@async_command
async def update_date(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to update.')],
    new_date: Annotated[
        str, typer.Argument(help='New date (ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).')
    ],
):
    """
    Update a note's publish_date and cascade the delta to all memory unit timestamps.
    """
    from datetime import datetime

    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    try:
        parsed_date = datetime.fromisoformat(new_date)
    except ValueError:
        console.print(
            f'[red]Invalid date format: {new_date}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS.[/red]'
        )
        raise typer.Exit(code=1)

    async with get_api_context(config) as api:
        try:
            result = await api.update_note_date(uuid_obj, parsed_date)
        except Exception as e:
            handle_api_error(e)
            return

    console.print('[green]Note date updated successfully.[/green]')
    console.print(f'  Old date: {result.get("old_date")}')
    console.print(f'  New date: {result.get("new_date")}')
    console.print(f'  Memory units updated: {result.get("units_updated", 0)}')


@app.command('rename')
@async_command
async def rename_note(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to rename.')],
    new_title: Annotated[str, typer.Argument(help='New title for the note.')],
):
    """
    Rename a note (updates title in metadata, page index, and doc_metadata).
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            await api.update_note_title(uuid_obj, new_title)
        except Exception as e:
            handle_api_error(e)
            return

    console.print(f'[green]Note {note_id} renamed to "{new_title}".[/green]')


@app.command('migrate')
@async_command
async def migrate_note(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to migrate.')],
    target_vault: Annotated[str, typer.Argument(help='Target vault name or UUID.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Move a note and all associated data to a different vault.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    if not force:
        if not typer.confirm(f'Migrate note {note_id} to vault "{target_vault}"?'):
            console.print('[yellow]Aborted.[/yellow]')
            return

    async with get_api_context(config) as api:
        try:
            result = await api.migrate_note(uuid_obj, target_vault)
        except Exception as e:
            handle_api_error(e)
            return

    if result.get('status') == 'noop':
        console.print(
            f'[yellow]Note {note_id} is already in vault "{target_vault}". No changes made.[/yellow]'
        )
    else:
        console.print(f'[green]Note {note_id} migrated successfully.[/green]')
    console.print(f'  Source vault: {result.get("source_vault_id")}')
    console.print(f'  Target vault: {result.get("target_vault_id")}')
    console.print(f'  Entities affected: {result.get("entities_affected", 0)}')


@app.command('view')
@async_command
async def view_note(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View content and metadata of a note.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            note = await api.get_note(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(note.model_dump(), default=str))
        return

    name = note.name or 'Untitled Note'
    note_id_val = note.id
    created_at = note.created_at
    doc_metadata = note.doc_metadata
    original_text = note.original_text or ''

    console.print(f'\n[bold cyan]{name}[/bold cyan]')
    console.print(f'[dim]ID: {note_id_val}[/dim]')
    console.print(f'[dim]Created: {created_at}[/dim]')

    if doc_metadata:
        console.print('\n[bold]Metadata:[/bold]')
        console.print(doc_metadata)

    console.print('\n[bold]Content:[/bold]')
    console.print(Markdown(original_text))


def _render_metadata_table(metadata: dict[str, Any], note_id: str) -> None:
    """Render a metadata dict as a Rich table."""
    table = Table(title=f'Note Metadata ({note_id})')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='white')

    for key, value in metadata.items():
        if isinstance(value, list):
            value = ', '.join(str(v) for v in value)
        table.add_row(key, str(value) if value is not None else '-')

    console.print(table)


@app.command('metadata')
@async_command
async def view_metadata(
    ctx: typer.Context,
    note_ids: Annotated[list[str], typer.Argument(help='One or more note UUIDs.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View the metadata (title, description, tags, etc.) of one or more notes."""
    config: MemexConfig = ctx.obj
    uuids = [parse_uuid(nid, 'note') for nid in note_ids]

    async with get_api_context(config) as api:
        try:
            if len(uuids) == 1:
                metadata_list = [await api.get_note_metadata(uuids[0])]
            else:
                metadata_list = await api.get_notes_metadata(uuids)
        except Exception as e:
            handle_api_error(e)
            return

    if len(uuids) == 1:
        metadata = metadata_list[0] if metadata_list else None
        if metadata is None:
            console.print('[yellow]This note has no metadata.[/yellow]')
            console.print('[dim]Only notes with a page index have metadata.[/dim]')
            return
        if json_output:
            console.print_json(json.dumps(metadata, default=str))
            return
        _render_metadata_table(metadata, note_ids[0])
        return

    # Multiple notes
    if not metadata_list:
        console.print('[yellow]No metadata found for any of the provided notes.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps(metadata_list, default=str))
        return

    for i, metadata in enumerate(metadata_list):
        if i > 0:
            console.print(Rule())
        nid = metadata.get('note_id', note_ids[i] if i < len(note_ids) else '?')
        _render_metadata_table(metadata, str(nid))


@app.command('page-index')
@async_command
async def view_page_index(
    ctx: typer.Context,
    note_ids: Annotated[list[str], typer.Argument(help='One or more note UUIDs.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View the page index (slim tree) of one or more notes."""
    config: MemexConfig = ctx.obj
    uuids = [parse_uuid(nid, 'note') for nid in note_ids]

    async with get_api_context(config) as api:
        try:
            if len(uuids) == 1:
                results = [(note_ids[0], await api.get_note_page_index(uuids[0]))]
            else:
                raw = await asyncio.gather(
                    *[api.get_note_page_index(uid) for uid in uuids],
                    return_exceptions=True,
                )
                results = []
                for nid, r in zip(note_ids, raw):
                    if isinstance(r, Exception):
                        console.print(f'[red]Error fetching page index for {nid}: {r}[/red]')
                    else:
                        results.append((nid, r))
                if not results:
                    return
        except Exception as e:
            handle_api_error(e)
            return

    if len(results) == 1:
        nid, page_index = results[0]
        if page_index is None:
            console.print('[yellow]This note has no page index.[/yellow]')
            console.print(
                '[dim]Only notes ingested with page_index strategy have a slim tree.[/dim]'
            )
            return
        if json_output:
            console.print_json(json.dumps(page_index, default=str))
            return
        nodes = page_index if isinstance(page_index, list) else page_index.get('toc', [])
        tree = Tree(f'[bold cyan]Page Index[/bold cyan] [dim]({nid})[/dim]')
        _render_toc_nodes(nodes, tree)
        console.print(tree)
        return

    # Multiple notes
    if json_output:
        out = []
        for nid, pi in results:
            out.append({'note_id': nid, 'page_index': pi})
        console.print_json(json.dumps(out, default=str))
        return

    for i, (nid, page_index) in enumerate(results):
        if i > 0:
            console.print(Rule())
        if page_index is None:
            console.print(f'[yellow]Note {nid} has no page index.[/yellow]')
            continue
        nodes = page_index if isinstance(page_index, list) else page_index.get('toc', [])
        tree = Tree(f'[bold cyan]Page Index[/bold cyan] [dim]({nid})[/dim]')
        _render_toc_nodes(nodes, tree)
        console.print(tree)


def _render_node(node: Any) -> None:
    """Render a single node to the console."""
    title = node.title or '(untitled)'
    heading = '#' * node.level
    console.print(f'\n[bold cyan]{heading} {title}[/bold cyan]')
    console.print(f'[dim]Node ID: {node.id}[/dim]')
    console.print(f'[dim]Note ID: {node.note_id}[/dim]')
    console.print(f'[dim]Level: {node.level} | Seq: {node.seq} | Status: {node.status}[/dim]')

    if node.text:
        console.print()
        console.print(Panel(Markdown(node.text), title='Content', border_style='green'))
    else:
        console.print('\n[dim][No text content][/dim]')


@app.command('node')
@async_command
async def view_node(
    ctx: typer.Context,
    node_ids: Annotated[list[str], typer.Argument(help='One or more node UUIDs.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View one or more page-index nodes (sections) by ID."""
    config: MemexConfig = ctx.obj
    uuids = [parse_uuid(nid, 'node') for nid in node_ids]

    async with get_api_context(config) as api:
        try:
            if len(uuids) == 1:
                node = await api.get_node(uuids[0])
                nodes = [node] if node else []
            else:
                nodes = await api.get_nodes(uuids)
        except Exception as e:
            handle_api_error(e)
            return

    if not nodes:
        console.print('[yellow]No nodes found.[/yellow]')
        return

    if json_output:
        if len(uuids) == 1:
            console.print_json(json.dumps(nodes[0].model_dump(), default=str))
        else:
            console.print_json(json.dumps([n.model_dump() for n in nodes], default=str))
        return

    for i, node in enumerate(nodes):
        if i > 0:
            console.print(Rule())
        _render_node(node)


def _render_toc_nodes(nodes: list[dict[str, Any]], parent: Tree) -> None:
    """Recursively add TOC nodes to a Rich Tree."""
    for node in nodes:
        level = node.get('level', 1)
        title = node.get('title', '(untitled)')
        tokens = node.get('token_estimate') or 0
        label = f'[bold]{"#" * level}[/bold] {title}'
        if tokens:
            label += f' [dim]({tokens} tokens)[/dim]'
        summary = node.get('summary') or {}
        if what := summary.get('what'):
            label += f'\n  [dim italic]{what}[/dim italic]'
        branch = parent.add(label)
        _render_toc_nodes(node.get('children', []), branch)


@app.command('search')
@async_command
async def search_notes(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Search query.')],
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max number of notes.')] = 5,
    expand: Annotated[bool, typer.Option('--expand', help='Enable query expansion.')] = False,
    blend: Annotated[bool, typer.Option('--blend', help='Enable position-aware blending.')] = False,
    vault: Annotated[
        list[str],
        typer.Option('--vault', '-v', help='Vault(s) to search. Use "*" for all vaults.'),
    ] = [],
    reason: Annotated[
        bool,
        typer.Option('--reason', help='Run skeleton-tree identification; shows relevant sections.'),
    ] = False,
    summarize: Annotated[
        bool,
        typer.Option('--summarize', help='Synthesize a full answer (implies --reason).'),
    ] = False,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[bool, typer.Option('--minimal', help='Output note IDs only.')] = False,
    no_semantic: Annotated[
        bool, typer.Option('--no-semantic', help='Exclude semantic (vector) strategy.')
    ] = False,
    no_keyword: Annotated[
        bool, typer.Option('--no-keyword', help='Exclude keyword (BM25) strategy.')
    ] = False,
    no_graph: Annotated[
        bool, typer.Option('--no-graph', help='Exclude graph (entity) strategy.')
    ] = False,
    no_temporal: Annotated[
        bool, typer.Option('--no-temporal', help='Exclude temporal strategy.')
    ] = False,
    reference_date: Annotated[
        str | None,
        typer.Option(
            '--reference-date',
            help=(
                'ISO-8601 timestamp for resolving relative dates '
                '(e.g. "last week"). Defaults to now.'
            ),
        ),
    ] = None,
):
    """
    Search for notes using multi-channel fusion (RRF).
    """
    config: MemexConfig = ctx.obj
    fusion_strategy = 'position_aware' if blend else 'rrf'

    # Compute strategy inclusion list from exclusion flags
    all_strategies = ['semantic', 'keyword', 'graph', 'temporal']
    exclusions = {
        'semantic': no_semantic,
        'keyword': no_keyword,
        'graph': no_graph,
        'temporal': no_temporal,
    }
    active = [s for s in all_strategies if not exclusions[s]]
    strategies: list[str] | None = active if len(active) < len(all_strategies) else None

    if strategies is not None:
        console.print(f'[dim]Active strategies: {", ".join(strategies)}[/dim]')

    vault_ids = vault if vault else config.read_vaults

    from datetime import datetime as _dt, timezone as _tz

    ref_dt = _dt.fromisoformat(reference_date).replace(tzinfo=_tz.utc) if reference_date else None

    async with get_api_context(config) as api:
        try:
            results = await api.search_notes(
                query=query,
                limit=limit,
                vault_ids=vault_ids,
                expand_query=expand,
                fusion_strategy=fusion_strategy,
                strategies=strategies,
                reason=reason,
                summarize=summarize,
                reference_date=ref_dt,
            )
        except Exception as e:
            handle_api_error(e)
            return

    if not results:
        console.print('[yellow]No notes found.[/yellow]')
        return

    if minimal:
        for doc in results:
            console.print(str(doc.note_id))
        return

    if json_output:
        console.print_json(json.dumps([r.model_dump() for r in results], default=str))
        return

    table = Table(title=f'Search Results: "{query}"', show_lines=True)
    table.add_column('Score', style='magenta', justify='right', no_wrap=True)
    table.add_column('Title', style='cyan', ratio=2)
    table.add_column('Preview', style='white', ratio=4)
    table.add_column('ID', style='dim', no_wrap=True)

    for doc in results:
        metadata = doc.metadata or {}
        title = (
            metadata.get('name') or metadata.get('title') or metadata.get('filename') or 'Untitled'
        )

        # Build preview from block summaries
        if doc.summaries:
            parts = [s.topic for s in doc.summaries]
            preview = ' | '.join(parts) if parts else '[No preview available]'
        else:
            preview = '[No preview available]'

        # Truncate preview if it's excessively long (though Rich wraps, this keeps it cleaner)
        if len(preview) > 300:
            preview = preview[:297] + '...'

        score_str = f'{doc.score:.2f}' if doc.score > 0 else '-'

        table.add_row(score_str, title, preview, str(doc.note_id))

    console.print(table)
    console.print('\n[dim]Tip: Use `memex note view <ID>` to see full note.[/dim]')

    # Display relevant sections when --reason (but not --summarize)
    if reason and not summarize:
        has_reasoning = any(r.reasoning for r in results)
        if has_reasoning:
            console.print('\n[bold]Relevant sections:[/bold]')
            for doc in results:
                if not doc.reasoning:
                    continue
                metadata = doc.metadata or {}
                doc_title = (
                    metadata.get('name')
                    or metadata.get('title')
                    or metadata.get('filename')
                    or 'Untitled'
                )
                for item in doc.reasoning:
                    node_uuid = item.get('node_uuid', '')
                    reasoning_text = item.get('reasoning', '')
                    node_ref = f'[dim]({node_uuid})[/dim] ' if node_uuid else ''
                    console.print(f'  [cyan]{doc_title}[/cyan] → {node_ref}"{reasoning_text}"')
            console.print(
                "\n[dim]Tip: Use `memex note node <node-uuid>` to view a section's full text.[/dim]"
            )

    # Display LLM-synthesized answer when --summarize is used
    if summarize:
        answer_text = next((r.answer for r in results if r.answer), None)
        if answer_text:
            console.print(Panel(Markdown(answer_text), title='Answer', border_style='green'))
            console.print()


def _sanitize_filename(name: str) -> str:
    """Convert a note title to a safe filename."""
    # Replace path-unsafe chars with underscores
    safe = name.replace('/', '_').replace('\\', '_').replace(':', '_')
    safe = safe.replace('<', '_').replace('>', '_').replace('"', '_')
    safe = safe.replace('|', '_').replace('?', '_').replace('*', '_')
    # Collapse runs of underscores / whitespace
    import re

    safe = re.sub(r'[\s_]+', '_', safe).strip('_')
    return safe[:200] if safe else 'untitled'


async def _export_note(
    api: RemoteMemexAPI,
    note: NoteDTO,
    output_dir: pathlib.Path,
) -> int:
    """Export a single note and its assets to output_dir.

    Returns the number of assets exported.
    """
    title = note.title or note.name or 'Untitled'
    safe_name = _sanitize_filename(title)
    note_dir = output_dir / f'{safe_name}_{note.id}'
    note_dir.mkdir(parents=True, exist_ok=True)

    # Write markdown content
    content = note.original_text or ''
    (note_dir / 'note.md').write_text(content, encoding='utf-8')

    # Write metadata
    metadata = {
        'id': str(note.id),
        'title': title,
        'created_at': str(note.created_at),
        'vault_id': str(note.vault_id),
        'doc_metadata': note.doc_metadata,
    }
    (note_dir / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, default=str), encoding='utf-8'
    )

    # Export assets
    asset_count = 0
    if note.assets:
        assets_dir = note_dir / 'assets'
        assets_dir.mkdir(exist_ok=True)
        for asset_path in note.assets:
            try:
                data = await api.get_resource(asset_path)
                # Use the filename from the asset path
                filename = pathlib.PurePosixPath(asset_path).name
                (assets_dir / filename).write_bytes(data)
                asset_count += 1
            except Exception as e:
                console.print(f'[yellow]Warning: failed to export asset {asset_path}: {e}[/yellow]')

    return asset_count


@app.command('export')
@async_command
async def export_notes(
    ctx: typer.Context,
    note_id: Annotated[
        str | None, typer.Argument(help='UUID of a specific note to export. Omit to export all.')
    ] = None,
    output: Annotated[
        str,
        typer.Option('--output', '-o', help='Output directory path.'),
    ] = './memex-export',
    vault: Annotated[
        list[str],
        typer.Option('--vault', '-v', help='Vault(s) to filter by. Use "*" for all vaults.'),
    ] = [],
):
    """
    Export notes (and their assets) to a local directory.

    Each note is written to a subdirectory containing note.md, metadata.json,
    and an assets/ folder (if the note has attached files).
    """
    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    config: MemexConfig = ctx.obj
    output_dir = pathlib.Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if vault and ALL_VAULTS_WILDCARD in vault:
        export_vault_ids = None
    else:
        export_vault_ids = vault if vault else config.read_vaults

    async with get_api_context(config) as api:
        if note_id:
            uuid_obj = parse_uuid(note_id, 'note')
            try:
                note = await api.get_note(uuid_obj)
            except Exception as e:
                handle_api_error(e)
            notes = [note]
        else:
            try:
                notes = await api.list_notes(limit=10000, vault_ids=export_vault_ids)
            except Exception as e:
                handle_api_error(e)

        total_assets = 0
        for i, note_obj in enumerate(notes):
            # list_notes returns ORM objects (dicts via model_dump), get_note returns NoteDTO
            if not isinstance(note_obj, NoteDTO):
                note_obj = NoteDTO.model_validate(note_obj, from_attributes=True)
            title = note_obj.title or note_obj.name or 'Untitled'
            console.print(f'[dim]({i + 1}/{len(notes)})[/dim] Exporting "{title}"...')
            asset_count = await _export_note(api, note_obj, output_dir)
            total_assets += asset_count

    console.print(
        f'\n[green]Exported {len(notes)} note(s) and {total_assets} asset(s) '
        f'to {output_dir.resolve()}[/green]'
    )


def _get_template_registry(ctx: typer.Context) -> TemplateRegistry:
    """Build a TemplateRegistry from the CLI config."""
    import logging as _log

    config: MemexConfig = ctx.obj
    dirs: list[tuple[str, pathlib.Path]] = [('builtin', BUILTIN_PROMPTS_DIR)]
    root = config.server.file_store.root
    if '://' not in root:
        dirs.append(('global', pathlib.Path(root) / 'templates'))
    else:
        _log.debug('Skipping global templates: remote filestore (%s)', root)
    dirs.append(('local', pathlib.Path('.memex/templates')))
    return TemplateRegistry(dirs)


template_app = typer.Typer(
    name='template',
    help='Manage note templates (list, get, register, delete).',
    no_args_is_help=True,
)
app.add_typer(template_app)


@template_app.command('list')
def template_list(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """List all available templates with metadata."""
    registry = _get_template_registry(ctx)
    templates = registry.list_templates()

    if not templates:
        if json_output:
            console.print('[]')
        else:
            console.print('[dim]No templates available.[/dim]')
        return

    if json_output:
        console.print(
            json.dumps(
                [
                    {
                        'slug': t.slug,
                        'name': t.display_name,
                        'description': t.description,
                        'source': t.source,
                    }
                    for t in templates
                ],
                indent=2,
            )
        )
        return

    table = Table(title='Available Templates')
    table.add_column('Slug', style='cyan')
    table.add_column('Name')
    table.add_column('Description')
    table.add_column('Source', style='dim')

    for t in templates:
        table.add_row(t.slug, t.display_name, t.description, t.source)

    console.print(table)


@template_app.command('get')
def template_get(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(help='Template slug (e.g. general_note).')],
) -> None:
    """Print the markdown content of a template."""
    registry = _get_template_registry(ctx)
    try:
        content = registry.get_template(slug)
    except KeyError:
        console.print(f'[red]Unknown template: {slug}[/red]')
        console.print('[dim]Use "memex note template list" to see available templates.[/dim]')
        raise typer.Exit(1)

    console.print(content)


@template_app.command('register')
def template_register(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(
            help='Path to a .toml template file, or relative path within a --github-url repo.',
        ),
    ],
    local: Annotated[
        bool, typer.Option('--local', help='Register in project-local scope instead of global.')
    ] = False,
    github_url: Annotated[
        str | None,
        typer.Option(
            '--github-url',
            '-g',
            help='GitHub repo URL (https://github.com/USER/REPO/tree/BRANCH). '
            'When set, path is relative to the repo root.',
        ),
    ] = None,
) -> None:
    """Register a template by copying a .toml file to the templates directory."""
    if github_url is not None:
        from memex_common.github_cache import (
            download_and_cache_github_repo,
            resolve_template_in_repo,
        )

        try:
            repo_dir = download_and_cache_github_repo(github_url)
            resolved = resolve_template_in_repo(repo_dir, path)
        except (ValueError, FileNotFoundError) as e:
            console.print(f'[red]{e}[/red]')
            raise typer.Exit(1)
        except httpx.HTTPStatusError as e:
            console.print(f'[red]Failed to download repository: {e}[/red]')
            raise typer.Exit(1)
    else:
        resolved = pathlib.Path(path).resolve()
        if not resolved.exists():
            console.print(f'[red]File not found: {path}[/red]')
            raise typer.Exit(1)
        if not resolved.is_file():
            console.print(f'[red]Not a file: {path}[/red]')
            raise typer.Exit(1)

    if resolved.suffix != '.toml':
        console.print('[red]Template file must be a .toml file.[/red]')
        raise typer.Exit(1)

    registry = _get_template_registry(ctx)
    scope = 'local' if local else 'global'
    try:
        info = registry.register(resolved, scope=scope)
    except ValueError as e:
        console.print(f'[red]{e}[/red]')
        raise typer.Exit(1)

    console.print(
        f'[green]Registered template: {info.slug} ({info.display_name}) '
        f'in {info.source} scope.[/green]'
    )


@template_app.command('delete')
def template_delete(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(help='Template slug to delete.')],
    local: Annotated[
        bool, typer.Option('--local', help='Delete from project-local scope instead of global.')
    ] = False,
    yes: Annotated[bool, typer.Option('--yes', '-y', help='Skip confirmation prompt.')] = False,
) -> None:
    """Delete a user template. Cannot delete built-in templates."""
    scope = 'local' if local else 'global'

    if not yes:
        confirm = typer.confirm(
            f'Delete template "{slug}" from {scope} scope? This cannot be undone.'
        )
        if not confirm:
            console.print('[dim]Cancelled.[/dim]')
            raise typer.Exit(0)

    registry = _get_template_registry(ctx)
    try:
        registry.delete(slug, scope=scope)
    except ValueError as e:
        console.print(f'[red]{e}[/red]')
        raise typer.Exit(1)
    except KeyError as e:
        console.print(f'[red]{e}[/red]')
        raise typer.Exit(1)

    console.print(f'[green]Deleted template: {slug} from {scope} scope.[/green]')


@template_app.command('dir')
def template_dir(
    ctx: typer.Context,
    local: Annotated[
        bool, typer.Option('--local', help='Show project-local templates directory.')
    ] = False,
) -> None:
    """Print the templates directory path."""
    if local:
        console.print(str(pathlib.Path('.memex/templates').resolve()))
    else:
        config: MemexConfig = ctx.obj
        console.print(str(pathlib.Path(config.server.file_store.root) / 'templates'))


@app.command('update-user-notes')
@async_command
async def update_user_notes(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of the note to update.')],
    user_notes: Annotated[
        str | None,
        typer.Option('--text', '-t', help='User notes text. Pass empty string to clear.'),
    ] = None,
    file: Annotated[
        pathlib.Path | None,
        typer.Option('--file', '-f', help='Read user notes from a file.'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Update user notes on an existing note.

    User notes are your own commentary or context attached to a note.
    They are extracted into the memory graph with source_context='user_notes'.
    """
    config: MemexConfig = ctx.obj
    nid = parse_uuid(note_id, 'note_id')

    if file and user_notes is not None:
        console.print('[red]Provide either --text or --file, not both.[/red]')
        raise typer.Exit(1)

    if file:
        if not file.exists():
            console.print(f'[red]File not found: {file}[/red]')
            raise typer.Exit(1)
        async with aiofiles.open(file, 'r') as f:
            user_notes = await f.read()
    elif user_notes is None:
        console.print('[red]Provide user notes via --text or --file.[/red]')
        raise typer.Exit(1)

    async with get_api_context(config) as api:
        try:
            result = await api.update_user_notes(nid, user_notes)
        except Exception as e:
            handle_api_error(e)

    if json_output:
        console.print_json(json.dumps(result, default=str))
        return

    console.print(f'[green]User notes updated for note {nid}.[/green]')
    if 'units_deleted' in result:
        console.print(f'  Old units removed: {result["units_deleted"]}')
    if 'units_created' in result:
        console.print(f'  New units created: {result["units_created"]}')
