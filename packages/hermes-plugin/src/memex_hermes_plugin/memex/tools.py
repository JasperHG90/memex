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
# <streams append here>

# --- Entities/memory/lineage (Stream 3) ---
# <streams append here>

# --- Lifecycle/templates (Stream 4) ---

_VALID_NOTE_STATUSES = frozenset({'active', 'superseded', 'appended', 'archived'})

SET_NOTE_STATUS_SCHEMA: dict[str, Any] = {
    'name': 'memex_set_note_status',
    'description': (
        'Set note lifecycle status: active, superseded, appended, or archived. '
        'Use to supersede an outdated note, mark it as appended, or archive it. '
        'When superseded, all memory units are marked stale. Optionally link to '
        'the replacing/parent note via linked_note_id.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_id': {
                'type': 'string',
                'description': 'Note UUID to update.',
            },
            'status': {
                'type': 'string',
                'description': 'New status: active, superseded, appended, or archived.',
            },
            'linked_note_id': {
                'type': 'string',
                'description': 'UUID of the note that supersedes/contains this one (optional).',
            },
        },
        'required': ['note_id', 'status'],
    },
}

UPDATE_USER_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_update_user_notes',
    'description': (
        'Update user_notes on an existing note and reprocess into the memory graph. '
        'Pass null or omit user_notes to delete all user annotations. Old user_notes '
        'memory units are deleted and new ones extracted.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_id': {
                'type': 'string',
                'description': 'Note UUID to update.',
            },
            'user_notes': {
                'type': ['string', 'null'],
                'description': 'New user_notes text, or null to delete all annotations.',
            },
        },
        'required': ['note_id'],
    },
}

RENAME_NOTE_SCHEMA: dict[str, Any] = {
    'name': 'memex_rename_note',
    'description': ('Rename a note. Updates the title in metadata, page index, and doc_metadata.'),
    'parameters': {
        'type': 'object',
        'properties': {
            'note_id': {
                'type': 'string',
                'description': 'Note UUID to rename.',
            },
            'new_title': {
                'type': 'string',
                'description': 'New title for the note.',
            },
        },
        'required': ['note_id', 'new_title'],
    },
}

GET_TEMPLATE_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_template',
    'description': (
        'Get a markdown template for memex_retain. Use memex_list_templates '
        'to discover available template slugs.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'slug': {
                'type': 'string',
                'description': 'Template slug. Use memex_list_templates to discover available slugs.',
            },
        },
        'required': ['slug'],
    },
}

LIST_TEMPLATES_SCHEMA: dict[str, Any] = {
    'name': 'memex_list_templates',
    'description': (
        'List all available note templates with metadata (slug, display name, '
        'description, source layer).'
    ),
    'parameters': {
        'type': 'object',
        'properties': {},
    },
}

REGISTER_TEMPLATE_SCHEMA: dict[str, Any] = {
    'name': 'memex_register_template',
    'description': (
        'Register a new note template from inline markdown content. Stored in '
        'the global scope by default.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'slug': {
                'type': 'string',
                'description': 'Template identifier (e.g. sprint_retro).',
            },
            'template': {
                'type': 'string',
                'description': 'Markdown template content. Should include YAML frontmatter.',
            },
            'name': {
                'type': 'string',
                'description': 'Human-readable template name (optional).',
            },
            'description': {
                'type': 'string',
                'description': 'Short description of the template (optional).',
            },
        },
        'required': ['slug', 'template'],
    },
}

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
    # <Stream 2 appends>
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    SET_NOTE_STATUS_SCHEMA,
    UPDATE_USER_NOTES_SCHEMA,
    RENAME_NOTE_SCHEMA,
    GET_TEMPLATE_SCHEMA,
    LIST_TEMPLATES_SCHEMA,
    REGISTER_TEMPLATE_SCHEMA,
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


# --- Lifecycle/templates (Stream 4) ---


def _parse_uuid(raw: Any, label: str = 'note_id') -> UUID:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f'Invalid {label}: {raw!r}') from exc


def _build_template_registry() -> Any:
    """Build a ``TemplateRegistry`` for the current hermes process.

    Uses the same layering the MCP server uses — builtin → global → local —
    with paths sourced from Memex's own ``MemexConfig`` (filestore root) and
    the current working directory for local overrides. Reflects the RFC
    decision that templates are bundled client-side; no HTTP call.
    """
    import pathlib

    from memex_common.templates import BUILTIN_PROMPTS_DIR, TemplateRegistry

    dirs: list[tuple[str, pathlib.Path]] = [('builtin', BUILTIN_PROMPTS_DIR)]
    try:
        from memex_common.config import MemexConfig

        root = MemexConfig().server.file_store.root
        if '://' not in root:
            dirs.append(('global', pathlib.Path(root) / 'templates'))
    except Exception as exc:
        logger.debug('Template registry: skipping global layer (%s)', exc)
    dirs.append(('local', pathlib.Path('.memex/templates')))
    return TemplateRegistry(dirs)


