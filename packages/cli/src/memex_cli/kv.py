"""
Key-Value Store Commands.
"""

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_cli.utils import get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='kv',
    help='Key-value fact store (lightweight structured memory).',
    no_args_is_help=True,
)


@app.command('write')
@async_command
async def kv_write(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(help='The fact/value to store.')],
    key: Annotated[
        str,
        typer.Option(
            '--key',
            '-k',
            help='Namespaced key (must start with global:, user:, project:, or app:).',
        ),
    ],
):
    """
    Write a fact to the KV store. Key must be namespace-prefixed.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entry = await api.kv_put(value=value, key=key)
        except Exception as e:
            handle_api_error(e)

    console.print(f'[green]Stored:[/green] {entry.key} = {entry.value}')


@app.command('get')
@async_command
async def kv_get(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help='Key to look up.')],
    value_only: Annotated[
        bool, typer.Option('--value-only', help='Print only the raw value (no formatting).')
    ] = False,
):
    """
    Get a fact by exact key.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entry = await api.kv_get(key=key)
        except Exception as e:
            handle_api_error(e)

    if entry is None:
        if not value_only:
            console.print(f'[yellow]Key not found: {key}[/yellow]')
        raise typer.Exit(1)

    if value_only:
        print(entry.value)
        return

    console.print(f'[bold cyan]{entry.key}[/bold cyan] = {entry.value}')
    console.print(f'[dim]Updated: {entry.updated_at}[/dim]')


@app.command('search')
@async_command
async def kv_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Search query.')],
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max results.')] = 5,
    namespace: Annotated[
        list[str] | None,
        typer.Option('--namespace', '-n', help='Filter by namespace prefix (repeatable).'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Fuzzy search facts by semantic similarity.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            results = await api.kv_search(query=query, namespaces=namespace, limit=limit)
        except Exception as e:
            handle_api_error(e)

    if not results:
        console.print('[yellow]No results found.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps([r.model_dump() for r in results], default=str))
        return

    table = Table(title=f'KV Search: "{query}"')
    table.add_column('Key', style='cyan')
    table.add_column('Value', style='white', ratio=3)
    table.add_column('Namespace', style='dim')
    table.add_column('Updated', style='dim')

    for entry in results:
        ns = entry.key.split(':', 1)[0] if ':' in entry.key else '?'
        table.add_row(entry.key, entry.value, ns, str(entry.updated_at))

    console.print(table)


@app.command('list')
@async_command
async def kv_list(
    ctx: typer.Context,
    namespace: Annotated[
        list[str] | None,
        typer.Option('--namespace', '-n', help='Filter by namespace prefix (repeatable).'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    List all facts in the KV store.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entries = await api.kv_list(namespaces=namespace)
        except Exception as e:
            handle_api_error(e)

    if not entries:
        console.print('[yellow]No KV entries found.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps([e.model_dump() for e in entries], default=str))
        return

    table = Table(title='KV Entries')
    table.add_column('Key', style='cyan')
    table.add_column('Value', style='white', ratio=3)
    table.add_column('Namespace', style='dim')
    table.add_column('Updated', style='dim')

    for entry in entries:
        ns = entry.key.split(':', 1)[0] if ':' in entry.key else '?'
        table.add_row(entry.key, entry.value, ns, str(entry.updated_at))

    console.print(table)


@app.command('delete')
@async_command
async def kv_delete(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help='Key to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete a fact by key.
    """
    config: MemexConfig = ctx.obj

    if not force:
        if not typer.confirm(f'Delete KV entry "{key}"?'):
            console.print('[yellow]Aborted.[/yellow]')
            return

    async with get_api_context(config) as api:
        try:
            deleted = await api.kv_delete(key=key)
        except Exception as e:
            handle_api_error(e)

    if deleted:
        console.print(f'[green]Deleted: {key}[/green]')
    else:
        console.print(f'[red]Key not found: {key}[/red]')
