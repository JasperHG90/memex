"""
Vault Management Commands.
"""

import json
from typing import Annotated
import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_common.schemas import CreateVaultRequest
from memex_cli.utils import get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='vault',
    help='Manage Memex Vaults (scopes).',
    no_args_is_help=True,
)


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

            lines = [
                '| Name | Notes | Active | Description |',
                '|------|-------|--------|-------------|',
            ]
            for row in rows:
                v = row['vault']
                count = row['note_count']
                active = 'yes' if v.is_active else ''
                desc = v.description or ''
                lines.append(f'| {v.name} | {count} | {active} | {desc} |')
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
        console.print_json(json.dumps([v.model_dump() for v in vaults], default=str))
        return

    table = Table(title='Available Vaults')
    table.add_column('ID', style='dim')
    table.add_column('Name', style='cyan')
    table.add_column('Description', style='white')

    if not vaults:
        console.print('[yellow]No vaults found.[/yellow]')
    else:
        for v in vaults:
            table.add_row(str(v.id), v.name, v.description or '')
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

    req = CreateVaultRequest(name=name, description=description)
    async with get_api_context(config) as api:
        try:
            vault = await api.create_vault(req)
        except Exception as e:
            handle_api_error(e)

    console.print(f'[bold green]Vault created successfully![/bold green] ID: {vault.id}')


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
