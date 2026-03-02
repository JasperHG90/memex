"FastMCP Memex server implementation"

import logging
import os
import pathlib as plb
import asyncio
import base64
from typing import Annotated, cast
from uuid import UUID
import mimetypes

import aiofiles
import httpx
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image, Audio, File
from fastmcp.exceptions import ToolError
from fastmcp.utilities.logging import configure_logging
from pydantic import Field

from memex_mcp.lifespan import lifespan, get_api
from memex_mcp.types import NoteTemplateType
from memex_common.schemas import (
    BatchJobStatus,
    IngestURLRequest,
    LineageResponse,
    NoteCreateDTO,
    ReflectionRequest,
)

prompts_dir = plb.Path(__file__).parent / 'prompts'

configure_logging(level='CRITICAL')

persona_logger = logging.getLogger('persona')
persona_logger.setLevel(os.getenv('PERSONA_LOG_LEVEL', 'INFO'))

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system.

Workflow:
1. Discovery: `memex_search` for facts/entities; `memex_note_search` for source documents.
2. Reading: `memex_get_page_index` → `memex_get_node`. Fall back to `memex_read_note` for small notes.
3. AVOID: `memex_list_notes` for discovery — use search tools instead.
""".strip(),
    version='0.1.0',
    lifespan=lifespan,
    log_level='CRITICAL',
)


@mcp.tool(
    name='memex_reflect',
    description=(
        'Trigger reflection on an entity to synthesize and update its mental model from recent memories. '
        'Reflection runs automatically in the background, but calling this tool triggers it immediately.'
    ),
)
async def memex_reflect(
    ctx: Context,
    entity_id: Annotated[str, Field(description='The UUID of the entity to reflect upon.')],
    limit: Annotated[int, Field(description='Limit recent memories to consider.')] = 20,
    vault_id: Annotated[
        str | None,
        Field(default=None, description='The UUID of the vault. Defaults to Global Vault.'),
    ] = None,
):
    """Reflect on an entity to update its mental model."""
    try:
        api = get_api(ctx)

        try:
            e_uuid = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        v_uuid = None
        if vault_id:
            try:
                v_uuid = UUID(vault_id)
            except ValueError:
                raise ToolError(f'Invalid Vault UUID: {vault_id}')

        req_kwargs: dict = {
            'entity_id': e_uuid,
            'limit_recent_memories': limit,
        }
        if v_uuid:
            req_kwargs['vault_id'] = v_uuid

        request = ReflectionRequest(**req_kwargs)

        results = await api.reflect_batch([request])

        if not results:
            return 'Reflection produced no results.'

        res = results[0]
        output = [f'Reflection complete for entity {res.entity_id}.']
        output.append(f'Status: {res.status}')

        if res.new_observations:
            output.append('\nNew Observations:')
            for obs in res.new_observations:
                output.append(f'- {obs.title}: {obs.content}')
        else:
            output.append('\nNo new observations formed.')

        return '\n'.join(output)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Reflection failed: {e}', exc_info=True)
        raise ToolError(f'Reflection failed: {e}')


@mcp.tool(
    name='memex_get_lineage',
    description='Retrieve the provenance chain (lineage) of a memory unit, observation, note, or mental model.',
)
async def memex_get_lineage(
    ctx: Context,
    unit_id: Annotated[str, Field(description='The UUID of the memory unit or observation.')],
    entity_type: Annotated[
        str,
        Field(
            description='Entity type. Valid: "memory_unit" (default), "observation", "note", "mental_model".'
        ),
    ] = 'memory_unit',
) -> str:
    """Retrieve the lineage of a memory unit."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(unit_id)
        except ValueError:
            raise ToolError(f'Invalid Unit UUID: {unit_id}')

        lineage: LineageResponse
        if entity_type == 'note':
            lineage = await api.get_note_lineage(uuid_obj)
        else:
            lineage = await api.get_entity_lineage(uuid_obj)

        def format_lineage(node: LineageResponse, depth: int = 0) -> list[str]:
            indent = '  ' * depth
            entity_id = node.entity.get('id', 'Unknown ID')
            content = (
                node.entity.get('content')
                or node.entity.get('text')
                or node.entity.get('name')
                or 'No content'
            )
            if isinstance(content, bytes):
                content = '[Binary Content]'

            content_str = str(content)
            if len(content_str) > 100:
                content_str = content_str[:97] + '...'

            lines = [f'{indent}- [{node.entity_type}] {content_str} (ID: {entity_id})']

            for child in node.derived_from:
                lines.extend(format_lineage(child, depth + 1))

            return lines

        output = ['# Lineage Graph']
        output.extend(format_lineage(lineage))

        return '\n'.join(output)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get lineage failed: {e}', exc_info=True)
        raise ToolError(f'Get lineage failed: {e}')


