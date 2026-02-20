"""
Document Management Commands.
"""

import json
from typing import Annotated, Any
from uuid import UUID
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from memex_common.config import MemexConfig
from memex_cli.utils import get_api_context, async_command, handle_api_error

console = Console()

app = typer.Typer(
    name='document',
    help='Manage and view source documents.',
    no_args_is_help=True,
)


@app.command('list')
@async_command
async def list_documents(
    ctx: typer.Context,
    limit: int = 50,
    offset: int = 0,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one document ID per line.')
    ] = False,
):
    """
    List all documents.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            docs = await api.list_documents(limit=limit, offset=offset)
        except Exception as e:
            handle_api_error(e)

    if minimal:
        for d in docs:
            console.print(str(d.id))
        return

    if json_output:
        console.print_json(json.dumps([d.model_dump() for d in docs], default=str))
        return

    table = Table(title='Documents')
    table.add_column('Title', style='cyan')
    table.add_column('Created At', style='dim')
    table.add_column('ID', style='dim')

    for d in docs:
        table.add_row(d.name or 'Untitled', str(d.created_at), str(d.id))

    console.print(table)


@app.command('recent')
@async_command
async def list_recent(
    ctx: typer.Context,
    limit: int = 10,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[
        bool, typer.Option('--minimal', help='Output one document ID per line.')
    ] = False,
):
    """
    Show most recent documents.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            docs = await api.get_recent_documents(limit=limit)
        except Exception as e:
            handle_api_error(e)

    if minimal:
        for d in docs:
            console.print(str(d.id))
        return

    if json_output:
        console.print_json(json.dumps([d.model_dump() for d in docs], default=str))
        return

    table = Table(title='Recent Documents')
    table.add_column('Title', style='cyan')
    table.add_column('Created At', style='green')
    table.add_column('ID', style='dim')

    for d in docs:
        table.add_row(d.name or 'Untitled', str(d.created_at), str(d.id))

    console.print(table)


