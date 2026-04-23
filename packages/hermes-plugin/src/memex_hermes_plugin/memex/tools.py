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
import json
import logging
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

LIST_VAULTS_SCHEMA: dict[str, Any] = {
    'name': 'memex_list_vaults',
    'description': (
        'List all vaults with note counts and active status. Call this before '
        'using vault_ids on other tools so you know what vault names/UUIDs exist.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {},
        'required': [],
    },
}

GET_VAULT_SUMMARY_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_vault_summary',
    'description': (
        'Return the precomputed narrative summary for a vault: themes, key '
        'entities, inventory stats. Use to orient on "what\'s in vault X?" '
        'without running expensive searches.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_id': {
                'type': 'string',
                'description': ('Vault UUID or name. Omit to use the session-bound vault.'),
            },
        },
    },
}

FIND_NOTE_SCHEMA: dict[str, Any] = {
    'name': 'memex_find_note',
    'description': (
        'Fuzzy title search for notes. Returns note IDs, titles, and similarity '
        'scores. Use when you know (part of) the title; for content search use '
        'memex_retrieve_notes.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Title fragment (partial or fuzzy match).',
            },
            'vault_ids': _vault_ids_schema(),
            'limit': {
                'type': 'integer',
                'description': 'Max matches to return (default: 5).',
            },
        },
        'required': ['query'],
    },
}

READ_NOTE_SCHEMA: dict[str, Any] = {
    'name': 'memex_read_note',
    'description': (
        'Read a full note by ID. Use only for small notes; for large notes '
        'fetch the page index with memex_get_page_indices and read individual '
        'sections with memex_get_nodes.'
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

GET_PAGE_INDICES_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_page_indices',
    'description': (
        'Get the table of contents (section titles, node IDs, token counts) '
        'for a single note. Pass leaf node IDs to memex_get_nodes to read the '
        'content of specific sections.'
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

GET_NODES_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_nodes',
    'description': (
        'Batch-read note sections by node IDs. Get node IDs from '
        'memex_get_page_indices. Accepts 1 or more IDs.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'node_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of node UUIDs.',
            },
        },
        'required': ['node_ids'],
    },
}

GET_NOTES_METADATA_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_notes_metadata',
    'description': (
        'Batch-fetch metadata (title, tags, token count, has_assets) for 1+ '
        'notes. Use after memex_recall to filter results before reading.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of note UUIDs.',
            },
        },
        'required': ['note_ids'],
    },
}

LIST_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_list_notes',
    'description': (
        'List notes with optional date/template/tag/status filters. Default '
        'date field is created_at (ingest time).'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_ids': _vault_ids_schema(),
            'after': {
                'type': 'string',
                'description': 'ISO 8601 date — only notes on/after this date.',
            },
            'before': {
                'type': 'string',
                'description': 'ISO 8601 date — only notes on/before this date.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max notes to return (default: 100).',
            },
            'template': {
                'type': 'string',
                'description': 'Filter by template slug (e.g. "general_note").',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Filter by tags (note metadata; NOT vaults).',
            },
            'status': {
                'type': 'string',
                'description': 'Filter by lifecycle status (active/superseded/appended/archived).',
            },
            'date_by': {
                'type': 'string',
                'description': (
                    "Which date column after/before filter on: 'created_at' "
                    "(ingest time; default), 'publish_date' (authored), or "
                    "'coalesce' (publish_date if set else created_at)."
                ),
            },
        },
    },
}

RECENT_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_recent_notes',
    'description': (
        'Browse the most recently ingested notes. Filter by vault, date range, '
        'or template. Defaults to all vaults.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'limit': {
                'type': 'integer',
                'description': 'Max notes to return (default: 20).',
            },
            'vault_ids': _vault_ids_schema(),
            'after': {
                'type': 'string',
                'description': 'ISO 8601 date — only notes on/after this date.',
            },
            'before': {
                'type': 'string',
                'description': 'ISO 8601 date — only notes on/before this date.',
            },
            'template': {
                'type': 'string',
                'description': 'Filter by template slug.',
            },
            'date_by': {
                'type': 'string',
                'description': (
                    "Which date column after/before filter on: 'created_at' "
                    "(ingest time; default), 'publish_date' (authored), or "
                    "'coalesce' (publish_date if set else created_at)."
                ),
            },
        },
    },
}

