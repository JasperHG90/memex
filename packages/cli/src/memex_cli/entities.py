"""
Entity Management Commands.
"""

import json
import logging
from typing import Annotated, Any
from uuid import UUID
import typer

logger = logging.getLogger('memex_cli.entities')
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_common.schemas import EntityDTO
from memex_cli.utils import get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='entity',
    help='Explore and manage entities (People, Organizations, Concepts).',
    no_args_is_help=True,
)


async def _resolve_entity(api: Any, identifier: str) -> EntityDTO:
    """
    Smart resolution:
    1. Try as UUID.
    2. Try as exact name match search.
    3. Fail if ambiguous or not found.
    """
    # 1. Try UUID
    try:
        uuid_obj = UUID(identifier)
        return await api.get_entity(uuid_obj)
    except ValueError:
        pass  # Not a UUID

    # 2. Search by name
    results = await api.search_entities(query=identifier, limit=5)

    if not results:
        raise typer.Exit(code=1)

    # Check for exact match
    exact_matches = [e for e in results if e.name.lower() == identifier.lower()]
    if len(exact_matches) == 1:
        return exact_matches[0]

    # If single result, assume that's it
    if len(results) == 1:
        return results[0]

    # Ambiguous
    console.print(f'[yellow]Ambiguous identifier "{identifier}". Did you mean:[/yellow]')
    for e in results:
        console.print(f' - {e.name} ([dim]{e.id}[/dim])')
    raise typer.Exit(code=1)


@app.command('delete')
@async_command
async def delete_entity(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the entity to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete an entity and all associated data (mental models, aliases, links, cooccurrences).
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entity = await _resolve_entity(api, identifier)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise
            handle_api_error(e)
            return

        if not force:
            if not typer.confirm(
                f'Are you sure you want to delete entity "{entity.name}"? This is destructive.'
            ):
                console.print('[yellow]Aborted.[/yellow]')
                return

        console.print(f'[red]Deleting entity:[/red] {entity.name} ({entity.id})')
        try:
            success = await api.delete_entity(entity.id)
        except Exception as e:
            handle_api_error(e)
            return

    if success:
        console.print(f'[green]Entity "{entity.name}" deleted successfully.[/green]')
    else:
        console.print(f'[red]Entity "{entity.name}" not found.[/red]')


@app.command('delete-mental-model')
@async_command
async def delete_mental_model(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the entity.')],
    vault_id: Annotated[
        str | None, typer.Option('--vault', '-v', help='Vault UUID. Defaults to active vault.')
    ] = None,
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete the mental model for an entity in a specific vault. Does NOT delete the entity itself.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entity = await _resolve_entity(api, identifier)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise
            handle_api_error(e)
            return

        parsed_vault_id = None
        if vault_id:
            try:
                parsed_vault_id = UUID(vault_id)
            except ValueError:
                console.print(f'[red]Invalid vault UUID: {vault_id}[/red]')
                return

        vault_label = vault_id or 'active vault'
        if not force:
            if not typer.confirm(f'Delete mental model for "{entity.name}" in {vault_label}?'):
                console.print('[yellow]Aborted.[/yellow]')
                return

        try:
            success = await api.delete_mental_model(entity.id, vault_id=parsed_vault_id)
        except Exception as e:
            handle_api_error(e)
            return

    if success:
        console.print(f'[green]Mental model for "{entity.name}" in {vault_label} deleted.[/green]')
    else:
        console.print(f'[red]Mental model for "{entity.name}" in {vault_label} not found.[/red]')


@app.command('list')
@async_command
async def list_entities(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max number of entities.')] = 50,
    query: Annotated[str | None, typer.Option('--query', '-q', help='Search query.')] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    List or search entities.
    """
    config: MemexConfig = ctx.obj

    entities: list[EntityDTO] = []
    async with get_api_context(config) as api:
        try:
            if query:
                entities = await api.search_entities(query=query, limit=limit)
            else:
                async for ent in api.list_entities_ranked(limit=limit):
                    entities.append(ent)
        except Exception as exc:
            handle_api_error(exc)

    if json_output:
        console.print_json(json.dumps([e.model_dump() for e in entities], default=str))
        return

    table = Table(title=f'Entities (Top {limit})')
    table.add_column('Name', style='cyan')
    table.add_column('Mentions', style='green')
    table.add_column('ID', style='dim')

    for e in entities:
        table.add_row(e.name, str(e.mention_count), str(e.id))

    console.print(table)


@app.command('view')
@async_command
async def view_entity(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the entity.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View details of a specific entity.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entity = await _resolve_entity(api, identifier)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(entity.model_dump(), default=str))
        return

    console.print(f'[bold cyan]Entity:[/bold cyan] {entity.name}')
    console.print(f'[dim]ID:[/dim] {entity.id}')
    console.print(f'[green]Mentions:[/green] {entity.mention_count}')


@app.command('mentions')
@async_command
async def list_mentions(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the entity.')],
    limit: int = 20,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Show memories and notes mentioning this entity.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entity = await _resolve_entity(api, identifier)
            results = await api.get_entity_mentions(entity.id, limit=limit)
        except Exception as e:
            if isinstance(e, typer.Exit):
                raise
            handle_api_error(e)
            return

    if not results:
        console.print(f'[yellow]No mentions found for {entity.name}.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps(results, default=str))
        return

    table = Table(title=f'Mentions for {entity.name}')
    table.add_column('Memory Segment', style='white')
    table.add_column('Source Note', style='dim')
    table.add_column('Date', style='cyan')

    for item in results:
        unit = item.get('unit')
        doc = item.get('note')

        text = unit.text if unit else 'N/A'
        # Truncate text
        if len(text) > 80:
            text = text[:77] + '...'

        source = doc.name if doc and doc.name else 'Unknown'
        date_str = str(unit.mentioned_at or unit.occurred_start or 'Unknown')

        table.add_row(text, source, date_str)

    console.print(table)


@app.command('related')
@async_command
async def list_related(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help='Name or UUID of the entity.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Show related (co-occurring) entities.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            entity = await _resolve_entity(api, identifier)
            edges = await api.get_entity_cooccurrences(entity.id)
            edges = sorted(edges, key=lambda x: -x['cooccurrence_count'])[:20]
            # Fetch names for related entities to be user-friendly
            # Collect IDs
            related_ids = []
            for edge in edges:
                other_id = (
                    edge['entity_id_2']
                    if str(edge['entity_id_1']) == str(entity.id)
                    else edge['entity_id_1']
                )
                related_ids.append(other_id)

            resolved_names = {}
            if len(related_ids):
                for rid in related_ids:
                    try:
                        r_ent = await api.get_entity(rid)
                        resolved_names[str(rid)] = r_ent.name
                    except Exception as e:
                        logger.debug('Failed to resolve entity %s: %s', rid, e)
                        resolved_names[str(rid)] = 'Unknown'

        except Exception as e:
            if isinstance(e, typer.Exit):
                raise
            handle_api_error(e)
            return

    if not edges:
        console.print(f'[yellow]No related entities found for {entity.name}.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps(edges, default=str))
        return

    table = Table(title=f'Related to: {entity.name}')
    table.add_column('Related Entity', style='cyan')
    table.add_column('Strength', style='green')
    table.add_column('ID', style='dim')

    for edge in edges:
        other_id = (
            edge['entity_id_2']
            if str(edge['entity_id_1']) == str(entity.id)
            else edge['entity_id_1']
        )
        count = edge['cooccurrence_count']
        name = resolved_names.get(str(other_id), 'Unknown')

        table.add_row(name, str(count), str(other_id))

    console.print(table)