@app.command('delete')
@async_command
async def delete_document(
    ctx: typer.Context,
    doc_id: Annotated[str, typer.Argument(help='UUID of the document to delete.')],
    force: Annotated[bool, typer.Option('--force', '-f', help='Skip confirmation.')] = False,
):
    """
    Delete a document and all associated data (memory units, chunks, links, assets).
    """
    config: MemexConfig = ctx.obj

    try:
        uuid_obj = UUID(doc_id)
    except ValueError:
        console.print(f'[red]Invalid UUID: {doc_id}[/red]')
        return

    if not force:
        if not typer.confirm(
            f'Are you sure you want to delete document {doc_id}? This is destructive.'
        ):
            console.print('[yellow]Aborted.[/yellow]')
            return

    async with get_api_context(config) as api:
        try:
            success = await api.delete_document(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if success:
        console.print(f'[green]Document {doc_id} deleted successfully.[/green]')
    else:
        console.print(f'[red]Document {doc_id} not found.[/red]')


@app.command('view')
@async_command
async def view_document(
    ctx: typer.Context,
    doc_id: Annotated[str, typer.Argument(help='UUID of the document.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
):
    """
    View content and metadata of a document.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            uuid_obj = UUID(doc_id)
            doc = await api.get_document(uuid_obj)
        except ValueError:
            console.print(f'[red]Invalid UUID: {doc_id}[/red]')
            return
        except Exception as e:
            handle_api_error(e)
            return

    if json_output:
        console.print_json(json.dumps(doc.model_dump(), default=str))
        return

    name = doc.name or 'Untitled Document'
    doc_id_val = doc.id
    created_at = doc.created_at
    doc_metadata = doc.doc_metadata
    original_text = doc.original_text or ''

    console.print(f'\n[bold cyan]{name}[/bold cyan]')
    console.print(f'[dim]ID: {doc_id_val}[/dim]')
    console.print(f'[dim]Created: {created_at}[/dim]')

    if doc_metadata:
        console.print('\n[bold]Metadata:[/bold]')
        console.print(doc_metadata)

    console.print('\n[bold]Content:[/bold]')
    console.print(Markdown(original_text))


@app.command('page-index')
@async_command
async def view_page_index(
    ctx: typer.Context,
    doc_id: Annotated[str, typer.Argument(help='UUID of the document.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View the page index (slim tree) of a document."""
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        try:
            uuid_obj = UUID(doc_id)
            page_index = await api.get_document_page_index(uuid_obj)
        except ValueError:
            console.print(f'[red]Invalid UUID: {doc_id}[/red]')
            return
        except Exception as e:
            handle_api_error(e)
            return

    if page_index is None:
        console.print('[yellow]This document has no page index.[/yellow]')
        console.print(
            '[dim]Only documents ingested with the page_index strategy have a slim tree.[/dim]'
        )
        return

    if json_output:
        console.print_json(json.dumps(page_index, default=str))
        return

    # page_index may be a list (raw TOC nodes) or a dict with a 'toc' key
    nodes = page_index if isinstance(page_index, list) else page_index.get('toc', [])

    tree = Tree(f'[bold cyan]Page Index[/bold cyan] [dim]({doc_id})[/dim]')
    _render_toc_nodes(nodes, tree)
    console.print(tree)


@app.command('node')
@async_command
async def view_node(
    ctx: typer.Context,
    node_id: Annotated[str, typer.Argument(help='UUID of the node.')],
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
) -> None:
    """View a specific page-index node (section) by its ID."""
    config: MemexConfig = ctx.obj

    try:
        uuid_obj = UUID(node_id)
    except ValueError:
        console.print(f'[red]Invalid UUID: {node_id}[/red]')
        return

    async with get_api_context(config) as api:
        try:
            node = await api.get_node(uuid_obj)
        except Exception as e:
            handle_api_error(e)
            return

    if node is None:
        console.print(f'[yellow]Node {node_id} not found.[/yellow]')
        return

    if json_output:
        console.print_json(json.dumps(node.model_dump(), default=str))
        return

    title = node.title or '(untitled)'
    heading = '#' * node.level
    console.print(f'\n[bold cyan]{heading} {title}[/bold cyan]')
    console.print(f'[dim]Node ID: {node.id}[/dim]')
    console.print(f'[dim]Document ID: {node.document_id}[/dim]')
    console.print(f'[dim]Level: {node.level} | Seq: {node.seq} | Status: {node.status}[/dim]')

    if node.text:
        console.print()
        console.print(Panel(Markdown(node.text), title='Content', border_style='green'))
    else:
        console.print('\n[dim][No text content][/dim]')


def _render_toc_nodes(nodes: list[dict[str, Any]], parent: Tree) -> None:
    """Recursively add TOC nodes to a Rich Tree."""
    for node in nodes:
        level = node.get('level', 1)
        title = node.get('title', '(untitled)')
        tokens = node.get('token_estimate') or 0
        label = f'[bold]{"#" * level}[/bold] {title}'
        if tokens:
            label += f' [dim]({tokens} tokens)[/dim]'
        summary = node.get('summary') or {}
        if what := summary.get('what'):
            label += f'\n  [dim italic]{what}[/dim italic]'
        branch = parent.add(label)
        _render_toc_nodes(node.get('children', []), branch)


@app.command('search')
@async_command
async def search_documents(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help='Search query.')],
    limit: Annotated[int, typer.Option('--limit', '-l', help='Max number of documents.')] = 5,
    expand: Annotated[bool, typer.Option('--expand', help='Enable query expansion.')] = False,
    blend: Annotated[bool, typer.Option('--blend', help='Enable position-aware blending.')] = False,
    vault: Annotated[list[str], typer.Option('--vault', '-v', help='Vault(s) to search.')] = [],
    reason: Annotated[
        bool,
        typer.Option('--reason', help='Run skeleton-tree identification; shows relevant sections.'),
    ] = False,
    summarize: Annotated[
        bool,
        typer.Option('--summarize', help='Synthesize a full answer (implies --reason).'),
    ] = False,
    json_output: Annotated[bool, typer.Option('--json', help='Output as JSON.')] = False,
    minimal: Annotated[bool, typer.Option('--minimal', help='Output document IDs only.')] = False,
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
):
    """
    Search for documents using multi-channel fusion (RRF).
    """
    config: MemexConfig = ctx.obj
    fusion_strategy = 'position_aware' if blend else 'rrf'

    # Compute strategy inclusion list from exclusion flags
    all_strategies = ['semantic', 'keyword', 'graph', 'temporal']
    exclusions = {
        'semantic': no_semantic,
        'keyword': no_keyword,
        'graph': no_graph,
        'temporal': no_temporal,
    }
    active = [s for s in all_strategies if not exclusions[s]]
    strategies: list[str] | None = active if len(active) < len(all_strategies) else None

    if strategies is not None:
        console.print(f'[dim]Active strategies: {", ".join(strategies)}[/dim]')

    async with get_api_context(config) as api:
        try:
            results = await api.search_documents(
                query=query,
                limit=limit,
                vault_ids=vault or None,
                expand_query=expand,
                fusion_strategy=fusion_strategy,
                strategies=strategies,
                reason=reason,
                summarize=summarize,
            )
        except Exception as e:
            handle_api_error(e)
            return

    if not results:
        console.print('[yellow]No documents found.[/yellow]')
        return

    if minimal:
        for doc in results:
            console.print(str(doc.document_id))
        return

    if json_output:
        console.print_json(json.dumps([r.model_dump() for r in results], default=str))
        return

    table = Table(title=f'Search Results: "{query}"', show_lines=True)
    table.add_column('Score', style='magenta', justify='right', no_wrap=True)
    table.add_column('Title', style='cyan', ratio=2)
    table.add_column('Preview', style='white', ratio=4)
    table.add_column('ID', style='dim', no_wrap=True)

    for doc in results:
        metadata = doc.metadata or {}
        title = (
            metadata.get('name') or metadata.get('title') or metadata.get('filename') or 'Untitled'
        )

        # Aggregate snippets into a single preview string
        preview_texts = [s.text.strip() for s in doc.snippets]
        preview = ' ... '.join(preview_texts) if preview_texts else '[No preview available]'

        # Truncate preview if it's excessively long (though Rich wraps, this keeps it cleaner)
        if len(preview) > 300:
            preview = preview[:297] + '...'

        score_str = f'{doc.score:.2f}' if doc.score > 0 else '-'

        table.add_row(score_str, title, preview, str(doc.document_id))

    console.print(table)
    console.print('\n[dim]Tip: Use `memex document view <ID>` to see the full document.[/dim]')

    # Display relevant sections when --reason (but not --summarize)
    if reason and not summarize:
        has_reasoning = any(r.reasoning for r in results)
        if has_reasoning:
            console.print('\n[bold]Relevant sections:[/bold]')
            for doc in results:
                if not doc.reasoning:
                    continue
                metadata = doc.metadata or {}
                doc_title = (
                    metadata.get('name')
                    or metadata.get('title')
                    or metadata.get('filename')
                    or 'Untitled'
                )
                for item in doc.reasoning:
                    node_uuid = item.get('node_uuid', '')
                    reasoning_text = item.get('reasoning', '')
                    node_ref = f'[dim]({node_uuid})[/dim] ' if node_uuid else ''
                    console.print(f'  [cyan]{doc_title}[/cyan] → {node_ref}"{reasoning_text}"')
            console.print(
                '\n[dim]Tip: Use `memex document node <node-uuid>` '
                "to view a section's full text.[/dim]"
            )

    # Display LLM-synthesized answer when --summarize is used
    if summarize:
        answer_text = next((r.answer for r in results if r.answer), None)
        if answer_text:
            console.print(Panel(Markdown(answer_text), title='Answer', border_style='green'))
            console.print()