SEARCH_USER_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_search_user_notes',
    'description': (
        'Search only user annotations (user_notes frontmatter) across all '
        'notes. Returns memory units extracted from your annotations — use to '
        'recall what you have been thinking or annotating.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Natural-language query.',
            },
            'vault_ids': _vault_ids_schema(),
            'limit': {
                'type': 'integer',
                'description': 'Max results (default: 10).',
            },
        },
        'required': ['query'],
    },
}

# --- Entities/memory/lineage (Stream 3) ---
# <streams append here>

# --- Lifecycle/templates (Stream 4) ---
# <streams append here>

# --- Assets (Stream 5) ---
# <streams append here>

# --- KV store (Stream 5) ---
# <streams append here>


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
    LIST_VAULTS_SCHEMA,
    GET_VAULT_SUMMARY_SCHEMA,
    FIND_NOTE_SCHEMA,
    READ_NOTE_SCHEMA,
    GET_PAGE_INDICES_SCHEMA,
    GET_NODES_SCHEMA,
    GET_NOTES_METADATA_SCHEMA,
    LIST_NOTES_SCHEMA,
    RECENT_NOTES_SCHEMA,
    SEARCH_USER_NOTES_SCHEMA,
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    # <Stream 5 appends>
    # --- KV store (Stream 5) ---
    # <Stream 5 appends>
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


# --- Read/discovery (Stream 2) ---


def _serialize_vault(vault: Any) -> dict[str, Any]:
    last_added = getattr(vault, 'last_note_added_at', None)
    return {
        'id': str(getattr(vault, 'id', '')),
        'name': getattr(vault, 'name', ''),
        'description': getattr(vault, 'description', None),
        'is_active': bool(getattr(vault, 'is_active', False)),
        'note_count': int(getattr(vault, 'note_count', 0) or 0),
        'last_note_added_at': last_added.isoformat() if last_added else None,
    }


def _serialize_find_note(result: Any) -> dict[str, Any]:
    publish_date = getattr(result, 'publish_date', None)
    return {
        'note_id': str(getattr(result, 'note_id', '')),
        'title': getattr(result, 'title', ''),
        'score': getattr(result, 'score', 0.0),
        'status': getattr(result, 'status', None),
        'publish_date': publish_date.isoformat() if publish_date else None,
    }


def _serialize_note_dto(note: Any) -> dict[str, Any]:
    created_at = getattr(note, 'created_at', None)
    publish_date = getattr(note, 'publish_date', None)
    return {
        'id': str(getattr(note, 'id', '')),
        'title': getattr(note, 'title', None),
        'name': getattr(note, 'name', None),
        'description': getattr(note, 'description', None),
        'vault_id': str(v) if (v := getattr(note, 'vault_id', None)) else None,
        'vault_name': getattr(note, 'vault_name', None),
        'created_at': created_at.isoformat() if created_at else None,
        'publish_date': publish_date.isoformat() if publish_date else None,
        'original_text': getattr(note, 'original_text', None),
        'assets': list(getattr(note, 'assets', []) or []),
        'doc_metadata': dict(getattr(note, 'doc_metadata', {}) or {}),
        'template': getattr(note, 'template', None),
    }


def _serialize_node_dto(node: Any) -> dict[str, Any]:
    created_at = getattr(node, 'created_at', None)
    return {
        'id': str(getattr(node, 'id', '')),
        'note_id': str(n) if (n := getattr(node, 'note_id', None)) else None,
        'vault_id': str(v) if (v := getattr(node, 'vault_id', None)) else None,
        'title': getattr(node, 'title', ''),
        'text': getattr(node, 'text', ''),
        'level': getattr(node, 'level', 0),
        'seq': getattr(node, 'seq', 0),
        'status': getattr(node, 'status', None),
        'created_at': created_at.isoformat() if created_at else None,
    }


def _serialize_note_list_item(item: Any) -> dict[str, Any]:
    created_at = getattr(item, 'created_at', None)
    publish_date = getattr(item, 'publish_date', None)
    return {
        'id': str(getattr(item, 'id', '')),
        'title': getattr(item, 'title', None),
        'created_at': created_at.isoformat() if created_at else None,
        'publish_date': publish_date.isoformat() if publish_date else None,
        'vault_id': str(v) if (v := getattr(item, 'vault_id', None)) else None,
        'template': getattr(item, 'template', None),
    }