@mcp.tool(
    name='memex_list_assets',
    description='List all file assets attached to a note.',
)
async def memex_list_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='The UUID of the note.')],
) -> str:
    """List assets for a note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        try:
            note = await api.get_note(uuid_obj)
        except FileNotFoundError:
            raise ToolError(f'Note {note_id} not found.')

        assets = note.assets

        if not assets:
            return 'No assets found for this note.'

        meta = note.doc_metadata
        name = meta.get('name') or meta.get('title') or 'Untitled'

        output = [f'Assets for Note: {name}']

        for asset_path in assets:
            path_obj = plb.Path(asset_path)
            filename = path_obj.name

            mime_type, _ = mimetypes.guess_type(filename)
            mime_str = f' ({mime_type})' if mime_type else ''

            output.append(f'- **{filename}**{mime_str}')
            output.append(f'  - Path: `{asset_path}`')

        return '\n'.join(output)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'List assets failed: {e}', exc_info=True)
        raise ToolError(f'List assets failed: {e}')


@mcp.tool(
    name='memex_read_note',
    description=(
        'Retrieve the full content and metadata of a note by its UUID. '
        'FALLBACK: Prefer memex_get_page_index + memex_get_node to read specific sections.'
    ),
)
async def memex_read_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='The UUID of the note to retrieve.')],
) -> str:
    """Read a full note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        note = await api.get_note(uuid_obj)
        meta = note.doc_metadata
        name = meta.get('name') or meta.get('title') or 'Untitled'

        output = [f'# {name}']

        description = meta.get('description')
        if description:
            output.append(f'> {description}')

        output.append(f'\n**ID:** {note.id}')
        output.append(f'**Vault:** {note.vault_id}')
        output.append(f'**Created:** {note.created_at}')

        content = note.original_text

        if content:
            output.append(f'\n## Content\n{content}')
        else:
            output.append('\n[No content available]')

        return '\n'.join(output)

    except FileNotFoundError:
        raise ToolError(
            f'Note with ID {note_id} not found. '
            'Note: Retrieving full source notes is only available for fact or event units. '
            'If you are attempting to read an observation, it does not have a single source note '
            'as it is a synthesized insight. Please check your search results for linkable unit types.'
        )
    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Read note failed: {e}', exc_info=True)
        raise ToolError(f'Read note failed: {e}')


@mcp.tool(
    name='memex_get_resource',
    description=(
        'Retrieve a file resource (image, audio, or document) by its path. '
        'Get asset paths from `memex_list_assets` (for notes) or `memex_get_lineage` (for memory units).'
    ),
)
async def memex_get_resource(
    ctx: Context,
    path: Annotated[str, Field(description='The path to the resource file.')],
) -> Image | Audio | File | str:
    """Retrieve a file resource. Returns an Image, Audio, or File object."""
    try:
        api = get_api(ctx)
        content_bytes = await api.get_resource(path)

        mime_type, _ = mimetypes.guess_type(path)

        if mime_type:
            if mime_type.startswith('image/'):
                return Image(data=content_bytes, format=mime_type.split('/')[-1])
            elif mime_type.startswith('audio/'):
                return Audio(data=content_bytes, format=mime_type.split('/')[-1])

        path_obj = plb.Path(path)
        filename = path_obj.name
        ext = path_obj.suffix.lstrip('.') if path_obj.suffix else None

        return File(data=content_bytes, name=filename, format=ext)

    except Exception as e:
        logging.error(f'Get resource failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve resource: {e}')


