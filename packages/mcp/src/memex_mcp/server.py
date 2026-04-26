"FastMCP Memex server implementation"

import contextlib
import os
import pathlib as plb
import asyncio
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID
import mimetypes

import aiofiles
import httpx
import structlog
from fastmcp import FastMCP, Context
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.exceptions import ToolError

from memex_common.asset_resize import resize_image
from fastmcp.utilities.logging import configure_logging
import json

from pydantic import BeforeValidator, Field

from memex_mcp.lifespan import lifespan, get_api, get_asset_cache, get_config
from memex_mcp.models import (
    McpAddAssetsResult,
    McpAddNoteResult,
    McpAsset,
    McpDeleteAssetsResult,
    McpCitation,
    McpCooccurrence,
    McpEntity,
    McpEntityMention,
    McpFact,
    McpEvent,
    McpFindResult,
    McpKVEntry,
    McpKVWriteResult,
    _scope_from_key,
    McpLineageNode,
    McpMemoryLink,
    McpNode,
    McpRelatedNote,
    McpNote,
    McpNoteContent,
    McpNoteSummary,
    McpNoteMetadata,
    McpNoteSearchResult,
    McpObservation,
    McpOverlap,
    McpPageIndex,
    McpPageMetadata,
    McpSupersession,
    McpSurveyFact,
    McpSurveyResult,
    McpSurveyTopic,
    McpVault,
    Staleness,
)
from memex_common.templates import TemplateRegistry, BUILTIN_PROMPTS_DIR
from memex_common.schemas import (
    BatchJobStatus,
    LineageDirection,
    LineageResponse,
    NoteCreateDTO,
    PageIndexDTO,
    PageMetadataDTO,
    TOCNodeDTO,
    filter_toc,
)


_SESSION_DEDUP_TTL = 1800  # 30 minutes

_MAX_RESOURCE_BYTES: Final[int] = 50 * 1024 * 1024
_MAX_GET_RESOURCES_PATHS: Final[int] = 50

# Link types always inlined in search results (all others available via
# memex_get_memory_links on demand).
_CONTRADICTION_LINK_TYPES = frozenset({'contradicts', 'weakens'})


@dataclass
class SessionDedup:
    """Tracks seen note/memory IDs for a single MCP session."""

    seen_note_ids: set[str] = field(default_factory=set)
    seen_memory_ids: set[str] = field(default_factory=set)
    last_access: float = field(default_factory=time.monotonic)


# Module-level session dedup state, keyed by session ID
_session_dedup: dict[str, SessionDedup] = {}


def _get_session_dedup(session_id: str) -> SessionDedup:
    """Get or create session dedup state, purging stale entries."""
    now = time.monotonic()
    # Purge stale entries
    stale = [k for k, v in _session_dedup.items() if now - v.last_access > _SESSION_DEDUP_TTL]
    for k in stale:
        del _session_dedup[k]
    # Get or create
    if session_id not in _session_dedup:
        _session_dedup[session_id] = SessionDedup(last_access=now)
    dedup = _session_dedup[session_id]
    dedup.last_access = now
    return dedup


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
    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    if ALL_VAULTS_WILDCARD in vault_ids:
        vaults = await api.list_vaults()
        return [v.id for v in vaults]

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
    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    if vault_id == ALL_VAULTS_WILDCARD:
        raise ToolError(
            '"*" (all vaults) is not supported for this parameter. '
            'Use a specific vault name or UUID.'
        )
    try:
        return await api.resolve_vault_identifier(vault_id)
    except Exception:
        raise ToolError(f'Vault not found: {vault_id!r}')


def _default_write_vault(ctx: Context) -> str:
    return get_config(ctx).write_vault


def _default_read_vaults(ctx: Context) -> list[str]:
    return get_config(ctx).read_vaults


configure_logging(level=os.environ.get('MEMEX_MCP_LOG_LEVEL', 'WARNING'))

logger = structlog.get_logger(__name__)

