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
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.utilities.types import Image, Audio, File
from fastmcp.exceptions import ToolError
from fastmcp.utilities.logging import configure_logging
import json

from pydantic import BeforeValidator, Field

from memex_mcp.lifespan import lifespan, get_api, get_config
from memex_mcp.models import (
    McpAddNoteResult,
    McpAsset,
    McpCitation,
    McpCooccurrence,
    McpEntity,
    McpEntityMention,
    McpFact,
    McpEvent,
    McpFindResult,
    McpKVEntry,
    McpKVWriteResult,
    McpNode,
    McpNote,
    McpNoteContent,
    McpNoteMetadata,
    McpNoteSearchResult,
    McpObservation,
    McpOverlap,
    McpPageIndex,
    McpPageMetadata,
    McpSnippet,
    McpSupersession,
    McpVault,
)
from memex_mcp.types import NoteTemplateType
from memex_common.schemas import (
    BatchJobStatus,
    NoteCreateDTO,
    PageIndexDTO,
    PageMetadataDTO,
    TOCNodeDTO,
    filter_toc,
)


def _coerce_list(v: Any) -> Any:
    """Coerce a stringified JSON array back to a list."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return v


def _coerce_bool(v: Any) -> Any:
    """Coerce a stringified bool back to a bool."""
    if isinstance(v, str):
        low = v.lower()
        if low in ('true', '1'):
            return True
        if low in ('false', '0'):
            return False
    return v


def _coerce_int(v: Any) -> Any:
    """Coerce a stringified int back to an int."""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            pass
    return v


def _validate_vault_ids(vault_ids: list[str]) -> list[str]:
    """Validate vault_ids is a real list, not a stringified JSON array."""
    if isinstance(vault_ids, str):
        try:
            parsed = json.loads(vault_ids)
            if isinstance(parsed, list):
                vault_ids = parsed
        except (json.JSONDecodeError, ValueError):
            pass
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


async def _resolve_vault_ids(api: Any, vault_ids: list[str]) -> list[UUID]:
    """Resolve and validate that all vault identifiers exist."""
    resolved: list[UUID] = []
    for vid in vault_ids:
        try:
            r = await api.resolve_vault_identifier(vid)
            resolved.append(UUID(str(r)) if not isinstance(r, UUID) else r)
        except Exception:
            raise ToolError(f'Vault not found: {vid!r}')
    return resolved


async def _resolve_vault_id(api: Any, vault_id: str) -> 'UUID':
    """Resolve and validate a single vault identifier exists."""
    try:
        return await api.resolve_vault_identifier(vault_id)
    except Exception:
        raise ToolError(f'Vault not found: {vault_id!r}')


def _default_write_vault(ctx: Context) -> str:
    return get_config(ctx).write_vault


def _default_read_vaults(ctx: Context) -> list[str]:
    return get_config(ctx).read_vaults


prompts_dir = plb.Path(__file__).parent / 'prompts'

configure_logging(level=os.environ.get('MEMEX_MCP_LOG_LEVEL', 'WARNING'))

persona_logger = logging.getLogger('persona')
persona_logger.setLevel(os.getenv('PERSONA_LOG_LEVEL', 'INFO'))

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system.

VAULT DEFAULTS — vault parameters are optional. Writes default to the active vault;
reads default to search vaults (from .memex.yaml or global config). Only pass
vault_id/vault_ids to override.

ROUTING — select retrieval strategy by query type:

IF you know (part of) the note title:
  → TITLE SEARCH
  1. `memex_find_note(query="title fragment")` → note IDs, titles, scores
  2. Read via `memex_get_page_indices` → `memex_get_nodes` as needed

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

IF storing/retrieving structured facts, preferences, or conventions:
  → KV STORE
  - `memex_kv_write(value, key, vault_id)` — store a user fact or preference
  - `memex_kv_get(key)` — exact key lookup
  - `memex_kv_search(query)` — fuzzy semantic search over stored facts
  - `memex_kv_list()` — list all stored facts
  When the user states a preference, convention, or static fact (e.g. "always use uv", "my role is Staff Engineer"), proactively store it via `memex_kv_write`.
  Deletion is user-only (CLI). Do NOT attempt to delete KV entries.

RESPONSE FORMAT — MANDATORY for every response:
- Cite every claim from Memex with numbered references [1], [2], etc. inline.
- End response with a reference list. Each entry uses a type prefix:
  `[note]` title + note ID | `[memory]` title + memory ID + source note ID | `[asset]` filename + note ID
- Example: `[1] [note] Detailing the Sys Layer architecture — 2eb202ed-bee6-7b2a-f0b9-917e8d5dd6f0`

RULES:
- Only use IDs from tool output. Never fabricate IDs.
- Filter before reading. Never call `memex_get_page_indices` on unconfirmed notes.
- Never use `memex_recent_notes` for discovery.

`memex_memory_search` strategies: `["semantic"]` vector similarity, `["keyword"]` BM25 full-text, `["graph"]` entity-centric, `["temporal"]` chronological, `["mental_model"]` synthesized. Default (all) is best for general queries.
""".strip(),
    version='0.1.0',
    lifespan=lifespan,
    on_duplicate='error',
)

mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=False, transform_errors=True))


@mcp.tool(
    name='memex_list_assets',
    description='List file assets for a note. REQUIRED when has_assets is true. Feed paths to memex_get_resources.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_list_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
) -> list[McpAsset]:
    """List assets for a note."""
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)
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
            return []

        result: list[McpAsset] = []
        for asset_path in assets:
            path_obj = plb.Path(asset_path)
            filename = path_obj.name
            mime_type, _ = mimetypes.guess_type(filename)
            result.append(McpAsset(filename=filename, path=asset_path, mime_type=mime_type))

        return result

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'List assets failed: {e}', exc_info=True)
        raise ToolError(f'List assets failed: {e}')


@mcp.tool(
    name='memex_read_note',
    description='Read full note. ONLY when total_tokens < 500 (use force=True to override). Otherwise: memex_get_page_indices + memex_get_nodes.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_read_note(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    force: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            description='Override the 500-token limit and read the full note regardless of size.'
        ),
    ] = False,
) -> McpNoteContent:
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

        return McpNoteContent(
            id=note.id,
            title=name,
            description=meta.get('description'),
            vault_id=note.vault_id,
            created_at=note.created_at,
            content=note.original_text,
        )

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
    annotations={'readOnlyHint': False, 'idempotentHint': True},
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
    annotations={'readOnlyHint': False, 'idempotentHint': True},
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
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_resources(
    ctx: Context,
    paths: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='Resource path(s).')
    ],
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
) -> list[Image | Audio | File | str]:
    """Retrieve file resources. Returns a list of Image, Audio, File, or error strings."""
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)
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
    annotations={'readOnlyHint': True},
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
    description=(
        '[DEPRECATED — use memex_list_vaults instead, which includes is_active flag] '
        'Get the active vault name and ID. Shows both server default and client-resolved vault.'
    ),
    annotations={'readOnlyHint': True},
)
async def memex_active_vault(ctx: Context) -> str:
    """Retrieve the currently active vault information.

    .. deprecated::
        Use ``memex_list_vaults`` which now includes an ``is_active`` flag
        on each vault. This tool will be removed in a future version.
    """
    try:
        api = get_api(ctx)
        config = get_config(ctx)

        lines: list[str] = []
        lines.append('_Note: this tool is deprecated. Use memex_list_vaults instead._')
        lines.append('')

        # Client-resolved vaults (from vault config + server defaults)
        lines.append(f'**Write vault (client):** {config.write_vault}')
        lines.append(f'**Read vaults (client):** {", ".join(config.read_vaults)}')

        # Server defaults
        vault = await api.get_active_vault()
        if vault:
            lines.append(f'**Server default write:** {vault.name} (ID: {vault.id})')
        lines.append(f'**Server default read:** {config.server.default_reader_vault}')

        return '\n'.join(lines)

    except Exception as e:
        logging.error(f'Get active vault failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve active vault: {e}')


