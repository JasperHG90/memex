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
    PageIndexDTO,
    PageMetadataDTO,
    ReflectionRequest,
    TOCNodeDTO,
)

prompts_dir = plb.Path(__file__).parent / 'prompts'

configure_logging(level='CRITICAL')

persona_logger = logging.getLogger('persona')
persona_logger.setLevel(os.getenv('PERSONA_LOG_LEVEL', 'INFO'))

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system. Follow this retrieval workflow:

STEP 1 — SEARCH: Pick by query type, or run both in parallel when unsure:
  - `memex_memory_search` (memory search): best for broad/exploratory queries ("What do I know about X?"). Returns atomic facts, events, observations across all notes.
  - `memex_note_search` (note search): best for targeted document lookup ("Find the doc about X"). Returns ranked source notes with snippets.
  When unsure, run both in parallel and combine results (deduplicate by Note ID).

STEP 2 — FILTER (parallel, per note): Call `memex_get_note_metadata` on each candidate Note ID.
  - Cheap (~50 tokens). Use title, description, and tags to confirm relevance.
  - Drop irrelevant notes BEFORE proceeding to Step 3.

STEP 3 — READ (only confirmed-relevant notes):
  - `memex_get_page_index` → get TOC and node IDs. Expensive — only after Step 2 confirms relevance.
  - `memex_get_node` (parallel) → read specific sections by node ID.
  - `memex_read_note` → fallback only, for very short notes.

RULES:
- Never skip Step 2. `memex_get_page_index` costs 5-10x more tokens than `memex_get_note_metadata`.
- Never use `memex_list_notes` for discovery.
- Parallelize aggressively: metadata calls together, node reads together.
""".strip(),
    version='0.1.0',
    lifespan=lifespan,
    log_level='CRITICAL',
)


@mcp.tool(
    name='memex_reflect',
    description='Trigger immediate reflection on an entity to synthesize its mental model from recent memories.',
)
async def memex_reflect(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
    limit: Annotated[int, Field(description='Recent memories to consider.')] = 20,
    vault_id: Annotated[
        str | None,
        Field(default=None, description='Vault UUID. Defaults to Global Vault.'),
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
    description='Get provenance chain of a memory unit, observation, note, or mental model.',
)
async def memex_get_lineage(
    ctx: Context,
    unit_id: Annotated[str, Field(description='Unit or observation UUID.')],
    entity_type: Annotated[
        str,
        Field(description='Type: memory_unit (default), observation, note, mental_model.'),
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
    note_id: Annotated[str, Field(description='Note UUID.')],
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
    description='Read full note content. FALLBACK ONLY — prefer memex_get_page_index + memex_get_node for section-level reading.',
)
async def memex_read_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
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
    description='Retrieve a file resource (image, audio, document) by path. Get paths from memex_list_assets or memex_get_lineage.',
)
async def memex_get_resource(
    ctx: Context,
    path: Annotated[str, Field(description='Resource path.')],
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
    description='Get a markdown template for memex_add_note.',
)
def memex_get_template(
    type: Annotated[
        NoteTemplateType,
        Field(
            description='Template type: technical_brief, general_note, architectural_decision_record, request_for_comments, quick_note.',
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
    description='Get the active vault name and ID.',
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
    description='Add a note to Memex. Confirm vault with user first, or pass vault_id.',
)
async def memex_add_note(
    ctx: Context,
    title: Annotated[
        str,
        Field(description='Note title.'),
    ],
    markdown_content: Annotated[
        str,
        Field(
            description='Markdown content. Keep concise: 5-15 lines, key insight only.',
        ),
    ],
    description: Annotated[
        str,
        Field(
            description='Summary, max 250 words. Cover context/intent and key insights.',
        ),
    ],
    author: Annotated[
        str,
        Field(description='Author name.'),
    ],
    tags: Annotated[
        list[str],
        Field(description='Tags for retrieval.'),
    ],
    supporting_files: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Absolute paths to supporting files.',
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Target vault UUID. Defaults to active vault.',
        ),
    ] = None,
    note_key: Annotated[
        str | None,
        Field(
            default=None,
            description='Stable key for incremental updates.',
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

        effective_note_key = note_key if note_key else f'mcp:add_note:{title}'

        note = NoteCreateDTO(
            name=title,
            description=description,
            content=base64.b64encode(full_content.encode('utf-8')),
            files=files_content,
            tags=tags,
            vault_id=vault_id,
            note_key=effective_note_key,
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
    name='memex_memory_search',
    description=(
        'Search extracted facts, events, and observations across all notes (memory search). '
        'Best for broad/exploratory queries. '
        'For targeted document lookup, use memex_note_search. When unsure, run both in parallel.'
    ),
)
async def memex_memory_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query.')],
    limit: Annotated[
        int,
        Field(description='Max results. Ignored when token_budget is set.'),
    ] = 10,
    vault_ids: Annotated[
        list[str] | None,
        Field(default=None, description='Vault UUIDs or names to search.'),
    ] = None,
    token_budget: Annotated[
        int | None,
        Field(
            description='Token budget. When set, overrides limit — packs results greedily to budget.',
        ),
    ] = None,
    strategies: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Strategies: semantic, keyword, graph, temporal, mental_model. Default: all.',
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

        output.append(
            'Tip: Use Note IDs above with memex_get_note_metadata to check relevance before deeper reading.'
        )
        return '\n'.join(output)

    except Exception as e:
        logging.error(f'Search failed: {e}', exc_info=True)
        raise ToolError(f'Search failed: {e}')


@mcp.tool(
    name='memex_note_search',
    description=(
        'Search source notes by hybrid retrieval (note search). '
        'Returns ranked notes with snippets. Best for targeted document lookup. '
        'For broad exploration, use memex_memory_search. When unsure, run both in parallel.'
    ),
)
async def memex_note_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query.')],
    limit: Annotated[int, Field(description='Max notes to return.')] = 5,
    expand_query: Annotated[bool, Field(description='LLM-based multi-query expansion.')] = False,
    reason: Annotated[
        bool,
        Field(description='Annotate results with relevant section IDs and reasoning.'),
    ] = False,
    vault_ids: Annotated[
        list[str] | None,
        Field(default=None, description='Vault UUIDs or names to search.'),
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

        lines.append(
            'Next: call memex_get_note_metadata on each Note ID to confirm relevance before reading sections.'
        )
        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'Note search failed: {e}', exc_info=True)
        raise ToolError(f'Note search failed: {e}')


@mcp.tool(
    name='memex_get_page_index',
    description=(
        'Get note TOC: section titles, summaries, and node IDs. '
        'Expensive \u2014 only call AFTER memex_get_note_metadata confirms relevance. '
        'Use returned node IDs with memex_get_node.'
    ),
)
async def memex_get_page_index(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
) -> str | PageIndexDTO:
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

        return PageIndexDTO(
            metadata=PageMetadataDTO(**(page_index.get('metadata') or {})),
            toc=[TOCNodeDTO.model_validate(n) for n in page_index.get('toc', [])],
        )

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get page index failed: {e}', exc_info=True)
        raise ToolError(f'Get page index failed: {e}')


@mcp.tool(
    name='memex_get_note_metadata',
    description=(
        'Cheap relevance check (~50 tokens): returns title, description, tags, publish date, source URI. '
        'Call on search results BEFORE memex_get_page_index to filter out irrelevant notes.'
    ),
)
async def memex_get_note_metadata(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
) -> str | PageMetadataDTO:
    """Get just the metadata from a note's page index."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        metadata = await api.get_note_metadata(uuid_obj)
        if metadata is None:
            return 'No metadata available for this note. The note may not have a page index.'

        return PageMetadataDTO(**metadata)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get note metadata failed: {e}', exc_info=True)
        raise ToolError(f'Get note metadata failed: {e}')


