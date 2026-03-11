"FastMCP Memex server implementation"

import logging
import os
import pathlib as plb
import asyncio
import base64
from typing import Annotated, Any
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
    NoteCreateDTO,
    PageIndexDTO,
    PageMetadataDTO,
    TOCNodeDTO,
)


def _validate_vault_ids(vault_ids: list[str]) -> list[str]:
    """Validate vault_ids is a real list, not a stringified JSON array."""
    if not isinstance(vault_ids, list):
        raise ToolError(
            f'vault_ids must be a list of strings, got {type(vault_ids).__name__}. '
            'Pass a JSON array, e.g. ["my-vault"], not a string.'
        )
    for v in vault_ids:
        if not isinstance(v, str):
            raise ToolError(f'Each vault_id must be a string, got {type(v).__name__}: {v!r}')
        if v.startswith('[') or v.startswith('"'):
            raise ToolError(
                f'vault_id looks like serialized JSON: {v!r}. '
                'Pass plain vault names/UUIDs, e.g. ["my-vault"].'
            )
    return vault_ids


async def _resolve_vault_ids(api: Any, vault_ids: list[str]) -> list['UUID | str']:
    """Resolve and validate that all vault identifiers exist."""
    resolved: list[UUID | str] = []
    for vid in vault_ids:
        try:
            r = await api.resolve_vault_identifier(vid)
            resolved.append(r)
        except Exception:
            raise ToolError(f'Vault not found: {vid!r}')
    return resolved


async def _resolve_vault_id(api: Any, vault_id: str) -> 'UUID':
    """Resolve and validate a single vault identifier exists."""
    try:
        return await api.resolve_vault_identifier(vault_id)
    except Exception:
        raise ToolError(f'Vault not found: {vault_id!r}')


prompts_dir = plb.Path(__file__).parent / 'prompts'

configure_logging(level='CRITICAL')

persona_logger = logging.getLogger('persona')
persona_logger.setLevel(os.getenv('PERSONA_LOG_LEVEL', 'INFO'))

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system.

ROUTING — select retrieval strategy by query type:

IF query asks about relationships, connections, "how X fits in", "what relates to X", or landscape:
  → ENTITY EXPLORATION (can combine with SEARCH)
  1. `memex_list_entities(query="X")` → entity IDs, types, mention counts
  2. `memex_get_entity_cooccurrences(entity_id)` → related entities with names, types, counts (single call, no follow-up needed)
  3. `memex_get_entity_mentions(entity_id)` → source facts linking back to notes
  4. Read source notes via SEARCH/READ below as needed

IF query asks about specific content, topics, or document lookup:
  → SEARCH
  1. `memex_memory_search` (broad/exploratory) and/or `memex_note_search` (targeted). Run in parallel.
  2. FILTER: after `memex_memory_search`, call `memex_get_notes_metadata` with Note IDs. After `memex_note_search`, metadata is inline — skip this.
  3. READ: `memex_get_page_indices` → `memex_get_nodes` (batch). `memex_read_note` only when total_tokens < 500.
  4. ASSETS: IF `has_assets: true` in page_index/metadata → call `memex_list_assets` then `memex_get_resources` with all paths at once. Use images as visual input. Reproduce diagrams as Mermaid/ASCII in response. NEVER create diagrams without checking assets first.

IF query is broad (e.g. "explain X and how it fits the architecture"):
  → Run ENTITY EXPLORATION and SEARCH in parallel, then synthesize.

RESPONSE FORMAT — MANDATORY for every response:
- Cite every claim from Memex with numbered references [1], [2], etc. inline.
- End response with a reference list. Each entry uses a type prefix:
  `[note]` title + note ID | `[memory]` title + memory ID + source note ID | `[asset]` filename + note ID
- Example: `[1] [note] Detailing the Sys Layer architecture — 2eb202ed-bee6-7b2a-f0b9-917e8d5dd6f0`

RULES:
- Only use IDs from tool output. Never fabricate IDs.
- Filter before reading. Never call `memex_get_page_indices` on unconfirmed notes.
- Never use `memex_recent_notes` for discovery.