@mcp.tool(
    name='memex_add_note',
    description='Add a note to Memex. Confirm vault with user first, or pass vault_id.',
    annotations={'readOnlyHint': False},
    timeout=120.0,
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
        BeforeValidator(_coerce_list),
        Field(description='Tags for retrieval.'),
    ],
    vault_id: Annotated[
        str | None,
        Field(description='Target vault UUID or name. Omit to use config defaults.'),
    ] = None,
    supporting_files: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
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
        BeforeValidator(_coerce_bool),
        Field(default=False, description='Queue ingestion in background.'),
    ] = False,
) -> McpAddNoteResult:
    try:
        if len(description.split(' ')) > 250:
            raise ToolError('Description exceeds 250 words limit.')

        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)

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
            return McpAddNoteResult(
                note_id=UUID(result.job_id) if result.job_id else UUID(int=0),
                status='queued',
                job_id=result.job_id,
            )

        overlaps = [
            McpOverlap(
                note_id=o.note_id,
                title=o.title or 'Untitled',
                similarity_pct=int(o.similarity * 100),
            )
            for o in (result.overlapping_notes or [])
        ]

        return McpAddNoteResult(
            note_id=UUID(result.note_id) if result.note_id else UUID(int=0),
            status=result.status,
            overlapping_notes=overlaps,
        )

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Add note failed: {e}', exc_info=True)
        raise ToolError(f'Add note failed: {e}')


def _build_memory_unit_model(
    res: Any,
    note_titles: dict[UUID, str] | None = None,
) -> McpFact | McpEvent | McpObservation:
    """Convert a MemoryUnitDTO into the appropriate MCP model."""
    note_titles = note_titles or {}
    fact_type = getattr(res.fact_type, 'value', res.fact_type)
    supersessions = [
        McpSupersession(
            unit_id=s.unit_id,
            unit_text=s.unit_text,
            relation=s.relation,
            note_title=s.note_title,
        )
        for s in (getattr(res, 'superseded_by', None) or [])
    ]
    note_title = None
    if res.note_id and res.note_id in note_titles:
        note_title = note_titles[res.note_id]

    unit_metadata = res.metadata if isinstance(res.metadata, dict) else {}
    tags = unit_metadata.get('tags', [])

    base_kwargs: dict[str, Any] = {
        'id': res.id,
        'text': res.text,
        'score': res.score,
        'confidence': getattr(res, 'confidence', 1.0),
        'note_id': res.note_id,
        'note_title': note_title,
        'node_ids': getattr(res, 'node_ids', []),
        'tags': tags,
        'status': getattr(res, 'status', 'active'),
        'superseded_by': supersessions,
    }

    if fact_type == 'event':
        return McpEvent(
            **base_kwargs,
            occurred_start=res.occurred_start,
            occurred_end=res.occurred_end,
        )
    elif fact_type == 'observation':
        citations_raw = unit_metadata.get('citations', [])
        citations = [
            McpCitation(unit_id=c['unit_id'], text=c['text'], date=c.get('date'))
            for c in citations_raw
            if isinstance(c, dict) and 'unit_id' in c and 'text' in c
        ]
        return McpObservation(
            **base_kwargs,
            mentioned_at=res.mentioned_at,
            citations=citations,
        )
    else:
        return McpFact(**base_kwargs)