@mcp.tool(
    name='memex_get_template',
    description='Retrieve a markdown template for note creation. Use the returned template as the structure for `memex_add_note`.',
)
def memex_get_template(
    type: Annotated[
        NoteTemplateType,
        Field(
            description=(
                'Template type. '
                '`technical_brief`: structured technical summary. '
                '`general_note`: flexible note on any topic. '
                '`architectural_decision_record`: architectural decisions and rationale. '
                '`request_for_comments`: RFC/proposal document. '
                '`quick_note`: short informal note.'
            ),
            examples=[
                'technical_brief',
                'general_note',
                'architectural_decision_record',
                'request_for_comments',
                'quick_note',
            ],
        ),
    ],
) -> str:
    """Retrieve a markdown template for note creation."""
    try:
        if type == NoteTemplateType.TECHNICAL_BRIEF:
            return prompts_dir.joinpath('technical_brief_template.md').read_text()
        elif type == NoteTemplateType.GENERAL_NOTE:
            return prompts_dir.joinpath('general_note_template.md').read_text()
        elif type == NoteTemplateType.ADR:
            return prompts_dir.joinpath('adr_template.md').read_text()
        elif type == NoteTemplateType.RFC:
            return prompts_dir.joinpath('rfc_template.md').read_text()
        elif type == NoteTemplateType.QUICK_NOTE:
            return '# Note: [Insert title here]\n\n## Content\n[Content in markdown format]'
        else:
            raise ToolError(f'Unknown template type: {type}')

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get template failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve template: {e}')


@mcp.tool(
    name='memex_active_vault',
    description='Retrieve the currently active vault information.',
)
async def memex_active_vault(ctx: Context) -> str:
    """Retrieve the currently active vault information."""
    try:
        api = get_api(ctx)
        vault = await api.get_active_vault()
        if not vault:
            return 'No active vault found.'
        return f'Active Vault: {vault.name} (ID: {vault.id})'

    except Exception as e:
        logging.error(f'Get active vault failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve active vault: {e}')


@mcp.tool(
    name='memex_add_note',
    description=(
        'Add a note to the Memex knowledge base. '
        'Confirm the target vault with the user before calling; '
        'use `memex_active_vault` to check or `memex_list_vaults` to enumerate, '
        'or pass vault_id explicitly.'
    ),
)
async def memex_add_note(
    ctx: Context,
    title: Annotated[
        str,
        Field(
            description='The title of the note being added.',
            examples=['My First Note', 'Research on DuckDB'],
        ),
    ],
    markdown_content: Annotated[
        str,
        Field(
            description='Note content in markdown. Keep concise: 5-15 lines capturing the key insight, not a detailed report.',
        ),
    ],
    description: Annotated[
        str,
        Field(
            description='Summary of note content, max 250 words. Cover: (1) context and intent, (2) key insights.',
        ),
    ],
    author: Annotated[
        str,
        Field(description='Name of the model authoring this note.'),
    ],
    tags: Annotated[
        list[str],
        Field(
            description='List of tags to associate with the note for easier retrieval.',
            examples=[['research', 'duckdb', 'database']],
        ),
    ],
    supporting_files: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='List of file paths to any supporting files associated with the note.',
            examples=['/path/to/image1.png', '/path/to/data.csv'],
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='The UUID of the vault to add the note to. Defaults to the active vault.',
        ),
    ] = None,
    note_key: Annotated[
        str | None,
        Field(
            default=None,
            description='A unique stable key for the note to enable incremental updates.',
        ),
    ] = None,
    background: Annotated[
        bool,
        Field(default=False, description='Queue ingestion in background.'),
    ] = False,
):
    try:
        if len(description.split(' ')) > 250:
            raise ToolError('Description exceeds 250 words limit.')

        api = get_api(ctx)

        # Load supporting files
        files_content: dict[str, bytes] = {}
        if supporting_files:
            for file_path in supporting_files:
                path = plb.Path(file_path)
                if path.exists() and path.is_file():
                    async with aiofiles.open(path, 'rb') as f:
                        files_content[path.name] = base64.b64encode(await f.read())
                else:
                    logging.warning(f'Supporting file not found or not a file: {file_path}')

        # Construct frontmatter
        fm_data = {
            'title': title,
            'description': description,
            'author': author,
            'supporting_files': supporting_files,
            'tags': tags,
        }

        import yaml

        frontmatter = yaml.safe_dump(fm_data, sort_keys=False).strip()

        full_content = f"""
---
{frontmatter}
---

# {title}

{markdown_content}
        """.strip()

        note = NoteCreateDTO(
            name=title,
            description=description,
            content=base64.b64encode(full_content.encode('utf-8')),
            files=files_content,
            tags=tags,
            vault_id=vault_id,
            note_key=note_key,
        )

        result = await api.ingest(note, background=background)
        if isinstance(result, BatchJobStatus):
            return f'Note queued. Job ID: {result.job_id}'
        return f'Note added successfully. ID: {result.note_id}'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Add note failed: {e}', exc_info=True)
        raise ToolError(f'Add note failed: {e}')


