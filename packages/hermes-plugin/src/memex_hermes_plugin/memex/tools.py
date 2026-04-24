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
from typing import Any, Protocol
from uuid import UUID

from tools.registry import tool_error  # type: ignore[import-not-found]

from .async_bridge import run_sync
from .config import HermesMemexConfig
from .templates import HERMES_USER_NOTE_TEMPLATE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API protocol — structural surface used by the handlers
# ---------------------------------------------------------------------------


class MemexAPIProtocol(Protocol):
    """Structural protocol covering the async methods ``RemoteMemexAPI`` exposes
    and that the handlers below call.

    Return types are intentionally ``Any`` — the handlers use ``getattr`` for
    DTO fields and are robust to minor schema drift. Keeping the Protocol
    lightweight is important: tests pass ``unittest.mock.Mock`` instances that
    satisfy the surface structurally.
    """

    # Ingestion & retrieval
    async def ingest(self, *args: Any, **kwargs: Any) -> Any: ...
    async def search(self, *args: Any, **kwargs: Any) -> Any: ...
    async def search_notes(self, *args: Any, **kwargs: Any) -> Any: ...
    async def search_entities(self, *args: Any, **kwargs: Any) -> Any: ...
    async def survey(self, *args: Any, **kwargs: Any) -> Any: ...
    async def find_notes_by_title(self, *args: Any, **kwargs: Any) -> Any: ...

    # Notes / nodes
    async def list_notes(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_recent_notes(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_note(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_note_metadata(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_note_page_index(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_nodes(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_notes_metadata(self, *args: Any, **kwargs: Any) -> Any: ...
    async def update_note_title(self, *args: Any, **kwargs: Any) -> Any: ...
    async def update_user_notes(self, *args: Any, **kwargs: Any) -> Any: ...
    async def set_note_status(self, *args: Any, **kwargs: Any) -> Any: ...

    # Entities / memory / lineage
    async def get_entities(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_entity(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_entity_cooccurrences(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_entity_mentions(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_memory_unit(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_memory_links(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_lineage(self, *args: Any, **kwargs: Any) -> Any: ...

    # Vaults
    async def list_vaults(self, *args: Any, **kwargs: Any) -> Any: ...
    async def resolve_vault_identifier(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_vault_summary(self, *args: Any, **kwargs: Any) -> Any: ...

    # Assets / resources
    async def add_note_assets(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_resource(self, *args: Any, **kwargs: Any) -> Any: ...

    # KV + embeddings
    async def embed_text(self, *args: Any, **kwargs: Any) -> Any: ...
    async def kv_put(self, *args: Any, **kwargs: Any) -> Any: ...
    async def kv_get(self, *args: Any, **kwargs: Any) -> Any: ...
    async def kv_list(self, *args: Any, **kwargs: Any) -> Any: ...
    async def kv_search(self, *args: Any, **kwargs: Any) -> Any: ...


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
    api: MemexAPIProtocol, args: dict[str, Any], bound_vault_id: UUID | None
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
        if len(supplied) > 1:
            logger.debug("vault_ids=['*', ...]: wildcard dominates; other entries ignored")
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
            'offset': {
                'type': 'integer',
                'description': 'Number of notes to skip for pagination (default: 0).',
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

GET_ENTITIES_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_entities',
    'description': (
        'Batch lookup of entity details by ID. Returns canonical name, type, '
        'mention count, and optional description for each entity.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of entity UUIDs to fetch.',
            },
        },
        'required': ['entity_ids'],
    },
}

GET_MEMORY_UNITS_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_memory_units',
    'description': (
        'Batch lookup of memory units (facts, events, observations) by ID. '
        'Includes status and supersession info.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of memory unit UUIDs to fetch.',
            },
        },
        'required': ['unit_ids'],
    },
}

GET_MEMORY_LINKS_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_memory_links',
    'description': (
        'Retrieve typed relationship links (semantic, temporal, causal, '
        'contradiction) for a list of memory units. Returns a flat list — each '
        'link carries its unit_id so callers can re-group by source unit. '
        'Intended for ~10 unit_ids at a time; larger batches multiply API calls.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Memory unit UUIDs.',
            },
            'link_type': {
                'type': 'string',
                'description': 'Filter to one link type: semantic, temporal, causal, contradiction.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max links per unit (default: 20, max: 100).',
            },
        },
        'required': ['unit_ids'],
    },
}

GET_LINEAGE_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_lineage',
    'description': (
        'Trace provenance between documents and facts. '
        'Upstream: mental_model → observation → memory_unit → note. '
        'Downstream: note → memory_unit → observation → mental_model.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_type': {
                'type': 'string',
                'description': 'Entity type: mental_model, observation, memory_unit, or note.',
            },
            'entity_id': {
                'type': 'string',
                'description': 'UUID of the entity.',
            },
            'direction': {
                'type': 'string',
                'description': 'Traversal direction: upstream (default), downstream, or both.',
            },
            'depth': {
                'type': 'integer',
                'description': 'Max recursion depth (default: 3).',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max children per node (default: 5).',
            },
        },
        'required': ['entity_type', 'entity_id'],
    },
}

# --- Lifecycle/templates (Stream 4) ---

_VALID_NOTE_STATUSES = frozenset({'active', 'superseded', 'appended', 'archived'})

# Canonical set accepted by ``client.list_notes(date_field=...)`` — see the
# docstring at ``packages/common/src/memex_common/client.py:list_notes``.
_VALID_DATE_BY = frozenset({'coalesce', 'created_at', 'publish_date'})

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
                'enum': ['active', 'superseded', 'appended', 'archived'],
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
                'type': 'string',
                'nullable': True,
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
                'maxItems': 50,
                'description': 'Resource paths to fetch (max 50 per call).',
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
                'maxItems': 20,
                'description': 'List of {filename, content_b64} objects (max 20 per call).',
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
    GET_ENTITIES_SCHEMA,
    GET_MEMORY_UNITS_SCHEMA,
    GET_MEMORY_LINKS_SCHEMA,
    GET_LINEAGE_SCHEMA,
    # --- Lifecycle/templates (Stream 4) ---
    SET_NOTE_STATUS_SCHEMA,
    UPDATE_USER_NOTES_SCHEMA,
    RENAME_NOTE_SCHEMA,
    GET_TEMPLATE_SCHEMA,
    LIST_TEMPLATES_SCHEMA,
    REGISTER_TEMPLATE_SCHEMA,
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


def _is_unsafe_asset_filename(filename: Any) -> bool:
    """Reject path-traversal-capable or hidden filenames before they hit the asset store.

    A safe filename is a plain basename: no separators, no parent-dir components,
    no leading dot, and no control characters. ``add_note_assets`` joins the filename
    with the vault/note prefix, so traversal here would let a caller escape the note's
    asset directory.
    """
    if not isinstance(filename, str) or not filename:
        return True
    if '/' in filename or '\\' in filename:
        return True
    if filename.startswith('.'):
        return True
    if '..' in filename:
        return True
    for c in filename:
        if ord(c) < 32:
            return True
    return False


# Bulk-input caps on list-type tool arguments. These mirror the ``maxItems``
# values declared in the tool schemas; the handlers also enforce them
# defensively because some clients ignore schema-level limits.
_MAX_GET_RESOURCES_PATHS = 50
_MAX_ADD_ASSETS_ITEMS = 20

# Per-asset byte cap for ``handle_get_resources``. A single oversized asset
# would otherwise OOM the host when base64-encoded (base64 inflates by ~33%).
_MAX_RESOURCE_BYTES = 50 * 1024 * 1024  # 50 MiB

# Canonical KV key namespaces per RFC-012. Hermes is the ``key`` holder here;
# ``_scope_from_key`` above derives these from stored entries.
_VALID_KV_NAMESPACES = ('global:', 'user:', 'project:', 'app:')


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
    # ':leading' (empty prefix before the colon) is treated as unknown.
    result = key.split(':', 1)[0] if ':' in key else ''
    return result or 'unknown'


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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol,
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        vaults = run_sync(api.list_vaults(), timeout=30.0)
    except Exception as e:
        logger.warning('memex_list_vaults failed: %s', e)
        return tool_error(f'List vaults failed: {e}')

    return json.dumps({'results': [_serialize_vault(v) for v in vaults or []]})


def handle_get_vault_summary(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw = args.get('vault_id')
    if not raw and vault_id is None:
        return tool_error('No vault specified and no session-bound vault.')

    # Delegate to _resolve_vault_ids so UUID-parsing and name-resolution stay in
    # one place. It takes a plural ``vault_ids`` key; we pass a single-element
    # list and unwrap the first result.
    try:
        resolved = _resolve_vault_ids(
            api,
            {'vault_ids': [raw]} if raw else {},
            vault_id,
        )
    except VaultResolutionError as exc:
        logger.warning('memex_get_vault_summary resolve failed: %s', exc)
        return tool_error(f'Unknown vault: {raw!r}')
    except Exception as e:
        logger.warning('memex_get_vault_summary resolve failed: %s', e)
        return tool_error(f'Vault summary failed: {e}')

    if not resolved:
        return tool_error('No vault specified and no session-bound vault.')
    target = resolved[0]

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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        raw = _require(args, 'node_ids')
    except ValueError as e:
        return tool_error(str(e))

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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        raw = _require(args, 'note_ids')
    except ValueError as e:
        return tool_error(str(e))

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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        parsed_after = _parse_iso_or_raise(args['after'], 'after') if args.get('after') else None
        parsed_before = (
            _parse_iso_or_raise(args['before'], 'before') if args.get('before') else None
        )
    except ValueError as e:
        return tool_error(str(e))

    status = args.get('status') or None
    if status is not None and status not in _VALID_NOTE_STATUSES:
        return tool_error(
            f'Invalid status: {status!r}. Must be one of: {", ".join(sorted(_VALID_NOTE_STATUSES))}'
        )

    date_by = args.get('date_by') or 'created_at'
    if date_by not in _VALID_DATE_BY:
        return tool_error(
            f'Invalid date_by: {date_by!r}. Must be one of: {", ".join(sorted(_VALID_DATE_BY))}'
        )

    vault_ids = _resolve_vault_ids(api, args, vault_id)
    limit = min(int(args.get('limit') or 100), 500)
    offset = max(int(args.get('offset') or 0), 0)

    try:
        notes = run_sync(
            api.list_notes(
                vault_ids=vault_ids,
                after=parsed_after,
                before=parsed_before,
                template=args.get('template') or None,
                tags=args.get('tags') or None,
                status=status,
                date_field=date_by,
                limit=limit,
                offset=offset,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_list_notes failed: %s', e)
        return tool_error(f'List notes failed: {e}')

    return json.dumps({'results': [_serialize_note_list_item(n) for n in notes or []]})


def handle_recent_notes(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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


# --- Entities/memory/lineage (Stream 3) ---


_LINEAGE_ENTITY_TYPES = frozenset({'mental_model', 'observation', 'memory_unit', 'note'})


def _serialize_lineage(resp: Any) -> dict[str, Any]:
    """Recursively serialize a ``LineageResponse`` for JSON output."""
    return {
        'entity_type': getattr(resp, 'entity_type', None),
        'entity': getattr(resp, 'entity', None) or {},
        'derived_from': [
            _serialize_lineage(child) for child in (getattr(resp, 'derived_from', None) or [])
        ],
    }


def handle_get_entities(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw_ids = args.get('entity_ids') or []
    uuids: list[UUID] = []
    for eid in raw_ids:
        try:
            uuids.append(UUID(str(eid)))
        except (ValueError, TypeError):
            continue

    if not uuids:
        return json.dumps({'results': []})

    items: list[dict[str, Any]] = []
    try:
        entities = run_sync(api.get_entities(uuids), timeout=30.0)
        for ent in entities or []:
            items.append(_serialize_full_entity(ent))
        return json.dumps({'results': items})
    except Exception as batch_exc:
        logger.warning('get_entities batch failed, falling back to singular: %s', batch_exc)

    for uid in uuids:
        try:
            ent = run_sync(api.get_entity(uid), timeout=30.0)
        except Exception as e:
            logger.warning('get_entity(%s) failed: %s', uid, e)
            continue
        if ent is None:
            continue
        items.append(_serialize_full_entity(ent))
    return json.dumps({'results': items})


def _serialize_full_entity(ent: Any) -> dict[str, Any]:
    metadata = getattr(ent, 'metadata', None) or {}
    return {
        'id': str(getattr(ent, 'id', '')),
        'name': getattr(ent, 'name', ''),
        'type': getattr(ent, 'entity_type', None),
        'mention_count': getattr(ent, 'mention_count', 0),
        'description': metadata.get('description') if isinstance(metadata, dict) else None,
    }


def handle_get_memory_units(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Fetch memory units by ID.

    Issues one ``api.get_memory_unit`` call per ID — ``RemoteMemexAPI`` does
    not currently expose a batch ``get_memory_units`` endpoint. If one is
    added later, adopt the try-batch-then-fallback pattern used by
    ``handle_get_entities`` (batch call wrapped in try/except, falling back
    to the per-ID loop on failure).
    """
    raw_ids = args.get('unit_ids') or []
    uuids: list[UUID] = []
    for uid_str in raw_ids:
        try:
            uuids.append(UUID(str(uid_str)))
        except (ValueError, TypeError):
            continue

    items: list[dict[str, Any]] = []
    # Singular per-ID loop — no batch API available; see docstring for the
    # pattern to adopt if RemoteMemexAPI grows a batch variant.
    for uid in uuids:
        try:
            unit = run_sync(api.get_memory_unit(uid), timeout=30.0)
        except Exception as e:
            logger.warning('get_memory_unit(%s) failed: %s', uid, e)
            continue
        if unit is None:
            continue
        items.append(_serialize_memory_unit_full(unit))

    return json.dumps({'results': items})


def _serialize_memory_unit_full(unit: Any) -> dict[str, Any]:
    """Serialize a MemoryUnitDTO with supersession/contradiction context."""
    superseded = []
    for s in getattr(unit, 'superseded_by', None) or []:
        superseded.append(
            {
                'unit_id': str(getattr(s, 'unit_id', '')),
                'unit_text': getattr(s, 'unit_text', ''),
                'relation': getattr(s, 'relation', None),
                'note_title': getattr(s, 'note_title', None),
            }
        )

    metadata = getattr(unit, 'metadata', None) or {}
    links_raw = metadata.get('links', []) if isinstance(metadata, dict) else []
    # Only surface contradiction links; the field name is load-bearing for
    # downstream consumers. Other link types (semantic, temporal, causal) are
    # available via handle_get_memory_links with an explicit link_type filter.
    contradictions = [
        lnk for lnk in links_raw if isinstance(lnk, dict) and lnk.get('relation') == 'contradiction'
    ]

    return {
        'id': str(getattr(unit, 'id', '')),
        'text': getattr(unit, 'text', ''),
        'fact_type': getattr(unit, 'fact_type', None),
        'status': getattr(unit, 'status', None),
        'note_id': str(n) if (n := getattr(unit, 'note_id', None)) else None,
        'superseded_by': superseded,
        'contradictions': contradictions,
    }


def handle_get_memory_links(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw_ids = args.get('unit_ids') or []
    link_type = args.get('link_type') or None
    limit = min(int(args.get('limit') or 20), 100)

    uuids: list[UUID] = []
    for uid_str in raw_ids:
        try:
            uuids.append(UUID(str(uid_str)))
        except (ValueError, TypeError):
            continue

    if not uuids:
        return json.dumps({'results': []})

    all_links: list[dict[str, Any]] = []
    for uid in uuids:
        try:
            links = run_sync(
                api.get_memory_links(unit_id=uid, link_type=link_type, limit=limit),
                timeout=30.0,
            )
        except Exception as e:
            logger.warning('get_memory_links(%s) failed: %s', uid, e)
            continue
        for lnk in links or []:
            all_links.append(
                {
                    'unit_id': str(getattr(lnk, 'unit_id', '')),
                    'note_id': str(n) if (n := getattr(lnk, 'note_id', None)) else None,
                    'note_title': getattr(lnk, 'note_title', None),
                    'relation': getattr(lnk, 'relation', None),
                    'weight': getattr(lnk, 'weight', None),
                    'time': t.isoformat() if (t := getattr(lnk, 'time', None)) else None,
                    'metadata': getattr(lnk, 'metadata', None) or {},
                }
            )

    return json.dumps({'results': all_links})


def handle_get_lineage(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    from memex_common.schemas import LineageDirection

    try:
        entity_type = _require(args, 'entity_type')
        entity_id = _require(args, 'entity_id')
    except ValueError as e:
        return tool_error(str(e))

    if entity_type not in _LINEAGE_ENTITY_TYPES:
        return tool_error(
            f'Invalid entity_type: {entity_type}. '
            f'Must be one of: {", ".join(sorted(_LINEAGE_ENTITY_TYPES))}'
        )

    try:
        uuid_obj = UUID(str(entity_id))
    except (ValueError, TypeError):
        return tool_error(f'Invalid UUID: {entity_id}')

    direction_raw = args.get('direction') or 'upstream'
    try:
        dir_enum = LineageDirection(direction_raw)
    except ValueError:
        return tool_error(
            f'Invalid direction: {direction_raw}. Must be upstream, downstream, or both.'
        )

    depth = int(args.get('depth') or 3)
    limit = int(args.get('limit') or 5)

    try:
        response = run_sync(
            api.get_lineage(
                entity_type=entity_type,
                entity_id=uuid_obj,
                direction=dir_enum,
                depth=depth,
                limit=limit,
            ),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_get_lineage failed: %s', e)
        return tool_error(f'Lineage failed: {e}')

    return json.dumps(_serialize_lineage(response))


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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        note_id_raw = _require(args, 'note_id')
        status = _require(args, 'status')
    except ValueError as e:
        return tool_error(str(e))

    if status not in _VALID_NOTE_STATUSES:
        return tool_error(
            f'Invalid status: {status!r}. Must be one of: {", ".join(sorted(_VALID_NOTE_STATUSES))}'
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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

    return json.dumps({'status': 'ok', 'note_id': str(note_uuid), 'new_title': new_title})


def handle_get_template(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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


# --- Assets (Stream 5) ---


def handle_list_assets(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """List asset filenames/paths/mime types for a note.

    Known perf trade-off: fetches the full ``NoteDTO`` via ``api.get_note`` to
    read ``note.assets``. ``NoteDTO.original_text`` may be large. The lighter
    ``api.get_note_metadata`` endpoint exposes only a ``has_assets: bool``
    flag, not the asset list. If the server ever adds an asset-list-only
    endpoint (e.g. ``GET /notes/{id}/assets``), swap to that here.
    """
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Fetch assets by path; return base64-encoded bytes with per-path failure isolation."""
    try:
        paths = _require(args, 'paths')
    except ValueError as e:
        return tool_error(str(e))

    if not isinstance(paths, list):
        return tool_error("'paths' must be an array of strings")

    if len(paths) > _MAX_GET_RESOURCES_PATHS:
        return tool_error(f'Too many paths: {len(paths)} (max {_MAX_GET_RESOURCES_PATHS}).')

    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            content_bytes = run_sync(api.get_resource(path), timeout=30.0)
            if len(content_bytes) > _MAX_RESOURCE_BYTES:
                results.append(
                    {
                        'path': path,
                        'error': (
                            f'Resource exceeds max size '
                            f'({len(content_bytes)} > {_MAX_RESOURCE_BYTES} bytes)'
                        ),
                    }
                )
                continue
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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

    if len(assets) > _MAX_ADD_ASSETS_ITEMS:
        return tool_error(f'Too many assets: {len(assets)} (max {_MAX_ADD_ASSETS_ITEMS}).')

    for a in assets:
        if not isinstance(a, dict):
            return tool_error(f'Invalid asset entry: {a!r}')
        fn = a.get('filename')
        if _is_unsafe_asset_filename(fn):
            return tool_error(f'Invalid filename: {fn!r}')

    filenames = [a['filename'] for a in assets]
    if len(set(filenames)) != len(filenames):
        return tool_error('Duplicate filenames in assets payload.')

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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    """Write a fact or preference to the KV store with semantic embedding."""
    try:
        value = _require(args, 'value')
        key = _require(args, 'key')
    except ValueError as e:
        return tool_error(str(e))

    if not isinstance(key, str) or not key.startswith(_VALID_KV_NAMESPACES):
        return tool_error(
            f'Invalid key {key!r}: must start with one of {", ".join(_VALID_KV_NAMESPACES)}'
        )

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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
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
    'memex_get_entities': handle_get_entities,
    'memex_get_memory_units': handle_get_memory_units,
    'memex_get_memory_links': handle_get_memory_links,
    'memex_get_lineage': handle_get_lineage,
    # --- Lifecycle/templates (Stream 4) ---
    'memex_set_note_status': handle_set_note_status,
    'memex_update_user_notes': handle_update_user_notes,
    'memex_rename_note': handle_rename_note,
    'memex_get_template': handle_get_template,
    'memex_list_templates': handle_list_templates,
    'memex_register_template': handle_register_template,
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
    api: MemexAPIProtocol,
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
    'MemexAPIProtocol',
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
    'GET_ENTITIES_SCHEMA',
    'GET_LINEAGE_SCHEMA',
    'GET_MEMORY_LINKS_SCHEMA',
    'GET_MEMORY_UNITS_SCHEMA',
    # --- Lifecycle/templates (Stream 4) ---
    'GET_TEMPLATE_SCHEMA',
    'LIST_TEMPLATES_SCHEMA',
    'REGISTER_TEMPLATE_SCHEMA',
    'RENAME_NOTE_SCHEMA',
    'SET_NOTE_STATUS_SCHEMA',
    'UPDATE_USER_NOTES_SCHEMA',
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