@mcp.tool(
    name='memex_memory_search',
    description=(
        'Search extracted facts, events, and observations across all notes (memory search). '
        'Best for broad/exploratory queries. '
        'For targeted document lookup, use memex_note_search. When unsure, run both in parallel.'
    ),
    annotations={'readOnlyHint': True},
    timeout=60.0,
)
async def memex_memory_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query.')],
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            description='Vault UUIDs or names. Omit to use config defaults.',
        ),
    ] = None,
    limit: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max results. Ignored when token_budget is set.'),
    ] = 10,
    token_budget: Annotated[
        int | None,
        BeforeValidator(_coerce_int),
        Field(
            description='Token budget. When set, overrides limit — packs results greedily to budget.',
        ),
    ] = None,
    strategies: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            default=None,
            description='Strategies: semantic, keyword, graph, temporal, mental_model. Default: all.',
        ),
    ] = None,
    include_superseded: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
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
        BeforeValidator(_coerce_list),
        Field(default=None, description='Only results from notes with ALL of these tags.'),
    ] = None,
) -> list[McpFact | McpEvent | McpObservation]:
    """Search Memex for relevant information."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
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
            return []

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

        return [_build_memory_unit_model(res, note_titles) for res in results]

    except ToolError:
        raise
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
    annotations={'readOnlyHint': True},
    timeout=60.0,
)
async def memex_note_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query.')],
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            description='Vault UUIDs or names. Omit to use config defaults.',
        ),
    ] = None,
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max notes to return.')
    ] = 10,
    expand_query: Annotated[
        bool, BeforeValidator(_coerce_bool), Field(description='LLM-based multi-query expansion.')
    ] = False,
    strategies: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
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
        BeforeValidator(_coerce_list),
        Field(default=None, description='Only notes with ALL of these tags.'),
    ] = None,
) -> list[McpNoteSearchResult]:
    """Search Memex for source notes by hybrid retrieval."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
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
            return []

        output: list[McpNoteSearchResult] = []
        for doc in results:
            metadata = doc.metadata or {}
            title = (
                metadata.get('title')
                or metadata.get('name')
                or metadata.get('filename')
                or 'Untitled'
            )
            snippets = [
                McpSnippet(
                    text=s.text.strip(),
                    node_id=s.node_id,
                    node_title=s.node_title,
                )
                for s in (doc.snippets or [])
            ]
            output.append(
                McpNoteSearchResult(
                    note_id=doc.note_id,
                    title=title,
                    score=doc.score,
                    vault_name=doc.vault_name,
                    status=getattr(doc, 'note_status', None),
                    description=metadata.get('description'),
                    tags=metadata.get('tags', []),
                    source_uri=metadata.get('source_uri'),
                    has_assets=metadata.get('has_assets', False),
                    snippets=snippets,
                )
            )

        return output

    except ToolError:
        raise
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


def _backfill_subtree_tokens(nodes: list[dict[str, Any]]) -> int:
    """Backfill ``subtree_tokens`` on old page-index data missing the field."""
    total = 0
    for node in nodes:
        if node.get('subtree_tokens') is not None:
            total += node['subtree_tokens']
            continue
        own = node.get('token_estimate', 0) or 0
        children_sum = _backfill_subtree_tokens(node.get('children', []))
        node['subtree_tokens'] = own + children_sum
        total += node['subtree_tokens']
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
    _backfill_subtree_tokens(raw_toc)

    if depth is not None or parent_node_id is not None:
        raw_toc = filter_toc(raw_toc, depth=depth, parent_node_id=parent_node_id)

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
        'Get note TOC: section titles, summaries, node IDs, and subtree_tokens for 1+ notes. '
        'Each node includes subtree_tokens (own + all descendant tokens) for read budgeting. '
        'Expensive for large notes — only call AFTER memex_get_notes_metadata confirms relevance. '
        'For large notes (total_tokens > 3000): use depth=0 to get top-level sections (H1+H2) first, '
        'then drill into specific sections with parent_node_id. '
        'Pass leaf node IDs (nodes without children) to memex_get_nodes to read content.'
    ),
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_page_indices(
    ctx: Context,
    note_ids: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='List of Note UUIDs.')
    ],
    depth: Annotated[
        int | None,
        BeforeValidator(_coerce_int),
        Field(
            default=None,
            description='Detail level: 0=top-level overview (H1+H2), 1+=full tree.',
        ),
    ] = None,
    parent_node_id: Annotated[
        str | None,
        Field(default=None, description='Return only the subtree under this node ID.'),
    ] = None,
) -> list[McpPageIndex]:
    """Get the hierarchical page index for one or more notes."""
    try:
        api = get_api(ctx)

        output: list[McpPageIndex] = []

        for nid_str in note_ids:
            try:
                result = await _get_single_page_index(api, nid_str, depth, parent_node_id)
                if isinstance(result, str):
                    continue  # skip notes without page index
                metadata_dict = result.metadata.model_dump() if result.metadata else {}
                output.append(
                    McpPageIndex(
                        note_id=UUID(nid_str),
                        metadata=McpPageMetadata(**metadata_dict),
                        toc=result.toc,
                        total_tokens=result.total_tokens,
                    )
                )
            except ToolError as te:
                # Re-raise TOC guard errors (they contain actionable guidance)
                if 'Page index has' in str(te):
                    raise
                continue  # skip invalid UUIDs and other errors
            except Exception:
                continue

        return output

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
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_notes_metadata(
    ctx: Context,
    note_ids: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='List of Note UUIDs.')
    ],
) -> list[McpNoteMetadata]:
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

        raw_results: list[dict] = []
        try:
            batch_results = await api.get_notes_metadata(uuid_list)
            for meta in batch_results:
                nid_str = meta.get('note_id') or meta.get('id')
                raw_results.append({'note_id': str(nid_str), **meta} if nid_str else meta)
        except Exception:
            # Fallback to individual lookups
            for uid in uuid_list:
                try:
                    metadata = await api.get_note_metadata(uid)
                    if metadata is not None:
                        raw_results.append({'note_id': str(uid), **metadata})
                except Exception:
                    pass

        output: list[McpNoteMetadata] = []
        for meta in raw_results:
            nid = meta.get('note_id')
            if not nid:
                continue
            output.append(
                McpNoteMetadata(
                    note_id=UUID(nid),
                    title=meta.get('title') or meta.get('name') or 'Untitled',
                    total_tokens=meta.get('total_tokens'),
                    vault_name=meta.get('vault_name'),
                    tags=meta.get('tags', []),
                    has_assets=meta.get('has_assets', False),
                )
            )

        return output

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
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_nodes(
    ctx: Context,
    node_ids: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='List of Node UUIDs.')
    ],
) -> list[McpNode]:
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

        output: list[McpNode] = []
        for node in nodes:
            output.append(
                McpNode(
                    id=node.id,
                    note_id=node.note_id,
                    title=node.title,
                    text=node.text,
                    level=node.level,
                )
            )

        return output

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get nodes failed: {e}', exc_info=True)
        raise ToolError(f'Get nodes failed: {e}')


