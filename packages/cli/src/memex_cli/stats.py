"""
System Statistics Commands.
"""

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from memex_common.config import MemexConfig
from memex_cli.utils import get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='stats',
    help='View system statistics and usage.',
    no_args_is_help=True,
)


@app.command('system')
@async_command
async def system_stats(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Show overview of system counts (memories, entities, queue).
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            counts = await api.get_stats_counts()
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(counts.model_dump(), default=str))
        return

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column()
    grid.add_column()

    grid.add_row(
        Panel(f'[bold green]{counts.memories}[/bold green]', title='Memories (Docs)'),
        Panel(f'[bold cyan]{counts.entities}[/bold cyan]', title='Entities'),
        Panel(f'[bold yellow]{counts.reflection_queue}[/bold yellow]', title='Reflection Queue'),
    )

    console.print(grid)


@app.command('tokens')
@async_command
async def token_usage(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Show daily token usage statistics.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            resp = await api.get_token_usage()
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(resp.model_dump(), default=str))
        return

    if not resp.usage:
        console.print('[dim]No token usage data available.[/dim]')
        return

    table = Table(title='Daily Token Usage')
    table.add_column('Date', style='cyan')
    table.add_column('Tokens', style='bold white', justify='right')

    total = 0
    for stat in resp.usage:
        table.add_row(str(stat.date), f'{stat.total_tokens:,}')
        total += stat.total_tokens

    table.add_section()
    table.add_row('Total', f'{total:,}', style='bold green')

    console.print(table)
