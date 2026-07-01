"FastMCP Memex server implementation"

import os
import pathlib as plb
import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast
from uuid import UUID
from datetime import datetime
import mimetypes

import aiofiles
import httpx
import structlog
from fastmcp import FastMCP, Context
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.exceptions import ToolError

from memex_common.asset_cache import MAX_GET_RESOURCES_PATHS, MAX_RESOURCE_BYTES
from memex_common.asset_resize import validate_and_resize
from fastmcp.utilities.logging import configure_logging
import json

from pydantic import BeforeValidator, Field

from memex_mcp.lifespan import lifespan, get_api, get_asset_cache, get_config
from memex_mcp._layer_primer_descriptions import (
    LAYER_ROUTING_PRIMER_FRAGMENT as _LAYER_ROUTING_PRIMER,
)
from memex_common.agent_surface import MCP_TRANSPORT_INSTRUCTIONS
from memex_common.kv_utils import VALID_NAMESPACES
from memex_common.vault_utils import ALL_VAULTS_WILDCARD, expand_vault_scope
from memex_common.tool_descriptions import (
    MEMEX_CASE_SUBMIT_DESC as _MEMEX_CASE_SUBMIT_DESCRIPTION,
    MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC as _MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESCRIPTION,
    MEMEX_PROCEDURAL_GET_DESC as _MEMEX_PROCEDURAL_GET_DESCRIPTION,
    MEMEX_PROCEDURAL_SEARCH_DESC as _MEMEX_PROCEDURAL_SEARCH_DESCRIPTION,
    MEMEX_KV_PUT_DESC as _MEMEX_KV_PUT_DESCRIPTION,
)
from memex_mcp.models import (
    McpAddAssetsResult,
    McpAddNoteResult,
    McpAppendNoteResult,
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
    McpKVPutResult,
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
    McpCaseSubmitResult,
    McpProceduralEntry,
    McpProceduralPin,
    McpProceduralSearchHit,
    McpProceduralSearchResult,
    McpProceduralSource,
    Staleness,
)
from memex_common.procedural_schemas import (
    CaseSubmit,
    ProceduralEntryCreate,
    ProceduralSearchRequest,
)
from memex_common.templates import TemplateRegistry, BUILTIN_PROMPTS_DIR
from memex_common.schemas import (
    BatchJobStatus,
    IntentLiteral,
    LineageDirection,
    LineageResponse,
    NoteAppendRequest,
    NoteCreateDTO,
    PageIndexDTO,
    PageMetadataDTO,
    RiskLiteral,
    TOCNodeDTO,
    filter_toc,
)


_SESSION_DEDUP_TTL = 1800  # 30 minutes

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


def _coerce_float(v: Any) -> Any:
    """Coerce a stringified float back to a float."""
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
    return v


def _allowed_asset_roots() -> list[plb.Path]:
    """Directories that agent-supplied asset paths are confined to.

    Defaults to the server's CWD (the agent's working/project directory).
    Additional roots can be permitted via the ``MEMEX_MCP_ASSET_ROOTS`` env var
    (``os.pathsep``-separated absolute paths).
    """
    roots = [plb.Path.cwd().resolve()]
    for raw in os.environ.get('MEMEX_MCP_ASSET_ROOTS', '').split(os.pathsep):
        raw = raw.strip()
        if raw:
            roots.append(plb.Path(raw).expanduser().resolve())
    return roots


def _resolve_confined_asset_path(file_path: str) -> plb.Path:
    """Resolve an agent-supplied asset path, confined to the allowed roots.

    Guards against a prompt-injected agent exfiltrating sensitive local files
    (``~/.ssh/id_rsa``, ``/etc/passwd``, …) by ingesting them as note assets.
    Resolves symlinks before the check so they can't escape a permitted root.
    Raises ``ToolError`` if the path escapes every allowed root.
    """
    resolved = plb.Path(file_path).expanduser().resolve()
    roots = _allowed_asset_roots()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ToolError(
            f'Asset path {file_path!r} is outside the allowed roots '
            f'({", ".join(str(r) for r in roots)}). Set MEMEX_MCP_ASSET_ROOTS '
            'to permit additional directories.'
        )
    return resolved


def _to_utc_datetime(dt: datetime | None) -> datetime | None:
    """Convert a parsed datetime to UTC.

    Naive datetimes get UTC assigned. Aware datetimes are converted to UTC.
    Avoids ``.replace(tzinfo=)`` which silently overwrites existing timezones.
    """
    from datetime import timezone as _tz2

    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_tz2.utc)
    return dt.astimezone(_tz2.utc)


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


async def _resolve_vault_ids(
    api: Any, vault_ids: list[str], include_system_vaults: bool = False
) -> list[UUID]:
    """Resolve vault identifiers to UUIDs.

    Union semantics: a wildcard ``'*'`` expands to content vaults only; named
    vaults (content or system) resolve as given; ``include_system_vaults`` adds
    all system vaults. System vaults never enter implicitly.

    An empty/None ``vault_ids`` is treated the same as ``['*']`` — it expands
    to all content vaults. This matches :func:`VaultService.resolve_vault_scope`
    so a caller that forgets to forward a list still gets the content-only
    default universe instead of an empty scope.

    Pure scope-expansion logic lives in
    :func:`memex_common.vault_utils.expand_vault_scope` (shared SSOT with
    ``VaultService.resolve_vault_scope``). The MCP layer only does the
    parts that need the API surface — name resolution and the system-vs-
    content partition.
    """
    if not vault_ids:
        vault_ids = [ALL_VAULTS_WILDCARD]
    has_wildcard = ALL_VAULTS_WILDCARD in vault_ids
    named = [v for v in vault_ids if v != ALL_VAULTS_WILDCARD]

    named_ids: list[UUID] = []
    for vid in named:
        try:
            r = await api.resolve_vault_identifier(vid)
        except Exception:
            raise ToolError(f'Vault not found: {vid!r}')
        named_ids.append(UUID(str(r)) if not isinstance(r, UUID) else r)

    needs_partition = has_wildcard or include_system_vaults
    content_vault_ids: list[UUID] = []
    system_vault_ids: list[UUID] = []
    if needs_partition:
        for v in await api.list_vaults(include_system=True):
            (
                system_vault_ids if getattr(v, 'kind', 'content') == 'system' else content_vault_ids
            ).append(v.id)

    return expand_vault_scope(
        named_ids,
        content_vault_ids,
        system_vault_ids,
        has_wildcard=has_wildcard,
        include_system_vaults=include_system_vaults,
    )


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
    instructions=MCP_TRANSPORT_INSTRUCTIONS,
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
        "List a note's file attachments (images, audio, PDFs, docs). Call when "
        'has_assets is true to get asset paths, then pass them to '
        'memex_get_resources to fetch the bytes. Requires note_id.'
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
    description=(
        'Read a whole note in one shot. Use ONLY when total_tokens < 500. For '
        'larger notes do NOT set force — page through instead: '
        'memex_get_page_indices (TOC) then memex_get_nodes (sections). '
        'Errors if total_tokens >= 500 unless force=True. Requires note_id.'
    ),
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
        'Set a note lifecycle status: active, superseded, or archived. '
        'Use ONLY for explicit archival or an immediate forced state change; '
        'prefer ingesting a new note and letting contradiction detection '
        'auto-supersede facts. NOT for adding content (use memex_append_note, '
        'the only path that sets the appended_to relation). Cascades: '
        '`superseded` flags every extracted unit as stale; `archived` stamps '
        '`archived_at` and sets the units `is_deprioritized=true` (FSFM '
        'suppression) — they stay active, resurface via '
        '`include_deprioritized=True`, restore one-by-one with '
        'memex_memory_restore. Optionally pass linked_note_id to point at the '
        'replacing/parent note.'
    ),
    tags={'write'},
    annotations={'readOnlyHint': False, 'idempotentHint': True},
)
async def memex_set_note_status(
    ctx: Context,
    note_id: Annotated[str, Field(description='Note UUID.')],
    status: Annotated[
        Literal['active', 'superseded', 'archived'],
        Field(description='New status: active, superseded, or archived.'),
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
        'Replace the `user_notes` field on an existing note and re-extract it '
        'into the memory graph. Pass null to clear it. DESTRUCTIVE: old '
        'user_notes units are DELETED (not superseded) and new ones extracted '
        'from the new text — so history is lost. Use sparingly; for anything '
        'that must stay auditable, ingest a new note instead.'
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
    description=(
        'Rename a note (title only). Updates the title across metadata, page '
        'index, and doc_metadata; leaves body content and extracted units '
        'untouched. To change content use memex_append_note. Requires note_id, new_title.'
    ),
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
    if size > MAX_RESOURCE_BYTES:
        cache.invalidate(path)
        raise ToolError(f'Resource exceeds max size ({size} > {MAX_RESOURCE_BYTES} bytes)')
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
    if len(paths) > MAX_GET_RESOURCES_PATHS:
        raise ToolError(f'Too many paths requested ({len(paths)} > {MAX_GET_RESOURCES_PATHS})')
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
) -> dict[str, Any]:
    """Resize an image inside the session asset cache."""
    cache = get_asset_cache(ctx)
    try:
        dest_path, size = await asyncio.to_thread(
            validate_and_resize,
            cache,
            local_path,
            max_width=max_width,
            max_height=max_height,
            output_format=output_format,
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    return {'local_path': str(dest_path), 'size_bytes': size}


@mcp.tool(
    name='memex_add_assets',
    description=(
        'Attach 1+ file assets (images, audio, PDFs, docs) to an existing note. '
        'For text content use memex_append_note instead. Requires note_id and '
        'absolute local file_paths. Errors if the note is not found or no path '
        'resolves to a file.'
    ),
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
            path = _resolve_confined_asset_path(file_path)
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
    description=(
        'Detach 1+ asset files from a note. Get exact asset_paths from '
        'memex_list_assets first. Requires note_id. Returns deleted and '
        'not_found lists; errors if the note is not found.'
    ),
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
        'Add a new note or document to Memex — a FACT / DECISION / DOCUMENT '
        '("what is true"). NOT for how-to workflows, procedures, or worked '
        'episodes ("how we deploy", "how the run went") — those go to '
        'memex_case_submit, NEVER here (and never both). Ingest content into a '
        'vault. Confirm '
        'vault with user first, or pass vault_id. For structured captures (ADRs, '
        'retros, technical briefs, RFCs), call memex_list_templates first and pass '
        'the chosen slug as `template` for provenance and downstream filtering. '
        'For appending content to an existing note (one you previously created '
        'with this key), prefer memex_append_note to avoid resending the full body.'
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
    intent_class: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Optional intent override applied to all extracted facts: '
                '"permanent" (enduring user preferences/conventions), "durable" '
                '(default), or "ephemeral" (transient context — drains Memory Worth faster). '
                'Omit to let the write-time classifier decide.'
            ),
        ),
    ] = None,
    risk_class: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Optional risk override: "none" (default), "private" (PII/secrets), '
                '"sensitive" (restricted topic), "safety" (refuse persistence). '
                'Omit to let the write-time classifier decide.'
            ),
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
                path = _resolve_confined_asset_path(file_path)
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

        from memex_common.schemas import IntentClass as _IntentClass, RiskClass as _RiskClass

        parsed_intent: _IntentClass | None = None
        if intent_class:
            try:
                parsed_intent = _IntentClass(intent_class.lower())
            except ValueError:
                raise ToolError(
                    f'Invalid intent_class={intent_class!r}. '
                    f'Allowed: {[c.value for c in _IntentClass]}'
                )

        parsed_risk: _RiskClass | None = None
        if risk_class:
            try:
                parsed_risk = _RiskClass(risk_class.lower())
            except ValueError:
                raise ToolError(
                    f'Invalid risk_class={risk_class!r}. Allowed: {[c.value for c in _RiskClass]}'
                )

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
            intent_class=parsed_intent,
            risk_class=parsed_risk,
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


