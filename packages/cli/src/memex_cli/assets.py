"""
Note Asset Management Commands.
"""

import asyncio
import json
import mimetypes
import pathlib
import sys
from typing import Annotated

import aiofiles
import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import get_api_context, async_command, handle_api_error, parse_uuid

console = Console()

app = typer.Typer(
    name='assets',
    help='Manage note assets (add, list, get, delete).',
    no_args_is_help=True,
)


@app.command('list')
@async_command
async def list_assets(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """List file assets attached to a note."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    async with get_api_context(config) as api:
        try:
            note = await api.get_note(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    assets = note.assets or []

    if not assets:
        console.print('[dim]No assets found for this note.[/dim]')
        return

    if json_output:
        items = []
        for asset_path in assets:
            path_obj = pathlib.PurePosixPath(asset_path)
            mime_type, _ = mimetypes.guess_type(path_obj.name)
            items.append(
                {
                    'filename': path_obj.name,
                    'path': asset_path,
                    'mime_type': mime_type,
                }
            )
        console.print_json(json.dumps(items))
        return

    table = Table(title=f'Assets ({note_id})')
    table.add_column('Filename', style='cyan')
    table.add_column('Path', style='white')
    table.add_column('MIME Type', style='dim')

    for asset_path in assets:
        path_obj = pathlib.PurePosixPath(asset_path)
        mime_type, _ = mimetypes.guess_type(path_obj.name)
        table.add_row(path_obj.name, asset_path, mime_type or '-')

    console.print(table)


@app.command('get')
@async_command
async def get_asset(
    ctx: typer.Context,
    asset_paths: Annotated[list[str], typer.Argument(help='One or more asset paths (from list).')],
    output: Annotated[
        str | None,
        typer.Option(
            '--output', '-o', help='Output file path (single asset only). Defaults to stdout.'
        ),
    ] = None,
    output_dir: Annotated[
        str | None,
        typer.Option(
            '--output-dir', '-d', help='Directory to save files to (for multiple assets).'
        ),
    ] = None,
) -> None:
    """Download one or more assets from the server."""
    config: MemexConfig = ctx.obj

    if len(asset_paths) > 1 and output:
        console.print(
            '[red]Error: --output cannot be used with multiple assets. Use --output-dir instead.[/red]'
        )
        raise typer.Exit(1)

    async with get_api_context(config) as api:
        try:
            if len(asset_paths) == 1:
                fetched = [(asset_paths[0], await api.get_resource(asset_paths[0]))]
            else:
                raw = await asyncio.gather(
                    *[api.get_resource(p) for p in asset_paths],
                    return_exceptions=True,
                )
                fetched = []
                for path, r in zip(asset_paths, raw):
                    if isinstance(r, Exception):
                        console.print(f'[red]Error fetching {path}: {r}[/red]')
                    else:
                        fetched.append((path, r))
                if not fetched:
                    return
        except Exception as e:
            handle_api_error(e)
            return

    if len(fetched) == 1 and not output_dir:
        # Single asset: preserve original behavior
        _, data = fetched[0]
        if output:
            out = pathlib.Path(output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            console.print(f'Saved to [cyan]{out}[/cyan] ({len(data)} bytes)')
        else:
            sys.stdout.buffer.write(data)
        return

    # Multiple assets (or single with --output-dir): save to directory
    out_dir = pathlib.Path(output_dir or '.')
    out_dir.mkdir(parents=True, exist_ok=True)
    for asset_path, data in fetched:
        filename = pathlib.Path(asset_path).name
        out_file = out_dir / filename
        out_file.write_bytes(data)
        console.print(f'Saved [cyan]{filename}[/cyan] ({len(data)} bytes)')


@app.command('add')
@async_command
async def add_asset(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to add assets to.')],
    asset: Annotated[
        list[pathlib.Path],
        typer.Option('--asset', '-a', help='Path to an asset file to attach.'),
    ],
) -> None:
    """Add one or more asset files to an existing note."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    files_dict: dict[str, bytes] = {}
    for asset_path in asset:
        if not asset_path.exists():
            console.print(f'[red]Error: Asset not found: {asset_path}[/red]')
            raise typer.Exit(1)
        if not asset_path.is_file():
            console.print(f'[red]Error: Not a file: {asset_path}[/red]')
            raise typer.Exit(1)

        async with aiofiles.open(asset_path, 'rb') as f:
            asset_data = await f.read()
        files_dict[asset_path.name] = asset_data

    console.print(f'[cyan]Adding {len(files_dict)} asset(s) to note {note_id}...[/cyan]')

    async with get_api_context(config) as api:
        try:
            result = await api.add_note_assets(uuid_obj, files_dict)
        except Exception as e:
            handle_api_error(e)
            return

    added = result.get('added_assets', [])
    skipped = result.get('skipped', [])
    console.print(f'[green]Added {len(added)} asset(s) to note.[/green]')
    if skipped:
        console.print(f'[yellow]Skipped {len(skipped)} duplicate(s): {", ".join(skipped)}[/yellow]')
    console.print(f'Total assets: {result.get("asset_count", "?")}')


@app.command('delete')
@async_command
async def delete_asset(
    ctx: typer.Context,
    note_id: Annotated[str, typer.Argument(help='UUID of note to delete assets from.')],
    asset_paths: Annotated[
        list[str], typer.Argument(help='One or more asset paths to delete (from list).')
    ],
) -> None:
    """Delete one or more asset files from an existing note."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(note_id, 'note')

    console.print(f'[cyan]Deleting {len(asset_paths)} asset(s) from note {note_id}...[/cyan]')

    async with get_api_context(config) as api:
        try:
            result = await api.delete_note_assets(uuid_obj, asset_paths)
        except Exception as e:
            handle_api_error(e)
            return

    deleted = result.get('deleted_assets', [])
    not_found = result.get('not_found', [])
    console.print(f'[green]Deleted {len(deleted)} asset(s) from note.[/green]')
    if not_found:
        console.print(
            f'[yellow]{len(not_found)} path(s) not found: {", ".join(not_found)}[/yellow]'
        )
    console.print(f'Remaining assets: {result.get("asset_count", "?")}')
