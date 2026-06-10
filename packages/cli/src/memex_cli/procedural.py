"""
Procedural-plane commands.

The CLI mirrors the HTTP /procedural/* surface. Two groups:

* ``memex procedural`` — CRUD on case / procedure / strategy entries
  (the third plane alongside notes and KV).
* ``memex case`` — short-form submit for cases. Cases are the durable
  record of specific experiences (what happened, with what trigger);
  ``memex case submit`` is the most common entry point and gets a
  top-level group with a single verb so it's discoverable without
  the heavier ``memex procedural create`` surface.

Engine internals (DTOs, error classes) still ship as
``memex_common.procedural_schemas`` and
``memex_core.services.procedural_repository``; the public CLI/HTTP
surface is ``procedural``.
"""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex_common.config import MemexConfig
from memex_common.procedural_schemas import (
    ProceduralEntryCreate,
    ProceduralEntryUpdate,
    ProceduralSearchRequest,
)
from memex_cli.utils import (
    async_command,
    emit_json,
    get_api_context,
    handle_api_error,
)

console = Console()

app = typer.Typer(
    name='procedural',
    help=(
        'Manage procedural-plane entries — cases (specific experiences), '
        'procedures (synthesised how-tos), and strategies (play-books). '
        'Identity-anchored on (kind, scope, verb, context).'
    ),
    no_args_is_help=True,
)

case_app = typer.Typer(
    name='case',
    help=(
        'Case-specific shortcuts. Cases are durable records of a specific '
        'experience — what happened, with what trigger. They are the input '
        'side of the procedural plane; procedures and strategies are derived.'
    ),
    no_args_is_help=True,
)


def _print_entry(entry, json_output: bool) -> None:
    """Render a single entry — JSON or Rich tree."""
    if json_output:
        emit_json(entry.model_dump(mode='json'))
        return
    console.print(f'[bold cyan]{entry.title}[/bold cyan]  [dim]({entry.kind}/{entry.scope})[/dim]')
    console.print(f'  [dim]id:[/dim] {entry.id}')
    console.print(f'  [dim]kind:[/dim] {entry.kind}  [dim]status:[/dim] {entry.status}')
    if entry.verb:
        console.print(
            f'  [dim]verb:[/dim] {entry.verb}  [dim]context:[/dim] {entry.context or "-"}'
        )
    if entry.trigger:
        console.print(f'  [dim]trigger:[/dim] {entry.trigger}')
    console.print(f'  [dim]summary:[/dim] {entry.summary}')
    if entry.body:
        console.print(f'\n{entry.body}')
    if entry.tags:
        console.print(f'\n  [dim]tags:[/dim] {", ".join(entry.tags)}')
    console.print(
        f'  [dim]created:[/dim] {entry.created_at.isoformat()}  '
        f'[dim]updated:[/dim] {entry.updated_at.isoformat()}'
    )


# ---------------------------------------------------------------------------
# procedural create
# ---------------------------------------------------------------------------