@mcp.tool(
    name='memex_append_note',
    description=(
        'Atomically append a delta to an existing note. Prefer over '
        'memex_add_note whenever extending a note you already created (session '
        'log, ongoing reflection) — send ONLY the new snippet; the server reads '
        'and concatenates the existing body for you. Identify by note_key + '
        'vault_id (preferred) or note_id from a search. Reusing an append_id '
        'with a different delta/parent returns a 409.'
    ),
    tags={'write'},
    annotations={'readOnlyHint': False},
    timeout=120.0,
)
async def memex_append_note(
    ctx: Context,
    delta: Annotated[
        str,
        Field(
            description=(
                'New content to append. Just the new snippet — do NOT include the '
                'existing body. Must not begin with `---` (would be ambiguous '
                'with frontmatter). Server enforces a 200,000 UTF-8 byte cap.'
            ),
            min_length=1,
        ),
    ],
    note_key: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Stable user-facing key the note was created with (preferred). '
                'Either note_key + vault_id or note_id is required.'
            ),
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description='Vault scope. Required when identifying by note_key.',
        ),
    ] = None,
    note_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Direct note UUID. Use only if you already have one from a search; '
                'note_key is the preferred identifier.'
            ),
        ),
    ] = None,
    append_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Caller-supplied UUID idempotency token. Reusing the same value with '
                'the same delta+parent is a safe replay; auto-generated if omitted.'
            ),
        ),
    ] = None,
    joiner: Annotated[
        str,
        Field(
            default='paragraph',
            description=(
                "Separator between parent body and delta. 'paragraph' (default), "
                "'newline', or 'none'."
            ),
        ),
    ] = 'paragraph',
    user_notes: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Optional commentary stored on the note metadata. NOT injected '
                'into the body itself.'
            ),
        ),
    ] = None,
) -> McpAppendNoteResult:
    """Atomically append delta content to an existing note.

    The server takes a per-parent advisory lock + row lock, reads the parent's
    body, concatenates ``parent + sep + delta``, and re-runs incremental
    extraction. Because the same note_id is reused, only the new content's
    chunks invoke the LLM.

    Idempotent retries (same ``append_id``) return ``status='replayed'`` with
    the cached outcome. Reusing an ``append_id`` with a different parent or a
    different delta returns a 409-equivalent error.
    """
    try:
        api = get_api(ctx)
        if not note_key and not note_id:
            raise ToolError('One of note_key (with vault_id) or note_id is required.')
        if note_key and not vault_id:
            raise ToolError('vault_id is required when identifying by note_key.')

        from uuid import uuid4 as _uuid4

        resolved_append_id = UUID(append_id) if append_id else _uuid4()
        resolved_note_id: UUID | None = UUID(note_id) if note_id else None

        request = NoteAppendRequest(
            note_id=resolved_note_id,
            note_key=note_key,
            vault_id=vault_id,
            delta=delta,
            append_id=resolved_append_id,
            joiner=joiner,
            user_notes=user_notes,
        )
        response = await api.append_to_note(
            note_id=request.note_id,
            note_key=request.note_key,
            vault_id=request.vault_id,
            delta=request.delta,
            append_id=request.append_id,
            joiner=request.joiner,
            user_notes=request.user_notes,
        )

        return McpAppendNoteResult(
            note_id=response.note_id,
            append_id=response.append_id,
            status=response.status,
            delta_bytes=response.delta_bytes,
            new_unit_count=len(response.new_unit_ids),
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Append note failed: {e}', exc_info=True)
        raise ToolError(f'Append note failed: {e}')


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
        1. ``mentioned_at`` — set on observations; for world facts the server's
           ``build_memory_unit_dto`` copies ``event_date`` into this field as a
           fallback (see ``memex_core.server.common``), so it is normally
           populated even for world facts.
        2. ``occurred_start`` — set on events with a specific occurrence time.

    ``event_date`` is surfaced on the DTO for output but is deliberately NOT the
    head of this chain: ``mentioned_at`` already carries the event_date fallback,
    and anchoring on the raw event_date would shift staleness for units whose
    ``mentioned_at`` differs from it.

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
        event_date = _to_utc_datetime(event_date)
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
        'success_co_count': getattr(res, 'success_co_count', 0),
        'failure_co_count': getattr(res, 'failure_co_count', 0),
        'is_deprioritized': getattr(res, 'is_deprioritized', False),
        'intent_class': getattr(res, 'intent_class', 'durable'),
        'risk_class': getattr(res, 'risk_class', 'none'),
        'exploration': bool(unit_metadata.get('exploration', False)),
        'created_at': getattr(res, 'created_at', None),
        'event_date': getattr(res, 'event_date', None),
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
    # mentioned_at: observations; also backfilled from event_date for world facts
    #   (see build_memory_unit_dto), so it already carries the event_date anchor.
    # occurred_start: events with a specific occurrence time.
    # NB: res.event_date is now surfaced on the DTO for output, but is deliberately
    # NOT the head of this chain — feeding it in would shift the staleness anchor
    # for units whose mentioned_at differs from event_date, changing behavior
    # product-wide. mentioned_at-first preserves the prior effective behavior.
    staleness_anchor = getattr(res, 'mentioned_at', None) or getattr(res, 'occurred_start', None)
    base_kwargs['staleness'] = compute_staleness(
        event_date=staleness_anchor,
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
        '\n\n## Memory layers\n\n' + _LAYER_ROUTING_PRIMER
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
    include_deprioritized: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            default=False,
            description=(
                'Include deprioritized memories in results. '
                'Default (false) returns only active, non-deprioritized memories. '
                'Set to true for "remember when..." queries or explicit recall of past discussions.'
            ),
        ),
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
    intent_class: Annotated[
        IntentLiteral | None,
        Field(
            default=None,
            description=(
                'Filter by intent class: permanent | durable | ephemeral. None disables the filter.'
            ),
        ),
    ] = None,
    risk_class: Annotated[
        RiskLiteral | None,
        Field(
            default=None,
            description=(
                'Filter by risk class: none | sensitive | private | safety. '
                'None disables the filter.'
            ),
        ),
    ] = None,
    apply_pre_filter: Annotated[
        bool,
        BeforeValidator(_coerce_bool),
        Field(
            default=True,
            description=(
                'Pre-reranker Memory Worth/FSFM filter at hydration. Default True drops '
                'obviously-failed (low Memory Worth) or decayed candidates before the '
                'cross-encoder runs. Set False for HISTORICAL / AUDIT / LINEAGE queries '
                '("how has my view on X evolved", "show me everything I used to think '
                'about Y") — every pre-filter branch is bypassed in one go so '
                'contradicted, behaviorally-failed, and decayed units appear. Post-'
                'reranker boosts still apply, so contradicted units rank below clean ones.'
            ),
        ),
    ] = True,
    include_system_vaults: Annotated[
        bool,
        Field(
            default=False,
            description='Include system vaults (e.g. inbox) when expanding the "*" wildcard.',
        ),
    ] = False,
) -> list[McpFact | McpEvent | McpObservation]:
    """Search Memex for relevant information."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(
            api, vault_ids, include_system_vaults=include_system_vaults
        )

        from datetime import datetime as _dt

        after_dt = _to_utc_datetime(_dt.fromisoformat(after)) if after else None
        before_dt = _to_utc_datetime(_dt.fromisoformat(before)) if before else None
        ref_dt = _to_utc_datetime(_dt.fromisoformat(reference_date)) if reference_date else None

        # Defense-in-depth: FastMCP+Pydantic rejects invalid Literal values
        # upstream (the IntentLiteral / RiskLiteral annotations on the
        # parameters above), so this check is unreachable for real MCP
        # callers. We keep it as a safety net for direct-call paths that
        # bypass schema validation — tests that pass raw strings, internal
        # Python callers invoking the underlying ``.fn``, etc. — and to
        # surface a clean ToolError (not a 422) at the boundary. Mirrors
        # the CLI/Hermes-plugin pattern; canonical sets in memex_common.schemas.
        from memex_common.schemas import (
            VALID_INTENT_CLASSES,
            VALID_RISK_CLASSES,
            IntentClass,
            RiskClass,
        )

        # Widen to ``str`` so mypy doesn't flag the membership check as unreachable.
        # The Literal annotations on ``intent_class`` / ``risk_class`` constrain
        # values for FastMCP+Pydantic callers, which would make mypy treat the
        # ``not in`` branch as dead code; the cast preserves the defense-in-depth
        # check for direct-call paths (tests, internal Python via ``.fn``) that
        # bypass schema validation.
        if intent_class is not None and cast(str, intent_class) not in VALID_INTENT_CLASSES:
            raise ToolError(
                f'Invalid intent_class={intent_class!r}. Allowed: {sorted(VALID_INTENT_CLASSES)}'
            )
        if risk_class is not None and cast(str, risk_class) not in VALID_RISK_CLASSES:
            raise ToolError(
                f'Invalid risk_class={risk_class!r}. Allowed: {sorted(VALID_RISK_CLASSES)}'
            )

        results = await api.search(
            query=query,
            limit=limit,
            vault_ids=resolved_vids,
            token_budget=token_budget,
            strategies=strategies,
            include_superseded=include_superseded,
            include_deprioritized=include_deprioritized,
            apply_pre_filter=apply_pre_filter,
            after=after_dt,
            before=before_dt,
            tags=tags,
            source_context=source_context,
            reference_date=ref_dt,
            expand_query=expand_query,
            intent_class=IntentClass(intent_class) if intent_class is not None else None,
            risk_class=RiskClass(risk_class) if risk_class is not None else None,
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

        # Surface a degradation warning if a search timeout forced retrieval to drop
        # the graph/keyword signals — the agent must know the set is INCOMPLETE.
        # (``is True`` guards against truthy MagicMock attrs in unit tests.)
        if any(getattr(res, 'degraded', False) is True for res in results):
            _dropped = sorted(
                {s for res in results for s in getattr(res, 'dropped_strategies', None) or []}
            )
            output.append(
                McpFact(
                    id=UUID(int=0),
                    text=(
                        f'⚠️ Partial results — a statement timeout forced retrieval to drop the '
                        f'{", ".join(_dropped) or "graph/keyword"} signal(s); these results are '
                        'INCOMPLETE. Consider re-running the search or narrowing the query.'
                    ),
                    confidence=0.0,
                    tags=['system-hint', 'degraded'],
                )
            )

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
        '\n\n## Memory layers\n\n' + _LAYER_ROUTING_PRIMER
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
    include_system_vaults: Annotated[
        bool,
        Field(
            default=False,
            description='Include system vaults (e.g. inbox) when expanding the "*" wildcard.',
        ),
    ] = False,
) -> list[McpNoteSearchResult]:
    """Search Memex for source notes by hybrid retrieval."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(
            api, vault_ids, include_system_vaults=include_system_vaults
        )

        from datetime import datetime as _dt

        after_dt = _to_utc_datetime(_dt.fromisoformat(after)) if after else None
        before_dt = _to_utc_datetime(_dt.fromisoformat(before)) if before else None
        ref_dt = _to_utc_datetime(_dt.fromisoformat(reference_date)) if reference_date else None

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

        # Surface a degradation warning if a search timeout forced retrieval to drop
        # the graph/keyword signals — the agent must know the set is INCOMPLETE.
        if any(getattr(d, 'degraded', False) is True for d in results):
            _dropped = sorted(
                {s for d in results for s in getattr(d, 'dropped_strategies', None) or []}
            )
            output.append(
                McpNoteSearchResult(
                    note_id=UUID(int=0),
                    title='⚠️ Partial results — search degraded',
                    score=0.0,
                    description=(
                        f'A statement timeout forced retrieval to drop the '
                        f'{", ".join(_dropped) or "graph/keyword"} signal(s); these results are '
                        'INCOMPLETE. Consider re-running the search or narrowing the query.'
                    ),
                    tags=['system-hint', 'degraded'],
                )
            )

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
                        created_at=metadata.get('created_at'),
                        publish_date=metadata.get('publish_date'),
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
        'Pass leaf node IDs (nodes without children) to memex_get_nodes to read content. '
        'Each node carries assets[] — embedded image refs (path, alt_text, filename) parsed at ingest.'
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
                    created_at=meta.get('created_at'),
                    publish_date=meta.get('publish_date'),
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
        'Accepts 1 or more IDs — use for single and batch reads. '
        'Each node carries block_id (pass to memex_get_memory_units) and assets[] (alt_text + path per section).'
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
                    block_id=node.block_id,
                    assets=node.assets,
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
    description=(
        'List content vaults with note counts (is_active, note_count, kind). '
        'Pass include_system_vaults=true to also list system vaults (inbox etc.).'
    ),
    tags={'browse'},
    annotations={'readOnlyHint': True},
)
async def memex_list_vaults(ctx: Context, include_system_vaults: bool = False) -> list[McpVault]:
    """List vaults with active status and note counts (content vaults by default)."""
    try:
        api = get_api(ctx)
        config = get_config(ctx)

        # Use list_vaults_with_counts for local API, fall back for remote
        try:
            rows = await api.list_vaults_with_counts(include_system=include_system_vaults)
            active_vault_id = await api.resolve_vault_identifier(config.server.default_active_vault)
            return [
                McpVault(
                    id=row['vault'].id,
                    name=row['vault'].name,
                    description=row['vault'].description,
                    kind=getattr(row['vault'], 'kind', 'content'),
                    is_active=(row['vault'].id == active_vault_id),
                    note_count=row['note_count'],
                    last_note_added_at=row.get('last_note_added_at'),
                )
                for row in rows
            ]
        except AttributeError:
            # Remote API — fall back to list_vaults (VaultDTO with is_active)
            vaults = await api.list_vaults(include_system=include_system_vaults)
            return [
                McpVault(
                    id=v.id,
                    name=v.name,
                    description=v.description,
                    kind=getattr(v, 'kind', 'content'),
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
        'List notes in one vault with optional date/tag/status/template filters. '
        'Use for "documents from 2026" (after/before), topic filtering (tags, AND '
        'semantics), or lifecycle (status: active, archived). For a cross-vault '
        'newest-first feed use memex_recent_notes.'
    ),
    tags={'browse'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_list_notes(
    ctx: Context,
    vault_id: Annotated[
        str | None,
        Field(
            description=(
                'Vault UUID or name to scope to, or "*" for ALL content vaults. '
                'Omit to use config defaults.'
            )
        ),
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
    slim: Annotated[
        bool,
        Field(
            description='Drop per-note summaries to keep the response under hook-output caps.',
        ),
    ] = False,
) -> list[McpNote]:
    """List notes with optional date, tag, and status filters."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_read_vaults(ctx)[0]
        # "*" scopes across ALL content vaults (plural path); a named/UUID vault
        # resolves to a single scope. Keeps browse flows honest about cross-vault
        # listing instead of silently pinning the first default read vault.
        resolved_vault_id: UUID | None = None
        resolved_vault_ids: list[UUID] | None = None
        if vault_id == ALL_VAULTS_WILDCARD:
            resolved_vault_ids = await _resolve_vault_ids(api, [ALL_VAULTS_WILDCARD])
        else:
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
            vault_ids=resolved_vault_ids,
            after=parsed_after,
            before=parsed_before,
            template=template,
            tags=tags,
            status=status,
            date_field=date_by,
            slim=slim,
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
    description=(
        'Browse the most recently added notes (newest first), all vaults by '
        'default. Use for "what did I capture lately". For tag/status/template '
        'filtering within one vault use memex_list_notes. Optional vault_ids and '
        'after/before date range.'
    ),
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
    slim: Annotated[
        bool,
        Field(
            description='Drop per-note summaries to keep the response under hook-output caps.',
        ),
    ] = False,
    include_system_vaults: Annotated[
        bool,
        Field(
            default=False,
            description='Include system vaults (e.g. inbox) when expanding the "*" wildcard.',
        ),
    ] = False,
) -> list[McpNote]:
    """List recent notes."""
    from datetime import datetime as _dt

    try:
        api = get_api(ctx)
        resolved_vids = None
        if vault_ids:
            _validate_vault_ids(vault_ids)
            resolved_vids = await _resolve_vault_ids(
                api, vault_ids, include_system_vaults=include_system_vaults
            )

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
            slim=slim,
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
    slim: Annotated[
        bool,
        Field(
            description='Drop entity description to keep the response under hook-output caps.',
        ),
    ] = False,
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
                    limit=limit,
                    vault_ids=resolved_vids,
                    entity_type=entity_type,
                    slim=slim,
                )
            ]

        return [
            McpEntity(
                id=e.id,
                name=e.name,
                type=e.entity_type,
                mention_count=e.mention_count,
                description=None
                if slim
                else (getattr(e, 'metadata', None) or {}).get('description'),
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
    description=(
        'Get facts, observations, and events that mention an entity. '
        'Each mention links to its source note, revealing cross-note connections. '
        'Defaults to active, non-superseded, non-deprioritized memory units; '
        'set the include_* flags to widen to the historical record.'
        '\n\n## Memory layers\n\n' + _LAYER_ROUTING_PRIMER
    ),
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
    include_stale: Annotated[
        bool,
        Field(description='Include archived/deleted units (default: active only).'),
    ] = False,
    include_superseded: Annotated[
        bool,
        Field(
            description='Include units whose confidence has decayed below the '
            'superseded threshold (default: exclude).'
        ),
    ] = False,
    include_deprioritized: Annotated[
        bool,
        Field(description='Include units the user/agent has deprioritized (default: exclude).'),
    ] = False,
) -> list[McpEntityMention]:
    """Get memory units mentioning an entity."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid Entity UUID: {entity_id}')

        mentions = await api.get_entity_mentions(
            uuid_obj,
            limit=limit,
            include_stale=include_stale,
            include_superseded=include_superseded,
            include_deprioritized=include_deprioritized,
        )

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
    description=(
        'Find entities that frequently appear alongside a given entity — the fastest '
        'way to map relationships and discover connected concepts. Returns entity '
        'names, types, and co-occurrence counts inline (no follow-up calls needed). '
        'Use this for "what relates to X?" questions. '
        'Counts are corpus frequency across the entire historical record (including '
        'superseded / deprioritized / archived units) — a high count says "these have '
        'been mentioned together a lot", NOT "the current best understanding links '
        'them". For currency, follow up with memex_get_entity_mentions (which '
        'defaults to active, non-superseded units).'
    ),
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
    description=(
        'Batch lookup of memory units. Provide exactly one of '
        '`unit_ids` (direct ID lookup) or `chunk_ids` (returns all units '
        'extracted from the named chunks, vault-scoped). Includes '
        'contradiction links and supersession info.'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_memory_units(
    ctx: Context,
    unit_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(default=None, description='List of memory unit UUIDs.'),
    ] = None,
    chunk_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            default=None,
            description=(
                'List of chunk UUIDs. Returns all memory units extracted from '
                'these chunks, scoped to `vault_id`. Mutually exclusive with `unit_ids`.'
            ),
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                'Vault UUID or name. Required when `chunk_ids` is set; ignored '
                'for the `unit_ids` path. Defaults to the active read vault.'
            ),
        ),
    ] = None,
) -> list[McpFact | McpEvent | McpObservation]:
    """Retrieve multiple memory units with their contradiction context."""
    if (unit_ids is None) == (chunk_ids is None):
        raise ToolError('Provide exactly one of `unit_ids` or `chunk_ids` (not both, not neither).')

    try:
        api = get_api(ctx)
        output: list[McpFact | McpEvent | McpObservation] = []

        if unit_ids is not None:
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

        chunk_uuids: list[UUID] = []
        for cid_str in chunk_ids or []:
            try:
                chunk_uuids.append(UUID(cid_str))
            except ValueError:
                continue

        if not chunk_uuids:
            return []

        # Chunk traversal is a read operation — default to the active read
        # vault (matches the convention used by other MCP read tools, e.g.
        # memex_list_notes). Use the first read vault when multiple are
        # configured; the agent can pass `vault_id` explicitly to override.
        vault_str = vault_id or _default_read_vaults(ctx)[0]
        resolved_vault = await _resolve_vault_id(api, vault_str)

        units = await api.get_memory_units_by_chunks(chunk_uuids, resolved_vault)
        for unit in units:
            output.append(_build_memory_unit_model(unit))

        return output

    except ToolError:
        raise
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
    name='memex_get_unit_history',
    description=(
        'Walk the contradiction graph backward (newer -> older) from a memory '
        'unit, returning its supersession history as a tree. Use for '
        '"how has my view on X evolved" / audit / lineage queries. v1 '
        'returns supersession history (negative-evidence path: contradicts / '
        'weakens links), NOT full confidence evolution. A future forward=True '
        'extension can walk reinforces separately. No reranker, no boosts, '
        'no quality filtering — graph walk is for completeness, not relevance.'
    ),
    tags={'storage'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_unit_history(
    ctx: Context,
    unit_id: Annotated[
        str,
        Field(description='Memory unit UUID to start the walk from (root, depth=0).'),
    ],
    vault_id: Annotated[
        str,
        Field(
            description=(
                'Vault UUID or name the unit belongs to. REQUIRED for per-vault '
                'auth scoping (vault-scoping invariant). Cross-vault '
                'links are filtered out.'
            ),
        ),
    ],
    max_depth: Annotated[
        int,
        BeforeValidator(_coerce_int),
        Field(
            description=(
                'Maximum recursion depth for the contradiction walk. Nodes '
                'reached at the cap are returned with truncated=True.'
            ),
        ),
    ] = 10,
) -> dict[str, Any]:
    """Walk the contradiction graph backward from ``unit_id`` (newer -> older).

    v1 returns supersession history (negative-evidence path:
    contradicts/weakens links), NOT full confidence evolution. A future
    ``forward=True`` extension can walk ``reinforces`` separately.

    Returns a JSON-serialised ``UnitHistoryNodeDTO`` tree rooted at
    ``unit_id`` (depth=0). Predecessors are nested under each node and
    sorted oldest-first by ``event_date``. ``link_type`` on each
    non-root node names the supersession edge from that node to its
    parent (the newer authoritative unit).
    """
    try:
        api = get_api(ctx)

        try:
            unit_uuid = UUID(unit_id)
        except (ValueError, TypeError):
            raise ToolError(f'Invalid unit_id: {unit_id!r}')

        resolved_vault = await _resolve_vault_id(api, vault_id)

        if max_depth < 0:
            raise ToolError('max_depth must be >= 0.')

        history = await api.get_unit_history(
            unit_uuid,
            max_depth=max_depth,
            vault_id=resolved_vault,
        )

        return cast(dict[str, Any], history.model_dump(mode='json'))

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Get unit history failed: {e}', exc_info=True)
        raise ToolError(f'Get unit history failed: {e}')


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
            description='Vault UUIDs or names to search in, e.g. [\'rituals\']. "*" or None = all content vaults.',
        ),
    ] = None,
    limit: Annotated[int, BeforeValidator(_coerce_int), Field(description='Max results.')] = 5,
    include_system_vaults: Annotated[
        bool,
        Field(
            default=False,
            description='Include system vaults (e.g. inbox) when expanding the "*" wildcard.',
        ),
    ] = False,
) -> list[McpFindResult]:
    """Find notes by approximate title match."""
    try:
        api = get_api(ctx)

        resolved_vids: list[UUID] | None = None
        if vault_ids:
            _validate_vault_ids(vault_ids)
            resolved_vids = await _resolve_vault_ids(
                api, vault_ids, include_system_vaults=include_system_vaults
            )

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
    name='memex_kv_put',
    description=_MEMEX_KV_PUT_DESCRIPTION,
    tags={'storage'},
    annotations={'readOnlyHint': False, 'idempotentHint': True},
    timeout=15.0,
)
async def memex_kv_put(
    ctx: Context,
    value: Annotated[
        str,
        Field(description="The pointer's value (preference, binding, or convention)."),
    ],
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
) -> McpKVPutResult:
    """Write an operational pointer to the KV store with embedding generation."""
    try:
        # Mirror the server-side namespace gate (services/kv.py) client-side so
        # the LLM gets a fast, clear error instead of a round-trip 4xx.
        if not key.startswith(tuple(f'{ns}:' for ns in VALID_NAMESPACES)):
            raise ToolError(
                f'Invalid key {key!r}: must start with one of '
                f'{", ".join(f"{ns}:" for ns in VALID_NAMESPACES)}'
            )

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
        return McpKVPutResult(
            key=entry.key, value=entry.value, scope=scope, expires_at=entry.expires_at
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'KV put failed: {e}', exc_info=True)
        raise ToolError(f'KV put failed: {e}')