@mcp.tool(
    name='memex_search',
    description='Search memory units (facts, events, observations) via multi-strategy retrieval. Returns Unit IDs and Note IDs.',
)
async def memex_search(
    ctx: Context,
    query: Annotated[str, Field(description='The search query.')],
    limit: Annotated[
        int,
        Field(description='Maximum number of results to return. Ignored when token_budget is set.'),
    ] = 10,
    vault_ids: Annotated[
        list[str] | None,
        Field(default=None, description='Optional list of vault UUIDs or names to search in.'),
    ] = None,
    token_budget: Annotated[
        int | None,
        Field(
            description=(
                'Optional token budget for retrieval. '
                'When set, this is the leading constraint — results are packed greedily '
                'until the budget is reached and the limit parameter is ignored.'
            )
        ),
    ] = None,
    strategies: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                'Optional inclusion list of strategies to run. '
                'Valid: semantic, keyword, graph, temporal, mental_model. '
                'If omitted, all strategies are used.'
            ),
        ),
    ] = None,
):
    """Search Memex for relevant information."""
    try:
        api = get_api(ctx)

        results = await api.search(
            query=query,
            limit=limit,
            vault_ids=cast(list[UUID | str] | None, vault_ids),
            token_budget=token_budget,
            strategies=strategies,
        )

        if not results:
            return 'No results found.'

        output = [f"Found {len(results)} results for '{query}':\n"]

        for i, res in enumerate(results, 1):
            score_str = f' ({res.score:.2f})' if res.score is not None else ''

            snippet = res.text[:300] + '...' if len(res.text) > 300 else res.text

            unit_type = getattr(res, 'fact_type', 'unknown')
            unit_type = getattr(unit_type, 'value', unit_type)

            # Include date if available
            date = res.mentioned_at or res.occurred_start
            date_str = f' ({date.isoformat()})' if date else ''

            output.append(
                f'{i}. [{unit_type}] [Unit: {res.id}] [Note: {res.note_id}]'
                f'{score_str}{date_str}\n   {snippet}\n'
            )

        return '\n'.join(output)

    except Exception as e:
        logging.error(f'Search failed: {e}', exc_info=True)
        raise ToolError(f'Search failed: {e}')