@mcp.tool(
    name='memex_get_node',
    description='Read a specific note section by node ID. Get node IDs from memex_get_page_index or memex_note_search reasoning. Call multiple in parallel when reading several sections.',
)
async def memex_get_node(
    ctx: Context,
    node_id: Annotated[str, Field(description='Node UUID.')],
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
    description='Async batch ingest of local files.',
)
async def memex_batch_ingest(
    ctx: Context,
    file_paths: Annotated[list[str], Field(description='Absolute file paths.')],
    vault_id: Annotated[
        str | None,
        Field(default=None, description='Target vault UUID.'),
    ] = None,
    batch_size: Annotated[int, Field(description='Files per batch chunk.')] = 32,
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
                note_key=str(path.absolute()),
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
    description='Get batch ingestion job status.',
)
async def memex_get_batch_status(
    ctx: Context,
    job_id: Annotated[str, Field(description='Batch job UUID.')],
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
    description='List all vaults.',
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
    description='List notes in active vault. NOT for discovery — use memex_memory_search or memex_note_search.',
)
async def memex_list_notes(
    ctx: Context,
    limit: Annotated[int, Field(description='Max notes to return.')] = 20,
    offset: Annotated[int, Field(description='Pagination offset.')] = 0,
    vault_id: Annotated[
        str | None, Field(default=None, description='Vault UUID or name filter.')
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
    description='List or search entities. Without query, returns top entities by relevance.',
)
async def memex_list_entities(
    ctx: Context,
    query: Annotated[
        str | None, Field(default=None, description='Search term to filter by name.')
    ] = None,
    limit: Annotated[int, Field(description='Max entities to return.')] = 20,
    vault_id: Annotated[
        str | None, Field(default=None, description='Vault UUID or name filter.')
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
    description='Get entity details by UUID.',
)
async def memex_get_entity(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
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
    description='Get memory units mentioning an entity.',
)
async def memex_get_entity_mentions(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
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
    description='Get co-occurring entities for a given entity.',
)
async def memex_get_entity_cooccurrences(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
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
    description='Get a memory unit by UUID.',
)
async def memex_get_memory_unit(
    ctx: Context,
    unit_id: Annotated[str, Field(description='Memory unit UUID.')],
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
    name='memex_migrate_note',
    description='Move a note and all associated data to a different vault.',
)
async def memex_migrate_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    target_vault_id: Annotated[str, Field(description='Target vault UUID or name.')],
) -> str:
    """Migrate a note to a different vault."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        result = await api.migrate_note(uuid_obj, target_vault_id)
        source = result.get('source_vault_id', 'unknown')
        target = result.get('target_vault_id', 'unknown')
        entities = result.get('entities_affected', 0)
        return (
            f'Note {note_id} migrated successfully.\n'
            f'Source vault: {source}\n'
            f'Target vault: {target}\n'
            f'Entities affected: {entities}'
        )

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Migrate note failed: {e}', exc_info=True)
        raise ToolError(f'Migrate note failed: {e}')


@mcp.tool(
    name='memex_ingest_url',
    description='Ingest content from a URL.',
)
async def memex_ingest_url(
    ctx: Context,
    url: Annotated[str, Field(description='URL to ingest.')],
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