@mcp.tool(
    name='memex_kv_get',
    description=(
        'Fetch one KV entry by its exact full key (e.g. "global:preferences:editor"). '
        'Returns null if absent. When you do not know the exact key, use '
        'memex_kv_search (fuzzy) or memex_kv_list (browse by namespace).'
    ),
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
        'Fuzzy search KV entries by semantic similarity. '
        'Returns the closest matching entries. '
        'Optionally filter by namespace prefixes (global, user, project).'
        '\n\n## Memory layers\n\n' + _LAYER_ROUTING_PRIMER
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
        results = await api.kv_search_text(query=query, namespaces=namespaces, limit=limit)

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
        'Browse KV entries (preferences, project bindings, conventions) by '
        'namespace. Filter via namespaces (global, user, project) or a trailing-'
        'wildcard pattern. For exact-key fetch use memex_kv_get; for fuzzy lookup '
        'use memex_kv_search.'
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
        '\n\n## Memory layers\n\n' + _LAYER_ROUTING_PRIMER
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
    include_system_vaults: Annotated[
        bool,
        Field(
            default=False,
            description='Include system vaults (e.g. inbox) when expanding the "*" wildcard.',
        ),
    ] = False,
) -> McpSurveyResult:
    """Survey a broad topic — decompose, parallel search, grouped results."""
    try:
        api = get_api(ctx)
        vault_ids = vault_ids or _default_read_vaults(ctx)
        _validate_vault_ids(vault_ids)
        resolved_vids = await _resolve_vault_ids(
            api, vault_ids, include_system_vaults=include_system_vaults
        )

        from datetime import datetime as _dt

        after_dt = _to_utc_datetime(_dt.fromisoformat(after)) if after else None
        before_dt = _to_utc_datetime(_dt.fromisoformat(before)) if before else None
        ref_dt = _to_utc_datetime(_dt.fromisoformat(reference_date)) if reference_date else None

        result = await api.survey(
            query=query,
            vault_ids=resolved_vids,
            limit_per_query=limit_per_query,
            token_budget=token_budget,
            after=after_dt,
            before=before_dt,
            reference_date=ref_dt,
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


from memex_mcp._resolution_flow_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION as _DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION as _RECORD_OUTCOME_DESCRIPTION,
)


