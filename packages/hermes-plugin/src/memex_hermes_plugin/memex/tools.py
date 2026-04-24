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
# Tool schemas
# ---------------------------------------------------------------------------

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
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Filter by note tags (optional).',
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
                'description': (
                    'Full markdown body of the note. Use real headers, not '
                    'bold labels. Shape:\n'
                    '\n'
                    '# <Title>\n'
                    '\n'
                    '<one-paragraph summary>\n'
                    '\n'
                    '## <Section>\n'
                    '<body>\n'
                    '\n'
                    '## <Section>\n'
                    '- **<sub-label>**: <detail>\n'
                    '\n'
                    'Rules: one `#` title matching `name`; `##` for each '
                    'section; blank line between sections; put facts, dates, '
                    'and decisions under `##` headings (e.g. `## Date`, '
                    '`## Symptom`, `## Root Cause`), never as inline '
                    '`**Label:**` lines. Use `- **sub-label**: value` bullets '
                    'for short fielded items inside a section.'
                ),
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
        },
        'required': ['entity_id'],
    },
}

ALL_SCHEMAS: list[dict[str, Any]] = [
    RECALL_SCHEMA,
    RETRIEVE_NOTES_SCHEMA,
    SURVEY_SCHEMA,
    RETAIN_SCHEMA,
    LIST_ENTITIES_SCHEMA,
    GET_ENTITY_MENTIONS_SCHEMA,
    GET_ENTITY_COOCCURRENCES_SCHEMA,
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


def handle_recall(
    api: Any, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 10), 50)
    tags = args.get('tags') or None
    vault_ids: list[Any] | None = [vault_id] if vault_id else None

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
    vault_ids: list[Any] | None = [vault_id] if vault_id else None

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
    vault_ids: list[Any] | None = [vault_id] if vault_id else None

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

    try:
        entities = run_sync(
            api.search_entities(
                query=query,
                limit=limit,
                vault_id=vault_id,
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

    try:
        mentions = run_sync(
            api.get_entity_mentions(
                entity_id=entity_id,
                limit=limit,
                vault_id=vault_id,
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

    try:
        cooccurrences = run_sync(
            api.get_entity_cooccurrences(
                entity_id=entity_id,
                limit=limit,
                vault_id=vault_id,
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


HANDLERS = {
    'memex_recall': handle_recall,
    'memex_retrieve_notes': handle_retrieve_notes,
    'memex_survey': handle_survey,
    'memex_retain': handle_retain,
    'memex_list_entities': handle_list_entities,
    'memex_get_entity_mentions': handle_get_entity_mentions,
    'memex_get_entity_cooccurrences': handle_get_entity_cooccurrences,
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
    return handler(api, config, vault_id, args)


__all__ = [
    'ALL_SCHEMAS',
    'GET_ENTITY_COOCCURRENCES_SCHEMA',
    'GET_ENTITY_MENTIONS_SCHEMA',
    'HANDLERS',
    'LIST_ENTITIES_SCHEMA',
    'RECALL_SCHEMA',
    'RETAIN_SCHEMA',
    'RETRIEVE_NOTES_SCHEMA',
    'SURVEY_SCHEMA',
    'dispatch',
]