mcp = FastMCP(
    'memex_mcp',
    instructions="""Memex is a personal knowledge management system.

TOOL DISCOVERY — This server supports progressive disclosure.
If you see memex_tags/memex_search/memex_get_schema instead of the full tool list:
  1. `memex_tags()` — see tool categories and counts
  2. `memex_search(query, tags=[...])` — find tools by keyword, optionally filtered by tag
  3. `memex_get_schema(tools=[...])` — get parameter details before calling a tool
You can also call any tool directly by name if you already know it.

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
  Note: search results include `related_notes` and contradiction `links`.
  For full relationship links (temporal, semantic, causal), use `memex_get_memory_links`.

IF query asks about specific content, topics, or document lookup:
  → SEARCH
  1. `memex_memory_search` (broad/exploratory) and/or `memex_note_search` (targeted). Run in parallel.
  2. FILTER: after `memex_memory_search`, call `memex_get_notes_metadata` with Note IDs. After `memex_note_search`, metadata is inline — skip this.
  3. READ: `memex_get_page_indices` → `memex_get_nodes` (batch). `memex_read_note` only when total_tokens < 500.
  4. ASSETS: IF `has_assets: true` in page_index/metadata → call `memex_list_assets` then `memex_get_resources` with all paths at once. Use images as visual input. Reproduce diagrams as Mermaid/ASCII in response. NEVER create diagrams without checking assets first.

IF query is broad (e.g. "explain X and how it fits the architecture", "what do you know about X?"):
  → `memex_survey(query)` — auto-decomposes into sub-questions, parallel search, grouped results.
  For manual control, use ENTITY EXPLORATION and SEARCH in parallel instead.

IF storing/retrieving structured facts, preferences, or conventions:
  → KV STORE
  - `memex_kv_write(value, key)` — store a user fact or preference
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

IF checking vault overview, topics, or "what's in this vault":
  → VAULT SUMMARY + SURVEY
  1. `memex_get_vault_summary(vault_id)` → natural language summary, topics, stats
  2. `memex_survey(query)` → decompose into sub-questions, parallel search, grouped results
  Run both in parallel for comprehensive vault overview.

IF user wants to annotate a note or update their commentary:
  → USER NOTES
  - `memex_update_user_notes(note_id, user_notes)` — update user annotations on a note
  - `memex_search_user_notes(query)` — search only user annotations (source_context='user_notes')

IF capturing structured content (architecture decision, RFC, retro, technical brief, learning):
  → TEMPLATES (capture flow)
  1. `memex_list_templates()` — see what's available with descriptions
  2. `memex_get_template(slug)` — fetch the markdown scaffold
  3. Write your note body following the template structure
  4. `memex_add_note(..., template=slug)` — pass the slug so the note is tagged
     and filterable later (`memex_list_notes(template=slug)`, vault summaries).
  Built-ins: `general_note`, `technical_brief`, `architectural_decision_record`,
  `request_for_comments`, `agent_reflection`, `quick_note`. Prefer a template over
  free-form when the content has clear sections.

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

if os.environ.get('MEMEX_MCP_PROGRESSIVE_DISCLOSURE', '').lower() in ('1', 'true', 'yes'):
    from memex_mcp.transforms import DiscoveryMode

    mcp.add_transform(DiscoveryMode())


@mcp.tool(
    name='memex_list_assets',
    description=(
        'List file attachments (assets) for a note — images, audio, PDFs, documents. '
        'REQUIRED when has_assets is true. Feed paths to memex_get_resources to retrieve files.'
    ),
    tags={'assets'},
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
        logger.error(f'List assets failed: {e}', exc_info=True)
        raise ToolError(f'List assets failed: {e}')


@mcp.tool(
    name='memex_read_note',
    description='Read full note. ONLY when total_tokens < 500 (use force=True to override). Otherwise: memex_get_page_indices + memex_get_nodes.',
    tags={'read'},
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
        logger.error(f'Read note failed: {e}', exc_info=True)
        raise ToolError(f'Read note failed: {e}')


@mcp.tool(
    name='memex_set_note_status',
    description=(
        'Set note lifecycle status: active, superseded, appended, archived. '
        'Use to supersede an outdated note, mark it as appended, or archive it. '
        'When superseded, all memory units are marked stale. '
        'Optionally link to the replacing/parent note.'
    ),
    tags={'write'},
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
        logger.error(f'Set note status failed: {e}', exc_info=True)
        raise ToolError(f'Set note status failed: {e}')


@mcp.tool(
    name='memex_update_user_notes',
    description=(
        'Update user_notes on an existing note and reprocess into the memory graph. '
        'Pass null to delete all user annotations. '
        'Old user_notes MemoryUnits are deleted and new ones extracted.'
    ),
    tags={'write'},
    annotations={'readOnlyHint': False},
)
async def memex_update_user_notes(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    user_notes: Annotated[
        str | None,
        Field(description='New user_notes text, or null to delete all annotations.'),
    ] = None,
) -> dict:
    """Update user_notes on an existing note."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        result = await api.update_user_notes(uuid_obj, user_notes)
        return result

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Update user notes failed: {e}', exc_info=True)
        raise ToolError(f'Update user notes failed: {e}')


@mcp.tool(
    name='memex_rename_note',
    description='Rename a note. Updates title in metadata, page index, and doc_metadata.',
    tags={'write'},
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
        logger.error(f'Rename note failed: {e}', exc_info=True)
        raise ToolError(f'Rename note failed: {e}')


async def _fetch_single_resource(ctx: Context, path: str) -> str:
    """Fetch a single resource and return a ``file://`` URI to a session-cached copy.

    Bytes are written into the session ``SessionAssetCache`` so the agent can
    ``Read`` the asset directly off disk instead of getting it inlined as base64.
    """
    cache = get_asset_cache(ctx)
    api = get_api(ctx)
    local_path, _, size = await cache.get_or_fetch(path, api.get_resource)
    if size > _MAX_RESOURCE_BYTES:
        cache.invalidate(path)
        raise ToolError(f'Resource exceeds max size ({size} > {_MAX_RESOURCE_BYTES} bytes)')
    return f'file://{local_path}'


@mcp.tool(
    name='memex_get_resources',
    description=(
        'Retrieve 1+ file attachments (images, audio, documents) by path. '
        'Returns local file paths the agent must `Read` directly. '
        'Use `memex_resize_image` if the asset is too large to forward. '
        'Get paths from memex_list_assets. Accepts a single path or a list.'
    ),
    tags={'assets'},
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
) -> list[str]:
    """Retrieve file resources. Returns a list of file:// URIs or error strings."""
    if len(paths) > _MAX_GET_RESOURCES_PATHS:
        raise ToolError(f'Too many paths requested ({len(paths)} > {_MAX_GET_RESOURCES_PATHS})')
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)
        await _resolve_vault_id(api, vault_id)

        results: list[str] = []
        for path in paths:
            try:
                results.append(await _fetch_single_resource(ctx, path))
            except Exception as exc:
                results.append(f'Error fetching {path}: {exc}')

        return results

    except Exception as e:
        logger.error(f'Get resource failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve resources: {e}')


@mcp.tool(
    name='memex_resize_image',
    description=(
        'Resize an image previously fetched via `memex_get_resources` so it can '
        'be forwarded inline. The input MUST be a path under the session asset '
        'cache; arbitrary filesystem paths are rejected. Returns the resized '
        'file path. Allowed input formats: PNG, JPEG, WEBP, GIF.'
    ),
    tags={'assets'},
    annotations={'readOnlyHint': False},
)
async def memex_resize_image(
    ctx: Context,
    local_path: Annotated[
        str,
        Field(description='Path returned by memex_get_resources (under session cache).'),
    ],
    max_width: Annotated[int, Field(description='Maximum output width in pixels.')] = 1280,
    max_height: Annotated[int, Field(description='Maximum output height in pixels.')] = 1280,
    output_format: Annotated[
        str | None,
        Field(description='Output format override (PNG/JPEG/WEBP/GIF). Defaults to source.'),
    ] = None,
) -> str:
    """Resize an image inside the session asset cache. Returns the new path."""
    if max_width <= 0 or max_height <= 0:
        raise ToolError('max_width and max_height must be positive')

    cache = get_asset_cache(ctx)
    try:
        cache_root = cache.tempdir.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ToolError(f'Asset cache tempdir is unavailable: {exc}')

    try:
        resolved_input = Path(local_path).resolve(strict=True)
    except FileNotFoundError:
        raise ToolError(f'local_path does not exist: {local_path}')
    except OSError as exc:
        raise ToolError(f'cannot access local_path: {exc}')

    if not resolved_input.is_relative_to(cache_root):
        raise ToolError('local_path must point inside the session asset cache')

    try:
        dest_path, _ = await asyncio.to_thread(
            resize_image,
            resolved_input,
            max_width=max_width,
            max_height=max_height,
            output_format=output_format,
        )
    except ValueError as exc:
        raise ToolError(str(exc))

    # Re-check confinement post-resize to close the TOCTOU window between
    # the input resolve and Pillow's internal reopen.
    try:
        resolved_dest = dest_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(dest_path)
        raise ToolError('Resize destination escaped session cache')

    if not resolved_dest.is_relative_to(cache_root):
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(dest_path)
        raise ToolError('Resize destination escaped session cache')

    try:
        cache.register(dest_path)
    except ValueError as exc:
        raise ToolError(str(exc))
    return str(dest_path)


@mcp.tool(
    name='memex_add_assets',
    description='Add one or more file assets to an existing note. Provide local file paths.',
    tags={'assets'},
    annotations={'readOnlyHint': False},
    timeout=60.0,
)
async def memex_add_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    file_paths: Annotated[
        list[str],
        BeforeValidator(_coerce_list),
        Field(description='Absolute paths to asset files to attach.'),
    ],
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
) -> McpAddAssetsResult:
    """Add asset files to an existing note."""
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)

        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        await _resolve_vault_id(api, vault_id)

        files_content: dict[str, bytes] = {}
        for file_path in file_paths:
            path = plb.Path(file_path)
            if not path.exists() or not path.is_file():
                logger.warning(f'Asset file not found or not a file: {file_path}')
                continue
            async with aiofiles.open(path, 'rb') as f:
                files_content[path.name] = await f.read()

        if not files_content:
            raise ToolError('No valid asset files found at the given paths.')

        try:
            result = await api.add_note_assets(uuid_obj, files_content)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError(f'Note {note_id} not found.')
            raise

        added_assets = []
        for asset_path in result.get('added_assets', []):
            path_obj = plb.Path(asset_path)
            mime_type, _ = mimetypes.guess_type(path_obj.name)
            added_assets.append(
                McpAsset(filename=path_obj.name, path=asset_path, mime_type=mime_type)
            )

        return McpAddAssetsResult(
            note_id=str(uuid_obj),
            added_assets=added_assets,
            skipped=result.get('skipped', []),
            asset_count=result.get('asset_count', 0),
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Add assets failed: {e}', exc_info=True)
        raise ToolError(f'Add assets failed: {e}')


@mcp.tool(
    name='memex_delete_assets',
    description='Delete one or more asset files from an existing note. Get paths from memex_list_assets.',
    tags={'assets'},
    annotations={'readOnlyHint': False},
    timeout=30.0,
)
async def memex_delete_assets(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    asset_paths: Annotated[
        list[str],
        BeforeValidator(_coerce_list),
        Field(description='Asset path(s) to delete (from memex_list_assets).'),
    ],
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
) -> McpDeleteAssetsResult:
    """Delete asset files from an existing note."""
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)

        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            raise ToolError(f'Invalid Note UUID: {note_id}')

        await _resolve_vault_id(api, vault_id)

        try:
            result = await api.delete_note_assets(uuid_obj, asset_paths)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError(f'Note {note_id} not found.')
            raise

        return McpDeleteAssetsResult(
            note_id=str(uuid_obj),
            deleted=result.get('deleted_assets', []),
            not_found=result.get('not_found', []),
            asset_count=result.get('asset_count', 0),
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Delete assets failed: {e}', exc_info=True)
        raise ToolError(f'Delete assets failed: {e}')


def _get_template_registry(ctx: Context) -> TemplateRegistry:
    """Build a TemplateRegistry from the MCP server config."""
    config = get_config(ctx)
    dirs: list[tuple[str, plb.Path]] = [('builtin', BUILTIN_PROMPTS_DIR)]
    root = config.server.file_store.root
    if '://' not in root:
        dirs.append(('global', plb.Path(root) / 'templates'))
    else:
        logger.debug('Skipping global templates: remote filestore (%s)', root)
    dirs.append(('local', plb.Path('.memex/templates')))
    return TemplateRegistry(dirs)


@mcp.tool(
    name='memex_get_template',
    description=(
        'Fetch a markdown scaffold to follow when writing a structured note. '
        'Call this BEFORE memex_add_note for ADRs, retros, technical briefs, RFCs, '
        'or any note with clear sections. Use memex_list_templates to discover slugs.'
    ),
    tags={'write', 'templates'},
    annotations={'readOnlyHint': True},
)
def memex_get_template(
    ctx: Context,
    type: Annotated[
        str,
        Field(
            description='Template slug. Use memex_list_templates to discover available templates.',
        ),
    ],
) -> str:
    """Retrieve a markdown template for note creation."""
    try:
        registry = _get_template_registry(ctx)
        return registry.get_template(type)
    except KeyError as e:
        raise ToolError(str(e))
    except Exception as e:
        logger.error(f'Get template failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve template: {e}')


@mcp.tool(
    name='memex_list_templates',
    description=(
        'List note templates (built-in + user-registered). Call this when about to '
        'capture structured content — pick a slug, fetch the body with '
        'memex_get_template, then pass `template=slug` to memex_add_note.'
    ),
    tags={'write', 'templates'},
    annotations={'readOnlyHint': True},
)
def memex_list_templates(ctx: Context) -> str:
    """List all available templates."""
    try:
        registry = _get_template_registry(ctx)
        templates = registry.list_templates()
        if not templates:
            return 'No templates available.'
        lines = []
        for t in templates:
            source_tag = f'[{t.source}]'
            lines.append(f'- **{t.slug}** {source_tag} — {t.display_name}: {t.description}')
        lines.append('')
        lines.append(
            'Next: `memex_get_template(slug)` to fetch the body, then write your '
            'content following the structure and call `memex_add_note(..., template=slug)`.'
        )
        return '\n'.join(lines)
    except Exception as e:
        logger.error(f'List templates failed: {e}', exc_info=True)
        raise ToolError(f'Failed to list templates: {e}')


@mcp.tool(
    name='memex_register_template',
    description=(
        'Register a custom note template from inline markdown. Use when a recurring '
        'capture pattern (sprint retro, incident postmortem, etc.) does not match a '
        'built-in. To delete a template, use the CLI: memex note template delete <slug>'
    ),
    tags={'write', 'templates'},
    annotations={'readOnlyHint': False},
)
def memex_register_template(
    ctx: Context,
    slug: Annotated[str, Field(description='Template identifier (e.g. sprint_retro).')],
    template: Annotated[
        str, Field(description='Markdown template content. Should include YAML frontmatter.')
    ],
    name: Annotated[str | None, Field(description='Human-readable template name.')] = None,
    description: Annotated[
        str | None, Field(description='Short description of the template.')
    ] = None,
) -> str:
    """Register a new template from inline content."""
    try:
        registry = _get_template_registry(ctx)
        info = registry.register_from_content(
            slug=slug, template=template, name=name, description=description, scope='global'
        )
        return f'Registered template: {info.slug} ({info.display_name}) in {info.source} scope.'
    except Exception as e:
        logger.error(f'Register template failed: {e}', exc_info=True)
        raise ToolError(f'Failed to register template: {e}')


@mcp.tool(
    name='memex_active_vault',
    description=(
        '[DEPRECATED — use memex_list_vaults instead, which includes is_active flag] '
        'Get the active vault name and ID. Shows both server default and client-resolved vault.'
    ),
    tags={'browse'},
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
        logger.error(f'Get active vault failed: {e}', exc_info=True)
        raise ToolError(f'Failed to retrieve active vault: {e}')


@mcp.tool(
    name='memex_add_note',
    description=(
        'Add a new note or document to Memex. Ingest content into a vault. Confirm '
        'vault with user first, or pass vault_id. For structured captures (ADRs, '
        'retros, technical briefs, RFCs), call memex_list_templates first and pass '
        'the chosen slug as `template` for provenance and downstream filtering.'
    ),
    tags={'write'},
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
    user_notes: Annotated[
        str | None,
        Field(
            default=None,
            description='Optional user-provided context or commentary to include in the note.',
        ),
    ] = None,
    date: Annotated[
        str | None,
        Field(
            default=None,
            description='Note date in ISO 8601 format (e.g. 2026-03-27). Defaults to now.',
        ),
    ] = None,
    template: Annotated[
        str | None,
        Field(
            default=None,
            description='Template slug used to create this note (e.g. "general_note").',
        ),
    ] = None,
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
                    logger.warning(f'Supporting file not found or not a file: {file_path}')

        # Construct frontmatter
        fm_data: dict[str, Any] = {
            'title': title,
            'description': description,
            'author': author,
            'supporting_files': supporting_files,
            'tags': tags,
        }
        if date:
            fm_data['date'] = date

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
            user_notes=user_notes,
            author=author,
            template=template,
        )

        result = await api.ingest(note, background=background)
        if isinstance(result, BatchJobStatus):
            return McpAddNoteResult(
                note_id=result.job_id if result.job_id else UUID(int=0),
                status='queued',
                job_id=str(result.job_id) if result.job_id else None,
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
        logger.error(f'Add note failed: {e}', exc_info=True)
        raise ToolError(f'Add note failed: {e}')


def compute_staleness(
    *,
    event_date: Any | None,
    confidence: float,
    superseded_by: list[Any] | None,
    links: list[Any] | None,
    now: Any | None = None,
) -> Staleness:
    """Determine the staleness of a memory unit.

    Priority: CONTESTED > confidence-based STALE > time-based (FRESH / AGING / STALE).

    Date fallback chain (resolved in ``_build_memory_unit_model``):
        1. ``event_date`` — only present on the SQL model (MemoryUnit), not on
           MemoryUnitDTO. Will be None when the DTO comes from the HTTP API.
        2. ``mentioned_at`` — set on observations; for world facts the server's
           ``build_memory_unit_dto`` copies ``event_date`` into this field as a
           fallback (see ``memex_core.server.common``), so it is normally
           populated even for world facts.
        3. ``occurred_start`` — set on events with a specific occurrence time.

    If none of these dates are available (all None), staleness falls back to
    confidence alone: >= 0.7 → AGING, < 0.5 → STALE. This avoids penalising
    high-confidence world facts whose date was lost in DTO serialisation.

    Args:
        event_date: Best-effort date from the fallback chain above.
        confidence: Confidence score (0.0-1.0).
        superseded_by: Units that supersede this one.
        links: Typed relationship links (may contain contradiction relations).
        now: Current datetime (injectable for testing).
    """
    from datetime import datetime as _dt, timezone as _tz

    # --- Contested check (highest priority) ---
    if superseded_by:
        return Staleness.CONTESTED

    if links:
        contradiction_relations = {'contradicts', 'contradiction', 'weakens'}
        for lnk in links:
            relation = getattr(lnk, 'relation', None) or (
                lnk.get('relation') if isinstance(lnk, dict) else None
            )
            if relation and relation.lower() in contradiction_relations:
                return Staleness.CONTESTED

    # --- Time-based checks ---
    if now is None:
        now = _dt.now(tz=_tz.utc)

    if confidence < 0.5:
        return Staleness.STALE

    if event_date is not None and isinstance(event_date, _dt):
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=_tz.utc)
        age_days = (now - event_date).days

        if age_days > 30:
            return Staleness.STALE

        if age_days >= 7:
            return Staleness.AGING

        if confidence >= 0.7:
            return Staleness.FRESH

        return Staleness.AGING

    # No usable date — rely on confidence alone.
    # High confidence without a date should not be penalised as STALE;
    # treat as AGING (unknown age) so the LLM can still use the result.
    if confidence >= 0.7:
        return Staleness.AGING

    return Staleness.AGING


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

    # Virtual metadata stores UUIDs as strings (JSONB round-trip); coerce to
    # UUID at the MCP boundary so the Pydantic model stays typed, and skip
    # malformed entries rather than raising — corrupted metadata must not
    # break the whole response.
    is_virtual = bool(unit_metadata.get('virtual'))
    mental_model_id_raw = unit_metadata.get('mental_model_id') if is_virtual else None
    mental_model_id_uuid: UUID | None = None
    if mental_model_id_raw:
        try:
            mental_model_id_uuid = UUID(str(mental_model_id_raw))
        except (ValueError, TypeError):
            mental_model_id_uuid = None
    evidence_ids_raw = unit_metadata.get('evidence_ids', []) if is_virtual else []
    evidence_ids: list[UUID] = []
    for eid in evidence_ids_raw or []:
        try:
            evidence_ids.append(UUID(str(eid)))
        except (ValueError, TypeError):
            continue

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
        'virtual': is_virtual,
        'mental_model_id': mental_model_id_uuid,
        'evidence_ids': evidence_ids,
    }

    links_raw = unit_metadata.get('links', [])
    # Only inline contradiction/weakens links; other types available via
    # memex_get_memory_links
    contradiction_links = [
        McpMemoryLink(**lnk)
        for lnk in links_raw
        if isinstance(lnk, dict) and lnk.get('relation') in _CONTRADICTION_LINK_TYPES
    ]
    base_kwargs['links'] = contradiction_links

    # Staleness date fallback chain — see compute_staleness docstring for semantics.
    # event_date: only on SQL model (None on DTO from HTTP API)
    # mentioned_at: observations; also backfilled from event_date for world facts
    # occurred_start: events with a specific occurrence time
    event_date = (
        getattr(res, 'event_date', None)
        or getattr(res, 'mentioned_at', None)
        or getattr(res, 'occurred_start', None)
    )
    base_kwargs['staleness'] = compute_staleness(
        event_date=event_date,
        confidence=base_kwargs['confidence'],
        superseded_by=getattr(res, 'superseded_by', None) or [],
        links=links_raw,
    )

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
        'Find information about any topic. Best for broad/exploratory queries. '
        'Contradiction links are always included on returned units. '
        'For other link types (temporal, semantic, causal), use `memex_get_memory_links`. '
        'For targeted document lookup, use memex_note_search. When unsure, run both in parallel.'
    ),
    tags={'search'},
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
            description='Vault UUIDs or names. Use "*" for all vaults. Omit to use config defaults.',
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
    include_seen: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            default=True,
            description=(
                'Include previously returned results in full. '
                'Set to false to compress already-seen results '
                '(returns {id, note_title, previously_returned: true}).'
            ),
        ),
    ] = True,
    source_context: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Filter by source context (e.g. "user_notes" to search only user annotations).'
            ),
        ),
    ] = None,
    reference_date: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'ISO-8601 timestamp. Relative dates in the query (e.g. "last week") '
                'resolve against this instead of now(). Use for historical queries.'
            ),
        ),
    ] = None,
    expand_query: Annotated[
        bool,
        Field(
            default=False,
            description=(
                'Expand query using LLM-generated semantic variations for broader recall. '
                'Use when initial search returns insufficient results.'
            ),
        ),
    ] = False,
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
        ref_dt = (
            _dt.fromisoformat(reference_date).replace(tzinfo=_tz.utc) if reference_date else None
        )

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
            source_context=source_context,
            reference_date=ref_dt,
            expand_query=expand_query,
        )

        if not results:
            return [
                McpFact(
                    id=UUID(int=0),
                    text=(
                        'No results found. If you learn something about this topic '
                        'during this session, consider saving it.'
                    ),
                    confidence=0.0,
                    tags=['system-hint'],
                )
            ]

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

        # Session-level dedup
        dedup = _get_session_dedup(ctx.session_id)
        output: list[McpFact | McpEvent | McpObservation] = []
        for res in results:
            mid = str(res.id)
            if not include_seen and mid in dedup.seen_memory_ids:
                # Compressed representation for already-seen results
                output.append(
                    McpFact(
                        id=res.id,
                        text='',
                        note_id=res.note_id,
                        note_title=note_titles.get(res.note_id) if res.note_id else None,
                        previously_returned=True,
                    )
                )
            else:
                model = _build_memory_unit_model(res, note_titles)
                output.append(model)
            dedup.seen_memory_ids.add(mid)

        return output

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Search failed: {e}', exc_info=True)
        raise ToolError(f'Search failed: {e}')