@mcp.tool(
    name='memex_record_outcome',
    description=_RECORD_OUTCOME_DESCRIPTION,
    tags={'write'},
    annotations={'readOnlyHint': False},
)
async def memex_record_outcome(
    ctx: Context,
    success: Annotated[
        bool | None,
        BeforeValidator(_coerce_bool),
        Field(
            default=None,
            description=(
                'Legacy shape (FutureWarning). True if the task succeeded, false '
                'if it failed. Prefer the `units` parameter with per-unit verbs.'
            ),
        ),
    ] = None,
    units: Annotated[
        list[dict] | None,
        BeforeValidator(_coerce_list),
        Field(
            default=None,
            description=(
                'Per-unit verb classifications. Each entry: '
                '{unit_id: UUID, verb: "helpful"|"not_helpful"|"not_used", '
                'reason: str}. `reason` is required for helpful and not_helpful. '
                'Examples — good: [{"unit_id": "...", "verb": "helpful", '
                '"reason": "named the right module"}, {"unit_id": "...", '
                '"verb": "not_used", "reason": null}]. Bad: classifying everything '
                'helpful or omitting reason for credit-bearing verbs.'
            ),
        ),
    ] = None,
    unit_ids: Annotated[
        list[str] | None,
        BeforeValidator(_coerce_list),
        Field(
            default=None,
            description=(
                'Legacy shape (FutureWarning). UUIDs of memory units '
                'you actually used. Prefer the `units` parameter.'
            ),
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID or name. Omit to use config defaults.'),
    ] = None,
    outcome_confidence: Annotated[
        float,
        BeforeValidator(_coerce_float),
        Field(
            default=1.0,
            ge=0.0,
            le=1.0,
            description='Weight for this outcome signal (0.0-1.0). Default 1.0.',
        ),
    ] = 1.0,
    reason: Annotated[
        str | None,
        Field(
            default=None,
            description='Optional free-text reason for the outcome (logged, not stored on units).',
        ),
    ] = None,
    retrieved_set_size: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                'Size of the retrieved set the caller was asked to classify. '
                'Drives coverage_ratio on the audit log. Pass explicitly to '
                'enable coverage tracking; omitting leaves coverage_ratio NULL.'
            ),
        ),
    ] = None,
) -> dict:
    """Record an outcome for memory units to train Memory Worth scoring."""
    try:
        api = get_api(ctx)
        vault_id = vault_id or _default_write_vault(ctx)
        resolved_vid = await _resolve_vault_id(api, vault_id)

        return await api.record_outcome(
            unit_ids=unit_ids,
            success=success,
            vault_id=str(resolved_vid),
            outcome_confidence=outcome_confidence,
            reason=reason,
            units=units,
            retrieved_set_size=retrieved_set_size,
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Record outcome failed: {e}', exc_info=True)
        raise ToolError(f'Record outcome failed: {e}')


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


# ============================================================
# Tier A — Tool registry
# ============================================================

# --- Deprioritize / Restore ---

from memex_mcp._deprioritize_descriptions import (
    MEMEX_MEMORY_RESTORE_DESCRIPTION,
)


@mcp.tool(
    name='memex_memory_deprioritize',
    description=_DEPRIORITIZE_DESCRIPTION,
    tags={'write', 'storage'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True},
    timeout=30.0,
)
async def memex_memory_deprioritize(
    ctx: Context,
    unit_id: Annotated[str, Field(description='Memory unit UUID.')],
    reason: Annotated[
        str,
        Field(description='Why this unit is being deprioritized. Free text; logged to audit_logs.'),
    ],
    vault_id: Annotated[
        str | None,
        Field(
            description=(
                'Vault UUID or name the unit belongs to. Defaults to the active '
                'write vault. Required for vault-scoping; cross-vault calls '
                'are rejected.'
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Deprioritize a memory unit (non-destructive)."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(unit_id)
        except ValueError:
            raise ToolError(f'Invalid memory unit UUID: {unit_id}')
        resolved_vault = await _resolve_vault_id(
            api, vault_id if vault_id is not None else _default_write_vault(ctx)
        )
        try:
            unit = await api.deprioritize_memory_unit(
                uuid_obj, reason=reason, vault_id=resolved_vault
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError(f'Memory unit {unit_id} not found.')
            if exc.response.status_code == 403:
                raise ToolError(f'Access denied to vault for memory unit {unit_id}.')
            if exc.response.status_code == 400:
                # The server redirects observation-id targets to their source
                # MUs via a structured 400 (ObservationReadOnlyError →
                # {'detail': {'error': ..., 'source_memory_units': [...]}}).
                # httpx's raise_for_status flattens the body to a bare message
                # string, so the redirect must be re-surfaced HERE or the agent
                # loses the retry target the tool contract promises. Other 400s
                # (ambiguous/validation) carry a plain-string detail and fall
                # through to the bare raise unchanged.
                try:
                    detail = exc.response.json().get('detail')
                except Exception:
                    detail = None
                if isinstance(detail, dict) and detail.get('source_memory_units'):
                    sources = ', '.join(str(u) for u in detail['source_memory_units'])
                    raise ToolError(
                        f'{unit_id} is a read-only observation (a projection of '
                        f'mental-model evidence), not a deprioritizable memory unit. '
                        f'Deprioritize its source memory unit(s) instead: {sources}.'
                    )
            raise
        return {'unit_id': str(unit.id), 'is_deprioritized': True, 'reason': reason}
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Deprioritize failed: {e}', exc_info=True)
        raise ToolError(f'Deprioritize failed: {e}')


@mcp.tool(
    name='memex_memory_restore',
    description=MEMEX_MEMORY_RESTORE_DESCRIPTION,
    tags={'write', 'storage'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True},
    timeout=30.0,
)
async def memex_memory_restore(
    ctx: Context,
    unit_id: Annotated[str, Field(description='Memory unit UUID.')],
    vault_id: Annotated[
        str | None,
        Field(
            description=(
                'Vault UUID or name the unit belongs to. Defaults to the active '
                'write vault. Required for vault-scoping; cross-vault calls '
                'are rejected.'
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Restore a deprioritized memory unit."""
    try:
        api = get_api(ctx)
        try:
            uuid_obj = UUID(unit_id)
        except ValueError:
            raise ToolError(f'Invalid memory unit UUID: {unit_id}')
        resolved_vault = await _resolve_vault_id(
            api, vault_id if vault_id is not None else _default_write_vault(ctx)
        )
        try:
            unit = await api.restore_memory_unit(uuid_obj, vault_id=resolved_vault)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError(f'Memory unit {unit_id} not found.')
            if exc.response.status_code == 403:
                raise ToolError(f'Access denied to vault for memory unit {unit_id}.')
            raise
        return {'unit_id': str(unit.id), 'is_deprioritized': False}
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Restore failed: {e}', exc_info=True)
        raise ToolError(f'Restore failed: {e}')


# --- Summarize ---

from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


@mcp.tool(
    name='memex_memory_summarize_node',
    description=MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION,
    tags={'write', 'storage'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False},
    timeout=120.0,
)
async def memex_memory_summarize_node(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID to reflect on.')],
    scope: Annotated[
        str,
        Field(
            description=(
                "'incremental' (default — only new evidence) or 'full' "
                '(re-evaluate all evidence; capped at 100 units).'
            ),
        ),
    ] = 'incremental',
    vault_id: Annotated[
        str | None,
        Field(description='Vault UUID; defaults to the global vault when None.'),
    ] = None,
) -> dict[str, Any]:
    """Synchronously consolidate memories on an entity into its mental model."""
    from memex_common.client import RateLimitExceeded, ReflectionAbandoned

    try:
        api = get_api(ctx)
        try:
            entity_uuid = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid entity UUID: {entity_id}')
        vault_uuid: UUID | None
        if vault_id is None:
            vault_uuid = None
        else:
            try:
                vault_uuid = UUID(vault_id)
            except ValueError:
                raise ToolError(f'Invalid vault UUID: {vault_id}')
        if scope not in ('incremental', 'full'):
            raise ToolError(f"scope must be 'incremental' or 'full', got {scope!r}")
        try:
            result = await api.summarize_node(entity_uuid, scope=scope, vault_id=vault_uuid)
        except RateLimitExceeded as exc:
            return {
                'error': 'rate_limit_exceeded',
                'entity_id': entity_id,
                'retry_after_seconds': exc.retry_after_seconds,
                'message': str(exc),
            }
        except ReflectionAbandoned as exc:
            envelope: dict[str, Any] = {
                'error': 'reflection_abandoned',
                'entity_id': entity_id,
                'retry_after_seconds': exc.retry_after_seconds,
                'message': str(exc),
            }
            if exc.hint:
                envelope['hint'] = exc.hint
            return envelope
        return {
            'entity_id': str(result.entity_id),
            'observation_count': len(result.new_observations),
            'status': result.status,
            'scope': scope,
        }
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_memory_summarize_node failed: {e}', exc_info=True)
        raise ToolError(f'memex_memory_summarize_node failed: {e}')


# --- Lint ---

from memex_mcp._lint_flags_descriptions import MEMEX_GET_LINT_FLAGS_DESCRIPTION


@mcp.tool(
    name='memex_get_lint_flags',
    description=MEMEX_GET_LINT_FLAGS_DESCRIPTION,
    tags={'diagnostics'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_lint_flags(
    ctx: Context,
    vault_id: Annotated[
        str | None,
        Field(
            description=(
                'Vault UUID or name to scope the query. When omitted, falls '
                'through to the active write vault from session config (per '
                'vault-scoping invariant — never falls through to a '
                'global all-vault view).'
            ),
        ),
    ] = None,
    lint_type: Annotated[
        str | None,
        Field(description='structural | quality | governance | schema | routing'),
    ] = None,
    status: Annotated[
        str,
        Field(description='pending | resolved | dismissed (default: pending)'),
    ] = 'pending',
    limit: Annotated[int, Field(ge=1, le=200, description='Page size (default 20, max 200).')] = 20,
    cursor: Annotated[
        str | None,
        Field(description='Opaque cursor from a prior page; omit on first call.'),
    ] = None,
) -> dict[str, Any]:
    """Read-only surface: list pending memory-hygiene findings.

    Previously a missing ``vault_id`` would fall through to a
    global all-vault view, leaking findings across tenants. The tool now
    binds to the session's active write vault when no ``vault_id`` is
    provided. Cross-tenant probing requires an explicit ``vault_id`` that
    the principal's auth context allows.
    """
    try:
        api = get_api(ctx)
        # Never fall through to all-vault — always scope to a concrete vault.
        # Default to the session's active write vault when vault_id is omitted.
        effective_vault = vault_id if vault_id is not None else _default_write_vault(ctx)
        resolved_vault = str(await _resolve_vault_id(api, effective_vault))
        try:
            return await api.lint_get_flags(
                vault_id=resolved_vault,
                lint_type=lint_type,
                status=status,
                limit=limit,
                cursor=cursor,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                # translate the server's structured envelope.
                detail = exc.response.json().get('detail', {})
                if (
                    isinstance(detail, dict)
                    and detail.get('error') == 'lint_subsystem_not_initialized'
                ):
                    return detail
            raise
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_get_lint_flags failed: {e}', exc_info=True)
        raise ToolError(f'memex_get_lint_flags failed: {e}')


from memex_mcp._lint_resolution_descriptions import (
    MEMEX_LINT_APPLY_WINNER_DESCRIPTION,
    MEMEX_LINT_REVERSE_WINNER_DESCRIPTION,
)


@mcp.tool(
    name='memex_lint_apply_winner',
    description=MEMEX_LINT_APPLY_WINNER_DESCRIPTION,
    tags={'write', 'storage', 'diagnostics'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True},
    timeout=30.0,
)
async def memex_lint_apply_winner(
    ctx: Context,
    finding_id: Annotated[
        str,
        Field(description='UUID of the pending winner-proposal finding to apply.'),
    ],
) -> dict[str, Any]:
    """Write surface: apply the recommended action on a winner-proposal finding."""
    try:
        api = get_api(ctx)
        return await api.lint_apply_winner(finding_id)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_lint_apply_winner failed: {e}', exc_info=True)
        raise ToolError(f'memex_lint_apply_winner failed: {e}')


@mcp.tool(
    name='memex_lint_reverse_winner',
    description=MEMEX_LINT_REVERSE_WINNER_DESCRIPTION,
    tags={'write', 'storage', 'diagnostics'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True},
    timeout=30.0,
)
async def memex_lint_reverse_winner(
    ctx: Context,
    finding_id: Annotated[
        str,
        Field(description='UUID of the previously applied winner-proposal finding to reverse.'),
    ],
) -> dict[str, Any]:
    """Write surface: reverse a previously applied winner-proposal."""
    try:
        api = get_api(ctx)
        return await api.lint_reverse_winner(finding_id)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_lint_reverse_winner failed: {e}', exc_info=True)
        raise ToolError(f'memex_lint_reverse_winner failed: {e}')


# --- External lint proposals (closed action catalogue) ---

from memex_common.tool_descriptions import (
    MEMEX_LIST_LINT_ACTIONS_DESC,
    MEMEX_SUBMIT_LINT_PROPOSAL_DESC,
)


@mcp.tool(
    name='memex_list_lint_actions',
    description=MEMEX_LIST_LINT_ACTIONS_DESC,
    tags={'diagnostics'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_list_lint_actions(ctx: Context) -> dict[str, Any]:
    """Read-only catalogue dump; the registry only grows with core releases."""
    try:
        api = get_api(ctx)
        return await api.list_lint_actions()
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_list_lint_actions failed: {e}', exc_info=True)
        raise ToolError(f'memex_list_lint_actions failed: {e}')


@mcp.tool(
    name='memex_submit_lint_proposal',
    description=MEMEX_SUBMIT_LINT_PROPOSAL_DESC,
    tags={'write', 'diagnostics'},
    annotations={'readOnlyHint': False, 'destructiveHint': False},
    timeout=30.0,
)
async def memex_submit_lint_proposal(
    ctx: Context,
    rule_name: Annotated[
        str,
        Field(
            description=(
                'Caller-owned lowercase slug; internal rule names and the llm_ prefix are reserved.'
            ),
        ),
    ],
    lint_type: Annotated[
        str,
        Field(description='structural | quality | governance | schema | routing'),
    ],
    target_type: Annotated[
        str,
        Field(description="Construct kind: 'note' | 'memory_unit' | 'entity' | 'kv' | ..."),
    ],
    target_id: Annotated[
        str,
        Field(description='UUID of the targeted construct (KV key for kv targets).'),
    ],
    description: Annotated[
        str,
        Field(description='Why the rule fired — shown to the reviewer (max 500 chars).'),
    ],
    suggested_action: Annotated[
        str,
        Field(description='Free-text remediation summary (max 500 chars).'),
    ],
    vault_id: Annotated[
        str | None,
        Field(
            description=(
                'Vault UUID or name. Defaults to the active write vault when '
                'omitted (vault-scoping invariant).'
            ),
        ),
    ] = None,
    evidence: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                'Supporting payload; keys resolution / rule_metadata / '
                'proposed_action are server-owned and rejected.'
            ),
        ),
    ] = None,
    proposed_action: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                '{action_name, params} from memex_list_lint_actions; must '
                'apply to target_type and pass its params schema.'
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """File one pending finding for human review; mutates nothing else.

    The per-item submission contract (created / deduplicated /
    cooldown_suppressed / rejected) is surfaced verbatim from the server
    so retry-happy callers can branch on it.
    """
    try:
        api = get_api(ctx)
        effective_vault = vault_id if vault_id is not None else _default_write_vault(ctx)
        resolved_vault = str(await _resolve_vault_id(api, effective_vault))
        proposal: dict[str, Any] = {
            'vault_id': resolved_vault,
            'rule_name': rule_name,
            'lint_type': lint_type,
            'target_type': target_type,
            'target_id': target_id,
            'description': description,
            'suggested_action': suggested_action,
        }
        if evidence is not None:
            proposal['evidence'] = evidence
        if proposed_action is not None:
            proposal['proposed_action'] = proposed_action
        result = await api.submit_lint_proposals([proposal])
        # Non-2xx responses already raise httpx.HTTPStatusError upstream
        # (client._handle_response -> raise_for_status) and are surfaced as a
        # ToolError by the outer handler. A 200-with-error-body envelope, by
        # contrast, would slip through silently: the prior code returned the
        # raw dict whenever `results` was absent/empty, so an error payload
        # like {'error': 'rate_limited'} reached the caller masquerading as a
        # success. Detect the error envelope here and fail loudly instead.
        # Mirrors the structured-envelope handling in memex_get_lint_flags
        # (detail.get('error')).
        if not isinstance(result, dict):
            raise ToolError(
                f'memex_submit_lint_proposal: unexpected response (expected an '
                f'object with a results list, got {type(result).__name__})'
            )
        items = result.get('results')
        if not isinstance(items, list) or not items:
            # No usable result row: either an error envelope (e.g.
            # {'error': 'rate_limited'}) or a 200 with an empty/missing
            # results list. We submitted exactly one proposal, so a missing
            # row is never a normal success — surface the server's detail.
            error_detail = result.get('error') or result.get('detail') or result
            raise ToolError(f'memex_submit_lint_proposal failed: {error_detail}')
        return items[0]
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_submit_lint_proposal failed: {e}', exc_info=True)
        raise ToolError(f'memex_submit_lint_proposal failed: {e}')


# --- Consolidation ---

from memex_mcp._reconsolidate_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
)


@mcp.tool(
    name='memex_memory_reconsolidate',
    description=MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
    tags={'write', 'storage'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True},
    timeout=120.0,
)
async def memex_memory_reconsolidate(
    ctx: Context,
    entity_id: Annotated[str, Field(description='Entity UUID to reconsolidate.')],
    vault_id: Annotated[
        str, Field(description='Vault UUID — required for vault-scoped resolution.')
    ],
) -> dict[str, Any]:
    """Re-evaluate memories on an entity under a per-entity advisory lock."""
    try:
        api = get_api(ctx)
        try:
            entity_uuid = UUID(entity_id)
        except ValueError:
            raise ToolError(f'Invalid entity UUID: {entity_id}')
        try:
            vault_uuid = UUID(vault_id)
        except ValueError:
            raise ToolError(f'Invalid vault UUID: {vault_id}')
        try:
            return await api.reconsolidate_entity(entity_uuid, vault_uuid)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return {
                    'error': 'lock_contention',
                    'entity_id': entity_id,
                    'message': 'another reconsolidation is in progress; retry in a moment',
                }
            raise
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_memory_reconsolidate failed: {e}', exc_info=True)
        raise ToolError(f'memex_memory_reconsolidate failed: {e}')


@mcp.tool(
    name='memex_memory_consolidate',
    description=MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    tags={'write', 'storage'},
    annotations={'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False},
    timeout=300.0,
)
async def memex_memory_consolidate(
    ctx: Context,
    vault_id: Annotated[str, Field(description='Vault UUID to consolidate.')],
    dry_run: Annotated[
        bool,
        Field(description='If true, return preview without making changes.'),
    ] = False,
) -> dict[str, Any]:
    """Vault-wide low-Memory-Worth unit consolidation.

    Rate-limited per vault (default 1 call per vault per
    hour). On 429 the tool returns a structured envelope with
    ``retry_after_seconds`` rather than raising — mirrors the
    summarize-node contract so agents can back off without retry loops.
    """
    from memex_common.client import RateLimitExceeded

    try:
        api = get_api(ctx)
        try:
            vault_uuid = UUID(vault_id)
        except ValueError:
            raise ToolError(f'Invalid vault UUID: {vault_id}')
        try:
            return await api.consolidate_vault(vault_uuid, dry_run=dry_run)
        except RateLimitExceeded as exc:
            return {
                'error': 'rate_limit_exceeded',
                'vault_id': vault_id,
                'retry_after_seconds': exc.retry_after_seconds,
                'message': str(exc),
            }
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'memex_memory_consolidate failed: {e}', exc_info=True)
        raise ToolError(f'memex_memory_consolidate failed: {e}')


# --- Diagnostics ---
@mcp.tool(
    name='memex_get_diagnostics_summary',
    description=(
        'Vault diagnostics summary: unit counts by status (active/stale/deprioritized), '
        'lint pending counts by type, cluster_count (null on cold cache), avg Memory Worth score, '
        'and top-5 retrieved entities. Synchronous (no UMAP block) — surfaces '
        'manifold status without waiting on compute.'
    ),
    tags={'diagnostics'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_get_diagnostics_summary(
    ctx: Context,
    vault_id: Annotated[
        str,
        Field(description='Vault UUID or name.'),
    ],
) -> dict[str, Any]:
    """Return the diagnostics summary for a vault."""
    try:
        api = get_api(ctx)
        resolved_vault_id = await _resolve_vault_id(api, vault_id)
        return await api.get_diagnostics_summary(resolved_vault_id)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Diagnostics summary failed: {e}', exc_info=True)
        raise ToolError(f'Diagnostics summary failed: {e}')


# --- Procedural plane  ---


def _dto_to_mcp_entry(dto: ProceduralEntryCreate) -> McpProceduralEntry:
    """Convert a MemexAPI ProceduralEntryDTO to the MCP-facing shape.

    The DTO is the cross-package envelope; the MCP model is the
    tool-boundary shape. They share the same column set EXCEPT ``vault_id``,
    which is deliberately dropped at this boundary (the backing system vault
    must not leak to agents — see McpProceduralEntry). The McpProceduralEntry
    model uses ``extra='forbid'`` so any new DTO field added without a
    corresponding MCP field surfaces here rather than silently shipping to
    clients.
    """
    return McpProceduralEntry(
        id=dto.id,
        # vault_id intentionally NOT copied — the backing `procedural` system
        # vault is storage plumbing the agent must not see (see McpProceduralEntry).
        kind=dto.kind,
        scope=dto.scope,
        verb=dto.verb,
        context=dto.context,
        title=dto.title,
        summary=dto.summary,
        body=dto.body,
        trigger=dto.trigger,
        tags=list(dto.tags),
        extra_metadata=dict(dto.extra_metadata),
        status=dto.status,
        origin=dto.origin,
        supersedes_id=dto.supersedes_id,
        superseded_by_id=dto.superseded_by_id,
        published_at=dto.published_at,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
        sources=[
            McpProceduralSource(
                note_id=s.note_id,
                memory_unit_id=s.memory_unit_id,
                role=str(s.role),
                excerpt=s.excerpt,
            )
            for s in dto.sources
        ],
        pins=[McpProceduralPin(context_key=p.context_key, position=p.position) for p in dto.pins],
    )


# NOTE: There is deliberately NO agent-facing procedural WRITE tool
# (create/update/upsert/deprecate). Procedures and strategies are
# DERIVED from cases (design §5/§8/§9) — the agent's only procedural
# write is `memex_case_submit` (file the worked episode); the derivation
# pipeline + §18.6 governance produce and update entries. Direct
# authoring/editing stays on the operator surfaces (CLI `memex
# procedural create/update`, the curation TUI) and the HTTP/client CRUD
# the derivation worker uses. Agents only READ the plane (search / get /
# get_by_identity) and SUBMIT cases.


@mcp.tool(
    name='memex_procedural_get',
    description=_MEMEX_PROCEDURAL_GET_DESCRIPTION,
    tags={'storage', 'procedural'},
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_procedural_get(
    ctx: Context,
    entry_id: Annotated[str, Field(description='Procedural entry UUID.')],
    vault_id: Annotated[
        str | None,
        Field(
            description="Optional vault UUID or name. Mismatch with the entry's "
            'vault → 404. Omit to skip vault-scope enforcement.'
        ),
    ] = None,
) -> McpProceduralEntry | None:
    """Fetch a single entry by UUID. Returns null if not found."""
    try:
        api = get_api(ctx)
        try:
            entry_uuid = UUID(entry_id)
        except ValueError:
            raise ToolError(f'Invalid entry UUID: {entry_id}')
        resolved_vault: UUID | None = None
        if vault_id is not None:
            resolved_vault = await _resolve_vault_id(api, vault_id)
        dto = await api.procedural_get(entry_uuid, vault_id=resolved_vault)
        return _dto_to_mcp_entry(dto)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Procedural get failed: {e}', exc_info=True)
        raise ToolError(f'Procedural get failed: {e}')


@mcp.tool(
    name='memex_procedural_get_by_identity',
    description=_MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESCRIPTION,
    tags={'storage', 'procedural'},
    annotations={'readOnlyHint': True},
    timeout=15.0,
)
async def memex_procedural_get_by_identity(
    ctx: Context,
    kind: Annotated[
        Literal['procedure', 'strategy'],
        Field(description='Entity kind. Cases are notes — not on this plane.'),
    ],
    scope: Annotated[
        str,
        Field(description='Scope label: "global" | "project:<id>" | "app:<id>" (no user scope).'),
    ],
    verb: Annotated[
        str | None,
        Field(description='Anchor verb — required for both kinds.'),
    ] = None,
    context: Annotated[
        str | None,
        Field(
            description='Anchor context — required for procedures; MUST be null for '
            'strategies (a strategy groups all procedures sharing scope+verb).'
        ),
    ] = None,
    vault_id: Annotated[
        str | None,
        Field(description='Optional vault UUID or name for vault-scope enforcement.'),
    ] = None,
) -> McpProceduralEntry | None:
    """Fetch a single entry by its (kind, scope, verb, context) identity anchor."""
    try:
        api = get_api(ctx)
        if not verb:
            raise ToolError(f'kind="{kind}" requires a non-empty verb.')
        if kind == 'procedure' and not context:
            raise ToolError('kind="procedure" requires a non-empty context.')
        if kind == 'strategy' and context is not None:
            raise ToolError(
                'kind="strategy" requires context=null — strategies anchor on scope+verb.'
            )

        resolved_vault: UUID | None = None
        if vault_id is not None:
            resolved_vault = await _resolve_vault_id(api, vault_id)
        # Direct identity-anchor SELECT against the partial unique index
        # ``uq_procedural_identity`` (not a fuzzy search). A previous
        # implementation routed through ``api.procedural.search`` with
        # an empty query, which short-circuited to an empty response and
        # silently returned ``None`` for every anchor — the same
        # regression the HTTP route had before its fix. The facade
        # exposes a dedicated ``get_by_identity`` method (see
        # ``MemexAPIProceduralFacade.get_by_identity``) so the LLM hot
        # path is a single partial-index lookup, not a search +
        # post-filter round-trip.
        dto = await api.procedural_get_by_identity(
            kind=kind,
            scope=scope,
            verb=verb,
            context=context,
            vault_id=resolved_vault,
        )
        if dto is None:
            return None
        return _dto_to_mcp_entry(dto)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Procedural get_by_identity failed: {e}', exc_info=True)
        raise ToolError(f'Procedural get_by_identity failed: {e}')


@mcp.tool(
    name='memex_procedural_search',
    description=_MEMEX_PROCEDURAL_SEARCH_DESCRIPTION,
    tags={'storage', 'procedural'},
    annotations={'readOnlyHint': True},
    timeout=30.0,
)
async def memex_procedural_search(
    ctx: Context,
    request: ProceduralSearchRequest,
) -> McpProceduralSearchResult:
    """Hybrid BM25 + vector search with RRF aggregation."""
    try:
        api = get_api(ctx)
        response = await api.procedural_search(request)
        return McpProceduralSearchResult(
            hits=[
                McpProceduralSearchHit(
                    # The ProceduralSearchHit DTO nests the entry
                    # under .entry (so the agent can see both the
                    # RRF score and the full DTO). The MCP tool
                    # boundary wants a flat shape, so project
                    # entry.* into the hit fields. Reading these
                    # off the hit itself raised AttributeError on
                    # every search call before the fix.
                    entry_id=h.entry.id,
                    kind=h.entry.kind,
                    score=h.score,
                    matched_via=h.matched_via,
                    title=h.entry.title,
                    summary=h.entry.summary,
                    scope=h.entry.scope,
                    verb=h.entry.verb,
                    context=h.entry.context,
                    trigger=h.entry.trigger,
                    pin_position=h.pin_position,
                )
                for h in response.hits
            ],
            total=response.total,
            truncated=response.truncated,
            took_ms=response.took_ms,
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Procedural search failed: {e}', exc_info=True)
        raise ToolError(f'Procedural search failed: {e}')


@mcp.tool(
    name='memex_case_submit',
    description=_MEMEX_CASE_SUBMIT_DESCRIPTION,
    tags={'storage', 'procedural'},
    annotations={'readOnlyHint': False, 'idempotentHint': False},
    timeout=120.0,
)
async def memex_case_submit(
    ctx: Context,
    payload: CaseSubmit,
    background: Annotated[
        bool,
        Field(
            description='Queue the file+assign flow as a durable background job and '
            'return immediately (assignment_mode="queued" + job_id) instead of '
            'blocking on the assignment judge; assignment then resolves async and any '
            'escalation surfaces in the lint queue. Pass false to wait inline and get '
            'the assignment outcome in the response. The Claude Code plugin defaults '
            'this to true so capture never blocks the agent.',
        ),
    ] = False,
) -> McpCaseSubmitResult:
    """File a worked episode as a case note + run assignment.

    The note lands in the hidden `procedural` system vault with
    role='case'; the caller never names the vault. Assignment runs
    synchronously (explicit case_of / judge auto-assign / lint
    escalation) — see the result's assignment block — UNLESS
    ``background=true``, which queues the whole flow as a tracked job and
    returns ``assignment_mode='queued'`` + ``job_id`` without blocking.
    """
    try:
        api = get_api(ctx)
        result = await api.case_submit(payload, background=background)
        # background=true → the route returns 202 + a BatchJobStatus; the case
        # is filed off the request path and assignment resolves async.
        if isinstance(result, BatchJobStatus):
            return McpCaseSubmitResult(assignment_mode='queued', job_id=result.job_id)
        return McpCaseSubmitResult(
            note_id=result.note_id,
            assignment_mode=result.assignment.mode,
            entry_id=result.assignment.entry_id,
            finding_id=result.assignment.finding_id,
            separation=result.assignment.separation,
            reasoning=result.assignment.reasoning,
            scope=result.assignment.scope,
            scope_reasoning=result.assignment.scope_reasoning,
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f'Case submit failed: {e}', exc_info=True)
        raise ToolError(f'Case submit failed: {e}')
