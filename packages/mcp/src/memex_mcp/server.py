"FastMCP Memex server implementation"

import logging
import os
import pathlib as plb
import asyncio
import base64
from typing import Annotated
from uuid import UUID
import mimetypes

import aiofiles
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image, Audio, File
from fastmcp.exceptions import ToolError
from fastmcp.utilities.logging import configure_logging
from pydantic import Field

from memex_mcp.lifespan import lifespan, get_api
from memex_mcp.types import NoteTemplateType
from memex_common.schemas import BatchJobStatus, ReflectionRequest, LineageResponse, NoteCreateDTO

prompts_dir = plb.Path(__file__).parent / 'prompts'

configure_logging(level='CRITICAL')

persona_logger = logging.getLogger('persona')
persona_logger.setLevel(os.getenv('PERSONA_LOG_LEVEL', 'INFO'))

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system.

    Capabilities:
    1. Memory Search (`memex_search`): Find atomic facts, experiences, and mental models.
       Results include Unit IDs and Document IDs.
    2. Document Search (`memex_doc_search`): Find source documents using hybrid retrieval
       (semantic + keyword + graph + temporal). Returns ranked documents with text snippets.
       Use answer=True to synthesize an answer from the retrieved sections.
    3. Deep Dive:
       - Use `memex_get_lineage` with a Unit ID to trace the source of an Observation or Fact.
       - Use `memex_list_assets` with a Document ID to find attached files (images, PDFs).
       - Use `memex_read_note` with a Document ID (if type is not Observation) to read full text.
    4. Retrieval:
       - Use `memex_get_resource` to retrieve file content (images, audio) discovered via `memex_list_assets`.

    Workflow:
    - For specific facts or experiences: use `memex_search`.
    - For whole documents or notes: use `memex_doc_search`.
    - Summarize the search results to answer the user's query.
    - Ask them how they want to follow up.
    """.strip(),
    version='0.1.0',
    lifespan=lifespan,
    log_level='CRITICAL',
)


@mcp.tool(
    name='memex_reflect',
    description='Run the "Hindsight" reflection loop on a specific entity to update its mental model.',
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
    """
    Reflect on an entity to update its mental model.
    """
    try:
        api = get_api(ctx)

        try:
            e_uuid = UUID(entity_id)
        except ValueError:
            return f'Invalid Entity UUID: {entity_id}'

        v_uuid = None
        if vault_id:
            try:
                v_uuid = UUID(vault_id)
            except ValueError:
                return f'Invalid Vault UUID: {vault_id}'

        req_kwargs = {
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

    except Exception as e:
        logging.error(f'Reflection failed: {e}', exc_info=True)
        return f'Reflection failed: {str(e)}'


@mcp.tool(
    name='memex_get_lineage',
    description='Retrieve the lineage (source trail) of a memory unit or observation.',
)
async def memex_get_lineage(
    ctx: Context,
    unit_id: Annotated[str, Field(description='The UUID of the memory unit or observation.')],
    entity_type: Annotated[
        str, Field(description='The type of the entity (e.g., "memory_unit", "observation").')
    ] = 'memory_unit',
) -> str:
    """
    Retrieve the lineage of a memory unit.
    """
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(unit_id)
        except ValueError:
            return f'Invalid Unit UUID: {unit_id}'

        lineage: LineageResponse = await api.get_lineage(
            entity_type=entity_type, entity_id=uuid_obj
        )

        # Helper to format lineage recursively
        def format_lineage(node: LineageResponse, depth: int = 0) -> list[str]:
            indent = '  ' * depth
            entity_id = node.entity.get('id', 'Unknown ID')
            # Try to find meaningful content
            content = (
                node.entity.get('content')
                or node.entity.get('text')
                or node.entity.get('name')
                or 'No content'
            )
            if isinstance(content, bytes):
                content = '[Binary Content]'

            # Truncate content
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

    except Exception as e:
        logging.error(f'Get lineage failed: {e}', exc_info=True)
        return f'Get lineage failed: {str(e)}'


@mcp.tool(
    name='memex_list_assets',
    description='List all file assets attached to a document.',
)
async def memex_list_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='The UUID of the note.')],
) -> str:
    """
    List assets for a note.
    """
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            return f'Invalid Note UUID: {note_id}'

        try:
            note = await api.get_note(uuid_obj)
        except FileNotFoundError:
            return f'Note {note_id} not found.'

        assets = note.get('assets', [])

        if not assets:
            return 'No assets found for this note.'

        meta = note.get('doc_metadata', {})
        name = meta.get('name') or meta.get('title') or 'Untitled'

        output = [f'Assets for Note: {name}']

        for asset_path in assets:
            path_obj = plb.Path(asset_path)
            filename = path_obj.name

            # Detect MIME type (simple guess)
            mime_type, _ = mimetypes.guess_type(filename)
            mime_str = f' ({mime_type})' if mime_type else ''

            output.append(f'- **{filename}**{mime_str}')
            output.append(f'  - Path: `{asset_path}`')

        return '\n'.join(output)

    except Exception as e:
        logging.error(f'List assets failed: {e}', exc_info=True)
        return f'List assets failed: {str(e)}'


@mcp.tool(
    name='memex_read_note',
    description='Retrieve the full content and metadata of a note from Memex using its Note UUID.',
)
async def memex_read_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='The UUID of the note to retrieve.')],
) -> str:
    """
    Read a full note.
    """
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            return f'Invalid Note UUID: {note_id}'

        note = await api.get_note(uuid_obj)
        meta = note.get('doc_metadata', {})
        name = meta.get('name') or meta.get('title') or 'Untitled'

        # Format the output
        output = [f'# {name}']

        description = meta.get('description')
        if description:
            output.append(f'> {description}')

        output.append(f'\n**ID:** {note["id"]}')
        output.append(f'**Vault:** {note["vault_id"]}')
        output.append(f'**Created:** {note["created_at"]}')

        content = note.get('original_text')

        if content:
            output.append(f'\n## Content\n{content}')
        else:
            output.append('\n[No content available]')

        return '\n'.join(output)

    except FileNotFoundError:
        raise ToolError(
            f'Note with ID {note_id} not found. '
            'Note: Retrieving full source notes is only available for fact, opinion, or experience units. '
            'If you are attempting to read an observation, it does not have a single source note '
            'as it is a synthesized insight. Please check your search results for linkable unit types.'
        )
    except Exception as e:
        logging.error(f'Read note failed: {e}', exc_info=True)
        return f'Read note failed: {str(e)}'


@mcp.tool(
    name='memex_get_resource',
    description='Retrieve a file resource (image, audio, or document) from the Memex knowledge base. '
    'IMPORTANT:'
    '1. For document ids: you can retrieve the list of assets using `memex_list_assets` and then use the paths '
    'to retrieve specific files with this tool.'
    '2. For memory units and observations: you need to first trace the lineage using `memex_get_lineage` '
    'to find source documents and their attached assets, then use the asset paths to retrieve files with this tool.',
)
async def memex_get_resource(
    ctx: Context,
    path: Annotated[str, Field(description='The path to the resource file.')],
) -> Image | Audio | File | str:
    """
    Retrieve a file resource. Returns an Image, Audio, or File object.
    """
    try:
        api = get_api(ctx)
        content_bytes = await api.get_resource(path)

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(path)

        if mime_type:
            if mime_type.startswith('image/'):
                return Image(data=content_bytes, format=mime_type.split('/')[-1])
            elif mime_type.startswith('audio/'):
                return Audio(data=content_bytes, format=mime_type.split('/')[-1])

        # Default to File for PDF, text, binary, etc.
        path_obj = plb.Path(path)
        filename = path_obj.name
        ext = path_obj.suffix.lstrip('.') if path_obj.suffix else None

        return File(data=content_bytes, name=filename, format=ext)

    except Exception as e:
        logging.error(f'Get resource failed: {e}', exc_info=True)
        return f'Failed to retrieve resource: {str(e)}'


@mcp.tool(
    name='memex_get_template',
    description='Retrieve a markdown template for note creation. '
    'CRITICAL CONSTRAINT: You MUST use the templates provided by this tool for note creation to ensure proper formatting and metadata structure.',
)
def memex_get_template(
    type: Annotated[
        NoteTemplateType,
        Field(
            description='The type of template to retrieve. Use: '
            '1. `technical_brief` for structured technical summaries and investigations. '
            '2. `general_note` for flexible note-taking on any topic. '
            '3. `architectural_decision_record` for documenting architectural decisions and their rationale. '
            '4. `quick_note` for quick, informal notes. Always include an informative title and a detailed description '
            'to ensure the note is useful and discoverable.',
            examples=[
                'technical_brief',
                'general_note',
                'architectural_decision_record',
                'quick_note',
            ],
        ),
    ],
) -> str:
    """
    Retrieve a markdown template for note creation.
    """
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
            return f'Unknown template type: {type}'

    except Exception as e:
        logging.error(f'Get template failed: {e}', exc_info=True)
        return f'Failed to retrieve template: {str(e)}'


@mcp.tool(
    name='memex_active_vault',
    description='Retrieve the currently active vault information.',
)
async def memex_active_vault(ctx: Context) -> str:
    """
    Retrieve the currently active vault information.
    """
    try:
        api = get_api(ctx)
        vault = await api.get_active_vault()
        if not vault:
            return 'No active vault found.'
        return f'Active Vault: {vault.name} (ID: {vault.id})'

    except Exception as e:
        logging.error(f'Get active vault failed: {e}', exc_info=True)
        return f'Failed to retrieve active vault: {str(e)}'


@mcp.tool(
    name='memex_add_note',
    description='Add a note to the Memex knowledge base. '
    'IMPORTANT: **before** you do anything, you MUST ask the user if the current active vault (retrievable using `memex_active_vault`) is correct '
    'for adding the note. If not, then you should ask them to specify the vault to add to (you can list vaults using `memex_list_vaults`). '
    'then use the vault_id parameter for adding the note.',
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
            description='The content of the note in markdown formatting. The note '
            '**MUST** adhere to the Memex note template structure.',
        ),
    ],
    description: Annotated[
        str,
        Field(
            description='A summary of the note content in maximum 250 words.'
            ' Must describe the following: (1) Context & Intent, (2) Synthesis'
            ' & Key Insights.',
        ),
    ],
    author: Annotated[
        str,
        Field(
            description='The name of the model adding the note.', examples=['gemini-2.5', 'gpt-4o']
        ),
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

        # Create NoteCreateDTO (data transfer object)
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

    except Exception as e:
        logging.error(f'Add note failed: {e}', exc_info=True)
        return f'Add note failed: {str(e)}'


@mcp.tool(
    name='memex_search',
    description='Search for memories, notes, and entities in the Memex knowledge base.',
)
async def memex_search(
    ctx: Context,
    query: Annotated[str, Field(description='The search query.')],
    limit: Annotated[int, Field(description='Maximum number of results to return.')] = 10,
    vault_ids: Annotated[
        list[str] | None,
        Field(default=None, description='Optional list of vault UUIDs or names to search in.'),
    ] = None,
    token_budget: Annotated[
        int | None, Field(description='Optional token budget for retrieval.')
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
    """
    Search Memex for relevant information.
    """
    try:
        api = get_api(ctx)

        # NB: setting opinion formation to True (no skipping)
        results = await api.search(
            query=query,
            limit=limit,
            vault_ids=vault_ids,
            skip_opinion_formation=False,
            token_budget=token_budget,
            strategies=strategies,
        )

        if not results:
            return 'No results found.'

        output = [f"Found {len(results)} results for '{query}':\n"]

        for i, res in enumerate(results, 1):
            # Format the output clearly
            # Use 'score' if available (it's in MemoryUnitDTO)
            score_str = f' (Score: {res.score:.2f})' if res.score is not None else ''

            # Truncate very long text
            snippet = res.text[:500] + '...' if len(res.text) > 500 else res.text

            # Note: res is a MemoryUnit (SQLModel) or MemoryUnitDTO
            unit_type = getattr(res, 'fact_type', 'unknown')
            if hasattr(unit_type, 'value'):
                unit_type = unit_type.value

            output.append(
                f'{i}. [Type: {unit_type}] [Unit ID: {res.id}] [Note ID: {res.note_id}]{score_str}\n   {snippet}\n'
            )

        return '\n'.join(output)

    except Exception as e:
        logging.error(f'Search failed: {e}', exc_info=True)
        return f'Search failed: {str(e)}'


@mcp.tool(
    name='memex_doc_search',
    description=(
        'Search for source notes using hybrid retrieval (semantic + keyword + graph + temporal). '
        'Returns ranked notes with text snippets. '
        'Use when you need original notes rather than individual facts '
        '(use `memex_search` for those). '
        'Set summarize=True to synthesize an answer from the retrieved sections. '
        'Set reason=True to identify relevant sections with reasoning (without full answer).'
    ),
)
async def memex_doc_search(
    ctx: Context,
    query: Annotated[str, Field(description='The note search query.')],
    limit: Annotated[int, Field(description='Maximum number of notes to return.')] = 5,
    expand_query: Annotated[
        bool, Field(description='Enable multi-query expansion via LLM.')
    ] = False,
    reason: Annotated[
        bool, Field(description='Identify relevant sections with reasoning.')
    ] = False,
    summarize: Annotated[
        bool,
        Field(
            description='Synthesize an answer from the retrieved sections (implies reason=True).'
        ),
    ] = False,
) -> str:
    """Search Memex for source notes by hybrid retrieval."""
    try:
        api = get_api(ctx)
        results = await api.search_notes(
            query=query, limit=limit, expand_query=expand_query, reason=reason, summarize=summarize
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
                for snippet in doc.snippets[:3]:
                    text = snippet.text.strip()[:300]
                    prefix = f'*[{snippet.node_title}]* ' if snippet.node_title else ''
                    lines.append(f'  - {prefix}{text}')
            if doc.reasoning:
                lines.append('- **Relevant Sections:**')
                for section in doc.reasoning[:5]:
                    node_id = section.get('node_id', '')
                    reasoning_text = section.get('reasoning', '')
                    lines.append(f'  - Node `{node_id}`: {reasoning_text}')
            lines.append('')

        if summarize:
            if ans := next((r.answer for r in results if r.answer), None):
                lines += ['---', '## Synthesized Answer', ans]

        lines.append('Tip: Use `memex_read_note` with a Note ID to read the full note.')
        lines.append('Tip: Use `memex_get_page_index` to browse the note table of contents.')
        lines.append('Tip: Use `memex_get_node` with a Node ID to retrieve specific section text.')
        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'Document search failed: {e}', exc_info=True)
        return f'Document search failed: {str(e)}'


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
            return f'Invalid Note UUID: {note_id}'

        page_index = await api.get_note_page_index(uuid_obj)
        if page_index is None:
            return 'No page index available for this note. Only notes indexed with the page_index strategy have a table of contents.'

        import json as _json

        return _json.dumps(page_index, default=str, indent=2)

    except Exception as e:
        logging.error(f'Get page index failed: {e}', exc_info=True)
        return f'Get page index failed: {str(e)}'


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
            return f'Invalid Node UUID: {node_id}'

        node = await api.get_node(uuid_obj)
        if node is None:
            return f'Node {node_id} not found.'

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

    except Exception as e:
        logging.error(f'Get node failed: {e}', exc_info=True)
        return f'Get node failed: {str(e)}'


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
    """
    Submit a batch of local files for asynchronous ingestion.
    """
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

            # Minimal NoteCreateDTO for batch ingestion
            note_dto = NoteCreateDTO(
                name=path.name,
                description=f'Imported from {path}',
                content=base64.b64encode(content).decode('utf-8'),
            )
            notes.append(note_dto)

        if not notes:
            return 'No valid files found for ingestion.'

        job_id = await api.batch_manager.create_job(
            notes=notes, vault_id=vault_id, batch_size=batch_size
        )

        return (
            f'Batch ingestion job created successfully.\n'
            f'Job ID: {job_id}\n'
            f'Total files: {len(notes)}\n'
            f'Use `memex_get_batch_status` to track progress.'
        )

    except Exception as e:
        logging.error(f'Batch ingest failed: {e}', exc_info=True)
        return f'Batch ingest failed: {str(e)}'


@mcp.tool(
    name='memex_get_batch_status',
    description='Retrieve the status and results of a batch ingestion job.',
)
async def memex_get_batch_status(
    ctx: Context,
    job_id: Annotated[str, Field(description='The UUID of the batch job.')],
) -> str:
    """
    Check the status of a batch ingestion job.
    """
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(job_id)
        except ValueError:
            return f'Invalid Job UUID: {job_id}'

        job = await api.batch_manager.get_job_status(uuid_obj)
        if not job:
            return f'Job {job_id} not found.'

        output = [f'# Batch Job Status: {job.status.upper()}']
        output.append(f'Job ID: {job.id}')
        output.append(f'Notes Total: {job.notes_count}')
        output.append(f'Processed: {job.processed_count}')
        output.append(f'Skipped: {job.skipped_count}')
        output.append(f'Failed: {job.failed_count}')

        if job.started_at:
            output.append(f'Started: {job.started_at}')
        if job.completed_at:
            output.append(f'Completed: {job.completed_at}')

        if job.status == 'completed' and job.note_ids:
            output.append('\nCreated Note IDs (truncated):')
            for did in job.note_ids[:10]:
                output.append(f'- {did}')
            if len(job.note_ids) > 10:
                output.append(f'- ... and {len(job.note_ids) - 10} more.')

        if job.error_info:
            output.append('\nErrors:')
            if isinstance(job.error_info, list):
                for err in job.error_info[:5]:
                    output.append(f'- Chunk at {err.get("chunk_start")}: {err.get("error")}')
            else:
                output.append(f'- {job.error_info}')

        return '\n'.join(output)

    except Exception as e:
        logging.error(f'Get batch status failed: {e}', exc_info=True)
        return f'Get batch status failed: {str(e)}'


def entrypoint():
    """
    Entrypoint for the MCP server.
    """
    asyncio.run(mcp.run_async(transport='stdio'))


if __name__ == '__main__':
    entrypoint()