@mcp.tool(
    name='memex_note_search',
    description=(
        'Search source notes by hybrid retrieval (semantic + keyword + graph + temporal). '
        'Returns ranked notes with snippets. '
        'Use for whole notes; use `memex_search` for atomic facts.'
    ),
)
async def memex_note_search(
    ctx: Context,
    query: Annotated[str, Field(description='The note search query.')],
    limit: Annotated[int, Field(description='Maximum number of notes to return.')] = 5,
    expand_query: Annotated[
        bool, Field(description='Enable multi-query expansion via LLM.')
    ] = False,
    reason: Annotated[
        bool,
        Field(
            description=(
                'Identify relevant sections with reasoning. '
                'Note: prefer doing your own reasoning over search results rather than '
                'relying on this flag.'
            )
        ),
    ] = False,
    vault_ids: Annotated[
        list[str] | None,
        Field(default=None, description='Optional list of vault UUIDs or names to search in.'),
    ] = None,
) -> str:
    """Search Memex for source notes by hybrid retrieval."""
    try:
        api = get_api(ctx)
        results = await api.search_notes(
            query=query,
            limit=limit,
            expand_query=expand_query,
            reason=reason,
            summarize=False,
            vault_ids=vault_ids,
        )

        if not results:
            return f"No notes found for query: '{query}'"

        lines = [f"Found {len(results)} note(s) for '{query}':\n"]

        for i, doc in enumerate(results, 1):
            metadata = doc.metadata or {}
            title = (
                metadata.get('title')
                or metadata.get('name')
                or metadata.get('filename')
                or 'Untitled'
            )
            lines.append(f'## {i}. {title}')
            lines.append(f'- **Note ID:** {doc.note_id}')
            lines.append(f'- **Score:** {doc.score:.3f}')
            if src := metadata.get('source_uri'):
                lines.append(f'- **Source:** {src}')
            if doc.snippets:
                lines.append('- **Snippets:**')
                for snippet in doc.snippets[:2]:
                    text = snippet.text.strip()[:200]
                    prefix = f'*[{snippet.node_title}]* ' if snippet.node_title else ''
                    lines.append(f'  - {prefix}{text}')
            if doc.reasoning:
                lines.append('- **Relevant Sections:**')
                for section in doc.reasoning[:5]:
                    node_id = section.get('node_id', '')
                    reasoning_text = section.get('reasoning', '')
                    lines.append(f'  - Node `{node_id}`: {reasoning_text}')
            lines.append('')

        lines.append('Use memex_get_page_index / memex_get_node to read sections.')
        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'Note search failed: {e}', exc_info=True)
        raise ToolError(f'Note search failed: {e}')


@mcp.tool(
    name='memex_get_page_index',
    description='Get the hierarchical page index (table of contents) for a note. '
    'Returns the note structure with section titles, summaries, and node IDs. '
    'Use the node IDs with `memex_get_node` to retrieve specific section text.',
)
async def memex_get_page_index(
    ctx: Context,
    note_id: Annotated[str, Field(description='The UUID of the note.')],
) -> str:
    """Get the hierarchical page index for a note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        page_index = await api.get_note_page_index(uuid_obj)
        if page_index is None:
            return 'No page index available for this note. Only notes indexed with the page_index strategy have a table of contents.'

        import json as _json

        return _json.dumps(page_index, default=str, indent=2)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get page index failed: {e}', exc_info=True)
        raise ToolError(f'Get page index failed: {e}')


@mcp.tool(
    name='memex_get_node',
    description='Retrieve the full text content of a specific note section (node) by its ID. '
    'Node IDs can be found in search results (reasoning field) or via `memex_get_page_index`.',
)
async def memex_get_node(
    ctx: Context,
    node_id: Annotated[str, Field(description='The UUID of the node to retrieve.')],
) -> str:
    """Retrieve the full text content of a specific note node."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(node_id)
        except ValueError:
            raise ToolError(f'Invalid Node UUID: {node_id}')

        node = await api.get_node(uuid_obj)
        if node is None:
            raise ToolError(f'Node {node_id} not found.')

        title = node.title
        text = node.text
        doc_id = node.note_id
        level = node.level

        output = [f'# {"#" * level} {title}']
        output.append(f'**Node ID:** {node_id}')
        output.append(f'**Note ID:** {doc_id}')
        if text:
            output.append(f'\n{text}')
        else:
            output.append('\n[No text content]')

        return '\n'.join(output)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get node failed: {e}', exc_info=True)
        raise ToolError(f'Get node failed: {e}')