@mcp.tool(
    name='memex_search_user_notes',
    description=(
        'Search only your own annotations (user_notes) across all notes. '
        'Returns memory units extracted from user_notes frontmatter. '
        'Use this to recall what you yourself have been thinking or annotating.'
    ),
    tags={'search'},
    annotations={'readOnlyHint': True},
    timeout=60.0,
)
async def memex_search_user_notes(
    ctx: Context,
    query: Annotated[str, Field(description='Search query.')],
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            description='Vault UUIDs or names. Use "*" for all vaults. Omit to use config defaults.',
        ),
    ] = None,
    limit: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max results.'),
    ] = 10,
) -> list[McpFact | McpEvent | McpObservation]:
    """Search user annotations only (hardcodes source_context='user_notes')."""
    return await memex_memory_search(
        ctx=ctx,
        query=query,
        vault_ids=vault_ids,
        limit=limit,
        source_context='user_notes',
    )


@mcp.tool(
    name='memex_note_search',
    description=(
        'Search and find source notes by hybrid retrieval (note search). '
        'Find notes about any topic. Returns ranked notes with description. '
        'Results include `related_notes` (notes sharing entities, ranked by specificity) '
        'and contradiction `links` (contradicts/weakens only). '
        'For other link types (temporal, semantic, causal), use `memex_get_memory_links`. '
        'Best for targeted document lookup. '
        'For broad exploration, use memex_memory_search. When unsure, run both in parallel.'
    ),
    tags={'search'},
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
            description='Vault UUIDs or names. Use "*" for all vaults. Omit to use config defaults.',
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
    include_seen: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            default=True,
            description=(
                'Include previously returned results in full. '
                'Set to false to compress already-seen results '
                '(returns {note_id, title, previously_returned: true}).'
            ),
        ),
    ] = True,
    has_assets: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            default=False,
            description='Only return notes that have file attachments (images, PDFs, etc.).',
        ),
    ] = False,
    reference_date: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'ISO-8601 timestamp. Relative dates in the query (e.g. "last week") '
                'resolve against this instead of now(). Use for historical queries.'
            ),
        ),
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
        ref_dt = (
            _dt.fromisoformat(reference_date).replace(tzinfo=_tz.utc) if reference_date else None
        )

        search_limit = limit * 3 if has_assets else limit
        results = await api.search_notes(
            query=query,
            limit=search_limit,
            expand_query=expand_query,
            reason=False,
            summarize=False,
            vault_ids=resolved_vids,
            strategies=strategies,
            after=after_dt,
            before=before_dt,
            tags=tags,
            reference_date=ref_dt,
        )

        if has_assets:
            results = [r for r in results if (r.metadata or {}).get('has_assets', False)][:limit]

        if not results:
            return [
                McpNoteSearchResult(
                    note_id=UUID(int=0),
                    title='No results',
                    score=0.0,
                    description=(
                        'No results found. If you learn something about this topic '
                        'during this session, consider saving it.'
                    ),
                    tags=['system-hint'],
                )
            ]

        # Session-level dedup
        dedup = _get_session_dedup(ctx.session_id)
        output: list[McpNoteSearchResult] = []
        for doc in results:
            nid = str(doc.note_id)
            if not include_seen and nid in dedup.seen_note_ids:
                # Compressed representation for already-seen notes
                metadata = doc.metadata or {}
                title = (
                    metadata.get('title')
                    or metadata.get('name')
                    or metadata.get('filename')
                    or 'Untitled'
                )
                output.append(
                    McpNoteSearchResult(
                        note_id=doc.note_id,
                        title=title,
                        score=doc.score,
                        previously_returned=True,
                    )
                )
            else:
                metadata = doc.metadata or {}
                title = (
                    metadata.get('title')
                    or metadata.get('name')
                    or metadata.get('filename')
                    or 'Untitled'
                )
                # Use page_index description; fall back to first block summary
                description = metadata.get('description')
                if not description and doc.summaries:
                    s = doc.summaries[0]
                    description = (
                        s.topic if not s.key_points else f'{s.topic} — {" | ".join(s.key_points)}'
                    )
                rc = get_config(ctx).server.memory.retrieval.relations
                related_notes = [
                    McpRelatedNote(
                        note_id=rn.note_id,
                        title=rn.title,
                        shared_entities=(
                            rn.shared_entities[: rc.max_shared_entities]
                            if rc.max_shared_entities
                            else []
                        ),
                        strength=rn.strength,
                    )
                    for rn in getattr(doc, 'related_notes', [])[: rc.top_k_related]
                ]
                # Only inline contradiction/weakens links; other types via
                # memex_get_memory_links
                links = [
                    McpMemoryLink(
                        unit_id=lnk.unit_id,
                        note_id=lnk.note_id,
                        note_title=lnk.note_title,
                        relation=lnk.relation,
                        weight=lnk.weight,
                        time=lnk.time.isoformat() if lnk.time else None,
                        metadata={},
                    )
                    for lnk in getattr(doc, 'links', [])
                    if lnk.relation in _CONTRADICTION_LINK_TYPES
                ][: rc.max_links]
                output.append(
                    McpNoteSearchResult(
                        note_id=doc.note_id,
                        title=title,
                        score=doc.score,
                        vault_name=doc.vault_name,
                        status=getattr(doc, 'note_status', None),
                        description=description,
                        tags=metadata.get('tags', []),
                        source_uri=metadata.get('source_uri'),
                        has_assets=metadata.get('has_assets', False),
                        related_notes=related_notes,
                        links=links,
                    )
                )
            dedup.seen_note_ids.add(nid)

        return output

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Note search failed: {e}', exc_info=True)
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
        'Get note table of contents (TOC): section titles, summaries, node IDs, '
        'and subtree_tokens for 1+ notes. Includes `related_notes` — other notes sharing '
        'entities with this one, ranked by specificity. '
        'Each node includes subtree_tokens (own + all descendant tokens) for read budgeting. '
        'Expensive for large notes — only call AFTER memex_get_notes_metadata confirms relevance. '
        'For large notes (total_tokens > 3000): use depth=0 to get top-level sections (H1+H2) first, '
        'then drill into specific sections with parent_node_id. '
        'Pass leaf node IDs (nodes without children) to memex_get_nodes to read content.'
    ),
    tags={'read'},
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

        if output:
            note_ids_for_related = [o.note_id for o in output]
            related_map = await api.get_related_notes(note_ids_for_related)
            for o in output:
                o.related_notes = [
                    McpRelatedNote(
                        note_id=rn.note_id,
                        title=rn.title,
                        shared_entities=rn.shared_entities,
                        strength=rn.strength,
                    )
                    for rn in related_map.get(o.note_id, [])
                ]

        return output

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Get page index failed: {e}', exc_info=True)
        raise ToolError(f'Get page index failed: {e}')