`memex_memory_search` strategies: `["temporal"]` chronological, `["graph"]` entity-centric, `["mental_model"]` synthesized. Default (all) is best for general queries.
""".strip(),
    version='0.1.0',
    lifespan=lifespan,
    log_level='CRITICAL',
)


@mcp.tool(
    name='memex_list_assets',
    description='List file assets for a note. REQUIRED when has_assets is true. Feed paths to memex_get_resources.',
)
async def memex_list_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    vault_id: Annotated[
        str,
        Field(description="Vault UUID or name, e.g. 'rituals'."),
    ],
) -> str:
    """List assets for a note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        await _resolve_vault_id(api, vault_id)

        try:
            note = await api.get_note(uuid_obj)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError(f'Note {note_id} not found.')
            raise

        assets = note.assets

        if not assets:
            return 'No assets found for this note.'

        meta = note.doc_metadata
        name = note.title or meta.get('name') or meta.get('title') or 'Untitled'

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
    description='Read full note. ONLY when total_tokens < 500 (use force=True to override). Otherwise: memex_get_page_indices + memex_get_nodes.',
)
async def memex_read_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    force: Annotated[
        bool,
        Field(
            description='Override the 500-token limit and read the full note regardless of size.'
        ),
    ] = False,
) -> str:
    """Read a full note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        metadata = await api.get_note_metadata(uuid_obj)
        if metadata:
            total_tokens = metadata.get('total_tokens', 0)
            if not force and total_tokens and total_tokens >= 500:
                raise ToolError(
                    f'Note has {total_tokens} tokens (limit: 500). '
                    'Use force=True to override, or use '
                    'memex_get_page_indices + memex_get_nodes instead.'
                )

        note = await api.get_note(uuid_obj)
        meta = note.doc_metadata
        name = note.title or meta.get('name') or meta.get('title') or 'Untitled'

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
    name='memex_set_note_status',
    description=(
        'Set note lifecycle status: active, superseded, appended. '
        'When superseded, all memory units are marked stale. '
        'Optionally link to the replacing/parent note.'
    ),
)
async def memex_set_note_status(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    status: Annotated[
        str,
        Field(description='New status: active, superseded, or appended.'),
    ],
    linked_note_id: Annotated[
        str | None,
        Field(
            default=None,
            description='UUID of the note that supersedes/contains this one.',
        ),
    ] = None,
) -> str:
    """Set note lifecycle status."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        linked_uuid = None
        if linked_note_id:
            try:
                linked_uuid = UUID(linked_note_id)
            except ValueError:
                raise ToolError(f'Invalid linked Note UUID: {linked_note_id}')

        await api.set_note_status(uuid_obj, status, linked_uuid)
        return f'Note {note_id} status set to "{status}".'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Set note status failed: {e}', exc_info=True)
        raise ToolError(f'Set note status failed: {e}')


