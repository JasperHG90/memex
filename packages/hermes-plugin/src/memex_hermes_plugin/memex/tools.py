"""Memex tool schemas and handlers for Hermes.

Seven tools are exposed (in ``hybrid`` and ``tools`` memory modes):

- ``memex_recall`` — memory-unit search (TEMPR)
- ``memex_retrieve_notes`` — whole-note search
- ``memex_survey`` — broad query decomposition
- ``memex_retain`` — explicit ingest (supports session-note append)
- ``memex_list_entities`` — entity-graph search
- ``memex_get_entity_mentions`` — source facts for an entity
- ``memex_get_entity_cooccurrences`` — related entities

Tool descriptions describe *what the tool does*, not *when to combine it with
others*. Routing guidance (parallel dispatch for content lookup, sequential
for graph exploration, survey for broad queries) lives in the plugin's
``system_prompt_block`` so it's injected once per session rather than inflating
every tool description — mirroring how Memex's MCP server and Claude Code
plugin handle routing.

Handlers are synchronous wrappers that bridge to the async ``RemoteMemexAPI``
via ``async_bridge.run_sync``. All return JSON strings.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any
from uuid import UUID

from tools.registry import tool_error  # type: ignore[import-not-found]

from .async_bridge import run_sync
from .config import HermesMemexConfig
from .templates import HERMES_USER_NOTE_TEMPLATE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vault resolution helpers (Stream 1)
# ---------------------------------------------------------------------------


class VaultResolutionError(Exception):
    """Raised when a named vault cannot be resolved.

    Carries the failing name so the dispatcher can surface it in ``tool_error``.
    Only raised by ``_resolve_vault_ids``; never by handlers directly.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


_VAULT_IDS_DESCRIPTION = (
    'Vault names or UUIDs to search. Omit to use the session-bound vault '
    '(see "Active vault" in the Memex Memory system block). '
    'Use ["*"] for all vaults.'
)


def _vault_ids_schema() -> dict[str, Any]:
    """Return the canonical ``vault_ids`` property for schema merging."""
    return {
        'type': 'array',
        'items': {'type': 'string'},
        'description': _VAULT_IDS_DESCRIPTION,
    }


def _resolve_vault_ids(
    api: Any, args: dict[str, Any], bound_vault_id: UUID | None
) -> list[UUID] | None:
    """Resolve user-supplied ``vault_ids`` to a concrete list of UUIDs.

    Rules (in order):
    1. If ``args`` has no ``vault_ids`` key OR the value is falsy (empty list/None):
       return ``[bound_vault_id]`` if bound_vault_id else ``None``.
    2. If ``args['vault_ids']`` contains ``"*"``: return every vault from
       ``api.list_vaults()`` (executed via ``run_sync``).
    3. Otherwise each element is parsed as UUID locally; on parse failure,
       fall back to ``api.resolve_vault_identifier(name)`` (via ``run_sync``).

    Raises ``VaultResolutionError`` (module-local sentinel) when
    ``api.resolve_vault_identifier`` fails. The dispatcher catches it and
    returns a ``tool_error`` JSON string referencing the failing name.
    """
    import httpx

    from memex_common.vault_utils import ALL_VAULTS_WILDCARD

    supplied = args.get('vault_ids')
    if not supplied:
        return [bound_vault_id] if bound_vault_id else None

    if ALL_VAULTS_WILDCARD in supplied:
        vaults = run_sync(api.list_vaults(), timeout=30.0)
        return [v.id for v in vaults or []]

    resolved: list[UUID] = []
    for raw in supplied:
        try:
            resolved.append(UUID(str(raw)))
            continue
        except (ValueError, TypeError):
            pass
        try:
            r = run_sync(api.resolve_vault_identifier(str(raw)), timeout=30.0)
        except httpx.HTTPStatusError as exc:
            raise VaultResolutionError(str(raw)) from exc
        except Exception as exc:
            raise VaultResolutionError(str(raw)) from exc
        resolved.append(r if isinstance(r, UUID) else UUID(str(r)))
    return resolved


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