@mcp.tool(
    name='memex_list_vaults',
    description='List all vaults with note counts. Each vault includes is_active and note_count.',
    annotations={'readOnlyHint': True},
)
async def memex_list_vaults(ctx: Context) -> list[McpVault]:
    """List all available vaults with active status and note counts."""
    try:
        api = get_api(ctx)
        config = get_config(ctx)

        # Use list_vaults_with_counts for local API, fall back for remote
        try:
            rows = await api.list_vaults_with_counts()
            active_vault_id = await api.resolve_vault_identifier(config.server.default_active_vault)
            return [
                McpVault(
                    id=row['vault'].id,
                    name=row['vault'].name,
                    description=row['vault'].description,
                    is_active=(row['vault'].id == active_vault_id),
                    note_count=row['note_count'],
                    last_note_added_at=row.get('last_note_added_at'),
                )
                for row in rows
            ]
        except AttributeError:
            # Remote API — fall back to list_vaults (VaultDTO with is_active)
            vaults = await api.list_vaults()
            return [
                McpVault(
                    id=v.id,
                    name=v.name,
                    description=v.description,
                    is_active=v.is_active,
                    last_note_added_at=v.last_note_added_at,
                )
                for v in vaults
            ]

    except Exception as e:
        logging.error(f'List vaults failed: {e}', exc_info=True)
        raise ToolError(f'List vaults failed: {e}')


