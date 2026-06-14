"""
Procedural-plane commands.

The CLI mirrors the HTTP /procedural/* surface. Two groups:

* ``memex procedural`` — CRUD on procedure / strategy entries (the
  third plane alongside notes and KV) + pin/version curation and the
  ``tui`` curation app.
* ``memex case`` — short-form submit for cases. Cases are NOTES
  (role=case) in a hidden system vault — the durable record of a
  worked episode; ``memex case submit`` is the common entry point.

Engine internals (DTOs, error classes) still ship as
``memex_common.procedural_schemas`` and
``memex_core.services.procedural_repository``; the public CLI/HTTP
surface is ``procedural``.
"""

import re
from pathlib import Path
from typing import Annotated, Any

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
        'Manage procedural-plane entries — procedures (worked how-tos) and '
        'strategies (play-books generalising procedures). Identity-anchored '
        'on (kind, scope, verb, context); strategies anchor on (scope, verb). '
        'Cases are NOTES — submit them via `memex case submit`.'
    ),
    no_args_is_help=True,
)

case_app = typer.Typer(
    name='case',
    help=(
        'Submit worked episodes as cases. A case is a NOTE (role=case) in a '
        'hidden system vault — the input side of the procedural plane; '
        'procedures and strategies are derived from case clusters.'
    ),
    no_args_is_help=True,
)


@case_app.callback()
def _case_callback() -> None:
    """Manage worked-episode cases — run ``memex case submit`` to file one.

    Without this callback Typer collapses a single-command group into the
    command itself, so ``memex case submit`` would fail with "unexpected extra
    argument (submit)" and only the bare ``memex case`` would run. The callback
    keeps ``case`` a real group so the documented ``memex case submit`` works.
    """


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
        typer.Argument(help='procedure | strategy. (Cases are notes — `memex case submit`.)'),
    ],
    scope: Annotated[
        str,
        typer.Option(
            '--scope', '-s', help='Identity scope: "global" | "project:<id>" | "app:<id>".'
        ),
    ],
    title: Annotated[str, typer.Option('--title', '-t', help='Entry title.')],
    summary: Annotated[str, typer.Option('--summary', help='One-paragraph summary.')],
    verb: Annotated[
        str | None,
        typer.Option('--verb', help='Anchor verb — required for both kinds.'),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            '--context',
            '-c',
            help='Anchor context — required for procedure; FORBIDDEN for strategy.',
        ),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option('--body', '-b', help='Long-form body (steps, references).'),
    ] = None,
    trigger: Annotated[
        str | None,
        typer.Option(
            '--trigger',
            help='when_to_use / when_to_apply — the retrieval key. REQUIRED.',
        ),
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
        str, typer.Option('--origin', help='manual | derived | import | seed.')
    ] = 'manual',
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Create a new procedural-plane entry (procedure or strategy).

    procedure REQUIRES --verb AND --context; strategy REQUIRES --verb
    and FORBIDS --context. --trigger is always required (the retrieval
    key). Identity-anchor collision on (kind, scope, verb, context) is
    rejected; use ``memex procedural upsert`` for idempotent re-writes.
    Cases are notes — use ``memex case submit``.
    """
    config: MemexConfig = ctx.obj
    if kind == 'procedure':
        if not verb or not context:
            console.print('[red]kind=procedure requires --verb AND --context.[/red]')
            raise typer.Exit(2)
    elif kind == 'strategy':
        if not verb:
            console.print('[red]kind=strategy requires --verb.[/red]')
            raise typer.Exit(2)
        if context is not None:
            console.print(
                '[red]kind=strategy FORBIDS --context — a strategy covers all '
                'procedures sharing (scope, verb).[/red]'
            )
            raise typer.Exit(2)
    else:
        console.print(
            f'[red]Unknown kind: {kind!r}. Expected procedure|strategy. '
            '(Cases are notes — use `memex case submit`.)[/red]'
        )
        raise typer.Exit(2)
    if not trigger:
        console.print('[red]--trigger is required (it is what retrieval matches on).[/red]')
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
    kind: Annotated[str, typer.Argument(help='procedure | strategy.')],
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
    kind: Annotated[str, typer.Argument(help='procedure | strategy.')],
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
        typer.Option('--kind', help='procedure | strategy. (omit for all)'),
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


@app.command('list')
@async_command
async def procedural_list(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option('--status', help='draft | published | deprecated. (omit for all)'),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option('--scope', '-s', help='Restrict to a single scope.'),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option('--kind', help='procedure | strategy. (omit for both)'),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Restrict to a single vault.'),
    ] = None,
    limit: Annotated[
        int,
        typer.Option('--limit', '-l', help='Maximum number of entries to return.'),
    ] = 50,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """List entries by lifecycle status, newest first.

    Unlike `search`, this needs no query — it is the way to enumerate the
    curation/governance queue, e.g. drafts awaiting confirmation:

        memex procedural list --status draft
    """
    config: MemexConfig = ctx.obj
    vault_id = await _resolve_vault_id(config, vault) if vault is not None else None
    async with get_api_context(config) as api:
        try:
            entries = await api.procedural_list(
                status=status, scope=scope, kind=kind, vault_id=vault_id, limit=limit
            )
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json([e.model_dump(mode='json') for e in entries])
        return
    if not entries:
        console.print('[yellow]No entries.[/yellow]')
        return
    table = Table(title=f'Procedural entries (status={status or "all"})')
    table.add_column('ID', style='dim')
    table.add_column('Title', style='cyan')
    table.add_column('Kind', style='bold')
    table.add_column('Scope', style='dim')
    table.add_column('Verb', style='dim')
    table.add_column('Status', style='dim')
    for entry in entries:
        table.add_row(
            str(entry.id),
            entry.title,
            entry.kind,
            entry.scope,
            entry.verb or '-',
            entry.status,
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

_VALID_OUTCOMES = ('success', 'failure', 'mixed')
_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_H1_RE = re.compile(r'^#\s+(.+?)\s*$', re.MULTILINE)
_SECTION_RE = re.compile(
    r'^##\s+(?P<head>.+?)\s*\n(?P<body>.*?)(?=^##\s+|\Z)', re.DOTALL | re.MULTILINE
)
_LIST_ITEM_RE = re.compile(r'^\s*(?:\d+[.)]|[-*])\s+(.*)$')


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Best-effort YAML frontmatter parse (yaml if available, else key: value)."""
    try:
        import yaml

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        out: dict[str, Any] = {}
        for line in text.splitlines():
            if ':' in line and not line.lstrip().startswith('#'):
                k, _, v = line.partition(':')
                out[k.strip()] = v.strip().strip('"\'')
        return out