@mcp.tool(
    name='memex_rename_note',
    description='Rename a note. Updates title in metadata, page index, and doc_metadata.',
)
async def memex_rename_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    new_title: Annotated[str, Field(description='New title for the note.')],
) -> str:
    """Rename a note by updating its title across all stored locations."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        await api.update_note_title(uuid_obj, new_title)
        return f'Note {note_id} renamed to "{new_title}".'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Rename note failed: {e}', exc_info=True)
        raise ToolError(f'Rename note failed: {e}')


async def _fetch_single_resource(api: Any, path: str) -> Image | Audio | File | str:
    """Fetch a single resource by path. Raises on failure."""
    mime_type, _ = mimetypes.guess_type(path)
    # SVGs are XML text — Claude's vision API can't process them as images
    is_raster_image = (
        mime_type is not None and mime_type.startswith('image/') and mime_type != 'image/svg+xml'
    )

    # For local stores, return file:// URI to avoid base64 overhead
    local_path = api.get_resource_path(path) if hasattr(api, 'get_resource_path') else None
    if local_path and is_raster_image:
        return f'file://{local_path}'

    content_bytes = await api.get_resource(path)

    if mime_type:
        if is_raster_image:
            return Image(data=content_bytes, format=mime_type.split('/')[-1])
        elif mime_type.startswith('audio/'):
            return Audio(data=content_bytes, format=mime_type.split('/')[-1])

    path_obj = plb.Path(path)
    filename = path_obj.name
    ext = path_obj.suffix.lstrip('.') if path_obj.suffix else None

    return File(data=content_bytes, name=filename, format=ext)


@mcp.tool(
    name='memex_get_resources',
    description=(
        'Retrieve 1+ file resources (images, audio, documents) by path. '
        'Get paths from memex_list_assets. Accepts a single path or a list.'
    ),
)
async def memex_get_resources(
    ctx: Context,
    paths: Annotated[list[str], Field(description='Resource path(s).')],
    vault_id: Annotated[
        str,
        Field(description="Vault UUID or name, e.g. 'memex'."),
    ],
) -> list[Image | Audio | File | str]:
    """Retrieve file resources. Returns a list of Image, Audio, File, or error strings."""
    try:
        api = get_api(ctx)
        await _resolve_vault_id(api, vault_id)

        results: list[Image | Audio | File | str] = []
        for path in paths:
            try:
                results.append(await _fetch_single_resource(api, path))
            except Exception as exc:
                results.append(f'Error fetching {path}: {exc}')

        return results

    except Exception as e:
        logging.error(f'Get resource failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve resources: {e}')


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
    vault_id: Annotated[
        str,
        Field(description="Target vault UUID or name, e.g. 'rituals'."),
    ],
    supporting_files: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Absolute paths to supporting files.',
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

        msg = f'Note added successfully. ID: {result.note_id}'
        if result.overlapping_notes:
            msg += '\n\n⚠️ Similar notes detected:'
            for overlap in result.overlapping_notes:
                similarity_pct = int(overlap.similarity * 100)
                overlap_title = overlap.title or 'Untitled'
                msg += f'\n  - {overlap_title} ({similarity_pct}% similar) [{overlap.note_id}]'
        return msg

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
    vault_ids: Annotated[
        list[str],
        Field(
            description="Vault UUIDs or names, e.g. ['rituals'].",
        ),
    ],
    limit: Annotated[
        int,
        Field(description='Max results. Ignored when token_budget is set.'),
    ] = 10,
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
    include_superseded: Annotated[
        bool,
        Field(default=False, description='Include superseded (low-confidence) memory units.'),
    ] = False,
    after: Annotated[
        str | None,
        Field(default=None, description='Only results after this ISO 8601 date (e.g. 2025-01-01).'),
    ] = None,
    before: Annotated[
        str | None,
        Field(
            default=None, description='Only results before this ISO 8601 date (e.g. 2025-12-31).'
        ),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Field(default=None, description='Only results from notes with ALL of these tags.'),
    ] = None,
):
    """Search Memex for relevant information."""
    try:
        api = get_api(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(api, vault_ids)

        from datetime import datetime as _dt, timezone as _tz

        after_dt = _dt.fromisoformat(after).replace(tzinfo=_tz.utc) if after else None
        before_dt = _dt.fromisoformat(before).replace(tzinfo=_tz.utc) if before else None

        results = await api.search(
            query=query,
            limit=limit,
            vault_ids=resolved_vids,
            token_budget=token_budget,
            strategies=strategies,
            include_superseded=include_superseded,
            after=after_dt,
            before=before_dt,
            tags=tags,
        )

        if not results:
            return f"No results for '{query}'. Reformulate or try memex_note_search. Do not fabricate IDs."

        # Fetch note titles for enriched output
        note_ids = list({res.note_id for res in results if res.note_id})
        note_titles: dict[UUID, str] = {}
        if note_ids:
            try:
                metas = await api.get_notes_metadata(note_ids)
                for meta in metas:
                    nid_str = meta.get('note_id')
                    title = meta.get('title') or meta.get('name')
                    if nid_str and title:
                        note_titles[UUID(nid_str)] = title
            except Exception:
                pass  # Graceful degradation — titles are optional

        output = [f"Found {len(results)} results for '{query}':\n"]

        for i, res in enumerate(results, 1):
            score_str = f' ({res.score:.2f})' if res.score is not None else ''

            snippet = res.text[:300] + '...' if len(res.text) > 300 else res.text

            unit_type = getattr(res, 'fact_type', 'unknown')
            unit_type = getattr(unit_type, 'value', unit_type)

            # Include date if available
            date = res.mentioned_at or res.occurred_start
            date_str = f' ({date.isoformat()})' if date else ''

            confidence = getattr(res, 'confidence', 1.0)
            confidence_str = f' [conf: {confidence:.1f}]' if confidence < 1.0 else ''

            # Use note title if available
            if res.note_id and res.note_id in note_titles:
                note_ref = f'Note: "{note_titles[res.note_id]}" {res.note_id}'
            elif res.note_id:
                note_ref = f'Note: {res.note_id}'
            else:
                note_ref = 'Note: unknown'

            output.append(
                f'{i}. [{unit_type}] [Unit: {res.id}] [{note_ref}]'
                f'{score_str}{date_str}{confidence_str}\n   {snippet}\n'
            )

            # Show supersession context if available
            superseded_by = getattr(res, 'unit_metadata', {}).get('superseded_by')
            if superseded_by:
                for s in superseded_by:
                    output.append(
                        f'   \u26a0 Superseded by: {s.get("unit_text", "")[:100]}'
                        f' ({s.get("relation")})'
                    )

        output.append(
            'Tip: Use memex_get_notes_metadata for tags/token counts. '
            'Next: memex_get_page_indices → memex_get_nodes.'
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
    vault_ids: Annotated[
        list[str],
        Field(
            description="Vault UUIDs or names, e.g. ['rituals'].",
        ),
    ],
    limit: Annotated[int, Field(description='Max notes to return.')] = 5,
    expand_query: Annotated[bool, Field(description='LLM-based multi-query expansion.')] = False,
    strategies: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Retrieval strategies to use: semantic, keyword, graph, temporal. If None, all are used.',
        ),
    ] = None,
    after: Annotated[
        str | None,
        Field(default=None, description='Only notes after this ISO 8601 date.'),
    ] = None,
    before: Annotated[
        str | None,
        Field(default=None, description='Only notes before this ISO 8601 date.'),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Field(default=None, description='Only notes with ALL of these tags.'),
    ] = None,
) -> str:
    """Search Memex for source notes by hybrid retrieval."""
    try:
        api = get_api(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(api, vault_ids)

        from datetime import datetime as _dt, timezone as _tz

        after_dt = _dt.fromisoformat(after).replace(tzinfo=_tz.utc) if after else None
        before_dt = _dt.fromisoformat(before).replace(tzinfo=_tz.utc) if before else None

        results = await api.search_notes(
            query=query,
            limit=limit,
            expand_query=expand_query,
            reason=False,
            summarize=False,
            vault_ids=resolved_vids,
            strategies=strategies,
            after=after_dt,
            before=before_dt,
            tags=tags,
        )

        if not results:
            return f"No notes for '{query}'. Reformulate, try memex_memory_search, or set expand_query=true. Do not fabricate IDs."

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
            if doc.vault_name:
                lines.append(f'- **Vault:** {doc.vault_name}')
            if hasattr(doc, 'note_status') and doc.note_status:
                lines.append(f'- **Status:** {doc.note_status}')
            if desc := metadata.get('description'):
                lines.append(f'- **Description:** {desc}')
            if tags := metadata.get('tags'):
                lines.append(f'- **Tags:** {", ".join(tags)}')
            if src := metadata.get('source_uri'):
                lines.append(f'- **Source:** {src}')
            if metadata.get('has_assets'):
                lines.append('- **Has assets:** yes → call memex_list_assets before answering')
            if doc.snippets:
                lines.append('- **Snippets:**')
                for snippet in doc.snippets[:2]:
                    text = snippet.text.strip()[:200]
                    prefix = f'*[{snippet.node_title}]* ' if snippet.node_title else ''
                    lines.append(f'  - {prefix}{text}')
            lines.append('')

        lines.append(
            'Do NOT call memex_get_notes_metadata — metadata above is complete. '
            'Next: memex_get_page_indices → memex_get_nodes on relevant notes.'
        )
        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'Note search failed: {e}', exc_info=True)
        raise ToolError(f'Note search failed: {e}')


def _sum_tokens(nodes: list[TOCNodeDTO]) -> int:
    total = 0
    for node in nodes:
        if node.token_estimate is not None:
            total += node.token_estimate
        total += _sum_tokens(node.children)
    return total


def _estimate_toc_tokens(nodes: list[TOCNodeDTO]) -> int:
    """Estimate the serialized token cost of the TOC itself."""
    total = 0
    for node in nodes:
        total += len(node.title) // 4 + 20
        if node.summary:
            for field in (
                node.summary.who,
                node.summary.what,
                node.summary.how,
                node.summary.when,
                node.summary.where,
            ):
                if field:
                    total += len(field) // 4
        total += _estimate_toc_tokens(node.children)
    return total


async def _get_single_page_index(
    api: Any,
    note_id_str: str,
    depth: int | None,
    parent_node_id: str | None,
) -> PageIndexDTO | str:
    """Fetch and process a single note's page index. Raises ToolError on failure."""
    try:
        uuid_obj = UUID(note_id_str)
    except ValueError:
        raise ToolError(f'Invalid Note UUID: {note_id_str}')

    page_index = await api.get_note_page_index(uuid_obj)
    if page_index is None:
        return (
            f'Note {note_id_str}: No page index available. '
            'Only notes indexed with the page_index strategy have a table of contents.'
        )

    raw_toc = page_index.get('toc', [])

    if depth is not None or parent_node_id is not None:
        from memex_core.services.notes import NoteService

        raw_toc = NoteService._filter_toc(raw_toc, depth=depth, parent_node_id=parent_node_id)

    toc = [TOCNodeDTO.model_validate(n) for n in raw_toc]

    metadata_dict = page_index.get('metadata') or {}

    if depth is None and parent_node_id is None:
        total_tokens = metadata_dict.get('total_tokens') or _sum_tokens(toc) or None
    else:
        total_tokens = _sum_tokens(toc) or None

    if depth is None and parent_node_id is None:
        toc_cost = _estimate_toc_tokens(toc)
        if toc_cost > 3000:
            raise ToolError(
                f'Note {note_id_str}: Page index has ~{toc_cost} tokens. '
                'Call again with depth=0 to get top-level sections (H1+H2), '
                'then drill down with parent_node_id.'
            )

    return PageIndexDTO(
        metadata=PageMetadataDTO(**metadata_dict),
        toc=toc,
        total_tokens=total_tokens,
    )