@app.command('create')
@async_command
async def procedural_create(
    ctx: typer.Context,
    kind: Annotated[
        str,
        typer.Argument(help='case | procedure | strategy.'),
    ],
    scope: Annotated[
        str, typer.Option('--scope', '-s', help='Identity scope (e.g. "user", "project:foo").')
    ],
    title: Annotated[str, typer.Option('--title', '-t', help='Entry title.')],
    summary: Annotated[str, typer.Option('--summary', help='One-paragraph summary.')],
    verb: Annotated[
        str | None,
        typer.Option('--verb', help='Required for procedure/strategy. Omit for case.'),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option('--context', '-c', help='Optional context within (kind, scope, verb).'),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option('--body', '-b', help='Long-form body (procedure/strategy).'),
    ] = None,
    trigger: Annotated[
        str | None,
        typer.Option('--trigger', help='Required for case. What triggered the experience.'),
    ] = None,
    tags: Annotated[
        list[str] | None,
        typer.Option('--tag', help='Repeatable tag. (--tag foo --tag bar)'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault name or UUID. Defaults to global.'),
    ] = None,
    status: Annotated[
        str, typer.Option('--status', help='draft | published | deprecated.')
    ] = 'draft',
    origin: Annotated[
        str, typer.Option('--origin', help='manual | derived | imported | kv_backfill | seed.')
    ] = 'manual',
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Create a new procedural-plane entry.

    Cases MUST omit --verb and --context; procedures/strategies REQUIRE
    --verb. Identity-anchor collision on (kind, scope, verb, context)
    is rejected; use ``memex procedural upsert`` for idempotent
    re-writes.
    """
    config: MemexConfig = ctx.obj
    if kind == 'case':
        if verb is not None or context is not None:
            console.print('[red]kind=case requires --verb and --context to be OMITTED.[/red]')
            raise typer.Exit(2)
        if not trigger:
            console.print('[red]kind=case requires --trigger.[/red]')
            raise typer.Exit(2)
    elif kind in ('procedure', 'strategy'):
        if not verb:
            console.print(f'[red]kind={kind} requires --verb.[/red]')
            raise typer.Exit(2)
    else:
        console.print(f'[red]Unknown kind: {kind!r}. Expected case|procedure|strategy.[/red]')
        raise typer.Exit(2)

    if vault is None:
        console.print('[red]--vault is required for procedural create.[/red]')
        raise typer.Exit(2)

    vault_id = await _resolve_vault_id(config, vault)
    payload = ProceduralEntryCreate(
        vault_id=vault_id,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary=summary,
        body=body or '',
        trigger=trigger,
        tags=tags or [],
        status=status,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
    )
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_create(payload)
        except Exception as e:
            handle_api_error(e)

    if json_output:
        emit_json(entry.model_dump(mode='json'))
    else:
        console.print(f'[green]Created:[/green] {entry.id}')
        _print_entry(entry, json_output=False)


# ---------------------------------------------------------------------------
# procedural upsert
# ---------------------------------------------------------------------------


@app.command('upsert')
@async_command
async def procedural_upsert(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help='case | procedure | strategy.')],
    scope: Annotated[str, typer.Option('--scope', '-s')],
    title: Annotated[str, typer.Option('--title', '-t')],
    summary: Annotated[str, typer.Option('--summary')],
    verb: Annotated[str | None, typer.Option('--verb')] = None,
    context: Annotated[str | None, typer.Option('--context', '-c')] = None,
    body: Annotated[str | None, typer.Option('--body', '-b')] = None,
    trigger: Annotated[str | None, typer.Option('--trigger')] = None,
    tags: Annotated[list[str] | None, typer.Option('--tag')] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault name or UUID. (required for upsert)'),
    ] = None,
    status: Annotated[
        str, typer.Option('--status', help='draft | published | deprecated.')
    ] = 'draft',
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Idempotent write on the (kind, scope, verb, context) anchor.

    Same anchor → UPDATE (appends a version row); new anchor → INSERT.
    Status is preserved (deprecated stays deprecated).
    """
    config: MemexConfig = ctx.obj
    if vault is None:
        console.print('[red]--vault is required for procedural upsert.[/red]')
        raise typer.Exit(2)
    vault_id = await _resolve_vault_id(config, vault)
    payload = ProceduralEntryCreate(
        vault_id=vault_id,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary=summary,
        body=body or '',
        trigger=trigger,
        tags=tags or [],
        status=status,  # type: ignore[arg-type]
    )
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_upsert(payload)
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json(entry.model_dump(mode='json'))
    else:
        console.print(f'[green]Upserted:[/green] {entry.id}')


# ---------------------------------------------------------------------------
# procedural get / get-by-identity / list
# ---------------------------------------------------------------------------


@app.command('get')
@async_command
async def procedural_get(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Entry UUID.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Fetch a single entry by UUID."""
    from uuid import UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_get(UUID(entry_id))
        except Exception as e:
            handle_api_error(e)
    _print_entry(entry, json_output=json_output)


@app.command('get-by-identity')
@async_command
async def procedural_get_by_identity(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help='case | procedure | strategy.')],
    scope: Annotated[str, typer.Option('--scope', '-s')],
    verb: Annotated[str | None, typer.Option('--verb')] = None,
    context: Annotated[str | None, typer.Option('--context', '-c')] = None,
    vault: Annotated[str | None, typer.Option('--vault', '-v')] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Look up a single entry by (kind, scope, verb, context). Prints nothing on miss."""
    from uuid import UUID

    config: MemexConfig = ctx.obj
    vault_id: UUID | None = None
    if vault is not None:
        vault_id = await _resolve_vault_id(config, vault)
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_get_by_identity(
                kind=kind,
                scope=scope,
                verb=verb,
                context=context,
                vault_id=vault_id,
            )
        except Exception as e:
            handle_api_error(e)
    if entry is None:
        console.print('[yellow]No entry found.[/yellow]')
        raise typer.Exit(1)
    _print_entry(entry, json_output=json_output)


@app.command('search')
@async_command
async def procedural_search(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Argument(help='Search text. (omit for filter-only)')] = None,
    scope: Annotated[
        str | None,
        typer.Option('--scope', '-s', help='Restrict to a single scope.'),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option('--kind', help='case | procedure | strategy. (omit for all)'),
    ] = None,
    status: Annotated[
        str, typer.Option('--status', help='draft | published | deprecated.')
    ] = 'published',
    limit: Annotated[
        int,
        typer.Option(
            '--limit',
            '-l',
            help='Maximum number of results to return.',
        ),
    ] = 10,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Hybrid BM25 + vector search across the procedural plane."""
    config: MemexConfig = ctx.obj
    request = ProceduralSearchRequest(
        query=query,
        scope=scope,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        limit=limit,
    )
    async with get_api_context(config) as api:
        try:
            result = await api.procedural_search(request)
        except Exception as e:
            handle_api_error(e)
    if not result.hits:
        console.print('[yellow]No hits.[/yellow]')
        return
    if json_output:
        emit_json(result.model_dump(mode='json'))
        return
    table = Table(title=f'Procedural search ("{query or "(no query)"}")')
    table.add_column('Title', style='cyan')
    table.add_column('Kind', style='bold')
    table.add_column('Scope', style='dim')
    table.add_column('Verb', style='dim')
    table.add_column('Score', style='dim', justify='right')
    for hit in result.hits:
        table.add_row(
            hit.entry.title,
            hit.entry.kind,
            hit.entry.scope,
            hit.entry.verb or '-',
            f'{hit.score:.3f}',
        )
    console.print(table)


@app.command('briefing-cards')
@async_command
async def procedural_briefing_cards(
    ctx: typer.Context,
    context_key: Annotated[
        list[str],
        typer.Option('--context-key', '-c', help='Pin context key. (repeatable, required)'),
    ],
    scope: Annotated[
        str | None, typer.Option('--scope', '-s', help='Restrict to a single scope.')
    ] = None,
    limit_per_context: Annotated[
        int, typer.Option('--limit-per-context', help='Cap per-context card count.')
    ] = 5,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Pin-chain briefing cards for the session-briefing surface."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            result = await api.procedural_briefing_cards(
                list(context_key),
                scope=scope,
                limit_per_context=limit_per_context,
            )
        except Exception as e:
            handle_api_error(e)
    if not result.cards:
        console.print('[yellow]No pinned cards.[/yellow]')
        return
    if json_output:
        emit_json(result.model_dump(mode='json'))
        return
    table = Table(title='Briefing cards')
    table.add_column('Context', style='dim')
    table.add_column('Pos', style='dim', justify='right')
    table.add_column('Title', style='cyan')
    table.add_column('Kind', style='bold')
    table.add_column('Summary', style='white', ratio=2)
    for card in result.cards:
        table.add_row(
            card.context_key,
            str(card.pin_position),
            card.entry.title,
            card.entry.kind,
            card.entry.summary,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# procedural update / deprecate
# ---------------------------------------------------------------------------


@app.command('update')
@async_command
async def procedural_update(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Entry UUID.')],
    title: Annotated[str | None, typer.Option('--title', '-t')] = None,
    summary: Annotated[str | None, typer.Option('--summary')] = None,
    body: Annotated[str | None, typer.Option('--body', '-b')] = None,
    trigger: Annotated[str | None, typer.Option('--trigger')] = None,
    tags: Annotated[list[str] | None, typer.Option('--tag')] = None,
    status: Annotated[
        str | None,
        typer.Option('--status', help='draft | published | deprecated.'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Mutate an entry in place (appends a version row).

    Identity anchor (kind, scope, verb, context) is immutable — for
    identity changes, create a new entry and deprecate the old one.
    """
    from uuid import UUID

    config: MemexConfig = ctx.obj
    payload = ProceduralEntryUpdate(
        title=title,  # type: ignore[arg-type]
        summary=summary,
        body=body,
        trigger=trigger,
        tags=tags,
        status=status,  # type: ignore[arg-type]
    )
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_update(UUID(entry_id), payload)
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json(entry.model_dump(mode='json'))
    else:
        console.print(f'[green]Updated:[/green] {entry.id}')


@app.command('deprecate')
@async_command
async def procedural_deprecate(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Entry UUID.')],
    superseded_by: Annotated[
        str | None,
        typer.Option('--superseded-by', help='UUID of the entry that supersedes this one.'),
    ] = None,
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """Soft-deprecate an entry (status → 'deprecated')."""
    from uuid import UUID

    config: MemexConfig = ctx.obj
    if not force:
        if not typer.confirm(f'Deprecate entry {entry_id}?'):
            console.print('[yellow]Aborted.[/yellow]')
            return
    sb: UUID | None = UUID(superseded_by) if superseded_by else None
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_deprecate(UUID(entry_id), superseded_by_id=sb)
        except Exception as e:
            handle_api_error(e)
    console.print(f'[green]Deprecated:[/green] {entry.id}')


# ---------------------------------------------------------------------------
# memex case submit — short-form top-level group
# ---------------------------------------------------------------------------


@case_app.command('submit')
@async_command
async def case_submit(
    ctx: typer.Context,
    title: Annotated[str, typer.Option('--title', '-t', help='Case title.')],
    trigger: Annotated[
        str,
        typer.Option(
            '--trigger',
            help='What triggered the experience — required for findability.',
        ),
    ],
    summary: Annotated[
        str,
        typer.Option('--summary', help='One-paragraph summary of the experience.'),
    ],
    body: Annotated[
        str | None,
        typer.Option(
            '--body',
            '-b',
            help='Long-form body — what happened, what was tried, the outcome.',
        ),
    ] = None,
    scope: Annotated[
        str, typer.Option('--scope', '-s', help='Identity scope. (e.g. "user", "project:foo")')
    ] = 'user',
    tags: Annotated[list[str] | None, typer.Option('--tag')] = None,
    vault: Annotated[
        str | None,
        typer.Option(
            '--vault',
            '-v',
            help='Vault name or UUID. (defaults to active vault)',
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Submit a new case — durable record of a specific experience.

    Cases are the input side of the procedural plane: they record what
    happened, with what trigger. The derivation worker can later
    consolidate them into procedures or strategies.
    """
    config: MemexConfig = ctx.obj
    if vault is None:
        # Fall back to the active vault. The HTTP facade is the
        # authoritative resolver; this is a best-effort default.
        vault = config.server.default_active_vault
    vault_id = await _resolve_vault_id(config, vault)
    payload = ProceduralEntryCreate(
        vault_id=vault_id,
        kind='case',
        scope=scope,
        verb=None,
        context=None,
        title=title,
        summary=summary,
        body=body or '',
        trigger=trigger,
        tags=tags or [],
        status='draft',
        origin='manual',
    )
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_create(payload)
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json(entry.model_dump(mode='json'))
    else:
        console.print(f'[green]Case submitted:[/green] {entry.id}')
        _print_entry(entry, json_output=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _resolve_vault_id(config: MemexConfig, identifier: str):
    """Resolve a vault name or UUID string to a UUID via the running server."""
    from uuid import UUID

    try:
        return UUID(identifier)
    except ValueError:
        pass
    async with get_api_context(config) as api:
        try:
            return await api.resolve_vault_identifier(identifier)
        except Exception as e:
            handle_api_error(e)
