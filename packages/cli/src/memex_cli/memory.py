"""
Memory Management Commands (Ingest & Retrieval).
"""

import asyncio
import logging
from typing import Annotated
from uuid import UUID
import itertools

import typer
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown

from memex_cli.utils import (
    VaultOption,
    async_command,
    emit_json,
    resolve_active_vault,
    get_api_context,
    handle_api_error,
    parse_uuid,
)
from memex_common.config import MemexConfig
from memex_common.schemas import (
    ReflectionRequest,
    MemoryUnitDTO,
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


def _get_response_model():
    """Lazily define the DSPy Signature to avoid importing dspy at module level."""
    import dspy

    class ResponseModel(dspy.Signature):
        """Answer the user's query based on the provided memory context."""

        query: str = dspy.InputField(desc='User query string')
        memory: list[str] = dspy.InputField(desc='Relevant memory content')
        output: ResponseOutput = dspy.OutputField(
            desc='The response output containing the final answer'
        )

    return ResponseModel


async def generate_answer(query: str, memory: list[MemoryUnitDTO], model_name: str) -> str:
    """Generate an answer using DSPy."""
    import dspy

    ResponseModel = _get_response_model()
    # timeout= is required so httpx has a real socket deadline (see #50). The
    # CI grep guard at test_dspy_lm_timeout_guard.py asserts every dspy.LM(...)
    # construction passes one.
    lm = dspy.LM(model=model_name, timeout=120)
    predictor = dspy.Predict(ResponseModel)

    with dspy.context(lm=lm):
        try:
            response = predictor(query=query, memory=[t.text for t in memory])
            return response.output.answer
        except Exception as e:
            logger.error(f'Error generating answer: {e}')
            return 'Could not generate answer.'


@app.command('view')
@async_command
async def view_memory(
    ctx: typer.Context,
    unit_ids: Annotated[list[str], typer.Argument(help='One or more memory unit UUIDs.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View one or more memory units by ID.
    """
    from rich.rule import Rule

    config: MemexConfig = ctx.obj
    uuids = [parse_uuid(uid, 'memory unit') for uid in unit_ids]

    async with get_api_context(config) as api:
        try:
            if len(uuids) == 1:
                units = [await api.get_memory_unit(uuids[0])]
            else:
                raw = await asyncio.gather(
                    *[api.get_memory_unit(uid) for uid in uuids],
                    return_exceptions=True,
                )
                units = []
                for uid_str, r in zip(unit_ids, raw):
                    if isinstance(r, Exception):
                        console.print(f'[red]Error fetching {uid_str}: {r}[/red]')
                    else:
                        units.append(r)
                if not units:
                    return
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        # exclude embedding: vectors are an HTTP/Python-caller capability; CLI
        # JSON shape stays byte-identical to the pre-field era.
        if len(uuids) == 1:
            emit_json(units[0].model_dump(exclude={'embedding'}))
        else:
            emit_json([u.model_dump(exclude={'embedding'}) for u in units])
        return

    for i, unit in enumerate(units):
        if i > 0:
            console.print(Rule())
        console.print(f'[bold cyan]Memory Unit[/bold cyan] [dim]{unit.id}[/dim]')
        console.print(f'[dim]Type:[/dim] {unit.fact_type}')
        console.print(f'[dim]Status:[/dim] {unit.status}')
        intent = getattr(unit, 'intent_class', None)
        risk = getattr(unit, 'risk_class', None)
        if intent or risk:
            parts = []
            if intent:
                parts.append(f'intent={intent}')
            if risk and risk != 'none':
                parts.append(f'[yellow]risk={risk}[/yellow]')
            elif risk:
                parts.append(f'risk={risk}')
            console.print(f'[dim]Classifier:[/dim] {" · ".join(parts)}')
        if unit.note_id:
            console.print(f'[dim]Note ID:[/dim] {unit.note_id}')
        if unit.mentioned_at:
            console.print(f'[dim]Mentioned at:[/dim] {unit.mentioned_at}')
        if unit.occurred_start:
            date_range = str(unit.occurred_start)
            if unit.occurred_end:
                date_range += f' → {unit.occurred_end}'
            console.print(f'[dim]Occurred:[/dim] {date_range}')
        console.print()
        console.print(Panel(Markdown(unit.text), title='Content', border_style='green'))
        if unit.metadata:
            source = unit.metadata.get('note_name') or unit.metadata.get('filestore_path')
            if source:
                console.print(f'[dim]Source:[/dim] {source}')


@app.command('deprioritize')
@async_command
async def deprioritize_memory(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the memory unit to deprioritize.')],
    vault: Annotated[
        str | None,
        typer.Option(
            '--vault',
            '-v',
            help='Vault name or UUID. Defaults to the active vault.',
        ),
    ] = None,
    reason: Annotated[
        str,
        typer.Option('--reason', '-r', help='Why this unit is being deprioritized.'),
    ] = 'manual',
):
    """
    Deprioritize a memory unit (non-destructive). The unit remains accessible via
    `include_deprioritized=true` retrieval. Use `memex memory restore <id>` to undo.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    async with get_api_context(config) as api:
        try:
            resolved_vault = await resolve_active_vault(api, config, vault)
            unit = await api.deprioritize_memory_unit(
                uuid_obj, reason=reason, vault_id=resolved_vault
            )
        except Exception as e:
            handle_api_error(e)
            return

    console.print(
        f'[green]Memory unit {unit.id} deprioritized.[/green]  reason=[dim]{reason}[/dim]'
    )


@app.command('restore')
@async_command
async def restore_memory(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the memory unit to restore.')],
    vault: Annotated[
        str | None,
        typer.Option(
            '--vault',
            '-v',
            help='Vault name or UUID. Defaults to the active vault.',
        ),
    ] = None,
):
    """Restore a deprioritized memory unit (flips ``is_deprioritized`` back to false)."""
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    async with get_api_context(config) as api:
        try:
            resolved_vault = await resolve_active_vault(api, config, vault)
            unit = await api.restore_memory_unit(uuid_obj, vault_id=resolved_vault)
        except Exception as e:
            handle_api_error(e)
            return

    console.print(f'[green]Memory unit {unit.id} restored.[/green]')


@app.command('reconsolidate')
@async_command
async def reconsolidate_memory(
    ctx: typer.Context,
    entity_id: Annotated[str, typer.Argument(help='UUID of the entity to reconsolidate.')],
    vault: VaultOption = None,
):
    """Re-evaluate every memory unit linked to one entity. **Use sparingly.**

    Acquires a per-entity advisory lock (serializes against the scheduler's
    own reflection of this entity), runs contradiction detection across the
    full set of units that mention the entity, then triggers the Hindsight
    reflection cycle on the entity's mental model.

    When to use:
        - You (or an agent) have concrete evidence that this entity's
          mental model is wrong or contains contradictions — e.g. `memex
          lint findings` flagged it, retrieval is returning inconsistent
          answers, or you just resolved a maintenance proposal that merged
          two entities and the survivor needs its model rebuilt.
        - Targeted, deliberate maintenance. Always scoped to one entity.

    When NOT to use:
        - For routine maintenance. The scheduler reflects entities on a
          timer; calling this manually duplicates that work.
        - Across many entities. Run them one at a time with evidence —
          batch reconsolidation is what the scheduler is for.

    Cost: LLM-intensive. Contradiction detection plus a full reflection
    pass — typically multiple LLM calls per linked memory unit. A noisy
    entity with hundreds of units can cost dollars per invocation. The
    advisory lock means a concurrent scheduler reflection on the same
    entity will block until this completes.
    """
    config: MemexConfig = ctx.obj
    entity_uuid = parse_uuid(entity_id, 'entity')

    async with get_api_context(config) as api:
        try:
            vault_uuid = await resolve_active_vault(api, config, vault)
            result = await api.reconsolidate_entity(entity_uuid, vault_uuid)
        except Exception as e:
            handle_api_error(e)
            return

    emit_json(result)


@app.command('consolidate')
@async_command
async def consolidate_memory(
    ctx: typer.Context,
    vault: VaultOption = None,
    dry_run: Annotated[
        bool,
        typer.Option('--dry-run', help='Preview without making changes.'),
    ] = False,
):
    """Vault-wide low-Memory-Worth unit consolidation. **Use sparingly.**

    Scans every active memory unit in the vault, computes the FSFM composite
    deprioritization score (graph_pressure, mw_complement, temporal_staleness,
    entity_dormancy, weighted against the importance baseline), and flips
    units below the auto-band threshold to `is_deprioritized=True`. Those
    units stop appearing in retrieval unless `include_deprioritized=true`
    is passed; restore one with `memex memory restore <id>`.

    When to use:
        - After a large bulk ingest, to flush noise before a high-stakes
          retrieval session.
        - When `memex diagnostics retrieval` shows high-volume / low-MW
          entities and you want to flip their weakest units in one pass.

    When NOT to use:
        - For routine maintenance. The scheduler runs the same scorer +
          auto-band on a timer; on-demand calls duplicate work the
          background loop will do anyway.
        - For per-entity cleanup — prefer `memex memory reconsolidate
          <entity-uuid>`, which is scoped and runs contradiction detection
          first.

    Cost: DB scan + score computation over every active unit in the vault.
    No LLM calls. Use `--dry-run` to preview which units would flip without
    writing.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            vault_uuid = await resolve_active_vault(api, config, vault)
            result = await api.consolidate_vault(vault_uuid, dry_run=dry_run)
        except Exception as e:
            handle_api_error(e)
            return

    emit_json(result)


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


@app.command('search')
@async_command
async def search_memory(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Search query.')],
    vault: Annotated[
        list[str] | None,
        typer.Option(
            '--vault', '-v', help='Vault(s) to search. Accepts names or UUIDs. Use "*" for all.'
        ),
    ] = None,
    limit: int = 5,
    token_budget: Annotated[
        int | None, typer.Option('--token-budget', '-t', help='Token budget for retrieval.')
    ] = None,
    answer: Annotated[
        bool, typer.Option('--answer', '-a', help='Generate an AI answer from results.')
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
    source_context: Annotated[
        str | None,
        typer.Option(
            '--source-context',
            help='Filter by source context (e.g. "user_notes").',
        ),
    ] = None,
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
    expand: Annotated[bool, typer.Option('--expand', help='Enable query expansion.')] = False,
    intent: Annotated[
        str | None,
        typer.Option(
            '--intent',
            help='Filter by intent class (permanent | durable | ephemeral).',
        ),
    ] = None,
    risk: Annotated[
        str | None,
        typer.Option(
            '--risk',
            help='Filter by risk class (none | sensitive | private | safety).',
        ),
    ] = None,
):
    """
    Search for memories.
    """
    config: MemexConfig = ctx.obj

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

    # Resolve vault_ids: explicit --vault flags take precedence, else use config
    vault_ids: list[str] | None = None
    if vault:
        vault_ids = [v.strip() for v in vault]
    else:
        vault_ids = config.read_vaults

    from datetime import datetime as _dt, timezone as _tz

    ref_dt = _dt.fromisoformat(reference_date).replace(tzinfo=_tz.utc) if reference_date else None

    # Validate intent / risk against allowed values BEFORE the API call so
    # users see a clean error instead of a server 422. Allowed sets are
    # canonical in memex_common.schemas (derived from IntentClass / RiskClass).
    # We coerce to the enum after validation to match the typed
    # RemoteMemexAPI.search signature (IntentClass | None / RiskClass | None).
    from memex_common.schemas import (
        IntentClass,
        RiskClass,
        VALID_INTENT_CLASSES,
        VALID_RISK_CLASSES,
    )

    intent_value: IntentClass | None = None
    if intent is not None:
        intent_str = intent.lower()
        if intent_str not in VALID_INTENT_CLASSES:
            console.print(
                f'[red]Invalid --intent {intent!r}. Allowed: {sorted(VALID_INTENT_CLASSES)}[/red]'
            )
            raise typer.Exit(1)
        intent_value = IntentClass(intent_str)

    risk_value: RiskClass | None = None
    if risk is not None:
        risk_str = risk.lower()
        if risk_str not in VALID_RISK_CLASSES:
            console.print(
                f'[red]Invalid --risk {risk!r}. Allowed: {sorted(VALID_RISK_CLASSES)}[/red]'
            )
            raise typer.Exit(1)
        risk_value = RiskClass(risk_str)

    async with get_api_context(config) as api:
        try:
            results = await api.search(
                query=query,
                limit=limit,
                token_budget=token_budget,
                strategies=strategies,
                vault_ids=vault_ids,
                include_stale=include_stale,
                source_context=source_context,
                reference_date=ref_dt,
                expand_query=expand,
                intent_class=intent_value,
                risk_class=risk_value,
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
                console.print(f'- \\[{unit.fact_type}] {text}')
            return

        if json_output:
            emit_json([u.model_dump(exclude={'embedding'}) for u in results])
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

            # Check unit_metadata for source info
            source = 'Unknown'
            if unit.metadata:
                source = unit.metadata.get('note_name', 'Unknown')
                if source == 'Unknown':
                    source = unit.metadata.get('filestore_path', 'Unknown')

            table.add_row(unit.fact_type, content_preview, str(source))

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


@app.command('links')
@async_command
async def memory_links(
    ctx: typer.Context,
    unit_id: Annotated[str, typer.Argument(help='UUID of the memory unit.')],
    link_type: Annotated[
        str | None,
        typer.Option('--type', '-t', help='Filter by link type (e.g. contradicts).'),
    ] = None,
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max links to return.')] = 20,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View relationship links for a memory unit.
    Shows temporal, semantic, causal, contradiction, and other typed links.
    """
    config: MemexConfig = ctx.obj
    uuid_obj = parse_uuid(unit_id, 'memory unit')

    async with get_api_context(config) as api:
        try:
            links = await api.get_memory_links(uuid_obj, link_type=link_type, limit=limit)
        except Exception as e:
            handle_api_error(e)
            return

    if not links:
        console.print('[dim]No links found.[/dim]')
        return

    if json_output:
        emit_json([lnk.model_dump() for lnk in links])
        return

    table = Table(title=f'Links for {unit_id[:8]}...')
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
            if entity_type == 'note':
                response = await api.get_note_lineage(
                    note_id=uuid_obj,
                    direction=direction,
                    depth=depth,
                    limit=limit,
                )
            else:
                response = await api.get_entity_lineage(
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
                or node.entity.get('title')
                or node.entity.get('text')
                or ''
            )
            if len(name) > 50:
                name = name[:47] + '...'

            label = f'[bold cyan]{e_type}[/bold cyan] [dim]{e_id}[/dim]'
            if name:
                label += f': {name}'

            if tree is None:
                tree = Tree(label)
            else:
                tree = tree.add(label)

            for child in node.derived_from:
                build_tree(child, tree)
            return tree

        if json_output:
            emit_json(response.model_dump())
            return

        console.print(f'\n[bold green]Lineage Visualization ({direction.value})[/bold green]')
        tree = build_tree(response)
        console.print(tree)