@mcp.tool(
    name='memex_get_notes_metadata',
    description=(
        'Get metadata (title, tags, token count, has_assets) for 1+ notes. '
        'Use after memex_memory_search to filter results before reading. '
        'SKIP after memex_note_search (metadata already inline).'
    ),
    tags={'read'},
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
        logger.error(f'Get notes metadata failed: {e}', exc_info=True)
        raise ToolError(f'Get notes metadata failed: {e}')


@mcp.tool(
    name='memex_get_nodes',
    description=(
        'Read note sections by node IDs. Get node IDs from memex_get_page_indices. '
        'Accepts 1 or more IDs — use for single and batch reads.'
    ),
    tags={'read'},
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
        logger.error(f'Get nodes failed: {e}', exc_info=True)
        raise ToolError(f'Get nodes failed: {e}')


@mcp.tool(
    name='memex_list_vaults',
    description='List all vaults with note counts. Each vault includes is_active and note_count.',
    tags={'browse'},
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
                    access=v.access,
                )
                for v in vaults
            ]

    except Exception as e:
        logger.error(f'List vaults failed: {e}', exc_info=True)
        raise ToolError(f'List vaults failed: {e}')


@mcp.tool(
    name='memex_list_notes',
    description=(
        'List notes with optional date, tag, and status filters. '
        "Use after/before for temporal queries like 'documents from 2026'. "
        'Use tags for topic filtering (AND semantics). '
        'Use status to filter by lifecycle (active, archived, etc.).'
    ),
    tags={'browse'},
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
    template: Annotated[
        str | None,
        Field(default=None, description='Filter by template slug (e.g. "general_note").'),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Filter by tags (AND semantics). Only notes containing all specified tags.',
        ),
    ] = None,
    status: Annotated[
        str | None,
        Field(
            default=None,
            description='Filter by note lifecycle status (e.g. "active", "archived").',
        ),
    ] = None,
    date_by: Annotated[
        str,
        Field(
            default='created_at',
            description=(
                "Which date column --after/--before filter on: 'created_at' "
                "(ingest time, default), 'publish_date' (authored date), or "
                "'coalesce' (publish_date if set, else created_at). "
                'Default is created_at to avoid misextracted publish dates.'
            ),
        ),
    ] = 'created_at',
) -> list[McpNote]:
    """List notes with optional date, tag, and status filters."""
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
            template=template,
            tags=tags,
            status=status,
            date_field=date_by,
        )

        return [
            McpNote(
                id=n.id,
                title=n.title or 'Untitled',
                created_at=n.created_at,
                publish_date=n.publish_date,
                vault_id=n.vault_id,
                template=(n.doc_metadata or {}).get('template'),
                summaries=[
                    McpNoteSummary(topic=s.topic, key_points=s.key_points)
                    for s in (getattr(n, 'summaries', None) or [])
                ],
            )
            for n in notes
        ]

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'List notes failed: {e}', exc_info=True)
        raise ToolError(f'List notes failed: {e}')