@mcp.tool(
    name='memex_get_page_indices',
    description=(
        'Get note TOC: section titles, summaries, and node IDs for 1+ notes. '
        'Expensive for large notes — only call AFTER memex_get_notes_metadata confirms relevance. '
        'For large notes (total_tokens > 3000): use depth=0 to get top-level sections (H1+H2) first, '
        'then drill into specific sections with parent_node_id. '
        'Pass leaf node IDs (nodes without children) to memex_get_nodes to read content.'
    ),
)
async def memex_get_page_indices(
    ctx: Context,
    note_ids: Annotated[list[str], Field(description='List of Note UUIDs.')],
    depth: Annotated[
        int | None,
        Field(
            default=None,
            description='Detail level: 0=top-level overview (H1+H2), 1+=full tree.',
        ),
    ] = None,
    parent_node_id: Annotated[
        str | None,
        Field(default=None, description='Return only the subtree under this node ID.'),
    ] = None,
) -> str | PageIndexDTO:
    """Get the hierarchical page index for one or more notes."""
    try:
        api = get_api(ctx)

        # Single note: preserve original return type (PageIndexDTO)
        if len(note_ids) == 1:
            return await _get_single_page_index(api, note_ids[0], depth, parent_node_id)

        # Multiple notes: process each, collect results and errors
        sections: list[str] = []
        errors: list[str] = []

        for nid_str in note_ids:
            try:
                result = await _get_single_page_index(api, nid_str, depth, parent_node_id)
                if isinstance(result, str):
                    errors.append(result)
                else:
                    sections.append(f'## Note: {nid_str}\n' + result.model_dump_json(indent=2))
            except ToolError as te:
                errors.append(str(te))
            except Exception as exc:
                errors.append(f'Note {nid_str}: {exc}')

        output = '\n---\n'.join(sections) if sections else ''

        if errors:
            err_block = '\n### Errors\n' + '\n'.join(f'- {e}' for e in errors)
            output = (output + '\n' + err_block) if output else err_block

        return output if output else 'No page indices found for the provided note IDs.'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get page index failed: {e}', exc_info=True)
        raise ToolError(f'Get page index failed: {e}')