@mcp.tool(
    name='memex_batch_ingest',
    description='Asynchronously ingest multiple local files into Memex.',
)
async def memex_batch_ingest(
    ctx: Context,
    file_paths: Annotated[list[str], Field(description='List of absolute paths to local files.')],
    vault_id: Annotated[
        str | None,
        Field(default=None, description='Optional UUID of the vault to ingest into.'),
    ] = None,
    batch_size: Annotated[int, Field(description='Number of files to process per chunk.')] = 32,
) -> str:
    """Submit a batch of local files for asynchronous ingestion."""
    try:
        api = get_api(ctx)
        notes = []

        for path_str in file_paths:
            path = plb.Path(path_str)
            if not path.exists() or not path.is_file():
                continue

            async with aiofiles.open(path, 'rb') as f:
                content = await f.read()

            import base64

            note_dto = NoteCreateDTO(
                name=path.name,
                description=f'Imported from {path}',
                content=base64.b64encode(content),
                vault_id=vault_id,
            )
            notes.append(note_dto)

        if not notes:
            return 'No valid files found for ingestion.'

        job_ids: list[str] = []
        for note_dto in notes:
            result = await api.ingest(note_dto, background=True)
            job_ids.append(str(cast(BatchJobStatus, result).job_id))

        return (
            f'Batch ingestion started.\n'
            f'Files submitted: {len(notes)}\n'
            f'Job IDs:\n' + '\n'.join(f'- {jid}' for jid in job_ids) + '\n'
            'Use `memex_get_batch_status` with any job ID to track progress.'
        )

    except Exception as e:
        logging.error(f'Batch ingest failed: {e}', exc_info=True)
        raise ToolError(f'Batch ingest failed: {e}')