@mcp.tool(
    name='memex_recent_notes',
    description='Browse recent notes. Defaults to all vaults. '
    'Filter by vault names/UUIDs and optional date range.',
    tags={'browse'},
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
            description='Vault UUIDs or names. Use "*" for all vaults. Omit for all vaults.',
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
    template: Annotated[
        str | None,
        Field(default=None, description='Filter by template slug (e.g. "general_note").'),
    ] = None,
    date_by: Annotated[
        str,
        Field(
            default='created_at',
            description=(
                "Which date column --after/--before filter on: 'created_at' "
                "(ingest time, default), 'publish_date' (authored date), or "
                "'coalesce' (publish_date if set, else created_at)."
            ),
        ),
    ] = 'created_at',
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
            template=template,
            date_field=date_by,
        )

        return [
            McpNote(
                id=n.id,
                title=n.title or 'Untitled',
                created_at=n.created_at,
                publish_date=n.publish_date,
                vault_id=n.vault_id,
                template=(n.doc_metadata or {}).get('template'),
                summaries=[
                    McpNoteSummary(topic=s.topic, key_points=s.key_points)
                    for s in (getattr(n, 'summaries', None) or [])
                ],
            )
            for n in notes
        ]

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Recent notes failed: {e}', exc_info=True)
        raise ToolError(f'Recent notes failed: {e}')


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
    tags={'entities'},
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
        logger.error(f'List entities failed: {e}', exc_info=True)
        raise ToolError(f'List entities failed: {e}')


