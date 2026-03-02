"""
Memory Management Commands (Ingest & Retrieval).
"""

import json
import logging
import pathlib as plb
from typing import Annotated
from uuid import UUID
import itertools
import base64
import mimetypes

import aiofiles

import dspy
import typer
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown

from memex_cli.utils import (
    async_command,
    get_api_context,
    handle_api_error,
    parse_uuid,
)
from memex_common.config import MemexConfig
from memex_common.schemas import (
    BatchJobStatus,
    IngestResponse,
    ReflectionRequest,
    MemoryUnitDTO,
    NoteCreateDTO,
    IngestURLRequest,
    LineageDirection,
)

logger = logging.getLogger('memex_cli.memory')
console = Console()

app = typer.Typer(
    name='memory',
    help='Ingest and Search memories.',
    no_args_is_help=True,
)


class ResponseOutput(BaseModel):
    answer: str = Field(..., description="The final answer to the user's query")


class ResponseModel(dspy.Signature):
    """Answer the user's query based on the provided memory context."""

    query: str = dspy.InputField(desc='User query string')
    memory: list[str] = dspy.InputField(desc='Relevant memory content')
    output: ResponseOutput = dspy.OutputField(
        desc='The response output containing the final answer'
    )


async def generate_answer(query: str, memory: list[MemoryUnitDTO], model_name: str) -> str:
    """Generate an answer using DSPy."""

    lm = dspy.LM(model=model_name)
    predictor = dspy.Predict(ResponseModel)

    with dspy.context(lm=lm):
        try:
            response = predictor(query=query, memory=[t.text for t in memory])
            return response.output.answer
        except Exception as e:
            logger.error(f'Error generating answer: {e}')
            return 'Could not generate answer.'