# --- Vault-scoped (Stream 1) ---

RECALL_SCHEMA: dict[str, Any] = {
    'name': 'memex_recall',
    'description': (
        'Search memory units — individual facts, observations, and events '
        'extracted from stored notes. Uses TEMPR: temporal + entity + '
        'mental-model + keyword + semantic strategies fused via Reciprocal '
        'Rank Fusion. Returns distilled claims, not raw text.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Natural-language query. Preserve proper nouns, dates, and qualifiers.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default: 10, max: 50).',
            },
            'vault_ids': _vault_ids_schema(),
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'Filter by note metadata tags (e.g. "meeting", "bug"). '
                    'NOT for vault selection — use vault_ids for that.'
                ),
            },
            'after': {
                'type': 'string',
                'description': 'ISO 8601 date. Only return memory units dated after this.',
            },
            'before': {
                'type': 'string',
                'description': 'ISO 8601 date. Only return memory units dated before this.',
            },
            'include_stale': {
                'type': 'boolean',
                'description': 'Include stale/lower-confidence memory units (default: false).',
            },
        },
        'required': ['query'],
    },
}

RETRIEVE_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_retrieve_notes',
    'description': (
        'Search whole notes ranked by relevance. Returns note metadata plus '
        'section summaries (topic + key points). Returns source documents, '
        'not distilled facts.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Natural-language query.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default: 10).',
            },
            'vault_ids': _vault_ids_schema(),
            'expand_query': {
                'type': 'boolean',
                'description': 'Use LLM to generate query variations. Higher recall, higher cost (default: false).',
            },
        },
        'required': ['query'],
    },
}

SURVEY_SCHEMA: dict[str, Any] = {
    'name': 'memex_survey',
    'description': (
        'Broad / panoramic knowledge query. Memex decomposes your question into '
        'sub-questions, runs parallel retrievals, and returns facts grouped by '
        'source note. Use for "what do you know about X?" queries, project '
        'overviews, or when you need a landscape view rather than specific facts.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Broad natural-language question.',
            },
            'limit_per_query': {
                'type': 'integer',
                'description': 'Max results per decomposed sub-question (default: 10).',
            },
            'vault_ids': _vault_ids_schema(),
        },
        'required': ['query'],
    },
}

RETAIN_SCHEMA: dict[str, Any] = {
    'name': 'memex_retain',
    'description': (
        'Ingest a note into Memex. If note_key is provided and matches an existing '
        'note, the content is upserted (enables incremental session capture). Pass '
        'the session note key from the system prompt to append meaningful progress '
        'to the running session note.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': 'Short title for the note.',
            },
            'description': {
                'type': 'string',
                'description': "One-sentence summary of the note's contents.",
            },
            'content': {
                'type': 'string',
                'description': 'Full markdown body of the note.',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Topic/category tags (optional).',
            },
            'note_key': {
                'type': 'string',
                'description': (
                    'Stable key for upsert. Use the session note key to append to the '
                    'running session note. Omit for a fresh note.'
                ),
            },
        },
        'required': ['name', 'description', 'content'],
    },
}

LIST_ENTITIES_SCHEMA: dict[str, Any] = {
    'name': 'memex_list_entities',
    'description': (
        'Search entities in the knowledge graph by name or type. Returns '
        'entity IDs, canonical names, and mention counts. Entity IDs from '
        'this tool feed into memex_get_entity_mentions and '
        'memex_get_entity_cooccurrences.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Entity name or substring (e.g. "Rust", "Alice", "Q3 launch").',
            },
            'entity_type': {
                'type': 'string',
                'description': 'Filter by type (person/org/topic/event/etc). Optional.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default: 20).',
            },
            'vault_ids': _vault_ids_schema(),
        },
        'required': ['query'],
    },
}