@mcp.tool(
    name='memex_get_entities',
    description=(
        'Get entity details (name, type, mention count) for 1+ entities by UUID. '
        'Use after memex_list_entities to get full details.'
    ),
    tags={'entities'},
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
        logger.error(f'Get entities failed: {e}', exc_info=True)
        raise ToolError(f'Get entities failed: {e}')


@mcp.tool(
    name='memex_get_entity_mentions',
    description='Get facts, observations, and events that mention an entity. Each mention links to its source note, revealing cross-note connections.',
    tags={'entities'},
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
        logger.error(f'Get entity mentions failed: {e}', exc_info=True)
        raise ToolError(f'Get entity mentions failed: {e}')


@mcp.tool(
    name='memex_get_entity_cooccurrences',
    description='Find entities that frequently appear alongside a given entity — the fastest way to map relationships and discover connected concepts. Returns entity names, types, and co-occurrence counts inline (no follow-up calls needed). Use this for "what relates to X?" questions.',
    tags={'entities'},
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
        logger.error(f'Get entity cooccurrences failed: {e}', exc_info=True)
        raise ToolError(f'Get entity cooccurrences failed: {e}')


_LINEAGE_ENTITY_TYPES = frozenset({'mental_model', 'observation', 'memory_unit', 'note'})


def _lineage_to_mcp(resp: LineageResponse) -> McpLineageNode:
    """Recursively convert a LineageResponse to an McpLineageNode."""
    return McpLineageNode(
        entity_type=resp.entity_type,
        entity=resp.entity,
        derived_from=[_lineage_to_mcp(child) for child in resp.derived_from],
    )


@mcp.tool(
    name='memex_get_lineage',
    description=(
        'Trace provenance and connections between documents and facts. '
        'How does a fact connect to a document? '
        'Upstream: mental_model → observation → memory_unit → note. '
        'Downstream: note → memory_unit → observation → mental_model.'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_lineage(
    ctx: Context,
    entity_type: Annotated[
        str,
        Field(description='Entity type: mental_model, observation, memory_unit, or note.'),
    ],
    entity_id: Annotated[str, Field(description='UUID of the entity.')],
    direction: Annotated[
        str,
        Field(description='Traversal direction: upstream (default), downstream, or both.'),
    ] = 'upstream',
    depth: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max recursion depth.'),
    ] = 3,
    limit: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max children per node.'),
    ] = 5,
) -> McpLineageNode:
    """Get the lineage (provenance chain) of an entity."""
    try:
        if entity_type not in _LINEAGE_ENTITY_TYPES:
            raise ToolError(
                f'Invalid entity_type: {entity_type}. '
                f'Must be one of: {", ".join(sorted(_LINEAGE_ENTITY_TYPES))}'
            )

        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid UUID: {entity_id}')

        try:
            dir_enum = LineageDirection(direction)
        except ValueError:
            raise ToolError(
                f'Invalid direction: {direction}. Must be upstream, downstream, or both.'
            )

        api = get_api(ctx)

        response = await api.get_lineage(
            entity_type=entity_type,
            entity_id=uuid_obj,
            direction=dir_enum,
            depth=depth,
            limit=limit,
        )

        return _lineage_to_mcp(response)

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Get lineage failed: {e}', exc_info=True)
        raise ToolError(f'Get lineage failed: {e}')


@mcp.tool(
    name='memex_get_memory_units',
    description='Batch lookup of memory units by ID. Includes contradiction links and supersession info.',
    tags={'storage'},
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
        logger.error(f'Get memory units failed: {e}', exc_info=True)
        raise ToolError(f'Get memory units failed: {e}')


@mcp.tool(
    name='memex_get_memory_links',
    description=(
        'Get typed relationship links for memory units. Returns temporal, '
        'semantic, causal, contradiction, and other links. Filter by '
        'link_type for specific relationships. Use after search to explore '
        'relationship chains.'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_memory_links(
    ctx: Context,
    unit_ids: Annotated[
        list[str],
        BeforeValidator(_coerce_list),
        Field(description='List of memory unit UUIDs.'),
    ],
    link_type: Annotated[
        str | None,
        Field(
            default=None,
            description=('Filter by link type: contradicts, temporal, semantic, causal, etc.'),
        ),
    ] = None,
    limit: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max links per unit.'),
    ] = 20,
) -> list[McpMemoryLink]:
    """Retrieve relationship links for memory units."""
    try:
        api = get_api(ctx)
        uuids: list[UUID] = []
        for uid_str in unit_ids:
            try:
                uuids.append(UUID(uid_str))
            except ValueError:
                continue

        if not uuids:
            return []

        link_types = [link_type] if link_type else None
        links_map = await api.get_memory_links(uuids, link_types=link_types)

        output: list[McpMemoryLink] = []
        for uid in uuids:
            for lnk in links_map.get(uid, [])[:limit]:
                output.append(
                    McpMemoryLink(
                        unit_id=lnk.unit_id,
                        note_id=lnk.note_id,
                        note_title=lnk.note_title,
                        relation=lnk.relation,
                        weight=lnk.weight,
                        time=lnk.time.isoformat() if lnk.time else None,
                        metadata=lnk.metadata or {},
                    )
                )

        return output

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Get memory links failed: {e}', exc_info=True)
        raise ToolError(f'Get memory links failed: {e}')


@mcp.tool(
    name='memex_find_note',
    description=(
        'Lightweight fuzzy title search. Returns matching note titles, IDs, and scores. '
        'Use when you know (part of) the title. For content search, use memex_note_search.'
    ),
    tags={'search'},
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
            description='Vault UUIDs or names to search in, e.g. [\'rituals\']. Use "*" for all vaults. None = all vaults.',
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
        logger.error(f'Find note failed: {e}', exc_info=True)
        raise ToolError(f'Find note failed: {e}')


@mcp.tool(
    name='memex_kv_write',
    description=(
        'Write a fact to the key-value store. Generates an embedding for semantic search. '
        'Use for storing structured preferences, settings, or facts. '
        'Key MUST start with a namespace prefix: '
        '"global:" (always loaded), "user:" (personal prefs), '
        '"project:<project-id>:" (project-scoped), or '
        '"app:<app-id>:" (application-scoped). '
        'Examples: "global:tool:python:pkg_mgr", "user:work:employer", '
        '"project:github.com/user/repo:vault", "app:claude-code:theme".'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': False, 'idempotentHint': True},
    timeout=15.0,
)
async def memex_kv_write(
    ctx: Context,
    value: Annotated[str, Field(description='The fact or preference text to store.')],
    key: Annotated[
        str,
        Field(
            description=(
                'Namespaced key. Must start with global:, user:, project:, or app:. '
                'Examples: "global:lang:python:version", '
                '"project:github.com/user/repo:vault", "app:claude-code:theme".'
            ),
        ),
    ],
    ttl_seconds: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                'Optional time-to-live in seconds. Entry expires after this duration. '
                'Omit or pass null for no expiration.'
            ),
        ),
    ] = None,
) -> McpKVWriteResult:
    """Write a fact to the KV store with embedding generation."""
    try:
        api = get_api(ctx)

        # Generate embedding for semantic search via the API layer
        embedding = await api.embed_text(value)

        entry = await api.kv_put(
            value=value,
            key=key,
            embedding=embedding,
            ttl_seconds=ttl_seconds,
        )

        scope = _scope_from_key(entry.key)
        return McpKVWriteResult(
            key=entry.key, value=entry.value, scope=scope, expires_at=entry.expires_at
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'KV write failed: {e}', exc_info=True)
        raise ToolError(f'KV write failed: {e}')


@mcp.tool(
    name='memex_kv_get',
    description='Get a fact by exact key from the KV store.',
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_get(
    ctx: Context,
    key: Annotated[str, Field(description='Exact key to look up.')],
) -> McpKVEntry | None:
    """Exact key lookup in the KV store."""
    try:
        api = get_api(ctx)
        entry = await api.kv_get(key=key)

        if entry is None:
            return None

        return McpKVEntry(
            key=entry.key,
            value=entry.value,
            scope=_scope_from_key(entry.key),
            updated_at=entry.updated_at,
            expires_at=entry.expires_at,
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'KV get failed: {e}', exc_info=True)
        raise ToolError(f'KV get failed: {e}')


@mcp.tool(
    name='memex_kv_search',
    description=(
        'Fuzzy search facts in the KV store by semantic similarity. '
        'Returns the closest matching entries. '
        'Optionally filter by namespace prefixes (global, user, project).'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_search(
    ctx: Context,
    query: Annotated[str, Field(description='Search query text.')],
    namespaces: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Namespace prefixes to filter by (e.g. ["global", "user"]).',
        ),
    ] = None,
    limit: Annotated[int, BeforeValidator(_coerce_int), Field(description='Max results.')] = 5,
) -> list[McpKVEntry]:
    """Semantic search over KV store entries."""
    try:
        api = get_api(ctx)
        results = await api.kv_search(query=query, namespaces=namespaces, limit=limit)

        return [
            McpKVEntry(
                key=entry.key,
                value=entry.value,
                scope=_scope_from_key(entry.key),
                updated_at=entry.updated_at,
                expires_at=entry.expires_at,
            )
            for entry in results
        ]

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'KV search failed: {e}', exc_info=True)
        raise ToolError(f'KV search failed: {e}')


@mcp.tool(
    name='memex_kv_list',
    description=(
        'List all entries in the key-value store. Shows stored facts, preferences, '
        'and settings. Optionally filter by namespace prefixes (global, user, project).'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_kv_list(
    ctx: Context,
    namespaces: Annotated[
        list[str] | None,
        Field(
            default=None,
            description='Namespace prefixes to filter by (e.g. ["global", "user"]).',
        ),
    ] = None,
    pattern: Annotated[
        str | None,
        Field(
            default=None,
            description='Wildcard filter (e.g. "global:preferences:*"). Only trailing * supported.',
        ),
    ] = None,
) -> list[McpKVEntry]:
    """List KV store entries."""
    try:
        api = get_api(ctx)
        entries = await api.kv_list(namespaces=namespaces, pattern=pattern)

        return [
            McpKVEntry(
                key=entry.key,
                value=entry.value,
                scope=_scope_from_key(entry.key),
                updated_at=entry.updated_at,
                expires_at=entry.expires_at,
            )
            for entry in entries
        ]

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'KV list failed: {e}', exc_info=True)
        raise ToolError(f'KV list failed: {e}')


@mcp.tool(
    name='memex_survey',
    description=(
        'Survey a broad topic. Decomposes into 3-5 focused sub-questions, '
        'runs parallel searches, deduplicates, and returns facts grouped by source note. '
        'Use for panoramic queries like "what do you know about X?" instead of '
        'making many manual search calls.'
    ),
    tags={'search'},
    annotations={'readOnlyHint': True},
    timeout=120.0,
)
async def memex_survey(
    ctx: Context,
    query: Annotated[str, Field(description='Broad topic or panoramic query to survey.')],
    vault_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            description='Vault UUIDs or names. Use "*" for all vaults. Omit to use config defaults.',
        ),
    ] = None,
    limit_per_query: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(description='Max results per sub-question.'),
    ] = 10,
    token_budget: Annotated[
        int | None,
        BeforeValidator(_coerce_int),
        Field(description='Max token budget for all results. Truncates when exceeded.'),
    ] = None,
) -> McpSurveyResult:
    """Survey a broad topic — decompose, parallel search, grouped results."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(api, vault_ids)

        result = await api.survey(
            query=query,
            vault_ids=resolved_vids,
            limit_per_query=limit_per_query,
            token_budget=token_budget,
        )

        topics = [
            McpSurveyTopic(
                note_id=t.note_id,
                title=t.title,
                fact_count=t.fact_count,
                facts=[
                    McpSurveyFact(
                        id=f.id,
                        text=f.text,
                        fact_type=f.fact_type,
                        score=f.score,
                    )
                    for f in t.facts
                ],
            )
            for t in result.topics
        ]

        return McpSurveyResult(
            query=result.query,
            sub_queries=result.sub_queries,
            topics=topics,
            total_notes=result.total_notes,
            total_facts=result.total_facts,
            truncated=result.truncated,
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Survey failed: {e}', exc_info=True)
        raise ToolError(f'Survey failed: {e}')


@mcp.tool(
    name='memex_get_vault_summary',
    description='Get the structured summary for a vault. Returns inventory (computed stats), '
    'themes (with trends), key entities, and a short narrative. Use this to orient yourself.',
    tags={'browse'},
    annotations={'readOnlyHint': True},
)
async def memex_get_vault_summary(
    ctx: Context,
    vault_id: str | None = None,
) -> dict:
    """Retrieve the current vault summary."""
    try:
        api = get_api(ctx)
        config = get_config(ctx)

        if vault_id is None:
            vid = await api.resolve_vault_identifier(config.server.default_active_vault)
        else:
            vid = await api.resolve_vault_identifier(vault_id)

        summary = await api.get_vault_summary(vid)
        if summary is None:
            return {'message': 'No summary exists for this vault. Use regenerate to create one.'}

        return {
            'id': str(summary.id),
            'vault_id': str(summary.vault_id),
            'narrative': summary.narrative,
            'themes': summary.themes,
            'inventory': summary.inventory,
            'key_entities': summary.key_entities,
            'version': summary.version,
            'notes_incorporated': summary.notes_incorporated,
            'created_at': summary.created_at.isoformat() if summary.created_at else None,
            'updated_at': summary.updated_at.isoformat() if summary.updated_at else None,
        }
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Get vault summary failed: {e}', exc_info=True)
        raise ToolError(f'Get vault summary failed: {e}')


def entrypoint():
    """Entrypoint for the MCP server.

    Configurable via environment variables:
        MCP_TRANSPORT: 'stdio' (default), 'http', or 'sse'
        MCP_HOST: Host for network transports (default '0.0.0.0')
        MCP_PORT: Port for network transports (default 8000)
    """
    transport = os.environ.get('MCP_TRANSPORT', 'stdio')
    host = os.environ.get('MCP_HOST', '0.0.0.0')
    port = int(os.environ.get('MCP_PORT', '8000'))
    if transport in ('http', 'sse'):
        asyncio.run(mcp.run_async(transport=transport, host=host, port=port))
    else:
        asyncio.run(mcp.run_async(transport='stdio'))


if __name__ == '__main__':
    entrypoint()