def _parse_iso_or_raise(value: str, field: str) -> Any:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'Invalid {field} date: {value}') from exc


def handle_list_vaults(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        vaults = run_sync(api.list_vaults(), timeout=30.0)
    except Exception as e:
        logger.warning('memex_list_vaults failed: %s', e)
        return tool_error(f'List vaults failed: {e}')

    return json.dumps({'results': [_serialize_vault(v) for v in vaults or []]})


def handle_get_vault_summary(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw = args.get('vault_id')
    try:
        if raw:
            try:
                target = UUID(str(raw))
            except (ValueError, TypeError):
                target = run_sync(api.resolve_vault_identifier(str(raw)), timeout=30.0)
                if not isinstance(target, UUID):
                    target = UUID(str(target))
        elif vault_id is not None:
            target = vault_id
        else:
            return tool_error('No vault specified and no session-bound vault.')
    except Exception as e:
        logger.warning('memex_get_vault_summary resolve failed: %s', e)
        return tool_error(f'Unknown vault: {raw!r}')

    try:
        summary = run_sync(api.get_vault_summary(target), timeout=30.0)
    except Exception as e:
        logger.warning('memex_get_vault_summary failed: %s', e)
        return tool_error(f'Get vault summary failed: {e}')

    if summary is None:
        return tool_error(
            'vault summary not yet generated — it will be computed on the '
            'next background reflection cycle.'
        )

    created = getattr(summary, 'created_at', None)
    updated = getattr(summary, 'updated_at', None)
    return json.dumps(
        {
            'id': str(getattr(summary, 'id', '')),
            'vault_id': str(getattr(summary, 'vault_id', '')),
            'narrative': getattr(summary, 'narrative', ''),
            'themes': getattr(summary, 'themes', []) or [],
            'inventory': getattr(summary, 'inventory', {}) or {},
            'key_entities': getattr(summary, 'key_entities', []) or [],
            'version': getattr(summary, 'version', 0),
            'notes_incorporated': getattr(summary, 'notes_incorporated', 0),
            'created_at': created.isoformat() if created else None,
            'updated_at': updated.isoformat() if updated else None,
        }
    )


def handle_find_note(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 5), 50)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        results = run_sync(
            api.find_notes_by_title(
                query=query,
                vault_ids=vault_ids,
                limit=limit,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_find_note failed: %s', e)
        return tool_error(f'Find note failed: {e}')

    return json.dumps({'results': [_serialize_find_note(r) for r in results or []]})


def handle_read_note(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id = _require(args, 'note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        uuid_obj = UUID(str(note_id))
    except (ValueError, TypeError):
        return tool_error(f'Invalid note UUID: {note_id}')

    try:
        note = run_sync(api.get_note(uuid_obj), timeout=30.0)
    except Exception as e:
        logger.warning('memex_read_note failed: %s', e)
        return tool_error(f'Read note failed: {e}')

    return json.dumps(_serialize_note_dto(note))


def handle_get_page_indices(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id = _require(args, 'note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        uuid_obj = UUID(str(note_id))
    except (ValueError, TypeError):
        return tool_error(f'Invalid note UUID: {note_id}')

    try:
        page_index = run_sync(api.get_note_page_index(uuid_obj), timeout=30.0)
    except Exception as e:
        logger.warning('memex_get_page_indices failed: %s', e)
        return tool_error(f'Get page indices failed: {e}')

    return json.dumps({'note_id': str(uuid_obj), 'page_index': page_index})


def handle_get_nodes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw = args.get('node_ids')
    if not raw:
        return tool_error('Missing required parameter: node_ids')

    uuids: list[UUID] = []
    for nid in raw:
        try:
            uuids.append(UUID(str(nid)))
        except (ValueError, TypeError):
            continue

    if not uuids:
        return tool_error('No valid node UUIDs provided.')

    try:
        nodes = run_sync(api.get_nodes(uuids), timeout=30.0)
    except Exception as e:
        logger.warning('memex_get_nodes failed: %s', e)
        return tool_error(f'Get nodes failed: {e}')

    return json.dumps({'results': [_serialize_node_dto(n) for n in nodes or []]})


def handle_get_notes_metadata(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw = args.get('note_ids')
    if not raw:
        return tool_error('Missing required parameter: note_ids')

    uuids: list[UUID] = []
    for nid in raw:
        try:
            uuids.append(UUID(str(nid)))
        except (ValueError, TypeError):
            continue

    if not uuids:
        return tool_error('No valid note UUIDs provided.')

    try:
        metadata = run_sync(api.get_notes_metadata(uuids), timeout=30.0)
    except Exception as e:
        logger.warning('memex_get_notes_metadata failed: %s', e)
        return tool_error(f'Get notes metadata failed: {e}')

    return json.dumps({'results': list(metadata or [])})


def handle_list_notes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        parsed_after = _parse_iso_or_raise(args['after'], 'after') if args.get('after') else None
        parsed_before = (
            _parse_iso_or_raise(args['before'], 'before') if args.get('before') else None
        )
    except ValueError as e:
        return tool_error(str(e))

    vault_ids = _resolve_vault_ids(api, args, vault_id)
    limit = min(int(args.get('limit') or 100), 500)
    date_by = args.get('date_by') or 'created_at'

    try:
        notes = run_sync(
            api.list_notes(
                vault_ids=vault_ids,
                after=parsed_after,
                before=parsed_before,
                template=args.get('template') or None,
                tags=args.get('tags') or None,
                status=args.get('status') or None,
                date_field=date_by,
                limit=limit,
                offset=0,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_list_notes failed: %s', e)
        return tool_error(f'List notes failed: {e}')

    return json.dumps({'results': [_serialize_note_list_item(n) for n in notes or []]})


def handle_recent_notes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        parsed_after = _parse_iso_or_raise(args['after'], 'after') if args.get('after') else None
        parsed_before = (
            _parse_iso_or_raise(args['before'], 'before') if args.get('before') else None
        )
    except ValueError as e:
        return tool_error(str(e))

    vault_ids = _resolve_vault_ids(api, args, vault_id)
    limit = min(int(args.get('limit') or 20), 200)
    date_by = args.get('date_by') or 'created_at'

    try:
        notes = run_sync(
            api.get_recent_notes(
                limit=limit,
                vault_ids=vault_ids,
                after=parsed_after,
                before=parsed_before,
                template=args.get('template') or None,
                date_field=date_by,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_recent_notes failed: %s', e)
        return tool_error(f'Recent notes failed: {e}')

    return json.dumps({'results': [_serialize_note_list_item(n) for n in notes or []]})


def handle_search_user_notes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 10), 50)
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    try:
        results = run_sync(
            api.search(
                query=query,
                limit=limit,
                vault_ids=vault_ids,
                source_context='user_notes',
            ),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_search_user_notes failed: %s', e)
        return tool_error(f'User-notes search failed: {e}')

    items = [_serialize_memory_unit(u) for u in (results or [])]
    return json.dumps({'count': len(items), 'results': items})


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
    'memex_list_vaults': handle_list_vaults,
    'memex_get_vault_summary': handle_get_vault_summary,
    'memex_find_note': handle_find_note,
    'memex_read_note': handle_read_note,
    'memex_get_page_indices': handle_get_page_indices,
    'memex_get_nodes': handle_get_nodes,
    'memex_get_notes_metadata': handle_get_notes_metadata,
    'memex_list_notes': handle_list_notes,
    'memex_recent_notes': handle_recent_notes,
    'memex_search_user_notes': handle_search_user_notes,
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    # <Stream 5 appends>
    # --- KV store (Stream 5) ---
    # <Stream 5 appends>
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
    'FIND_NOTE_SCHEMA',
    'GET_NODES_SCHEMA',
    'GET_NOTES_METADATA_SCHEMA',
    'GET_PAGE_INDICES_SCHEMA',
    'GET_VAULT_SUMMARY_SCHEMA',
    'LIST_NOTES_SCHEMA',
    'LIST_VAULTS_SCHEMA',
    'READ_NOTE_SCHEMA',
    'RECENT_NOTES_SCHEMA',
    'SEARCH_USER_NOTES_SCHEMA',
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    # <Stream 4 appends>
    # --- Assets (Stream 5) ---
    # <Stream 5 appends>
    # --- KV store (Stream 5) ---
    # <Stream 5 appends>
]