@mcp.tool(
    name='memex_get_notes_metadata',
    description=(
        'Get metadata (title, tags, token count, has_assets) for 1+ notes. '
        'Use after memex_memory_search to filter results before reading. '
        'SKIP after memex_note_search (metadata already inline).'
    ),
)
async def memex_get_notes_metadata(
    ctx: Context,
    note_ids: Annotated[list[str], Field(description='List of Note UUIDs.')],
) -> str:
    """Get metadata for one or more notes."""
    try:
        api = get_api(ctx)
        uuid_list: list[UUID] = []
        errors: list[str] = []

        for nid in note_ids:
            try:
                uuid_list.append(UUID(nid))
            except ValueError:
                errors.append(f'Invalid UUID: {nid}')

        if not uuid_list and errors:
            raise ToolError('\n'.join(errors))

        results: list[dict] = []
        try:
            batch_results = await api.get_notes_metadata(uuid_list)
            for meta in batch_results:
                nid_str = meta.get('note_id') or meta.get('id')
                results.append({'note_id': str(nid_str), **meta} if nid_str else meta)
            # Find IDs not returned by batch
            returned_ids = {meta.get('note_id') or meta.get('id') for meta in batch_results}
            for uid in uuid_list:
                if str(uid) not in returned_ids:
                    errors.append(f'{uid}: no metadata available')
        except Exception:
            # Fallback to individual lookups
            for uid in uuid_list:
                try:
                    metadata = await api.get_note_metadata(uid)
                    if metadata is None:
                        errors.append(f'{uid}: no metadata available')
                    else:
                        results.append({'note_id': str(uid), **metadata})
                except Exception as exc:
                    errors.append(f'{uid}: {exc}')

        if not results and not errors:
            return 'No metadata found for any of the provided note IDs.'

        lines: list[str] = []
        for meta in results:
            nid = meta.get('note_id', 'N/A')
            title = meta.get('title') or meta.get('name') or 'Untitled'
            tokens = meta.get('total_tokens', 'N/A')
            has_assets = meta.get('has_assets', False)
            tags_list = meta.get('tags', [])
            vault_name = meta.get('vault_name', '')
            lines.append(f'## {title}')
            lines.append(f'- **Note ID:** {nid}')
            lines.append(f'- **Tokens:** {tokens}')
            if vault_name:
                lines.append(f'- **Vault:** {vault_name}')
            if tags_list:
                lines.append(f'- **Tags:** {", ".join(tags_list)}')
            if has_assets:
                lines.append('- **Has assets:** yes → call memex_list_assets before answering')
            lines.append('')

        if errors:
            lines.append('### Errors')
            for err in errors:
                lines.append(f'- {err}')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get notes metadata failed: {e}', exc_info=True)
        raise ToolError(f'Get notes metadata failed: {e}')


