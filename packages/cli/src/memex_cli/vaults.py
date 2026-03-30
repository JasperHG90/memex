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

            has_access = any(row['vault'].access is not None for row in rows)
            if has_access:
                lines = [
                    '| Name | Notes | Last Modified | Active | Access | Description |',
                    '|------|-------|---------------|--------|--------|-------------|',
                ]
            else:
                lines = [
                    '| Name | Notes | Last Modified | Active | Description |',
                    '|------|-------|---------------|--------|-------------|',
                ]
            for row in rows:
                v = row['vault']
                count = row['note_count']
                last_mod_dt = row.get('last_note_added_at')
                last_mod = last_mod_dt.strftime('%Y-%m-%d') if last_mod_dt else '\u2014'
                active = 'yes' if v.is_active else ''
                desc = v.description or ''
                if has_access:
                    access = ', '.join(v.access) if v.access else '\u2014'
                    lines.append(
                        f'| {v.name} | {count} | {last_mod} | {active} | {access} | {desc} |'
                    )
                else:
                    lines.append(f'| {v.name} | {count} | {last_mod} | {active} | {desc} |')
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

    has_access = any(v.access is not None for v in vaults)

    table = Table(title='Available Vaults')
    table.add_column('ID', style='dim')
    table.add_column('Name', style='cyan')
    table.add_column('Description', style='white')
    if has_access:
        table.add_column('Access', style='green')

    if not vaults:
        console.print('[yellow]No vaults found.[/yellow]')
    else:
        for v in vaults:
            row = [str(v.id), v.name, v.description or '']
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

    req = CreateVaultRequest(name=name, description=description)
    async with get_api_context(config) as api:
        try:
            vault = await api.create_vault(req)
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