@mcp.tool(
    name='memex_list_notes',
    description=(
        'List notes with optional date filters. '
        "Use after/before for temporal queries like 'documents from 2026'."
    ),
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_list_notes(
    ctx: Context,
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
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
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max notes to return.')
    ] = 50,
) -> list[McpNote]:
    """List notes with optional date filters."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_read_vaults(ctx)[0]
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

        return [
            McpNote(
                id=n.id,
                title=n.title or 'Untitled',
                created_at=n.created_at,
                publish_date=n.publish_date,
                vault_id=n.vault_id,
            )
            for n in notes
        ]

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'List notes failed: {e}', exc_info=True)
        raise ToolError(f'List notes failed: {e}')


@mcp.tool(
    name='memex_recent_notes',
    description='Browse recent notes. Defaults to all vaults. '
    'Filter by vault names/UUIDs and optional date range.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_recent_notes(
    ctx: Context,
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max notes to return.')
    ] = 20,
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            description='Vault UUIDs or names. Omit for all vaults.',
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
) -> list[McpNote]:
    """List recent notes."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        resolved_vids = None
        if vault_ids:
            _validate_vault_ids(vault_ids)
            resolved_vids = await _resolve_vault_ids(api, vault_ids)

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
            vault_ids=resolved_vids,
            after=parsed_after,
            before=parsed_before,
        )

        return [
            McpNote(
                id=n.id,
                title=n.title or 'Untitled',
                created_at=n.created_at,
                publish_date=n.publish_date,
                vault_id=n.vault_id,
            )
            for n in notes
        ]

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
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_list_entities(
    ctx: Context,
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
    query: Annotated[
        str | None, Field(default=None, description='Search term to filter by name.')
    ] = None,
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max entities to return.')
    ] = 20,
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
) -> list[McpEntity]:
    """List or search entities."""
    try:
        api = get_api(ctx)
        if entity_type:
            entity_type = entity_type.title()
        resolved_vids: list[UUID] | None = None
        if vault_id:
            resolved = await _resolve_vault_id(api, vault_id)
            resolved_vids = [resolved]

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

        return [
            McpEntity(
                id=e.id,
                name=e.name,
                type=e.entity_type,
                mention_count=e.mention_count,
                description=(e.metadata or {}).get('description'),
            )
            for e in entities
        ]

    except Exception as e:
        logging.error(f'List entities failed: {e}', exc_info=True)
        raise ToolError(f'List entities failed: {e}')


@mcp.tool(
    name='memex_get_entities',
    description=(
        'Get entity details (name, type, mention count) for 1+ entities by UUID. '
        'Use after memex_list_entities to get full details.'
    ),
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_entities(
    ctx: Context,
    entity_ids: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='List of Entity UUIDs.')
    ],
) -> list[McpEntity]:
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

        output: list[McpEntity] = []

        # Try batch fetch first
        try:
            entities = await api.get_entities(uuid_list)
            for entity in entities:
                output.append(
                    McpEntity(
                        id=entity.id,
                        name=entity.name,
                        type=entity.entity_type,
                        mention_count=entity.mention_count,
                        description=(entity.metadata or {}).get('description'),
                    )
                )
        except Exception:
            # Fall back to individual lookups
            for uid in uuid_list:
                try:
                    entity = await api.get_entity(uid)
                    if entity is None:
                        continue
                    output.append(
                        McpEntity(
                            id=entity.id,
                            name=entity.name,
                            type=entity.entity_type,
                            mention_count=entity.mention_count,
                            description=(entity.metadata or {}).get('description'),
                        )
                    )
                except Exception:
                    pass

        return output

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entities failed: {e}', exc_info=True)
        raise ToolError(f'Get entities failed: {e}')