GET_ENTITY_MENTIONS_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_entity_mentions',
    'description': (
        'Return memory units (facts/observations/events) that mention a '
        'specific entity, plus the source note for each. Requires an '
        'entity_id from memex_list_entities.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_id': {
                'type': 'string',
                'description': 'Entity UUID from a prior memex_list_entities call.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max mentions to return (default: 20).',
            },
            'vault_ids': _vault_ids_schema(),
        },
        'required': ['entity_id'],
    },
}

GET_ENTITY_COOCCURRENCES_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_entity_cooccurrences',
    'description': (
        'Return entities that co-occur with a given entity, with '
        'co-occurrence counts. Surfaces related concepts, people, or '
        'projects. Requires an entity_id from memex_list_entities.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_id': {
                'type': 'string',
                'description': 'Entity UUID from a prior memex_list_entities call.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max co-occurring entities (default: 20).',
            },
            'vault_ids': _vault_ids_schema(),
        },
        'required': ['entity_id'],
    },
}

# --- Read/discovery (Stream 2) ---
# <streams append here>

# --- Entities/memory/lineage (Stream 3) ---
# <streams append here>

# --- Lifecycle/templates (Stream 4) ---
# <streams append here>

# --- Assets (Stream 5) ---

LIST_ASSETS_SCHEMA: dict[str, Any] = {
    'name': 'memex_list_assets',
    'description': (
        'List file attachments (assets) for a note — images, audio, PDFs, '
        'documents. REQUIRED when has_assets is true in a search result. '
        'Feed the returned paths to memex_get_resources to retrieve bytes.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_id': {
                'type': 'string',
                'description': 'Note UUID.',
            },
        },
        'required': ['note_id'],
    },
}

GET_RESOURCES_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_resources',
    'description': (
        'Retrieve one or more file attachments by path. Returns base64-encoded '
        'bytes (diverges from MCP which returns native Image/Audio/File). '
        'Per-path failure isolation: failures produce {path, error} entries '
        'interleaved with successful {path, filename, mime_type, content_b64, '
        'size_bytes} entries. Get paths from memex_list_assets.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'paths': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Resource paths to fetch.',
            },
        },
        'required': ['paths'],
    },
}

ADD_ASSETS_SCHEMA: dict[str, Any] = {
    'name': 'memex_add_assets',
    'description': (
        'Attach one or more files to an existing note. Hermes accepts '
        'base64-encoded content inline (diverges from MCP which takes local '
        'file_paths) — this is required because the Hermes server may not '
        'share a filesystem with the caller.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_id': {
                'type': 'string',
                'description': 'Note UUID.',
            },
            'assets': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'filename': {'type': 'string'},
                        'content_b64': {
                            'type': 'string',
                            'description': 'Base64-encoded file bytes.',
                        },
                    },
                    'required': ['filename', 'content_b64'],
                },
                'description': 'List of {filename, content_b64} objects.',
            },
        },
        'required': ['note_id', 'assets'],
    },
}

# --- KV store (Stream 5) ---

KV_WRITE_SCHEMA: dict[str, Any] = {
    'name': 'memex_kv_write',
    'description': (
        'Write a fact or preference to the KV store with semantic embedding '
        'for later fuzzy search. Key must start with global:, user:, '
        'project:, or app:. Examples: "global:lang:python:version", '
        '"user:work:employer", "project:github.com/user/repo:vault", '
        '"app:claude-code:theme".'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'value': {
                'type': 'string',
                'description': 'The fact or preference text to store.',
            },
            'key': {
                'type': 'string',
                'description': (
                    'Namespaced key. Must start with global:, user:, project:, or app:.'
                ),
            },
            'ttl_seconds': {
                'type': 'integer',
                'description': ('Optional time-to-live in seconds. Omit for no expiration.'),
            },
        },
        'required': ['value', 'key'],
    },
}

KV_GET_SCHEMA: dict[str, Any] = {
    'name': 'memex_kv_get',
    'description': 'Get a KV entry by exact key. Returns null if not found.',
    'parameters': {
        'type': 'object',
        'properties': {
            'key': {
                'type': 'string',
                'description': 'Exact key to look up.',
            },
        },
        'required': ['key'],
    },
}

