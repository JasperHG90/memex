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