@mcp.tool(
    name='memex_get_entity_mentions',
    description='Get facts, observations, and events that mention an entity. Each mention links to its source note, revealing cross-note connections.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_entity_mentions(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max mentions to return.')
    ] = 10,
) -> list[McpEntityMention]:
    """Get memory units mentioning an entity."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        mentions = await api.get_entity_mentions(uuid_obj, limit=limit)

        output: list[McpEntityMention] = []
        for m in mentions:
            unit = m.get('unit')
            note = m.get('note') or m.get('document')
            if not unit:
                continue
            text = str(unit.text)
            unit_id = unit.id
            note_id = note.id if note else (getattr(unit, 'note_id', None) or None)
            fact_type = unit.fact_type if unit else 'unknown'
            note_title = getattr(note, 'title', None) or getattr(note, 'name', None) or None

            output.append(
                McpEntityMention(
                    unit_id=UUID(str(unit_id)),
                    text=text,
                    fact_type=str(fact_type),
                    note_id=UUID(str(note_id)) if note_id and str(note_id) != 'N/A' else None,
                    note_title=note_title,
                )
            )

        return output

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity mentions failed: {e}', exc_info=True)
        raise ToolError(f'Get entity mentions failed: {e}')


@mcp.tool(
    name='memex_get_entity_cooccurrences',
    description='Find entities that frequently appear alongside a given entity — the fastest way to map relationships and discover connected concepts. Returns entity names, types, and co-occurrence counts inline (no follow-up calls needed). Use this for "what relates to X?" questions.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_entity_cooccurrences(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID.')],
    limit: Annotated[
        int, BeforeValidator(_coerce_int), Field(description='Max co-occurring entities to return.')
    ] = 10,
) -> list[McpCooccurrence]:
    """Get co-occurring entities."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        cooccurrences = await api.get_entity_cooccurrences(uuid_obj, limit=limit)

        output: list[McpCooccurrence] = []
        for c in cooccurrences:
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

            output.append(
                McpCooccurrence(
                    entity_id=UUID(str(other_id)),
                    entity_name=other_name or str(other_id),
                    entity_type=other_type or None,
                    count=count,
                )
            )

        return output

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Get entity cooccurrences failed: {e}', exc_info=True)
        raise ToolError(f'Get entity cooccurrences failed: {e}')


@mcp.tool(
    name='memex_get_memory_units',
    description='Batch lookup of memory units by ID. Includes contradiction links and supersession info.',
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_memory_units(
    ctx: Context,
    unit_ids: Annotated[
        list[str], BeforeValidator(_coerce_list), Field(description='List of memory unit UUIDs.')
    ],
) -> list[McpFact | McpEvent | McpObservation]:
    """Retrieve multiple memory units with their contradiction context."""
    try:
        api = get_api(ctx)
        output: list[McpFact | McpEvent | McpObservation] = []
        for uid_str in unit_ids:
            try:
                uuid_obj = UUID(uid_str)
            except ValueError:
                continue

            try:
                unit = await api.get_memory_unit(uuid_obj)
            except Exception:
                continue

            if unit is None:
                continue

            output.append(_build_memory_unit_model(unit))

        return output

    except Exception as e:
        logging.error(f'Get memory units failed: {e}', exc_info=True)
        raise ToolError(f'Get memory units failed: {e}')


@mcp.tool(
    name='memex_find_note',
    description=(
        'Lightweight fuzzy title search. Returns matching note titles, IDs, and scores. '
        'Use when you know (part of) the title. For content search, use memex_note_search.'
    ),
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_find_note(
    ctx: Context,
    query: Annotated[str, Field(description='Title search query (partial or fuzzy match).')],
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            default=None,
            description="Vault UUIDs or names to search in, e.g. ['rituals']. None = all vaults.",
        ),
    ] = None,
    limit: Annotated[int, BeforeValidator(_coerce_int), Field(description='Max results.')] = 5,
) -> list[McpFindResult]:
    """Find notes by approximate title match."""
    try:
        api = get_api(ctx)

        resolved_vids: list[UUID] | None = None
        if vault_ids:
            _validate_vault_ids(vault_ids)
            resolved_vids = await _resolve_vault_ids(api, vault_ids)

        results = await api.find_notes_by_title(
            query=query,
            vault_ids=resolved_vids,
            limit=limit,
        )

        return [
            McpFindResult(
                note_id=r.note_id,
                title=r.title,
                score=r.score,
                status=r.status,
                publish_date=r.publish_date.date() if r.publish_date else None,
            )
            for r in results
        ]

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'Find note failed: {e}', exc_info=True)
        raise ToolError(f'Find note failed: {e}')


