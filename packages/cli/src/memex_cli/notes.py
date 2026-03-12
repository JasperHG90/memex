"""
Note Management Commands.
"""

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
from rich.table import Table
from rich.tree import Tree

from memex_common.config import MemexConfig
from memex_common.client import RemoteMemexAPI
from memex_common.schemas import (
    BatchJobStatus,
    IngestResponse,
    NoteCreateDTO,
    NoteDTO,
    IngestURLRequest,
)
from memex_cli.utils import get_api_context, async_command, handle_api_error, parse_uuid

console = Console()

app = typer.Typer(
    name='note',
    help='Manage and view source notes.',
    no_args_is_help=True,
)


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
):
    """
    Add a new note to Memex.
    You can provide text directly, use --file to load from disk, or --url to scrape a website.
    Use --asset to attach auxiliary files (images, PDFs) to a note.
    """
    config: MemexConfig = ctx.obj
    # Override active vault if specified
    if vault:
        config.server.active_vault = vault

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
                    url=url, assets=assets_dict, vault_id=config.server.active_vault
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
                if config.server.active_vault:
                    metadata['vault_id'] = str(config.server.active_vault)

                result = await api.ingest_upload(
                    files=files_to_upload, metadata=metadata, background=background
                )
            except Exception as e:
                handle_api_error(e)
        else:
            # Handle NoteDTO path (content + assets or file + assets)
            try:
                note_content = ''
                note_name = 'Quick Note'
                note_description = 'Added via CLI'

                if file:
                    if file.is_dir():
                        console.print(
                            '[red]Error: --asset cannot be used with a directory --file. Point --file to a markdown file instead.[/red]'
                        )
                        raise typer.Exit(1)

                    console.print(f'[cyan]Reading main note file {file.name}...[/cyan]')
                    async with aiofiles.open(file, 'r', encoding='utf-8') as f:
                        note_content = await f.read()
                    note_name = file.stem
                else:
                    note_content = content or ''

                # Encode content
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
                    tags=['cli', 'note-with-assets'] if asset else ['cli', 'quick-note'],
                    note_key=key,
                    vault_id=config.server.active_vault,
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
    vault: Annotated[list[str], typer.Option('--vault', '-v', help='Vault(s) to filter by.')] = [],
    after: Annotated[
        str | None,
        typer.Option('--after', help='Only notes on/after this date (ISO 8601).'),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option('--before', help='Only notes on/before this date (ISO 8601).'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one note ID per line.')
    ] = False,
    compact: Annotated[
        bool, typer.Option('--compact', help='One line per note: title, date, description.')
    ] = False,
):
    """
    List all notes.
    """
    from datetime import datetime

    config: MemexConfig = ctx.obj

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

    async with get_api_context(config) as api:
        try:
            notes = await api.list_notes(
                limit=limit,
                offset=offset,
                vault_ids=vault or None,
                after=parsed_after,
                before=parsed_before,
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
    table.add_column('Publish Date', style='green')
    table.add_column('Created At', style='dim')
    table.add_column('ID', style='dim')

    for d in notes:
        pub_date = ''
        if hasattr(d, 'publish_date') and d.publish_date:
            pub_date = str(d.publish_date.date())
        table.add_row(d.name or 'Untitled', pub_date, str(d.created_at), str(d.id))

    console.print(table)


@app.command('recent')
@async_command
async def list_recent(
    ctx: typer.Context,
    limit: int = 10,
    vault: Annotated[list[str], typer.Option('--vault', '-v', help='Vault(s) to filter by.')] = [],
    after: Annotated[
        str | None,
        typer.Option('--after', help='Only notes on/after this date (ISO 8601).'),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option('--before', help='Only notes on/before this date (ISO 8601).'),
    ] = None,
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

    config: MemexConfig = ctx.obj

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

    async with get_api_context(config) as api:
        try:
            notes = await api.get_recent_notes(
                limit=limit,
                vault_ids=vault or None,
                after=parsed_after,
                before=parsed_before,
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
    table.add_column('Publish Date', style='green')
    table.add_column('Created At', style='dim')
    table.add_column('ID', style='dim')

    for d in notes:
        pub_date = ''
        if hasattr(d, 'publish_date') and d.publish_date:
            pub_date = str(d.publish_date.date())
        table.add_row(d.name or 'Untitled', pub_date, str(d.created_at), str(d.id))

    console.print(table)


def _print_compact_note(d: Any) -> None:
    """Print a single note in compact one-line format."""
    title = d.title or d.name or 'Untitled'
    date = str(d.created_at.date()) if d.created_at else 'unknown'
    desc = ''
    if d.doc_metadata:
        desc = d.doc_metadata.get('description', '') or ''
    if len(desc) > 150:
        desc = desc[:147] + '...'
    suffix = f': {desc}' if desc else ''
    print(f'- **{title}** ({date}){suffix}')


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


@app.command('metadata')
@async_command
async def view_metadata(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View the metadata (title, description, tags, etc.) of a note."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            metadata = await api.get_note_metadata(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if metadata is None:
        console.print('[yellow]This note has no metadata.[/yellow]')
        console.print('[dim]Only notes with a page index have metadata.[/dim]')
        return

    if json_output:
        console.print_json(json.dumps(metadata, default=str))
        return

    table = Table(title=f'Note Metadata ({note_id})')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='white')

    for key, value in metadata.items():
        if isinstance(value, list):
            value = ', '.join(str(v) for v in value)
        table.add_row(key, str(value) if value is not None else '-')

    console.print(table)


@app.command('page-index')
@async_command
async def view_page_index(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View the page index (slim tree) of a note."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            page_index = await api.get_note_page_index(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if page_index is None:
        console.print('[yellow]This note has no page index.[/yellow]')
        console.print('[dim]Only notes ingested with page_index strategy have a slim tree.[/dim]')
        return

    if json_output:
        console.print_json(json.dumps(page_index, default=str))
        return

    # page_index may be a list (raw TOC nodes) or a dict with a 'toc' key
    nodes = page_index if isinstance(page_index, list) else page_index.get('toc', [])

    tree = Tree(f'[bold cyan]Page Index[/bold cyan] [dim]({note_id})[/dim]')
    _render_toc_nodes(nodes, tree)
    console.print(tree)


@app.command('node')
@async_command
async def view_node(
    ctx: typer.Context,
    node_id: Annotated[str, typer.Argument(help='UUID of node.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View a specific page-index node (section) by its ID."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(node_id, 'node')

    async with get_api_context(config) as api:
        try:
            node = await api.get_node(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if node is None:
        console.print(f'[yellow]Node {node_id} not found.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps(node.model_dump(), default=str))
        return

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
    vault: Annotated[list[str], typer.Option('--vault', '-v', help='Vault(s) to search.')] = [],
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

    async with get_api_context(config) as api:
        try:
            results = await api.search_notes(
                query=query,
                limit=limit,
                vault_ids=vault or None,
                expand_query=expand,
                fusion_strategy=fusion_strategy,
                strategies=strategies,
                reason=reason,
                summarize=summarize,
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

        # Aggregate snippets into a single preview string
        preview_texts = [s.text.strip() for s in doc.snippets]
        preview = ' ... '.join(preview_texts) if preview_texts else '[No preview available]'

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
    vault: Annotated[list[str], typer.Option('--vault', '-v', help='Vault(s) to filter by.')] = [],
):
    """
    Export notes (and their assets) to a local directory.

    Each note is written to a subdirectory containing note.md, metadata.json,
    and an assets/ folder (if the note has attached files).
    """
    config: MemexConfig = ctx.obj
    output_dir = pathlib.Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

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
                notes = await api.list_notes(limit=10000, vault_ids=vault or None)
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