def handle_set_note_status(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id_raw = _require(args, 'note_id')
        status = _require(args, 'status')
    except ValueError as e:
        return tool_error(str(e))

    if status not in _VALID_NOTE_STATUSES:
        return tool_error(
            f'Invalid status: {status!r}. Must be one of: {sorted(_VALID_NOTE_STATUSES)}'
        )

    try:
        note_uuid = _parse_uuid(note_id_raw, label='note_id')
    except ValueError as e:
        return tool_error(str(e))

    linked_raw = args.get('linked_note_id') or None
    linked_uuid: UUID | None = None
    if linked_raw:
        try:
            linked_uuid = _parse_uuid(linked_raw, label='linked_note_id')
        except ValueError as e:
            return tool_error(str(e))

    try:
        run_sync(
            api.set_note_status(note_uuid, status, linked_uuid),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_set_note_status failed: %s', e)
        return tool_error(f'Set note status failed: {e}')

    return json.dumps(
        {
            'status': status,
            'note_id': str(note_uuid),
            'linked_note_id': str(linked_uuid) if linked_uuid else None,
        }
    )


def handle_update_user_notes(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id_raw = _require(args, 'note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        note_uuid = _parse_uuid(note_id_raw, label='note_id')
    except ValueError as e:
        return tool_error(str(e))

    # Explicit ``null`` and missing key both clear annotations.
    user_notes = args.get('user_notes')

    try:
        result = run_sync(
            api.update_user_notes(note_uuid, user_notes),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_update_user_notes failed: %s', e)
        return tool_error(f'Update user notes failed: {e}')

    return json.dumps(result if isinstance(result, dict) else {'result': result})


def handle_rename_note(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id_raw = _require(args, 'note_id')
        new_title = _require(args, 'new_title')
    except ValueError as e:
        return tool_error(str(e))

    try:
        note_uuid = _parse_uuid(note_id_raw, label='note_id')
    except ValueError as e:
        return tool_error(str(e))

    try:
        run_sync(
            api.update_note_title(note_uuid, new_title),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_rename_note failed: %s', e)
        return tool_error(f'Rename note failed: {e}')

    return json.dumps({'note_id': str(note_uuid), 'new_title': new_title})


def handle_get_template(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        slug = _require(args, 'slug')
    except ValueError as e:
        return tool_error(str(e))

    try:
        registry = _build_template_registry()
        content = registry.get_template(slug)
    except KeyError as e:
        return tool_error(f'Unknown template: {slug!r} — {e}')
    except Exception as e:
        logger.warning('memex_get_template failed: %s', e)
        return tool_error(f'Get template failed: {e}')

    return json.dumps({'slug': slug, 'content': content})


def handle_list_templates(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        registry = _build_template_registry()
        templates = registry.list_templates()
    except Exception as e:
        logger.warning('memex_list_templates failed: %s', e)
        return tool_error(f'List templates failed: {e}')

    results = [
        {
            'slug': t.slug,
            'display_name': t.display_name,
            'description': t.description,
            'source': t.source,
        }
        for t in templates or []
    ]
    return json.dumps({'count': len(results), 'results': results})


def handle_register_template(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        slug = _require(args, 'slug')
        template = _require(args, 'template')
    except ValueError as e:
        return tool_error(str(e))

    name = args.get('name') or None
    description = args.get('description') or None

    try:
        registry = _build_template_registry()
        info = registry.register_from_content(
            slug=slug,
            template=template,
            name=name,
            description=description,
            scope='global',
        )
    except Exception as e:
        logger.warning('memex_register_template failed: %s', e)
        return tool_error(f'Register template failed: {e}')

    return json.dumps(
        {
            'slug': info.slug,
            'display_name': info.display_name,
            'description': info.description,
            'source': info.source,
        }
    )


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
    'memex_set_note_status': handle_set_note_status,
    'memex_update_user_notes': handle_update_user_notes,
    'memex_rename_note': handle_rename_note,
    'memex_get_template': handle_get_template,
    'memex_list_templates': handle_list_templates,
    'memex_register_template': handle_register_template,
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
    # <Stream 2 appends>
    # --- Entities/memory/lineage (Stream 3) ---
    # <Stream 3 appends>
    # --- Lifecycle/templates (Stream 4) ---
    'GET_TEMPLATE_SCHEMA',
    'LIST_TEMPLATES_SCHEMA',
    'REGISTER_TEMPLATE_SCHEMA',
    'RENAME_NOTE_SCHEMA',
    'SET_NOTE_STATUS_SCHEMA',
    'UPDATE_USER_NOTES_SCHEMA',
    # --- Assets (Stream 5) ---
    # <Stream 5 appends>
    # --- KV store (Stream 5) ---
    # <Stream 5 appends>
]