@mcp.tool(
    name='memex_kv_write',
    description=(
        'Write a fact to the key-value store. Generates an embedding for semantic search. '
        'Use for storing structured preferences, settings, or facts. '
        'Key should be a short, namespaced identifier (e.g. "tool:python:pkg_mgr").'
    ),
    annotations={'readOnlyHint': False, 'idempotentHint': True},
    timeout=15.0,
)
async def memex_kv_write(
    ctx: Context,
    value: Annotated[str, Field(description='The fact or preference text to store.')],
    key: Annotated[
        str,
        Field(
            description='Namespaced key, e.g. "tool:python:pkg_mgr".',
        ),
    ],
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Vault UUID or name. None = global (available in all vaults).',
        ),
    ] = None,
) -> McpKVWriteResult:
    """Write a fact to the KV store with embedding generation."""
    try:
        api = get_api(ctx)

        # Validate vault exists before proceeding
        resolved_vault: str | None = vault_id
        if vault_id:
            resolved_uuid = await _resolve_vault_id(api, vault_id)
            resolved_vault = str(resolved_uuid)

        # Generate embedding for semantic search via the API layer
        embedding = await api.embed_text(value)

        entry = await api.kv_put(
            value=value,
            key=key,
            vault_id=resolved_vault,
            embedding=embedding,
        )

        scope = f'vault {vault_id}' if vault_id else 'global'
        return McpKVWriteResult(key=entry.key, value=entry.value, scope=scope)

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'KV write failed: {e}', exc_info=True)
        raise ToolError(f'KV write failed: {e}')


@mcp.tool(
    name='memex_kv_get',
    description='Get a fact by exact key from the KV store.',
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_get(
    ctx: Context,
    key: Annotated[str, Field(description='Exact key to look up.')],
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Vault UUID or name. Checks vault-specific first, then global.',
        ),
    ] = None,
) -> McpKVEntry | None:
    """Exact key lookup in the KV store."""
    try:
        api = get_api(ctx)
        if vault_id:
            await _resolve_vault_id(api, vault_id)
        entry = await api.kv_get(key=key, vault_id=vault_id)

        if entry is None:
            return None

        scope = f'vault {entry.vault_id}' if entry.vault_id else 'global'
        return McpKVEntry(
            key=entry.key,
            value=entry.value,
            scope=scope,
            updated_at=entry.updated_at,
        )

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'KV get failed: {e}', exc_info=True)
        raise ToolError(f'KV get failed: {e}')


@mcp.tool(
    name='memex_kv_search',
    description=(
        'Fuzzy search facts in the KV store by semantic similarity. '
        'Returns the closest matching entries.'
    ),
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query text.')],
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Vault UUID or name. None = search global entries only.',
        ),
    ] = None,
    limit: Annotated[int, BeforeValidator(_coerce_int), Field(description='Max results.')] = 5,
) -> list[McpKVEntry]:
    """Semantic search over KV store entries."""
    try:
        api = get_api(ctx)
        if vault_id:
            await _resolve_vault_id(api, vault_id)
        results = await api.kv_search(query=query, vault_id=vault_id, limit=limit)

        return [
            McpKVEntry(
                key=entry.key,
                value=entry.value,
                scope=f'vault {entry.vault_id}' if entry.vault_id else 'global',
                updated_at=entry.updated_at,
            )
            for entry in results
        ]

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'KV search failed: {e}', exc_info=True)
        raise ToolError(f'KV search failed: {e}')


@mcp.tool(
    name='memex_kv_list',
    description='List all facts in the KV store. Without vault_id returns global only.',
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_list(
    ctx: Context,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Vault UUID or name. None = global entries only; with vault = both.',
        ),
    ] = None,
) -> list[McpKVEntry]:
    """List KV store entries."""
    try:
        api = get_api(ctx)
        if vault_id:
            await _resolve_vault_id(api, vault_id)
        entries = await api.kv_list(vault_id=vault_id)

        return [
            McpKVEntry(
                key=entry.key,
                value=entry.value,
                scope=f'vault {entry.vault_id}' if entry.vault_id else 'global',
                updated_at=entry.updated_at,
            )
            for entry in entries
        ]

    except ToolError:
        raise
    except Exception as e:
        logging.error(f'KV list failed: {e}', exc_info=True)
        raise ToolError(f'KV list failed: {e}')


def entrypoint():
    """Entrypoint for the MCP server."""
    asyncio.run(mcp.run_async(transport='stdio'))


if __name__ == '__main__':
    entrypoint()