def _parse_action_list(text: str) -> list[str]:
    """Pull ordered/bulleted list items out of the Actions section."""
    items: list[str] = []
    for line in text.splitlines():
        m = _LIST_ITEM_RE.match(line)
        if m and m.group(1).strip():
            items.append(m.group(1).strip())
    return items


def _split_outcome_lesson(text: str) -> tuple[str | None, str]:
    """`<outcome>. **Lesson:** …` or `Success — …\\n**Lessons:** …` → (outcome, lesson)."""
    stripped = text.strip()
    outcome: str | None = None
    low = stripped.lower()
    for oc in _VALID_OUTCOMES:
        if low.startswith(oc):
            outcome = oc
            break
    # Lesson is everything after a `**Lesson(s):**` marker, else the whole body.
    marker = re.search(r'\*\*Lessons?:?\*\*', stripped)
    lesson = stripped[marker.end() :].strip() if marker else stripped
    return outcome, lesson


def parse_case_markdown(content: str) -> dict[str, Any]:
    """Parse a case markdown file — the §5.1 episode template (## Trigger /
    ## Situation / ## Actions / ## Outcome / Lesson), optionally with YAML
    frontmatter — back into CaseSubmit fields. The deterministic inverse of the
    server's ``compose_case_markdown`` (no LLM). Returns only the fields it
    finds; the caller merges CLI flags over the top and validates the required
    ones (title / trigger / outcome)."""
    out: dict[str, Any] = {}

    body = content
    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        body = content[fm_match.end() :]
        fm = _parse_frontmatter(fm_match.group(1))
        for src, dst in (
            ('title', 'title'),
            ('note_key', 'title'),
            ('outcome', 'outcome'),
            ('project_id', 'project_id'),
            ('project', 'project_id'),
            ('case_of', 'case_of'),
        ):
            if fm.get(src) and dst not in out:
                out[dst] = str(fm[src])
        if fm.get('tags'):
            out['tags'] = fm['tags'] if isinstance(fm['tags'], list) else [str(fm['tags'])]

    if 'title' not in out:
        h1 = _H1_RE.search(body)
        if h1:
            out['title'] = h1.group(1).strip()

    sections = {
        m.group('head').strip().lower(): m.group('body').strip() for m in _SECTION_RE.finditer(body)
    }
    if sections.get('trigger'):
        out['trigger'] = sections['trigger']
    if 'situation' in sections:
        s = sections['situation']
        out['situation'] = '' if s in ('', '_not recorded_') else s
    if 'actions' in sections:
        out['actions'] = _parse_action_list(sections['actions'])
    ol = (
        sections.get('outcome / lesson')
        or sections.get('outcome/lesson')
        or sections.get('outcome')
    )
    if ol is not None:
        oc, lesson = _split_outcome_lesson(ol)
        if oc and 'outcome' not in out:
            out['outcome'] = oc
        if lesson:
            out.setdefault('lesson', lesson)
    return out