@app.command('delete')
@async_command
async def delete_memory(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the memory unit to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete a memory unit and all associated data (entity links, memory links, evidence).
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    if not force:
        if not typer.confirm(
            f'Are you sure you want to delete memory unit {unit_id}? This is destructive.'
        ):
            console.print('[yellow]Aborted.[/yellow]')
            return

    async with get_api_context(config) as api:
        try:
            success = await api.delete_memory_unit(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if success:
        console.print(f'[green]Memory unit {unit_id} deleted successfully.[/green]')
    else:
        console.print(f'[red]Memory unit {unit_id} not found.[/red]')


@app.command('add')
@async_command
async def add_memory(
    ctx: typer.Context,
    content: Annotated[str | None, typer.Argument(help='The content of the memory to add.')] = None,
    file: Annotated[
        plb.Path | None,
        typer.Option('--file', '-f', help='Path to a file or directory to ingest.', dir_okay=True),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option('--url', '-u', help='URL to scrape and ingest.'),
    ] = None,
    asset: Annotated[
        list[plb.Path] | None,
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
    Add a new memory to Memex.
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

    console.print('[bold green]Adding Memory[/bold green]')

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
            console.print(f'[yellow]Memory skipped: {result.reason}[/yellow]')
        else:
            console.print(f'[green]Memory added successfully![/green] UUID: {result.note_id}')
            if result.unit_ids:
                console.print(f'Extracted {len(result.unit_ids)} memory units.')


@app.command('search')
@async_command
async def search_memory(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Search query.')],
    vault: Annotated[
        list[str] | None, typer.Option('--vault', '-v', help='Filter by vault(s).')
    ] = None,
    limit: int = 5,
    token_budget: Annotated[
        int | None, typer.Option('--token-budget', '-t', help='Token budget for retrieval.')
    ] = None,
    answer: Annotated[
        bool, typer.Option('--answer', '-a', help='Generate an AI answer from results.')
    ] = False,
    skip_opinions: Annotated[
        bool, typer.Option('--skip-opinions', help='Skip automated opinion formation.')
    ] = False,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[bool, typer.Option('--minimal', help='Output unit IDs only.')] = False,
    compact: Annotated[
        bool, typer.Option('--compact', help='One line per result: type + truncated text.')
    ] = False,
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
    no_mental_model: Annotated[
        bool, typer.Option('--no-mental-model', help='Exclude mental model strategy.')
    ] = False,
    include_stale: Annotated[
        bool, typer.Option('--include-stale', help='Include stale memory units in results.')
    ] = False,
):
    """
    Search for memories.
    """
    config: MemexConfig = ctx.obj
    if vault:
        # Override config: treat provided vaults as the full search scope
        config.server.active_vault = vault[0]
        config.server.attached_vaults = vault[1:]

    # Compute strategy inclusion list from exclusion flags
    all_strategies = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model']
    exclusions = {
        'semantic': no_semantic,
        'keyword': no_keyword,
        'graph': no_graph,
        'temporal': no_temporal,
        'mental_model': no_mental_model,
    }
    active = [s for s in all_strategies if not exclusions[s]]
    strategies: list[str] | None = active if len(active) < len(all_strategies) else None

    console.print(f'[bold cyan]Searching:[/bold cyan] {query}')
    if strategies is not None:
        console.print(f'[dim]Active strategies: {", ".join(strategies)}[/dim]')

    # Resolve vault_ids to pass directly to the server
    vault_ids: list[str] | None = None
    if vault:
        vault_ids = [v.strip() for v in vault]

    async with get_api_context(config) as api:
        try:
            results = await api.search(
                query=query,
                limit=limit,
                skip_opinion_formation=skip_opinions,
                token_budget=token_budget,
                strategies=strategies,
                vault_ids=vault_ids,
                include_stale=include_stale,
            )
        except Exception as e:
            handle_api_error(e)

        if not results:
            console.print('[yellow]No results found.[/yellow]')
            return

        if minimal:
            for unit in results:
                console.print(str(unit.id))
            return

        if compact:
            for unit in results:
                text = unit.text.replace('\n', ' ')[:200]
                cs = unit.confidence_score
                marker = '[!] ' if cs is not None and cs < 0.3 else ''
                print(f'- [{unit.fact_type}] {marker}{text}')
            return

        if json_output:
            console.print_json(json.dumps([u.model_dump() for u in results], default=str))
            return

        # Display Table
        table = Table(title=f'Search Results ({len(results)})')
        table.add_column('Type', style='cyan')
        table.add_column('Memory', style='white')
        table.add_column('Source', style='dim')

        for unit in results:
            content_preview = unit.text.replace('\n', ' ')
            if len(content_preview) > 100:
                content_preview = content_preview[:100] + '...'

            # Visual indicator for contradicted results
            cs = unit.confidence_score
            marker = '[!] ' if cs is not None and cs < 0.3 else ''

            # Check unit_metadata for source info
            source = 'Unknown'
            if unit.metadata:
                if unit.fact_type == 'opinion':
                    source = unit.metadata.get('evidence_indices', 'Unknown')
                else:
                    source = unit.metadata.get('note_name', 'Unknown')
                    if source == 'Unknown':
                        source = unit.metadata.get('filestore_path', 'Unknown')

            table.add_row(unit.fact_type, f'{marker}{content_preview}', str(source))

        console.print(table)

        # Generate Answer
        if answer and results:
            ans = await api.summarize(query=query, texts=[r.enriched_text for r in results[:50]])
            console.print(Panel(Markdown(ans.summary), title='Answer', border_style='green'))


@app.command('reflect')
@async_command
async def reflect(
    ctx: typer.Context,
    entity_id: Annotated[
        str | None,
        typer.Argument(
            help='ID of the entity to reflect on. If omitted, reflects on top entities.'
        ),
    ] = None,
    limit: int = 5,
    batch_size: int = 10,
):
    """
    Manually trigger a reflection cycle.
    If entity_id is provided, reflects on that specific entity.
    Otherwise, picks top entities (by mention count) and reflects on them.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    config: MemexConfig = ctx.obj

    entities_to_process: list[UUID | tuple[UUID, UUID]] = []

    async with get_api_context(config) as api:
        try:
            if entity_id:
                entities_to_process.append(parse_uuid(entity_id, 'entity'))
            else:
                console.print(
                    f'[dim]No entity ID provided. Fetching {limit} items from Reflection Queue...[/dim]'
                )
                queue_items = await api.get_reflection_queue_batch(limit=limit)

                if not queue_items:
                    console.print(
                        '[yellow]Reflection Queue is empty. Fetching top entities as fallback...[/yellow]'
                    )
                    top_entities = await api.get_top_entities(limit=limit)
                    if not top_entities:
                        console.print('[yellow]No entities found.[/yellow]')
                        return
                    entities_to_process = [e.id for e in top_entities]
                    console.print(
                        f'[dim]Reflecting on: {", ".join([e.name for e in top_entities])}[/dim]'
                    )
                else:
                    # Store tuples of (entity_id, vault_id) to preserve context
                    entities_to_process = [(q.entity_id, q.vault_id) for q in queue_items]
                    console.print(
                        f'[dim]Processing {len(entities_to_process)} items from queue...[/dim]'
                    )

            # Batch Processing with Progress Bar
            if entities_to_process:
                total_entities = len(entities_to_process)
                console.print(
                    f'[bold green]Triggering Batch Reflection for {total_entities} entities...[/bold green]'
                )

                # Helper to chunk list
                def chunked(iterable, n):
                    it = iter(iterable)
                    while True:
                        chunk = list(itertools.islice(it, n))
                        if not chunk:
                            return
                        yield chunk

                all_results = []

                with Progress(
                    SpinnerColumn(),
                    TextColumn('[progress.description]{task.description}'),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=console,
                ) as progress:
                    task_id = progress.add_task('[cyan]Reflecting...', total=total_entities)

                    for batch in chunked(entities_to_process, batch_size):
                        # Handle both single UUIDs (top entities) and (UUID, UUID) tuples (queue items)
                        requests = []
                        for item in batch:
                            if isinstance(item, tuple):
                                eid, vid = item
                                requests.append(ReflectionRequest(entity_id=eid, vault_id=vid))
                            else:
                                requests.append(ReflectionRequest(entity_id=item))

                        batch_results = await api.reflect_batch(requests)
                        all_results.extend(batch_results)
                        progress.advance(task_id, advance=len(batch))

                console.print(
                    f'[green]Batch Reflection Scheduled! Queued {len(all_results)} entities for background processing.[/green]'
                )

                # Summary
                console.print(
                    '[dim]Reflection is running in the background. Check logs for progress.[/dim]'
                )
        except Exception as e:
            handle_api_error(e)


@app.command('submit-evidence')
@async_command
async def submit_evidence(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the opinion memory unit.')],
    evidence_type: Annotated[
        str,
        typer.Argument(
            help='Evidence type key: user_validation, user_rejection, corroboration, '
            'logical_contradiction, execution_success, execution_failure, '
            'catastrophic_failure, llm_consensus, minor_error.'
        ),
    ],
    description: Annotated[
        str | None, typer.Option('--description', '-d', help='Description of the evidence.')
    ] = None,
):
    """
    Submit evidence to adjust an opinion's confidence score (Bayesian update).
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    async with get_api_context(config) as api:
        try:
            result = await api.adjust_belief(uuid_obj, evidence_type, description=description)
        except Exception as e:
            handle_api_error(e)
            return

    before_pct = int(round(result.confidence_before * 100))
    after_pct = int(round(result.confidence_after * 100))
    console.print(f'[bold green]Evidence recorded:[/bold green] {evidence_type}')
    console.print(f'Confidence: {before_pct}% → {after_pct}%')
    console.print(f'[dim]Parameters: α={result.alpha:.1f}, β={result.beta:.1f}[/dim]')


@app.command('evidence-log')
@async_command
async def evidence_log(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the memory unit.')],
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max entries to show.')] = 20,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Show the evidence audit trail for a memory unit.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    async with get_api_context(config) as api:
        try:
            logs = await api.get_evidence_log(uuid_obj, limit=limit)
        except Exception as e:
            handle_api_error(e)
            return

    if not logs:
        console.print('[yellow]No evidence log entries found.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps([log.model_dump() for log in logs], default=str))
        return

    table = Table(title=f'Evidence Log ({len(logs)} entries)')
    table.add_column('Time', style='dim')
    table.add_column('Type', style='cyan')
    table.add_column('Before', style='yellow', justify='right')
    table.add_column('After', style='green', justify='right')
    table.add_column('Description', style='white')

    for log in logs:
        before_pct = f'{int(round(log.confidence_before * 100))}%'
        after_pct = f'{int(round(log.confidence_after * 100))}%'
        table.add_row(
            str(log.created_at.strftime('%Y-%m-%d %H:%M')),
            log.evidence_type,
            before_pct,
            after_pct,
            log.description or '',
        )

    console.print(table)


@app.command('lineage')
@async_command
async def get_lineage(
    ctx: typer.Context,
    entity_type: Annotated[
        str, typer.Argument(help='Type: mental_model, observation, memory_unit, note')
    ],
    entity_id: Annotated[str, typer.Argument(help='UUID of the entity.')],
    direction: Annotated[
        LineageDirection, typer.Option('--direction', '-d', help='Traverse direction.')
    ] = LineageDirection.UPSTREAM,
    depth: Annotated[int, typer.Option('--depth', help='Max recursion depth.')] = 3,
    limit: Annotated[int, typer.Option('--limit', help='Max children per node.')] = 5,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Visualize the lineage of a specific entity.
    """
    from rich.tree import Tree
    from memex_common.schemas import LineageResponse

    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(entity_id, entity_type)

    async with get_api_context(config) as api:
        try:
            response = await api.get_lineage(
                entity_type=entity_type,
                entity_id=uuid_obj,
                direction=direction,
                depth=depth,
                limit=limit,
            )
        except Exception as e:
            handle_api_error(e)

        def build_tree(node: LineageResponse, tree: Tree | None = None) -> Tree:
            # Format the node label
            e_type = node.entity_type.replace('_', ' ').title()
            e_id = str(node.entity.get('id') or node.entity.get('entity_id') or 'Unknown')[:8]

            # Extract some meaningful text/name
            name = (
                node.entity.get('name')
                or node.entity.get('canonical_name')
                or node.entity.get('content')
                or node.entity.get('text')
                or ''
            )
            if len(name) > 50:
                name = name[:47] + '...'

            # Check for assets (Notes only)
            assets_info = ''
            if node.entity_type == 'note':
                assets = node.entity.get('assets') or []
                if assets:
                    assets_info = f' ({len(assets)} assets)'

            label = f'[bold cyan]{e_type}[/bold cyan] [dim]{e_id}[/dim]'
            if name:
                label += f': {name}'
            if assets_info:
                label += f'[yellow]{assets_info}[/yellow]'

            if tree is None:
                tree = Tree(label)
            else:
                tree = tree.add(label)

            for child in node.derived_from:
                build_tree(child, tree)
            return tree

        if json_output:
            console.print_json(json.dumps(response.model_dump(), default=str))
            return

        console.print(f'\n[bold green]Lineage Visualization ({direction.value})[/bold green]')
        tree = build_tree(response)
        console.print(tree)