@mcp.tool(
    name='memex_get_nodes',
    description=(
        'Read note sections by node IDs. Get node IDs from memex_get_page_indices. '
        'Accepts 1 or more IDs — use for single and batch reads.'
    ),
)
async def memex_get_nodes(
    ctx: Context,
    node_ids: Annotated[list[str], Field(description='List of Node UUIDs.')],
) -> str:
    """Retrieve the full text content of one or more note nodes."""
    try:
        api = get_api(ctx)
        uuid_list: list[UUID] = []
        errors: list[str] = []

        for nid in node_ids:
            try:
                uuid_list.append(UUID(nid))
            except ValueError:
                errors.append(f'Invalid UUID: {nid}')

        if not uuid_list and errors:
            raise ToolError('\n'.join(errors))

        # Batch fetch all nodes, with fallback to individual lookups
        try:
            nodes = await api.get_nodes(uuid_list)
        except Exception:
            # Fallback to individual get_node calls (e.g. batch endpoint unavailable)
            nodes = []
            for uid in uuid_list:
                try:
                    node = await api.get_node(uid)
                    if node:
                        nodes.append(node)
                    else:
                        errors.append(f'Node {uid} not found')
                except Exception as exc:
                    errors.append(f'Node {uid}: {exc}')

        # Build lookup for found nodes — track both primary key and hash matches
        found_ids: set[UUID] = set()
        found_hashes: set[str] = set()
        sections: list[str] = []

        for node in nodes:
            found_ids.add(node.id)
            node_hash = getattr(node, 'node_hash', None)
            if node_hash:
                found_hashes.add(node_hash)

            title = node.title
            text = node.text
            doc_id = node.note_id
            level = node.level

            section = [f'# {"#" * level} {title}']
            section.append(f'**Node ID:** {node.id}')
            section.append(f'**Note ID:** {doc_id}')
            if text:
                section.append(f'\n{text}')
            else:
                section.append('\n[No text content]')
            sections.append('\n'.join(section))

        # Check for node IDs that weren't found (by either UUID or hash)
        # Skip IDs already reported as errors by the fallback path
        reported = {e.split(' ')[1] for e in errors if e.startswith('Node ')}
        not_found: list[str] = []
        for uid in uuid_list:
            if uid in found_ids or uid.hex in found_hashes:
                continue
            if str(uid) in reported:
                continue
            not_found.append(str(uid))

        output = '\n---\n'.join(sections) if sections else ''

        if errors:
            err_block = '\n### Errors\n' + '\n'.join(f'- {e}' for e in errors)
            output = (output + '\n' + err_block) if output else err_block

        if not_found:
            hint = (
                f'\n\n**Note:** {len(not_found)} node ID(s) not found: '
                + ', '.join(not_found[:5])
                + ('...' if len(not_found) > 5 else '')
                + '\nThese may be parent sections without stored content. '
                'Use child node IDs from memex_get_page_indices instead.'
            )
            output = (output + hint) if output else hint.strip()

        return output if output else 'No nodes found for the provided IDs.'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get nodes failed: {e}', exc_info=True)
        raise ToolError(f'Get nodes failed: {e}')


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
    description=(
        'List notes with optional date filters. '
        "Use after/before for temporal queries like 'documents from 2026'."
    ),
)
async def memex_list_notes(
    ctx: Context,
    vault_id: Annotated[
        str,
        Field(description="Vault UUID or name, e.g. 'rituals'."),
    ],
    after: Annotated[
        str | None,
        Field(
            default=None,
            description='Only notes on/after this date (ISO 8601, e.g. 2026-01-01).',
        ),
    ] = None,
    before: Annotated[
        str | None,
        Field(
            default=None,
            description='Only notes on/before this date (ISO 8601, e.g. 2026-12-31).',
        ),
    ] = None,
    limit: Annotated[int, Field(description='Max notes to return.')] = 50,
) -> str:
    """List notes with optional date filters."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        resolved_vault_id = await _resolve_vault_id(api, vault_id)

        parsed_after = None
        parsed_before = None
        if after is not None:
            try:
                parsed_after = _dt.fromisoformat(after)
            except ValueError:
                raise ToolError(f'Invalid after date: {after}')
        if before is not None:
            try:
                parsed_before = _dt.fromisoformat(before)
            except ValueError:
                raise ToolError(f'Invalid before date: {before}')

        notes = await api.list_notes(
            limit=limit,
            offset=0,
            vault_id=resolved_vault_id,
            after=parsed_after,
            before=parsed_before,
        )

        if not notes:
            return 'No notes found.'

        lines = [f'Found {len(notes)} note(s):\n']
        for i, n in enumerate(notes, 1):
            title = n.title or 'Untitled'
            pub = f', publish_date: {n.publish_date}' if n.publish_date else ''
            lines.append(f'{i}. **{title}** (ID: {n.id}, created: {n.created_at}{pub})')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'List notes failed: {e}', exc_info=True)
        raise ToolError(f'List notes failed: {e}')


@mcp.tool(
    name='memex_recent_notes',
    description='Browse recent notes. Filter by vault name or UUID and optional date range.',
)
async def memex_recent_notes(
    ctx: Context,
    limit: Annotated[int, Field(description='Max notes to return.')] = 20,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Vault UUID or name, e.g. 'rituals'. Omit for active vault.",
        ),
    ] = None,
    after: Annotated[
        str | None,
        Field(
            default=None,
            description='Only notes on/after this date (ISO 8601).',
        ),
    ] = None,
    before: Annotated[
        str | None,
        Field(
            default=None,
            description='Only notes on/before this date (ISO 8601).',
        ),
    ] = None,
) -> str:
    """List recent notes."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        resolved_vault_id = None
        if vault_id:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)

        parsed_after = None
        parsed_before = None
        if after is not None:
            try:
                parsed_after = _dt.fromisoformat(after)
            except ValueError:
                raise ToolError(f'Invalid after date: {after}')
        if before is not None:
            try:
                parsed_before = _dt.fromisoformat(before)
            except ValueError:
                raise ToolError(f'Invalid before date: {before}')

        notes = await api.get_recent_notes(
            limit=limit,
            vault_id=resolved_vault_id,
            after=parsed_after,
            before=parsed_before,
        )

        if not notes:
            return 'No notes found.'

        lines = [f'Found {len(notes)} note(s):\n']
        for i, n in enumerate(notes, 1):
            title = n.title or 'Untitled'
            pub = f', publish_date: {n.publish_date}' if n.publish_date else ''
            lines.append(f'{i}. **{title}** (ID: {n.id}, created: {n.created_at}{pub})')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'List notes failed: {e}', exc_info=True)
        raise ToolError(f'List notes failed: {e}')