@case_app.command('submit')
@async_command
async def case_submit(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option(
            '--file',
            help='Read the case from a markdown file: the §5.1 template '
            '(## Trigger / ## Situation / ## Actions / ## Outcome / Lesson) '
            'plus optional YAML frontmatter (outcome, title/note_key, tags). '
            'Any flag below overrides the parsed value.',
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    title: Annotated[str | None, typer.Option('--title', '-t', help='Case title.')] = None,
    trigger: Annotated[
        str | None,
        typer.Option('--trigger', help='What kicked the episode off.'),
    ] = None,
    outcome: Annotated[
        str | None,
        typer.Option('--outcome', '-o', help='success | failure | mixed.'),
    ] = None,
    situation: Annotated[
        str,
        typer.Option('--situation', help='Context going in (prior state, constraints).'),
    ] = '',
    action: Annotated[
        list[str] | None,
        typer.Option('--action', '-a', help='Repeatable. One ordered step per flag.'),
    ] = None,
    lesson: Annotated[
        str,
        typer.Option('--lesson', '-l', help='What to do differently / confirm next time.'),
    ] = '',
    project_id: Annotated[
        str | None,
        typer.Option('--project-id', '-p', help='Provenance — recorded in metadata.'),
    ] = None,
    case_of: Annotated[
        str | None,
        typer.Option(
            '--case-of',
            help='UUID of the procedural entry this case instantiates (skips the judge).',
        ),
    ] = None,
    tags: Annotated[list[str] | None, typer.Option('--tag')] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    Submit a worked episode as a case.

    The case is filed as a NOTE (role=case) in the hidden system
    vault — no --vault flag; the server owns the placement. Without
    --case-of the server judges which procedure the case instances;
    contested judgments land in the lint queue.
    """
    from uuid import UUID as _UUID

    from memex_common.procedural_schemas import CaseSubmit

    config: MemexConfig = ctx.obj

    # --file seeds every field from the §5.1 template; explicit flags win.
    if file is not None:
        parsed = parse_case_markdown(file.read_text(encoding='utf-8'))
        title = title or parsed.get('title')
        trigger = trigger or parsed.get('trigger')
        outcome = outcome or parsed.get('outcome')
        situation = situation or parsed.get('situation', '')
        action = action if action else parsed.get('actions')
        lesson = lesson or parsed.get('lesson', '')
        project_id = project_id or parsed.get('project_id')
        case_of = case_of or parsed.get('case_of')
        tags = tags if tags else parsed.get('tags')

    missing = [
        name
        for name, val in (('--title', title), ('--trigger', trigger), ('--outcome', outcome))
        if not val
    ]
    if missing:
        console.print(
            f'[red]Missing required: {", ".join(missing)} — provide as flags or in the '
            '--file template/frontmatter.[/red]'
        )
        raise typer.Exit(2)
    if outcome not in _VALID_OUTCOMES:
        console.print(f'[red]--outcome must be success|failure|mixed, got {outcome!r}.[/red]')
        raise typer.Exit(2)
    case_of_uuid = None
    if case_of is not None:
        try:
            case_of_uuid = _UUID(case_of)
        except ValueError:
            console.print(f'[red]--case-of is not a valid UUID: {case_of!r}[/red]')
            raise typer.Exit(2)

    payload = CaseSubmit(
        title=title,
        trigger=trigger,
        situation=situation,
        actions=action or [],
        outcome=outcome,  # type: ignore[arg-type]
        lesson=lesson,
        project_id=project_id,
        case_of=case_of_uuid,
        submitted_by='memex-cli',
        tags=tags or [],
    )
    async with get_api_context(config) as api:
        try:
            result = await api.case_submit(payload)
        except Exception as e:
            handle_api_error(e)

    if json_output:
        emit_json(result.model_dump(mode='json'))
        return
    console.print(f'[green]Case filed:[/green] note {result.note_id}')
    a = result.assignment
    if a.mode == 'explicit':
        console.print(f'  assigned to entry {a.entry_id} (explicit --case-of)')
    elif a.mode == 'auto_assigned':
        console.print(f'  auto-assigned to entry {a.entry_id} (separation={a.separation})')
    elif a.mode == 'new_procedure_draft':
        console.print(f'  seeded draft procedure {a.entry_id} (separation={a.separation})')
    elif a.mode == 'escalated':
        console.print(
            f'  [yellow]assignment contested[/yellow] — lint finding {a.finding_id}; '
            'resolve via `memex lint resolve --action assign_case`'
        )


# ---------------------------------------------------------------------------
# pin / unpin / pins — briefing-chain curation (§18.8 / §19.8)
# ---------------------------------------------------------------------------


@app.command('pin')
@async_command
async def procedural_pin(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Procedural entry UUID.')],
    context_key: Annotated[
        str,
        typer.Option(
            '--context',
            '-c',
            help='Pin-chain context: "global" | "project:<id>" | "app:<id>".',
        ),
    ],
    position: Annotated[
        int | None,
        typer.Option('--position', help='0-based chain position. Omit to append.'),
    ] = None,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Pin an entry into a briefing context chain (cap 10 per context)."""
    from uuid import UUID as _UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            pin = await api.procedural_pin(
                _UUID(entry_id),
                context_key=context_key,
                position=position,
                pinned_by='memex-cli',
            )
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json(pin.model_dump(mode='json'))
    else:
        console.print(f'[green]Pinned[/green] {entry_id} at {pin.context_key}[{pin.position}]')


@app.command('unpin')
@async_command
async def procedural_unpin(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Procedural entry UUID.')],
    context_key: Annotated[str, typer.Option('--context', '-c', help='Pin-chain context key.')],
):
    """Unpin an entry from a context (idempotent)."""
    from uuid import UUID as _UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            removed = await api.procedural_unpin(_UUID(entry_id), context_key=context_key)
        except Exception as e:
            handle_api_error(e)
    if removed:
        console.print(f'[green]Unpinned[/green] {entry_id} from {context_key}')
    else:
        console.print(f'[yellow]No pin found[/yellow] for {entry_id} at {context_key}')


@app.command('pins')
@async_command
async def procedural_pins(
    ctx: typer.Context,
    context_key: Annotated[str, typer.Option('--context', '-c', help='Pin-chain context key.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """List pins for a context, position ascending."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            pins = await api.procedural_list_pins(context_key)
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json([p.model_dump(mode='json') for p in pins])
        return
    if not pins:
        console.print(f'[dim]No pins at {context_key}.[/dim]')
        return
    for p_ in pins:
        console.print(f'  [{p_.position}] {p_.entry_id}  [dim]{p_.pinned_by or ""}[/dim]')


# ---------------------------------------------------------------------------
# versions / diff / rollback — the non-destructive ledger (§18.8)
# ---------------------------------------------------------------------------


@app.command('versions')
@async_command
async def procedural_versions(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Procedural entry UUID.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """List the entry's uncapped version ledger, newest first."""
    from uuid import UUID as _UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            versions = await api.procedural_list_versions(_UUID(entry_id))
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json([v.model_dump(mode='json') for v in versions])
        return
    if not versions:
        console.print('[dim]No versions yet (versions appear after the first edit).[/dim]')
        return
    for v in versions:
        reason = f'  [dim]{v.edit_reason}[/dim]' if v.edit_reason else ''
        console.print(f'  v{v.version}  {v.created_at.isoformat()}  {v.title}{reason}')


@app.command('diff')
@async_command
async def procedural_diff(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Procedural entry UUID.')],
    from_version: Annotated[int, typer.Option('--from', help='Older version number.')],
    to_version: Annotated[
        int | None,
        typer.Option('--to', help='Newer version number. Omit for the newest.'),
    ] = None,
):
    """Unified diff between two ledger versions (body + trigger + title)."""
    import difflib
    from uuid import UUID as _UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            versions = await api.procedural_list_versions(_UUID(entry_id))
        except Exception as e:
            handle_api_error(e)
    by_num = {v.version: v for v in versions}
    if from_version not in by_num:
        console.print(f'[red]No version {from_version} in the ledger.[/red]')
        raise typer.Exit(2)
    newest = max(by_num) if by_num else None
    target = to_version if to_version is not None else newest
    if target not in by_num:
        console.print(f'[red]No version {target} in the ledger.[/red]')
        raise typer.Exit(2)

    def _render(v) -> list[str]:
        return (f'title: {v.title}\ntrigger: {v.trigger or ""}\n\n{v.body}').splitlines(
            keepends=True
        )

    diff = difflib.unified_diff(
        _render(by_num[from_version]),
        _render(by_num[target]),
        fromfile=f'v{from_version}',
        tofile=f'v{target}',
    )
    out = ''.join(diff)
    console.print(out if out else f'[dim]v{from_version} and v{target} are identical.[/dim]')


@app.command('rollback')
@async_command
async def procedural_rollback(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help='Procedural entry UUID.')],
    version: Annotated[int, typer.Option('--to', help='Version number to restore.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """Non-destructive rollback: the snapshot is re-applied as a NEW version."""
    from uuid import UUID as _UUID

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            entry = await api.procedural_rollback(
                _UUID(entry_id), version, rolled_back_by='memex-cli'
            )
        except Exception as e:
            handle_api_error(e)
    if json_output:
        emit_json(entry.model_dump(mode='json'))
    else:
        console.print(f'[green]Rolled back[/green] {entry.id} to v{version} (as a new version)')
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


# ---------------------------------------------------------------------------
# procedural tui — curation app (pins / briefing preview / versions)
# ---------------------------------------------------------------------------


@app.command('tui')
@async_command
async def procedural_tui(
    ctx: typer.Context,
    project_id: Annotated[
        str | None,
        typer.Option('--project-id', '-p', help='project:<id> context for the briefing chain.'),
    ] = None,
    app_identity: Annotated[
        str | None,
        typer.Option('--app', '-a', help='app:<id> context for the briefing chain.'),
    ] = None,
):
    """Launch the procedural-plane curation TUI.

    Browse + search entries, pin/unpin them into the briefing chain
    (global → project:<id> → app:<consumer>), preview the assembled
    briefing, and diff/rollback the version ledger.
    """
    from memex_cli.procedural_tui.app import ProceduralCurationApp
    from memex_cli.procedural_tui.controller import ProceduralCurationController

    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        controller = ProceduralCurationController(api)
        tui = ProceduralCurationApp(controller, project_id=project_id, app_identity=app_identity)
        await tui.run_async()