@mcp.tool(
    name='memex_get_batch_status',
    description='Retrieve the status and results of a batch ingestion job.',
)
async def memex_get_batch_status(
    ctx: Context,
    job_id: Annotated[str, Field(description='The UUID of the batch job.')],
) -> str:
    """Check the status of a batch ingestion job."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(job_id)
        except ValueError:
            raise ToolError(f'Invalid Job UUID: {job_id}')

        try:
            job = await api.get_job_status(uuid_obj)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ToolError(f'Job {job_id} not found.')
            raise

        output = [f'# Batch Job Status: {job.status.upper()}']
        output.append(f'Job ID: {job.job_id}')
        if job.progress:
            output.append(f'Progress: {job.progress}')

        if job.result:
            output.append(f'Processed: {job.result.processed_count}')
            output.append(f'Skipped: {job.result.skipped_count}')
            output.append(f'Failed: {job.result.failed_count}')

            if job.status == 'completed' and job.result.note_ids:
                output.append('\nCreated Note IDs (truncated):')
                for did in job.result.note_ids[:10]:
                    output.append(f'- {did}')
                if len(job.result.note_ids) > 10:
                    output.append(f'- ... and {len(job.result.note_ids) - 10} more.')

            if job.result.errors:
                output.append('\nErrors:')
                for err in job.result.errors[:5]:
                    output.append(f'- {err}')

        return '\n'.join(output)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get batch status failed: {e}', exc_info=True)
        raise ToolError(f'Get batch status failed: {e}')


@mcp.tool(
    name='memex_list_vaults',
    description='List all available vaults.',
)
async def memex_list_vaults(ctx: Context) -> str:
    """List all available vaults."""
    try:
        api = get_api(ctx)
        vaults = await api.list_vaults()

        if not vaults:
            return 'No vaults found.'

        lines = [f'Found {len(vaults)} vault(s):\n']
        for i, v in enumerate(vaults, 1):
            desc = f' — {v.description}' if v.description else ''
            lines.append(f'{i}. **{v.name}** (ID: {v.id}){desc}')

        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'List vaults failed: {e}', exc_info=True)
        raise ToolError(f'List vaults failed: {e}')


@mcp.tool(
    name='memex_list_notes',
    description=(
        'List notes in the active vault. '
        'NOT recommended for discovery — use memex_search or memex_note_search instead.'
    ),
)
async def memex_list_notes(
    ctx: Context,
    limit: Annotated[int, Field(description='Max notes to return.')] = 20,
    offset: Annotated[int, Field(description='Pagination offset.')] = 0,
    vault_id: Annotated[
        str | None, Field(default=None, description='Optional vault UUID or name to filter by.')
    ] = None,
) -> str:
    """List notes in the active vault."""
    try:
        api = get_api(ctx)
        resolved_vault_id = None
        if vault_id:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)
        notes = await api.list_notes(limit=limit, offset=offset, vault_id=resolved_vault_id)

        if not notes:
            return 'No notes found.'

        lines = [f'Found {len(notes)} note(s):\n']
        for i, n in enumerate(notes, 1):
            title = n.title or 'Untitled'
            lines.append(f'{i}. **{title}** (ID: {n.id}, created: {n.created_at})')

        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'List notes failed: {e}', exc_info=True)
        raise ToolError(f'List notes failed: {e}')


@mcp.tool(
    name='memex_list_entities',
    description='List or search entities in the knowledge graph. Without a query, returns top entities by relevance.',
)
async def memex_list_entities(
    ctx: Context,
    query: Annotated[
        str | None, Field(default=None, description='Optional search term to filter by name.')
    ] = None,
    limit: Annotated[int, Field(description='Max entities to return.')] = 20,
    vault_id: Annotated[
        str | None, Field(default=None, description='Optional vault UUID or name to filter by.')
    ] = None,
) -> str:
    """List or search entities."""
    try:
        api = get_api(ctx)
        resolved_vault_id = None
        if vault_id:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)

        if query:
            entities = await api.search_entities(query, limit=limit, vault_id=resolved_vault_id)
        else:
            entities = [
                e async for e in api.list_entities_ranked(limit=limit, vault_id=resolved_vault_id)
            ]

        if not entities:
            return 'No entities found.'

        lines = [f'Found {len(entities)} entity/entities:\n']
        for i, e in enumerate(entities, 1):
            lines.append(f'{i}. **{e.name}** (ID: {e.id}, mentions: {e.mention_count})')

        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'List entities failed: {e}', exc_info=True)
        raise ToolError(f'List entities failed: {e}')


@mcp.tool(
    name='memex_get_entity',
    description='Get details for a specific entity by its UUID.',
)
async def memex_get_entity(
    ctx: Context,
    entity_id: Annotated[str, Field(description='The UUID of the entity.')],
) -> str:
    """Get entity details."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        entity = await api.get_entity(uuid_obj)

        lines = [
            f'# Entity: {entity.name}',
            f'**ID:** {entity.id}',
            f'**Mentions:** {entity.mention_count}',
        ]
        if entity.vault_id:
            lines.append(f'**Vault:** {entity.vault_id}')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity failed: {e}', exc_info=True)
        raise ToolError(f'Get entity failed: {e}')