@mcp.tool(
    name='memex_list_entities',
    description=(
        'List or search entities in the knowledge graph. '
        'Without a query, returns top entities by relevance. '
        'Use vault_id to scope to entities mentioned in a specific vault.\n\n'
        'Entity exploration workflow:\n'
        '1. memex_list_entities → browse/search entities by name (optionally vault-scoped)\n'
        '2. memex_get_entities → get details (type, mention count)\n'
        '3. memex_get_entity_mentions → find facts/observations mentioning entity\n'
        '4. memex_get_entity_cooccurrences → find related entities'
    ),
)
async def memex_list_entities(
    ctx: Context,
    vault_id: Annotated[
        str,
        Field(description="Vault UUID or name, e.g. 'rituals'."),
    ],
    query: Annotated[
        str | None, Field(default=None, description='Search term to filter by name.')
    ] = None,
    limit: Annotated[int, Field(description='Max entities to return.')] = 20,
    entity_type: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Filter by entity type. '
                'Valid values: Person, Organization, Location, '
                'Concept, Technology, File, Misc.'
            ),
        ),
    ] = None,
) -> str:
    """List or search entities."""
    try:
        api = get_api(ctx)
        if entity_type:
            entity_type = entity_type.title()
        resolved = await _resolve_vault_id(api, vault_id)
        resolved_vids: list[UUID | str] = [resolved]

        if query:
            entities = await api.search_entities(
                query, limit=limit, vault_ids=resolved_vids, entity_type=entity_type
            )
        else:
            entities = [
                e
                async for e in api.list_entities_ranked(
                    limit=limit, vault_ids=resolved_vids, entity_type=entity_type
                )
            ]

        if not entities:
            return 'No entities found.'

        lines = [f'Found {len(entities)} entity/entities:\n']
        for i, e in enumerate(entities, 1):
            type_str = f', type: {e.entity_type}' if e.entity_type else ''
            lines.append(f'{i}. **{e.name}** (ID: {e.id}{type_str}, mentions: {e.mention_count})')

        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'List entities failed: {e}', exc_info=True)
        raise ToolError(f'List entities failed: {e}')


@mcp.tool(
    name='memex_get_entities',
    description=(
        'Get entity details (name, type, mention count) for 1+ entities by UUID. '
        'Use after memex_list_entities to get full details.'
    ),
)
async def memex_get_entities(
    ctx: Context,
    entity_ids: Annotated[list[str], Field(description='List of Entity UUIDs.')],
) -> str:
    """Get details for one or more entities."""
    try:
        api = get_api(ctx)
        uuid_list: list[UUID] = []
        errors: list[str] = []

        for eid in entity_ids:
            try:
                uuid_list.append(UUID(eid))
            except ValueError:
                errors.append(f'Invalid UUID: {eid}')

        if not uuid_list and errors:
            raise ToolError('\n'.join(errors))

        sections: list[str] = []
        found_ids: set[UUID] = set()

        # Try batch fetch first
        try:
            entities = await api.get_entities(uuid_list)
            for entity in entities:
                found_ids.add(entity.id)
                lines = [
                    f'# Entity: {entity.name}',
                    f'**ID:** {entity.id}',
                    f'**Mentions:** {entity.mention_count}',
                ]
                if entity.entity_type:
                    lines.append(f'**Type:** {entity.entity_type}')
                if entity.vault_id:
                    lines.append(f'**Vault:** {entity.vault_id}')
                sections.append('\n'.join(lines))
        except Exception:
            # Fall back to individual lookups
            for uid in uuid_list:
                try:
                    entity = await api.get_entity(uid)
                    if entity is None:
                        errors.append(f'{uid}: entity not found')
                        continue
                    found_ids.add(entity.id)
                    lines = [
                        f'# Entity: {entity.name}',
                        f'**ID:** {entity.id}',
                        f'**Mentions:** {entity.mention_count}',
                    ]
                    if entity.entity_type:
                        lines.append(f'**Type:** {entity.entity_type}')
                    if entity.vault_id:
                        lines.append(f'**Vault:** {entity.vault_id}')
                    sections.append('\n'.join(lines))
                except Exception as exc:
                    errors.append(f'{uid}: {exc}')

        # Report missing IDs (only if not already reported via fallback path)
        reported = {e.split(':')[0] for e in errors}
        for uid in uuid_list:
            if uid not in found_ids and str(uid) not in reported:
                errors.append(f'{uid}: entity not found')

        output = '\n---\n'.join(sections) if sections else ''

        if errors:
            err_block = '\n### Errors\n' + '\n'.join(f'- {e}' for e in errors)
            output = (output + '\n' + err_block) if output else err_block

        return output if output else 'No entities found for the provided IDs.'

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entities failed: {e}', exc_info=True)
        raise ToolError(f'Get entities failed: {e}')