KV_SEARCH_SCHEMA: dict[str, Any] = {
    'name': 'memex_kv_search',
    'description': (
        'Semantic search over KV entries. Returns the closest matching '
        'entries. Optionally filter by namespace prefixes.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Search query text.',
            },
            'namespaces': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Namespace prefixes to filter (e.g. ["global", "user"]).',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default: 5).',
            },
        },
        'required': ['query'],
    },
}

KV_LIST_SCHEMA: dict[str, Any] = {
    'name': 'memex_kv_list',
    'description': ('List KV entries, optionally filtered by namespace prefixes.'),
    'parameters': {
        'type': 'object',
        'properties': {
            'namespaces': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Namespace prefixes to filter (e.g. ["global", "user"]).',
            },
        },
        'required': [],
    },
}


ALL_SCHEMAS: list[dict[str, Any]] = [
    # --- Vault-scoped (Stream 1) ---
    RECALL_SCHEMA,
    RETRIEVE_NOTES_SCHEMA,
    SURVEY_SCHEMA,
    RETAIN_SCHEMA,
    LIST_ENTITIES_SCHEMA,
    GET_ENTITY_MENTIONS_SCHEMA,
    GET_ENTITY_COOCCURRENCES_SCHEMA,
    # --- Read/discovery (Stream 2) ---
    # <Stream 2 appends>
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    LIST_ASSETS_SCHEMA,
    GET_RESOURCES_SCHEMA,
    ADD_ASSETS_SCHEMA,
    # --- KV store (Stream 5) ---
    KV_WRITE_SCHEMA,
    KV_GET_SCHEMA,
    KV_SEARCH_SCHEMA,
    KV_LIST_SCHEMA,
]


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str | None) -> Any:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _require(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value in (None, ''):
        raise ValueError(f'Missing required parameter: {name}')
    return value


def _serialize_memory_unit(unit: Any) -> dict[str, Any]:
    """Trim a MemoryUnitDTO to the fields useful to the model."""
    return {
        'id': str(getattr(unit, 'id', '')),
        'text': getattr(unit, 'text', ''),
        'type': getattr(unit, 'fact_type', None),
        'status': getattr(unit, 'status', None),
        'note_id': str(u) if (u := getattr(unit, 'note_id', None)) else None,
        'mentioned_at': (m.isoformat() if (m := getattr(unit, 'mentioned_at', None)) else None),
    }


def _serialize_note_result(result: Any) -> dict[str, Any]:
    """Flatten a ``NoteSearchResult`` into a dict for the model.

    ``metadata`` is populated server-side with ``name``, ``title``,
    ``description``, ``tags``, ``publish_date``, ``source_uri``, ``has_assets``,
    ``vault_id`` (see ``document_search.py``). ``summaries`` is
    ``list[BlockSummaryDTO]`` — each with ``topic: str`` and
    ``key_points: list[str]``.
    """
    metadata = getattr(result, 'metadata', None) or {}
    summaries = getattr(result, 'summaries', None) or []
    return {
        'note_id': str(getattr(result, 'note_id', '')),
        'name': metadata.get('name') or metadata.get('title'),
        'description': metadata.get('description'),
        'tags': metadata.get('tags') or [],
        'score': getattr(result, 'score', 0.0),
        'note_status': getattr(result, 'note_status', None),
        'vault_name': getattr(result, 'vault_name', None),
        'answer': getattr(result, 'answer', None),
        'summaries': [
            {
                'topic': getattr(s, 'topic', None),
                'key_points': list(getattr(s, 'key_points', []) or []),
            }
            for s in summaries
        ],
    }


def _serialize_entity(entity: Any) -> dict[str, Any]:
    return {
        'id': str(getattr(entity, 'id', '')),
        'name': getattr(entity, 'name', ''),
        'mention_count': getattr(entity, 'mention_count', 0),
    }


# NOTE: _scope_from_key MUST stay byte-for-byte in sync with `_scope_from_key`
# at `packages/mcp/src/memex_mcp/models.py:356`. If you change one, change the
# other. Both derive the KV namespace scope from a key's prefix. Copied here
# (rather than imported) to avoid cross-package runtime coupling between the
# Hermes plugin and the MCP server package. A drift-detector test
# (test_scope_from_key_matches_mcp_source_of_truth) imports both functions and
# asserts byte-equal output across the canonical namespace shapes. Shared
# source would live in memex_common if this becomes a maintenance burden.
def _scope_from_key(key: str) -> str:
    """Derive scope from the namespace prefix of a key.

    Examples:
        'global:foo' -> 'global'
        'user:work:employer' -> 'user'
        'project:github.com/user/repo:vault' -> 'project:github.com/user/repo'
    """
    if key.startswith('project:'):
        # project:<project-id>:<setting> -> scope is project:<project-id>
        rest = key[len('project:') :]
        colon_idx = rest.rfind(':')
        if colon_idx > 0:
            return f'project:{rest[:colon_idx]}'
        return 'project'
    return key.split(':', 1)[0] if ':' in key else 'unknown'


def _serialize_kv_entry(entry: Any) -> dict[str, Any]:
    """Serialize a KVEntryDTO to the MCP-compatible shape with derived scope."""
    key = getattr(entry, 'key', '')
    return {
        'key': key,
        'value': getattr(entry, 'value', ''),
        'scope': _scope_from_key(key),
        'updated_at': (u.isoformat() if (u := getattr(entry, 'updated_at', None)) else None),
        'expires_at': (e.isoformat() if (e := getattr(entry, 'expires_at', None)) else None),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# --- Vault-scoped (Stream 1) ---


def handle_recall(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 10), 50)
    tags = args.get('tags') or None
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        results = run_sync(
            api.search(
                query=query,
                limit=limit,
                vault_ids=vault_ids,
                token_budget=config.recall.token_budget,
                strategies=config.recall.strategies,
                include_stale=bool(args.get('include_stale', config.recall.include_stale)),
                include_superseded=config.recall.include_superseded,
                after=_parse_iso(args.get('after')),
                before=_parse_iso(args.get('before')),
                tags=tags,
            ),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_recall failed: %s', e)
        return tool_error(f'Recall failed: {e}')

    items = [_serialize_memory_unit(u) for u in (results or [])]
    return json.dumps({'count': len(items), 'results': items})


def handle_retrieve_notes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 10), 50)
    expand_query = bool(args.get('expand_query', config.recall.expand_query))
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        results = run_sync(
            api.search_notes(
                query=query,
                limit=limit,
                vault_ids=vault_ids,
                expand_query=expand_query,
                strategies=config.recall.strategies,
            ),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_retrieve_notes failed: %s', e)
        return tool_error(f'Note search failed: {e}')

    items = [_serialize_note_result(r) for r in (results or [])]
    return json.dumps({'count': len(items), 'results': items})


def handle_survey(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit_per_query = min(int(args.get('limit_per_query') or 10), 25)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        response = run_sync(
            api.survey(
                query=query,
                vault_ids=vault_ids,
                limit_per_query=limit_per_query,
                token_budget=config.recall.token_budget,
            ),
            timeout=120.0,
        )
    except Exception as e:
        logger.warning('memex_survey failed: %s', e)
        return tool_error(f'Survey failed: {e}')

    topics = []
    for t in getattr(response, 'topics', []) or []:
        topics.append(
            {
                'note_id': str(getattr(t, 'note_id', '')),
                'title': getattr(t, 'title', None),
                'fact_count': getattr(t, 'fact_count', 0),
                'facts': [
                    {
                        'id': str(getattr(f, 'id', '')),
                        'text': getattr(f, 'text', ''),
                        'fact_type': getattr(f, 'fact_type', None),
                        'score': getattr(f, 'score', None),
                    }
                    for f in getattr(t, 'facts', []) or []
                ],
            }
        )
    return json.dumps(
        {
            'query': getattr(response, 'query', query),
            'sub_queries': getattr(response, 'sub_queries', []) or [],
            'total_notes': getattr(response, 'total_notes', 0),
            'total_facts': getattr(response, 'total_facts', 0),
            'truncated': getattr(response, 'truncated', False),
            'topics': topics,
        }
    )


def handle_retain(
    api: Any,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        name = _require(args, 'name')
        description = _require(args, 'description')
        content = _require(args, 'content')
    except ValueError as e:
        return tool_error(str(e))

    tags = args.get('tags') or []
    note_key = args.get('note_key') or None

    from memex_common.schemas import NoteCreateDTO

    dto = NoteCreateDTO(
        name=name,
        description=description,
        content=base64.b64encode(content.encode('utf-8')),
        tags=tags,
        note_key=note_key,
        vault_id=str(vault_id) if vault_id else None,
        author='hermes',
        template=HERMES_USER_NOTE_TEMPLATE,
    )

    try:
        result = run_sync(api.ingest(dto, background=True), timeout=30.0)
    except Exception as e:
        logger.warning('memex_retain failed: %s', e)
        return tool_error(f'Retain failed: {e}')

    return json.dumps(
        {
            'status': getattr(result, 'status', 'ok'),
            'note_id': getattr(result, 'note_id', None),
            'note_key': note_key,
        }
    )


def handle_list_entities(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 20), 100)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        entities = run_sync(
            api.search_entities(
                query=query,
                limit=limit,
                vault_ids=vault_ids,
                entity_type=args.get('entity_type') or None,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_list_entities failed: %s', e)
        return tool_error(f'Entity list failed: {e}')

    return json.dumps({'results': [_serialize_entity(e) for e in entities or []]})


def handle_get_entity_mentions(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        entity_id = _require(args, 'entity_id')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 20), 100)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        mentions = run_sync(
            api.get_entity_mentions(
                entity_id=entity_id,
                limit=limit,
                vault_ids=vault_ids,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_get_entity_mentions failed: %s', e)
        return tool_error(f'Entity mentions failed: {e}')

    items: list[dict[str, Any]] = []
    for m in mentions or []:
        if isinstance(m, dict):
            unit = m.get('unit')
            note = m.get('note')
            items.append(
                {
                    'unit': _serialize_memory_unit(unit) if unit else None,
                    'note_id': str(getattr(note, 'id', '')) if note is not None else None,
                }
            )
        else:
            items.append({'unit': _serialize_memory_unit(m), 'note_id': None})
    return json.dumps({'results': items})


def handle_get_entity_cooccurrences(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        entity_id = _require(args, 'entity_id')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 20), 100)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        cooccurrences = run_sync(
            api.get_entity_cooccurrences(
                entity_id=entity_id,
                limit=limit,
                vault_ids=vault_ids,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_get_entity_cooccurrences failed: %s', e)
        return tool_error(f'Entity cooccurrences failed: {e}')

    # Server returns dicts with keys: entity_id_1, entity_id_2, entity_1_name,
    # entity_1_type, entity_2_name, entity_2_type, cooccurrence_count, vault_id.
    # Pivot onto the "other" entity relative to the queried one.
    queried_id = str(entity_id)
    pairs: list[dict[str, Any]] = []
    for c in cooccurrences or []:
        if not isinstance(c, dict):
            continue
        id_1 = str(c.get('entity_id_1') or '')
        id_2 = str(c.get('entity_id_2') or '')
        if queried_id == id_1:
            other_id, other_name, other_type = (
                id_2,
                c.get('entity_2_name'),
                c.get('entity_2_type'),
            )
        else:
            other_id, other_name, other_type = (
                id_1,
                c.get('entity_1_name'),
                c.get('entity_1_type'),
            )
        pairs.append(
            {
                'entity_id': other_id,
                'name': other_name,
                'type': other_type,
                'count': c.get('cooccurrence_count', 0),
            }
        )
    return json.dumps({'results': pairs})


# --- Assets (Stream 5) ---


def handle_list_assets(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """List asset filenames/paths/mime types for a note."""
    try:
        note_id_str = _require(args, 'note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        note_uuid = UUID(str(note_id_str))
    except (ValueError, TypeError):
        return tool_error(f'Invalid note_id: {note_id_str!r}')

    try:
        note = run_sync(api.get_note(note_uuid), timeout=30.0)
    except Exception as e:
        logger.warning('memex_list_assets failed: %s', e)
        return tool_error(f'List assets failed: {e}')

    assets = list(getattr(note, 'assets', None) or [])
    results: list[dict[str, Any]] = []
    for asset_path in assets:
        filename = Path(asset_path).name
        mime_type, _ = mimetypes.guess_type(filename)
        results.append({'filename': filename, 'path': asset_path, 'mime_type': mime_type})
    return json.dumps({'results': results})


def handle_get_resources(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Fetch assets by path; return base64-encoded bytes with per-path failure isolation."""
    try:
        paths = _require(args, 'paths')
    except ValueError as e:
        return tool_error(str(e))

    if not isinstance(paths, list):
        return tool_error("'paths' must be an array of strings")

    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            content_bytes = run_sync(api.get_resource(path), timeout=30.0)
            mime_type, _ = mimetypes.guess_type(path)
            results.append(
                {
                    'path': path,
                    'filename': Path(path).name,
                    'mime_type': mime_type,
                    'content_b64': base64.b64encode(content_bytes).decode('ascii'),
                    'size_bytes': len(content_bytes),
                }
            )
        except Exception as exc:
            results.append({'path': path, 'error': str(exc)})
    return json.dumps({'results': results})


def handle_add_assets(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Attach base64-encoded files to a note (diverges from MCP file_paths input)."""
    try:
        note_id_str = _require(args, 'note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        note_uuid = UUID(str(note_id_str))
    except (ValueError, TypeError):
        return tool_error(f'Invalid note_id: {note_id_str!r}')

    assets = args.get('assets') or []
    try:
        files: dict[str, bytes] = {
            a['filename']: base64.b64decode(a['content_b64'], validate=True) for a in assets
        }
    except (binascii.Error, ValueError, TypeError, KeyError) as e:
        return tool_error(f'Invalid asset payload: {e}')

    try:
        result = run_sync(api.add_note_assets(note_uuid, files), timeout=60.0)
    except Exception as e:
        logger.warning('memex_add_assets failed: %s', e)
        return tool_error(f'Add assets failed: {e}')

    added: list[dict[str, Any]] = []
    for asset_path in result.get('added_assets', []) or []:
        filename = Path(asset_path).name
        mime_type, _ = mimetypes.guess_type(filename)
        added.append({'filename': filename, 'path': asset_path, 'mime_type': mime_type})
    return json.dumps(
        {
            'status': 'ok',
            'note_id': str(note_uuid),
            'added_assets': added,
            'skipped': result.get('skipped', []),
            'asset_count': result.get('asset_count', 0),
        }
    )


# --- KV store (Stream 5) ---


def handle_kv_write(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Write a fact or preference to the KV store with semantic embedding."""
    try:
        value = _require(args, 'value')
        key = _require(args, 'key')
    except ValueError as e:
        return tool_error(str(e))

    ttl_seconds = args.get('ttl_seconds')

    try:
        embedding = run_sync(api.embed_text(value), timeout=15.0)
        entry = run_sync(
            api.kv_put(value=value, key=key, embedding=embedding, ttl_seconds=ttl_seconds),
            timeout=15.0,
        )
    except Exception as e:
        logger.warning('memex_kv_write failed: %s', e)
        return tool_error(f'KV write failed: {e}')

    return json.dumps(
        {
            'key': entry.key,
            'value': entry.value,
            'scope': _scope_from_key(entry.key),
            'expires_at': (e2.isoformat() if (e2 := getattr(entry, 'expires_at', None)) else None),
        }
    )


def handle_kv_get(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Exact key lookup in the KV store. Returns JSON null on miss."""
    try:
        key = _require(args, 'key')
    except ValueError as e:
        return tool_error(str(e))

    try:
        entry = run_sync(api.kv_get(key), timeout=15.0)
    except Exception as e:
        logger.warning('memex_kv_get failed: %s', e)
        return tool_error(f'KV get failed: {e}')

    if entry is None:
        return json.dumps(None)
    return json.dumps(_serialize_kv_entry(entry))


def handle_kv_search(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Semantic search over KV store entries."""
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    namespaces = args.get('namespaces') or None
    limit = int(args.get('limit') or 5)

    try:
        entries = run_sync(
            api.kv_search(query=query, namespaces=namespaces, limit=limit),
            timeout=15.0,
        )
    except Exception as e:
        logger.warning('memex_kv_search failed: %s', e)
        return tool_error(f'KV search failed: {e}')

    return json.dumps({'results': [_serialize_kv_entry(e) for e in entries or []]})


def handle_kv_list(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """List KV entries, optionally filtered by namespace prefixes."""
    namespaces = args.get('namespaces') or None
    try:
        entries = run_sync(api.kv_list(namespaces=namespaces), timeout=15.0)
    except Exception as e:
        logger.warning('memex_kv_list failed: %s', e)
        return tool_error(f'KV list failed: {e}')

    return json.dumps({'results': [_serialize_kv_entry(e) for e in entries or []]})


HANDLERS = {
    # --- Vault-scoped (Stream 1) ---
    'memex_recall': handle_recall,
    'memex_retrieve_notes': handle_retrieve_notes,
    'memex_survey': handle_survey,
    'memex_retain': handle_retain,
    'memex_list_entities': handle_list_entities,
    'memex_get_entity_mentions': handle_get_entity_mentions,
    'memex_get_entity_cooccurrences': handle_get_entity_cooccurrences,
    # --- Read/discovery (Stream 2) ---
    # <Stream 2 appends>
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    'memex_list_assets': handle_list_assets,
    'memex_get_resources': handle_get_resources,
    'memex_add_assets': handle_add_assets,
    # --- KV store (Stream 5) ---
    'memex_kv_write': handle_kv_write,
    'memex_kv_get': handle_kv_get,
    'memex_kv_search': handle_kv_search,
    'memex_kv_list': handle_kv_list,
}


def dispatch(
    tool_name: str,
    args: dict[str, Any],
    *,
    api: Any,
    config: HermesMemexConfig,
    vault_id: UUID | None,
) -> str:
    handler = HANDLERS.get(tool_name)
    if handler is None:
        return tool_error(f'Unknown tool: {tool_name}')
    try:
        return handler(api, config, vault_id, args)
    except VaultResolutionError as exc:
        return tool_error(f'Unknown vault: {exc.name!r}')


__all__ = [
    # --- Vault-scoped (Stream 1) ---
    'ALL_SCHEMAS',
    'GET_ENTITY_COOCCURRENCES_SCHEMA',
    'GET_ENTITY_MENTIONS_SCHEMA',
    'HANDLERS',
    'LIST_ENTITIES_SCHEMA',
    'RECALL_SCHEMA',
    'RETAIN_SCHEMA',
    'RETRIEVE_NOTES_SCHEMA',
    'SURVEY_SCHEMA',
    'VaultResolutionError',
    '_resolve_vault_ids',
    'dispatch',
    # --- Read/discovery (Stream 2) ---
    # <Stream 2 appends>
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    'ADD_ASSETS_SCHEMA',
    'GET_RESOURCES_SCHEMA',
    'LIST_ASSETS_SCHEMA',
    # --- KV store (Stream 5) ---
    'KV_GET_SCHEMA',
    'KV_LIST_SCHEMA',
    'KV_SEARCH_SCHEMA',
    'KV_WRITE_SCHEMA',
]