@mcp.tool(
    name='memex_get_entity_mentions',
    description='Get memory units that mention a specific entity.',
)
async def memex_get_entity_mentions(
    ctx: Context,
    entity_id: Annotated[str, Field(description='The UUID of the entity.')],
    limit: Annotated[int, Field(description='Max mentions to return.')] = 10,
) -> str:
    """Get memory units mentioning an entity."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        mentions = await api.get_entity_mentions(uuid_obj, limit=limit)

        if not mentions:
            return 'No mentions found for this entity.'

        lines = [f'Found {len(mentions)} mention(s):\n']
        for i, m in enumerate(mentions, 1):
            unit = m.get('unit', {})
            note = m.get('note', {})
            text = str(unit.get('text', ''))[:200]
            unit_id = unit.get('id', 'N/A')
            note_id = note.get('id', 'N/A')
            fact_type = unit.get('fact_type', 'unknown')
            lines.append(
                f'{i}. [Type: {fact_type}] [Unit ID: {unit_id}] [Note ID: {note_id}]\n   {text}\n'
            )

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity mentions failed: {e}', exc_info=True)
        raise ToolError(f'Get entity mentions failed: {e}')


@mcp.tool(
    name='memex_get_entity_cooccurrences',
    description='Get entities that frequently co-occur with a given entity.',
)
async def memex_get_entity_cooccurrences(
    ctx: Context,
    entity_id: Annotated[str, Field(description='The UUID of the entity.')],
) -> str:
    """Get co-occurring entities."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        cooccurrences = await api.get_entity_cooccurrences(uuid_obj)

        if not cooccurrences:
            return 'No co-occurrences found for this entity.'

        lines = [f'Found {len(cooccurrences)} co-occurring entity/entities:\n']
        for i, c in enumerate(cooccurrences, 1):
            e1 = c.get('entity_id_1', 'N/A')
            e2 = c.get('entity_id_2', 'N/A')
            count = c.get('cooccurrence_count', 0)
            other_id = e2 if str(e1) == entity_id else e1
            lines.append(f'{i}. Entity ID: {other_id} (co-occurrences: {count})')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity cooccurrences failed: {e}', exc_info=True)
        raise ToolError(f'Get entity cooccurrences failed: {e}')


@mcp.tool(
    name='memex_get_memory_unit',
    description='Retrieve a specific memory unit by its UUID.',
)
async def memex_get_memory_unit(
    ctx: Context,
    unit_id: Annotated[str, Field(description='The UUID of the memory unit.')],
) -> str:
    """Retrieve a memory unit by ID."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(unit_id)
        except ValueError:
            raise ToolError(f'Invalid Unit UUID: {unit_id}')

        unit = await api.get_memory_unit(uuid_obj)

        fact_type = getattr(unit.fact_type, 'value', unit.fact_type)

        lines = [
            '# Memory Unit',
            f'**ID:** {unit.id}',
            f'**Type:** {fact_type}',
            f'**Status:** {unit.status}',
            f'**Note ID:** {unit.note_id}',
        ]
        if unit.mentioned_at:
            lines.append(f'**Mentioned at:** {unit.mentioned_at}')
        if unit.occurred_start:
            lines.append(f'**Occurred:** {unit.occurred_start} — {unit.occurred_end or "ongoing"}')
        if unit.metadata:
            meta_str = str(unit.metadata)
            if len(meta_str) > 200:
                meta_str = meta_str[:197] + '...'
            lines.append(f'**Metadata:** {meta_str}')
        lines.append(f'\n## Text\n{unit.text}')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get memory unit failed: {e}', exc_info=True)
        raise ToolError(f'Get memory unit failed: {e}')


@mcp.tool(
    name='memex_ingest_url',
    description='Ingest content from a URL into Memex.',
)
async def memex_ingest_url(
    ctx: Context,
    url: Annotated[str, Field(description='The URL to ingest.')],
    vault_id: Annotated[
        str | None, Field(default=None, description='Target vault UUID. Defaults to active vault.')
    ] = None,
    background: Annotated[
        bool, Field(default=True, description='Queue ingestion in background.')
    ] = True,
) -> str:
    """Ingest content from a URL."""
    try:
        api = get_api(ctx)

        request = IngestURLRequest(url=url, vault_id=vault_id)
        result = await api.ingest_url(request, background=background)

        if isinstance(result, dict):
            status = result.get('status', 'unknown')
            job_id = result.get('job_id', '')
            msg = f'URL ingestion queued. Status: {status}'
            if job_id:
                msg += f', Job ID: {job_id}'
            return msg

        return f'URL ingested. Note ID: {getattr(result, "note_id", "N/A")}'

    except Exception as e:
        logging.error(f'Ingest URL failed: {e}', exc_info=True)
        raise ToolError(f'Ingest URL failed: {e}')


def entrypoint():
    """Entrypoint for the MCP server."""
    asyncio.run(mcp.run_async(transport='stdio'))


if __name__ == '__main__':
    entrypoint()