@mcp.tool(
    name='memex_get_entity_mentions',
    description='Get facts, observations, and events that mention an entity. Each mention links to its source note, revealing cross-note connections.',
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
            unit = m.get('unit')
            note = m.get('note') or m.get('document')
            text = str(unit.text if unit else '')[:200]
            unit_id = unit.id if unit else 'N/A'
            note_id = note.id if note else (getattr(unit, 'note_id', None) or 'N/A')
            fact_type = unit.fact_type if unit else 'unknown'

            # Extract note title for richer output
            note_title = getattr(note, 'title', None) or getattr(note, 'name', None) or ''
            if note_title:
                note_ref = f'Note: "{note_title}" ({note_id})'
            else:
                note_ref = f'Note ID: {note_id}'
            lines.append(f'{i}. [Type: {fact_type}] [{note_ref}] [Unit: {unit_id}]\n   {text}\n')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity mentions failed: {e}', exc_info=True)
        raise ToolError(f'Get entity mentions failed: {e}')


@mcp.tool(
    name='memex_get_entity_cooccurrences',
    description='Find entities that frequently appear alongside a given entity — the fastest way to map relationships and discover connected concepts. Returns entity names, types, and co-occurrence counts inline (no follow-up calls needed). Use this for "what relates to X?" questions.',
)
async def memex_get_entity_cooccurrences(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
    limit: Annotated[int, Field(description='Max co-occurring entities to return.')] = 10,
) -> str:
    """Get co-occurring entities."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        cooccurrences = await api.get_entity_cooccurrences(uuid_obj, limit=limit)

        if not cooccurrences:
            return 'No co-occurrences found for this entity.'

        lines = [f'Found {len(cooccurrences)} co-occurring entity/entities:\n']
        for i, c in enumerate(cooccurrences, 1):
            e1 = c['entity_id_1']
            e2 = c['entity_id_2']
            count = c['cooccurrence_count']
            if str(e1) == entity_id:
                other_name = c.get('entity_2_name', '')
                other_type = c.get('entity_2_type', '')
                other_id = e2
            else:
                other_name = c.get('entity_1_name', '')
                other_type = c.get('entity_1_type', '')
                other_id = e1
            co_label = 'co-occurrence' if count == 1 else 'co-occurrences'
            if other_name:
                type_prefix = f'{other_type}, ' if other_type else ''
                lines.append(
                    f'{i}. **{other_name}** ({type_prefix}ID: {other_id}) — {count} {co_label}'
                )
            else:
                lines.append(f'{i}. Entity ID: {other_id} — {count} {co_label}')

        return '\n'.join(lines)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity cooccurrences failed: {e}', exc_info=True)
        raise ToolError(f'Get entity cooccurrences failed: {e}')


@mcp.tool(
    name='memex_get_memory_units',
    description='Batch lookup of memory units by ID. Includes contradiction links and supersession info.',
)
async def memex_get_memory_units(
    ctx: Context,
    unit_ids: Annotated[list[str], Field(description='List of memory unit UUIDs.')],
) -> str:
    """Retrieve multiple memory units with their contradiction context."""
    try:
        api = get_api(ctx)
        lines: list[str] = []
        for uid_str in unit_ids:
            try:
                uuid_obj = UUID(uid_str)
            except ValueError:
                lines.append(f'Invalid UUID: {uid_str}')
                continue

            try:
                unit = await api.get_memory_unit(uuid_obj)
            except Exception as exc:
                lines.append(f'## Unit {uid_str}\n- **Error:** {exc}\n')
                continue

            if unit is None:
                lines.append(f'Unit {uid_str}: not found')
                continue

            fact_type = getattr(unit.fact_type, 'value', unit.fact_type)
            confidence = getattr(unit, 'confidence', 1.0)
            conf_str = f' [confidence: {confidence:.2f}]' if confidence < 1.0 else ''

            lines.append(f'## Unit {uid_str}')
            lines.append(f'- **Type:** {fact_type}{conf_str}')
            lines.append(f'- **Text:** {unit.text}')
            if unit.note_id:
                lines.append(f'- **Note ID:** {unit.note_id}')

            # Show supersession info if available
            meta = getattr(unit, 'metadata', {}) or getattr(unit, 'unit_metadata', {}) or {}
            superseded_by = meta.get('superseded_by')

            if superseded_by:
                lines.append('- **Superseded by:**')
                for s in superseded_by:
                    s_text = s.get('unit_text', '')[:150]
                    s_rel = s.get('relation', 'unknown')
                    s_note = s.get('note_title', '')
                    lines.append(f'  - [{s_rel}] {s_text}')
                    if s_note:
                        lines.append(f'    From note: {s_note}')

            lines.append('')

        return '\n'.join(lines) if lines else 'No units found.'

    except Exception as e:
        logging.error(f'Get memory units failed: {e}', exc_info=True)
        raise ToolError(f'Get memory units failed: {e}')


def entrypoint():
    """Entrypoint for the MCP server."""
    asyncio.run(mcp.run_async(transport='stdio'))


if __name__ == '__main__':
    entrypoint()
