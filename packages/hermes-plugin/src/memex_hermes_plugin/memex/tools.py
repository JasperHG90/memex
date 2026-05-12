"""Memex tool schemas and handlers for Hermes.

Eight Stream-1 tools are exposed (in ``hybrid`` and ``tools`` memory modes);
additional surfaces (note lifecycle, templates, assets, KV) bring the full
schema count to ~36. Tool names mirror the MCP server.

- ``memex_memory_search`` — memory-unit search (TEMPR)
- ``memex_note_search`` — whole-note search
- ``memex_survey`` — broad query decomposition
- ``memex_add_note`` — explicit ingest (NEW note or full overwrite)
- ``memex_append_note`` — atomic delta-append to an existing note
- ``memex_list_entities`` — entity-graph search
- ``memex_get_entity_mentions`` — source memory units for an entity
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
import inspect
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID


from memex_common.asset_cache import (
    MAX_GET_RESOURCES_PATHS,
    MAX_RESOURCE_BYTES,
    SessionAssetCache,
)
from memex_common.asset_resize import validate_and_resize
from memex_common.schemas import (
    VALID_INTENT_CLASSES,
    VALID_RISK_CLASSES,
    IntentClass,
    NoteAppendRequest,
    RiskClass,
)
from tools.registry import tool_error  # type: ignore[import-not-found]

from .async_bridge import run_sync
from .config import HermesMemexConfig
from .templates import HERMES_USER_NOTE_TEMPLATE

logger = logging.getLogger(__name__)


# Canonical-source-derived JSON Schema enum lists. Sourced from the
# ``IntentClass`` / ``RiskClass`` enums in ``memex_common.schemas`` so adding
# or renaming a class value does not silently desync the Hermes tool surface.
_INTENT_ENUM_VALUES: list[str] = [c.value for c in IntentClass]
_RISK_ENUM_VALUES: list[str] = [c.value for c in RiskClass]


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
    async def append_to_note(self, *args: Any, **kwargs: Any) -> Any: ...
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
    async def get_memory_units_by_chunks(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_memory_links(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_lineage(self, *args: Any, **kwargs: Any) -> Any: ...
    async def deprioritize_memory_unit(self, *args: Any, **kwargs: Any) -> Any: ...
    async def restore_memory_unit(self, *args: Any, **kwargs: Any) -> Any: ...
    async def get_unit_history(self, *args: Any, **kwargs: Any) -> Any: ...
    async def summarize_node(self, *args: Any, **kwargs: Any) -> Any: ...
    async def record_outcome(self, *args: Any, **kwargs: Any) -> Any: ...

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

    # F32 — Diagnostics
    async def get_diagnostics_summary(self, *args: Any, **kwargs: Any) -> Any: ...

    # F8 — Lint flags (read-only agent surface)
    async def lint_get_flags(self, *args: Any, **kwargs: Any) -> Any: ...

    # Lint resolution (winner-proposal apply / reverse)
    async def lint_apply_winner(self, *args: Any, **kwargs: Any) -> Any: ...
    async def lint_reverse_winner(self, *args: Any, **kwargs: Any) -> Any: ...

    # F9 — Per-entity advisory lock + vault-wide consolidate
    async def reconsolidate_entity(self, *args: Any, **kwargs: Any) -> Any: ...
    async def consolidate_vault(self, *args: Any, **kwargs: Any) -> Any: ...


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
        except Exception as exc:
            # Wraps every backend failure (HTTP 404, timeouts, network errors)
            # into a single sentinel so the dispatcher reports the offending
            # name uniformly.
            raise VaultResolutionError(str(raw)) from exc
        resolved.append(r if isinstance(r, UUID) else UUID(str(r)))
    return resolved


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

# --- Vault-scoped (Stream 1) ---

RECALL_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_search',
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
                'description': (
                    'Include stale memory units — facts whose supporting '
                    'evidence has decayed over time. Distinct from superseded '
                    'units (those replaced by a newer note); this tool does '
                    'not currently expose superseded filtering at the tool '
                    'level (default: false).'
                ),
            },
            'intent_class': {
                'type': 'string',
                'enum': _INTENT_ENUM_VALUES,
                'description': ('Filter by intent class. Omit to return all classes.'),
            },
            'risk_class': {
                'type': 'string',
                'enum': _RISK_ENUM_VALUES,
                'description': ('Filter by risk class. Omit to return all classes.'),
            },
            'apply_pre_filter': {
                'type': 'boolean',
                'description': (
                    'Pre-reranker MW/FSFM filter at hydration. Default true drops '
                    'obviously-failed (low Memory Worth) or decayed candidates before '
                    'the cross-encoder. Set false for HISTORICAL / AUDIT / LINEAGE '
                    'queries ("how has my view on X evolved", "show me everything I '
                    'used to think about Y") so contradicted, behaviorally-failed, '
                    'and decayed units appear.'
                ),
            },
        },
        'required': ['query'],
    },
}

RETRIEVE_NOTES_SCHEMA: dict[str, Any] = {
    'name': 'memex_note_search',
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
    'name': 'memex_add_note',
    'description': (
        'Ingest a NEW note into Memex, or fully replace the body of an existing one. '
        'If note_key matches an existing note the content is upserted. For appending '
        'NEW content to a session note (or any existing note) prefer memex_append_note — '
        'it sends only the delta and is atomic, avoiding the full-body re-send. '
        'For structured captures (ADRs, retros, technical briefs, RFCs), call '
        'memex_list_templates first and pass the chosen slug as `template` for '
        'provenance and downstream filtering.'
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
                    'Stable key for upsert — passing an existing key REPLACES '
                    "the note's body. To extend an existing note (e.g. the "
                    'running session note), use `memex_append_note` instead, which '
                    'sends only the delta. Omit for a fresh note.'
                ),
            },
            'template': {
                'type': 'string',
                'description': (
                    'Template slug used to create this note (provenance/filtering '
                    'only — does not scaffold the body). Use memex_list_templates '
                    'to discover slugs. Recommended for ADRs, retros, technical '
                    'briefs, RFCs. Omit to use the default hermes-user-note tag.'
                ),
            },
            'intent_class': {
                'type': 'string',
                'enum': _INTENT_ENUM_VALUES,
                'description': (
                    'Intent override for all extracted facts. "permanent" = enduring '
                    'preferences/conventions (kept indefinitely); "durable" (default) '
                    '= load-bearing facts; "ephemeral" = transient context (decays '
                    'faster). Omit to let the write-time classifier decide.'
                ),
            },
            'risk_class': {
                'type': 'string',
                'enum': _RISK_ENUM_VALUES,
                'description': (
                    'Risk override for all extracted facts. "none" (default); '
                    '"private" = PII/secrets; "sensitive" = restricted topic; '
                    '"safety" = refuse persistence entirely. Omit to let the '
                    'write-time classifier decide.'
                ),
            },
        },
        'required': ['name', 'description', 'content'],
    },
}

APPEND_SCHEMA: dict[str, Any] = {
    'name': 'memex_append_note',
    'description': (
        'Atomically append new content to an existing note. Send ONLY the delta '
        '— the server reads the existing body and concatenates server-side. Use '
        'this in preference to memex_add_note when continuing an in-progress note '
        '(e.g. the running session note): the round-trip cost is the delta size, '
        'not the cumulative body, and concurrent agents on the same note serialise '
        'cleanly. Identify the note by note_key (preferred) or note_id.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'delta': {
                'type': 'string',
                'description': (
                    'New content to append. Just the new snippet — do NOT include '
                    'the existing body. Must not start with `---` (would clash '
                    'with frontmatter detection).'
                ),
            },
            'note_key': {
                'type': 'string',
                'description': (
                    'Stable key the note was created with. Preferred — pass the '
                    'session note key from the system prompt to extend the '
                    'running session note.'
                ),
            },
            'note_id': {
                'type': 'string',
                'description': (
                    'Direct note UUID. Use only when you already have one from a '
                    'prior search; note_key is the preferred identifier.'
                ),
            },
            'append_id': {
                'type': 'string',
                'description': (
                    'Idempotency token (UUID). Reusing the same value with the '
                    'same delta+parent is a safe replay; auto-generated when '
                    'omitted.'
                ),
            },
            'joiner': {
                'type': 'string',
                'description': (
                    "Separator between parent body and delta: 'paragraph' "
                    "(default), 'newline', or 'none'."
                ),
            },
        },
        'required': ['delta'],
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
        'without running expensive searches. Returns '
        '``{"summaries": [...], "errors": [...]}`` so the ``*`` wildcard can '
        'fan out across every vault with per-vault error isolation.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_id': {
                'type': 'string',
                'description': (
                    'Vault UUID or name. Pass "*" to fan out across all '
                    'vaults. Omit to use the session-bound vault.'
                ),
            },
        },
    },
}

FIND_NOTE_SCHEMA: dict[str, Any] = {
    'name': 'memex_find_note',
    'description': (
        'Fuzzy title search for notes. Returns note IDs, titles, and similarity '
        'scores. Use when you know (part of) the title; for content search use '
        'memex_note_search.'
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
        'notes. Use after memex_memory_search to filter results before reading.'
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
        'Batch lookup of memory units (facts, events, observations). Provide '
        'exactly one of `unit_ids` (direct ID lookup) or `chunk_ids` (returns '
        'all units extracted from the named chunks, vault-scoped). Includes '
        'status and supersession info.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of memory unit UUIDs to fetch.',
            },
            'chunk_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'List of chunk UUIDs. Returns all memory units extracted '
                    'from these chunks, scoped to `vault_id`. Mutually '
                    'exclusive with `unit_ids`.'
                ),
            },
            'vault_id': {
                'type': 'string',
                'description': (
                    'Vault UUID or name. Required when `chunk_ids` is set; '
                    'ignored for the `unit_ids` path.'
                ),
            },
        },
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
        'Trace provenance between notes, memory units, observations, and '
        'mental models. '
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
        '**Cascading side-effect:** marking a note `superseded` flags every '
        'memory unit extracted from it as stale. Prefer letting contradiction '
        'detection auto-supersede facts via a new ingested note; reach for '
        'this tool only for explicit archival or when an immediate state '
        'change is required. Optionally link to the replacing/parent note via '
        'linked_note_id.'
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
        'Update the `user_notes` field on an existing note and reprocess it '
        'into the memory graph. Pass null or omit `user_notes` to clear the '
        "field. Note: this is one of the few surfaces where a note's extracted "
        'memory units are deleted rather than superseded — old `user_notes` '
        'memory units are removed and new ones are extracted from the new '
        'text. Use sparingly; for content that should remain auditable, '
        'retain a new note instead.'
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
                'description': ('New `user_notes` text, or null to clear the field.'),
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
        'Fetch a markdown scaffold to follow when writing a structured note. '
        'Call this BEFORE memex_add_note for ADRs, retros, technical briefs, RFCs, '
        'or any note with clear sections. Use memex_list_templates to discover slugs.'
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
        'List note templates (built-in + user-registered). Call this when about to '
        'capture structured content — pick a slug, fetch the body with '
        'memex_get_template, then pass `template=slug` to memex_add_note.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {},
    },
}

REGISTER_TEMPLATE_SCHEMA: dict[str, Any] = {
    'name': 'memex_register_template',
    'description': (
        'Register a custom note template from inline markdown. Use when a recurring '
        'capture pattern (sprint retro, incident postmortem, etc.) does not match a '
        'built-in. Stored in the global scope by default.'
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
        'Retrieve one or more file attachments by path. The bytes are written '
        'to a per-session tempdir on the local filesystem; the tool returns '
        '{path, local_path, filename, mime_type, size_bytes} per asset. The '
        'tool result NEVER contains the bytes inline — read each `local_path` '
        'directly from disk. Per-path failure isolation: failures produce '
        '{path, error} entries interleaved with successful entries. Repeat '
        'fetches of the same path within a session are served from a local '
        'LRU cache. Get paths from memex_list_assets.'
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

RESIZE_IMAGE_SCHEMA: dict[str, Any] = {
    'name': 'memex_resize_image',
    'description': (
        'Resize an image previously fetched into the session asset cache by '
        'memex_get_resources. The `local_path` MUST point inside the session '
        'tempdir — paths outside it are rejected to prevent reading or '
        'writing arbitrary filesystem locations. Allowed input formats are '
        'PNG, JPEG, WEBP, and GIF; SVG, PDF, audio, and other types are '
        'rejected. The resized copy is written as a sibling of the source '
        'inside the same tempdir; the tool returns {local_path, size_bytes} '
        'pointing at the new file.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'local_path': {
                'type': 'string',
                'description': (
                    'Absolute path inside the session asset cache tempdir, '
                    'e.g. a `local_path` returned by memex_get_resources.'
                ),
            },
            'max_width': {
                'type': 'integer',
                'description': 'Maximum width in pixels (default 1280).',
                'default': 1280,
            },
            'max_height': {
                'type': 'integer',
                'description': 'Maximum height in pixels (default 1280).',
                'default': 1280,
            },
            'output_format': {
                'type': 'string',
                'description': (
                    "Optional output format override; one of 'PNG', 'JPEG', "
                    "'WEBP', 'GIF'. Defaults to the input format."
                ),
            },
        },
        'required': ['local_path'],
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
        'Write a namespaced operational fact to the KV store — the canonical '
        'place for preferences, project conventions, and app settings. **Use '
        'this whenever the user asks you to remember anything operational '
        'for future sessions**: personal preferences ("remember I prefer '
        'Neovim"), project conventions ("we use 4-space indentation in this '
        'repo"), cross-project standards ("we standardise on Python 3.12"), '
        'app-specific behaviour ("default to dark theme in Claude Code"), '
        'or learned procedures. Pick the namespace by intent scope:\n'
        '- user:<facet> — personal preference / identity '
        '(e.g. user:editor, user:role)\n'
        '- project:<id>:<facet> — repo/project-bound '
        '(e.g. project:github.com/user/repo:formatter)\n'
        '- global:<facet> — cross-project ecosystem fact '
        '(e.g. global:lang:python:version)\n'
        '- app:<app-id>:<facet> — agent/app-specific behaviour '
        '(e.g. app:claude-code:theme)\n'
        '- procedure:<verb>:<context-tag> — learned how-tos; pair with '
        'memex_record_outcome(target_type="kv_key", kv_key=...) for MW tracking.\n'
        'Generates a semantic embedding for fuzzy lookup. NOT for facts '
        'learned from content; those become memory units via memex_add_note '
        '(or memex_append_note).'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'value': {
                'type': 'string',
                'description': ("The pointer's value (preference, binding, or convention)."),
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
    'description': (
        'Get a KV entry by exact key. Returns null if not found. For '
        'procedure: keys (RFC-007), the default response value is the '
        'unwrapped active procedure text. Pass include_history=true to '
        'receive the structured envelope ({value, version, history}) so '
        'you can review prior versions.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'key': {
                'type': 'string',
                'description': 'Exact key to look up.',
            },
            'include_history': {
                'type': 'boolean',
                'description': (
                    'For procedure: keys (RFC-007), return the full '
                    'envelope (value, version, capped history of 5 prior '
                    'versions) instead of just the active value. Ignored '
                    'for non-procedure keys.'
                ),
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

# --- F32 — Diagnostics ---

GET_DIAGNOSTICS_SUMMARY_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_diagnostics_summary',
    'description': (
        'Vault diagnostics summary: unit counts by status (active/stale/deprioritized), '
        'lint pending counts by type, cluster_count (null on cold cache), avg MW score, '
        'and top-5 retrieved entities. Synchronous — surfaces manifold status without '
        'waiting on UMAP compute.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_id': {
                'type': 'string',
                'description': 'Vault UUID or name.',
            },
        },
        'required': ['vault_id'],
    },
}


TOOLS_MODE_SCHEMAS: list[dict[str, Any]] = [
    # The minimal "primary" tool surface exposed when ``memory_mode='tools'``.
    # Memory-mode 'tools' opts the agent out of briefing + prefetch context,
    # so we hand it only the LLM-reaches-for-most-often verbs and rely on the
    # model to compose them. Keep this list narrow; do NOT auto-grow it when
    # new MCP verbs land — Tier-A's ALL_SCHEMAS expansion silently broke this
    # surface once already (46 tools leaking into tools-mode). New verbs ship
    # in 'hybrid' mode by default; promote one to TOOLS_MODE_SCHEMAS only when
    # there is a deliberate product decision and a paired test update.
    RECALL_SCHEMA,
    RETRIEVE_NOTES_SCHEMA,
    SURVEY_SCHEMA,
    RETAIN_SCHEMA,
    LIST_ENTITIES_SCHEMA,
    GET_ENTITY_MENTIONS_SCHEMA,
    GET_ENTITY_COOCCURRENCES_SCHEMA,
]


ALL_SCHEMAS: list[dict[str, Any]] = [
    # --- Vault-scoped (Stream 1) ---
    RECALL_SCHEMA,
    RETRIEVE_NOTES_SCHEMA,
    SURVEY_SCHEMA,
    RETAIN_SCHEMA,
    APPEND_SCHEMA,
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
    RESIZE_IMAGE_SCHEMA,
    ADD_ASSETS_SCHEMA,
    # --- KV store (Stream 5) ---
    KV_WRITE_SCHEMA,
    KV_GET_SCHEMA,
    KV_SEARCH_SCHEMA,
    KV_LIST_SCHEMA,
    # --- F32 diagnostics ---
    GET_DIAGNOSTICS_SUMMARY_SCHEMA,
    # --- Tier A schemas appended at module bottom (F4 / F5 / F8 / F9 / F20 / F32) ---
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
_MAX_ADD_ASSETS_ITEMS = 20

# Canonical KV key namespaces per RFC-012. Hermes is the ``key`` holder here;
# ``_scope_from_key`` above derives these from stored entries.
_VALID_KV_NAMESPACES = ('global:', 'user:', 'project:', 'app:')


def _serialize_memory_unit(unit: Any) -> dict[str, Any]:
    """Trim a MemoryUnitDTO to the fields useful to the model."""
    metadata = getattr(unit, 'metadata', None) or {}
    is_virtual = bool(metadata.get('virtual')) if isinstance(metadata, dict) else False
    out: dict[str, Any] = {
        'id': str(getattr(unit, 'id', '')),
        'text': getattr(unit, 'text', ''),
        'type': getattr(unit, 'fact_type', None),
        'status': getattr(unit, 'status', None),
        'note_id': str(u) if (u := getattr(unit, 'note_id', None)) else None,
        'mentioned_at': (m.isoformat() if (m := getattr(unit, 'mentioned_at', None)) else None),
    }
    if is_virtual:
        # Synthesized from a MentalModel observation; `id` is a deterministic
        # placeholder, not a DB row — agents must not point-lookup it.
        out['virtual'] = True
        out['mental_model_id'] = metadata.get('mental_model_id')
        out['evidence_ids'] = metadata.get('evidence_ids', [])
    return out


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


def handle_memory_search(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    try:
        query = _require(args, 'query')
    except ValueError as e:
        return tool_error(str(e))

    limit = min(int(args.get('limit') or 10), 50)
    tags = args.get('tags') or None
    vault_ids = _resolve_vault_ids(api, args, vault_id)

    # Allowed sets are canonical in memex_common.schemas (derived from the
    # IntentClass / RiskClass enums). After the local string check we coerce
    # to the enum so we satisfy ``RemoteMemexAPI.search``'s
    # ``IntentClass | None`` / ``RiskClass | None`` signature without relying
    # on implicit Pydantic coercion downstream.
    #
    # Use ``is not None`` rather than truthiness — IntentClass / RiskClass
    # subclass ``str``, so a hypothetical enum member with value ``''`` would
    # be falsy and silently coerce to None. Matches the convention in
    # ``memex_core.server.retrieval``.
    raw_intent = args.get('intent_class')
    if raw_intent is not None and raw_intent not in VALID_INTENT_CLASSES:
        return tool_error(
            f'Invalid intent_class: {raw_intent!r}. '
            f'Valid values: {" | ".join(sorted(VALID_INTENT_CLASSES))}'
        )
    raw_risk = args.get('risk_class')
    if raw_risk is not None and raw_risk not in VALID_RISK_CLASSES:
        return tool_error(
            f'Invalid risk_class: {raw_risk!r}. '
            f'Valid values: {" | ".join(sorted(VALID_RISK_CLASSES))}'
        )
    intent_class = IntentClass(raw_intent) if raw_intent is not None else None
    risk_class = RiskClass(raw_risk) if raw_risk is not None else None

    # F40 — default True. Set False for historical / audit / lineage queries
    # so contradicted, behaviorally-failed, and decayed units appear.
    apply_pre_filter = bool(args.get('apply_pre_filter', True))

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
                apply_pre_filter=apply_pre_filter,
                after=_parse_iso(args.get('after')),
                before=_parse_iso(args.get('before')),
                tags=tags,
                intent_class=intent_class,
                risk_class=risk_class,
            ),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning('memex_memory_search failed: %s', e)
        return tool_error(f'Recall failed: {e}')

    items = [_serialize_memory_unit(u) for u in (results or [])]
    return json.dumps({'count': len(items), 'results': items})


def handle_note_search(
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
        logger.warning('memex_note_search failed: %s', e)
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


def handle_add_note(
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
    template = args.get('template') or HERMES_USER_NOTE_TEMPLATE

    from memex_common.schemas import IntentClass, NoteCreateDTO, RiskClass

    raw_intent = args.get('intent_class')
    raw_risk = args.get('risk_class')
    parsed_intent: IntentClass | None = None
    parsed_risk: RiskClass | None = None
    if raw_intent:
        try:
            parsed_intent = IntentClass(str(raw_intent).lower())
        except ValueError:
            return tool_error(
                f'Invalid intent_class={raw_intent!r}. Allowed: {[c.value for c in IntentClass]}'
            )
    if raw_risk:
        try:
            parsed_risk = RiskClass(str(raw_risk).lower())
        except ValueError:
            return tool_error(
                f'Invalid risk_class={raw_risk!r}. Allowed: {[c.value for c in RiskClass]}'
            )

    dto = NoteCreateDTO(
        name=name,
        description=description,
        content=base64.b64encode(content.encode('utf-8')),
        tags=tags,
        note_key=note_key,
        vault_id=str(vault_id) if vault_id else None,
        author='hermes',
        template=template,
        intent_class=parsed_intent,
        risk_class=parsed_risk,
    )

    try:
        result = run_sync(api.ingest(dto, background=True), timeout=30.0)
    except Exception as e:
        logger.warning('memex_add_note failed: %s', e)
        return tool_error(f'Retain failed: {e}')

    return json.dumps(
        {
            'status': getattr(result, 'status', 'ok'),
            'note_id': getattr(result, 'note_id', None),
            'note_key': note_key,
        }
    )


def handle_append_note(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Atomic delta append for an existing note (issue #56).

    Identify by note_key (preferred) or note_id. Sends only the delta over
    the wire; the server reads the parent body, concatenates, and re-runs
    incremental extraction with the same note_id.

    Vault scope is the session-bound ``vault_id`` parameter (Hermes sessions
    are single-vault by design); any ``vault_id`` in ``args`` is ignored.
    Cross-vault appends require the REST or MCP surfaces.
    """
    from uuid import uuid4

    try:
        delta = _require(args, 'delta')
    except ValueError as e:
        return tool_error(str(e))

    note_key = args.get('note_key') or None
    note_id_raw = args.get('note_id') or None
    if not note_key and not note_id_raw:
        return tool_error('Pass note_key (preferred) or note_id to identify the note to append to.')

    try:
        note_id_uuid = UUID(note_id_raw) if note_id_raw else None
    except (ValueError, TypeError):
        return tool_error(f'note_id is not a valid UUID: {note_id_raw!r}')

    raw_append_id = args.get('append_id') or None
    try:
        append_id = UUID(raw_append_id) if raw_append_id else uuid4()
    except (ValueError, TypeError):
        return tool_error(f'append_id is not a valid UUID: {raw_append_id!r}')

    joiner = args.get('joiner') or 'paragraph'
    user_notes = args.get('user_notes') or None

    request = NoteAppendRequest(
        note_id=note_id_uuid,
        note_key=note_key,
        vault_id=str(vault_id) if vault_id else None,
        delta=delta,
        append_id=append_id,
        joiner=joiner,
        user_notes=user_notes,
    )

    try:
        response = run_sync(api.append_to_note(request), timeout=60.0)
    except Exception as e:
        logger.warning('memex_append_note failed: %s', e)
        return tool_error(f'Append failed: {e}')

    return json.dumps(
        {
            'status': getattr(response, 'status', 'ok'),
            'note_id': str(getattr(response, 'note_id', '') or ''),
            'append_id': str(getattr(response, 'append_id', '') or ''),
            'delta_bytes': getattr(response, 'delta_bytes', 0),
            'new_unit_count': len(getattr(response, 'new_unit_ids', []) or []),
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


def _serialize_vault_summary(summary: Any) -> dict[str, Any]:
    created = getattr(summary, 'created_at', None)
    updated = getattr(summary, 'updated_at', None)
    return {
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


def handle_get_vault_summary(
    api: MemexAPIProtocol, config: HermesMemexConfig, vault_id: UUID | None, args: dict[str, Any]
) -> str:
    raw = args.get('vault_id')
    if not raw and vault_id is None:
        return tool_error('No vault specified and no session-bound vault.')

    # Delegate to _resolve_vault_ids so UUID-parsing, name-resolution, and the
    # ``*`` wildcard expansion all stay in one place. It takes a plural
    # ``vault_ids`` key; we wrap the single arg in a one-element list. When
    # the user passes ``*``, the helper expands to every vault and we fan out
    # below, returning per-vault summaries with per-vault error isolation.
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

    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for target in resolved:
        try:
            summary = run_sync(api.get_vault_summary(target), timeout=30.0)
        except Exception as e:
            logger.warning('memex_get_vault_summary failed for %s: %s', target, e)
            errors.append({'vault_id': str(target), 'error': str(e)})
            continue
        if summary is None:
            errors.append(
                {
                    'vault_id': str(target),
                    'error': (
                        'vault summary not yet generated — it will be computed '
                        'on the next background reflection cycle.'
                    ),
                }
            )
            continue
        summaries.append(_serialize_vault_summary(summary))

    return json.dumps({'summaries': summaries, 'errors': errors})


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

    date_by = args.get('date_by') or 'created_at'
    if date_by not in _VALID_DATE_BY:
        return tool_error(
            f'Invalid date_by: {date_by!r}. Must be one of: {", ".join(sorted(_VALID_DATE_BY))}'
        )

    vault_ids = _resolve_vault_ids(api, args, vault_id)
    limit = min(int(args.get('limit') or 20), 200)

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
    """Fetch memory units by ID or by chunk_id.

    Provide exactly one of ``unit_ids`` or ``chunk_ids``. The chunk-traversal
    path is vault-scoped; an unknown chunk simply contributes nothing to the
    result set.
    """
    raw_unit_ids = args.get('unit_ids')
    raw_chunk_ids = args.get('chunk_ids')

    has_unit_ids = raw_unit_ids is not None
    has_chunk_ids = raw_chunk_ids is not None

    if has_unit_ids == has_chunk_ids:
        return json.dumps(
            {
                'error': (
                    'Provide exactly one of `unit_ids` or `chunk_ids` (not both, not neither).'
                ),
                'results': [],
            }
        )

    items: list[dict[str, Any]] = []

    if has_unit_ids:
        uuids: list[UUID] = []
        for uid_str in raw_unit_ids or []:
            try:
                uuids.append(UUID(str(uid_str)))
            except (ValueError, TypeError):
                continue

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

    chunk_uuids: list[UUID] = []
    for cid_str in raw_chunk_ids or []:
        try:
            chunk_uuids.append(UUID(str(cid_str)))
        except (ValueError, TypeError):
            continue

    if not chunk_uuids:
        return json.dumps({'results': []})

    arg_vault = args.get('vault_id')
    target_vault: UUID | None = None
    if arg_vault:
        # When the caller explicitly passes vault_id, an unparseable value
        # MUST fail fast — silently falling back to the session-bound vault
        # could return data from a different vault than the one the agent
        # asked for (Wave 0 vault-scoping invariant).
        try:
            target_vault = UUID(str(arg_vault))
        except (ValueError, TypeError):
            return tool_error(f'Invalid vault UUID: {arg_vault}')
    else:
        target_vault = vault_id
    if target_vault is None:
        return json.dumps(
            {
                'error': 'vault_id is required when chunk_ids is provided.',
                'results': [],
            }
        )

    try:
        units = run_sync(
            api.get_memory_units_by_chunks(chunk_uuids, target_vault),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('get_memory_units_by_chunks failed: %s', e)
        return json.dumps({'results': []})

    for unit in units or []:
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

    is_virtual = bool(metadata.get('virtual')) if isinstance(metadata, dict) else False
    out: dict[str, Any] = {
        'id': str(getattr(unit, 'id', '')),
        'text': getattr(unit, 'text', ''),
        'fact_type': getattr(unit, 'fact_type', None),
        'status': getattr(unit, 'status', None),
        'note_id': str(n) if (n := getattr(unit, 'note_id', None)) else None,
        'superseded_by': superseded,
        'contradictions': contradictions,
    }
    if is_virtual:
        out['virtual'] = True
        out['mental_model_id'] = metadata.get('mental_model_id')
        out['evidence_ids'] = metadata.get('evidence_ids', [])
    return out


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
    return json.dumps(
        {
            'count': len(results),
            'results': results,
            'next': (
                'memex_get_template(slug) → write content following the structure → '
                'memex_add_note(..., template=slug) so the note is tagged for filtering.'
            ),
        }
    )


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
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
    *,
    asset_cache: SessionAssetCache | None = None,
) -> str:
    """Fetch assets by path; hand off bytes via disk paths in a session tempdir.

    The tool result NEVER contains the bytes inline — agents read each
    ``local_path`` directly. This sidesteps the Hermes harness'
    ``tool_output.max_bytes`` ceiling that would otherwise truncate base64
    payloads. Repeat fetches of the same path within a session are served
    from a per-process LRU cache.
    """
    if asset_cache is None:
        return tool_error('Asset cache is not initialized.')

    try:
        paths = _require(args, 'paths')
    except ValueError as e:
        return tool_error(str(e))

    if not isinstance(paths, list):
        return tool_error("'paths' must be an array of strings")

    if len(paths) > MAX_GET_RESOURCES_PATHS:
        return tool_error(f'Too many paths: {len(paths)} (max {MAX_GET_RESOURCES_PATHS}).')

    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            local_path, mime_type, size_bytes = run_sync(
                asset_cache.get_or_fetch(path, api.get_resource),
                timeout=30.0,
            )
            if size_bytes > MAX_RESOURCE_BYTES:
                asset_cache.invalidate(path)
                results.append(
                    {
                        'path': path,
                        'error': (
                            f'Resource exceeds max size ({size_bytes} > {MAX_RESOURCE_BYTES} bytes)'
                        ),
                    }
                )
                continue
            results.append(
                {
                    'path': path,
                    'local_path': str(local_path),
                    'filename': Path(path).name,
                    'mime_type': mime_type,
                    'size_bytes': size_bytes,
                }
            )
        except Exception as exc:
            results.append({'path': path, 'error': str(exc)})
    return json.dumps({'results': results})


def handle_resize_image(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
    *,
    asset_cache: SessionAssetCache | None = None,
) -> str:
    """Resize an image previously fetched into the session asset cache.

    Path-confinement: ``local_path`` MUST resolve to a file inside the
    session tempdir. Anything that resolves outside (including via ``..``
    traversal) is rejected with ``tool_error`` before Pillow runs.
    """
    if asset_cache is None:
        return tool_error('Asset cache is not initialized.')

    try:
        local_path_arg = _require(args, 'local_path')
    except ValueError as e:
        return tool_error(str(e))

    if not isinstance(local_path_arg, str):
        return tool_error("'local_path' must be a string")

    max_width = args.get('max_width', 1280)
    max_height = args.get('max_height', 1280)
    if not isinstance(max_width, int) or not isinstance(max_height, int):
        return tool_error("'max_width' and 'max_height' must be integers")

    output_format = args.get('output_format')
    if output_format is not None and not isinstance(output_format, str):
        return tool_error("'output_format' must be a string when provided")

    try:
        dest_path, dest_size = validate_and_resize(
            asset_cache,
            local_path_arg,
            max_width=max_width,
            max_height=max_height,
            output_format=output_format,
        )
    except ValueError as exc:
        return tool_error(str(exc))

    return json.dumps({'local_path': str(dest_path), 'size_bytes': dest_size})


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
        # Pre-decode size guard: reject before allocating the decoded bytes.
        # ``len(b64) * 3 // 4`` is a tight upper bound on the decoded size
        # (slight over-estimate when ``=`` padding is present), so a payload
        # whose estimate exceeds the cap is guaranteed to also exceed it after
        # decode. This stops a multi-hundred-MiB base64 string from being
        # allocated in memory before the post-decode check rejects it.
        b64 = a.get('content_b64')
        if not isinstance(b64, str):
            return tool_error(f'Asset {fn!r} has non-string content_b64')
        if (len(b64) * 3) // 4 > MAX_RESOURCE_BYTES:
            return tool_error(
                f'Asset {fn!r} exceeds max size '
                f'(~{(len(b64) * 3) // 4} > {MAX_RESOURCE_BYTES} bytes)'
            )

    filenames = [a['filename'] for a in assets]
    if len(set(filenames)) != len(filenames):
        return tool_error('Duplicate filenames in assets payload.')

    try:
        files: dict[str, bytes] = {}
        for a in assets:
            decoded = base64.b64decode(a['content_b64'], validate=True)
            # Post-decode check kept as defense-in-depth: the pre-check is
            # an upper-bound estimate, so the exact bytes are still verified.
            if len(decoded) > MAX_RESOURCE_BYTES:
                return tool_error(
                    f'Asset {a["filename"]!r} exceeds max size '
                    f'({len(decoded)} > {MAX_RESOURCE_BYTES} bytes)'
                )
            files[a['filename']] = decoded
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


class _StdHandler(Protocol):
    def __call__(
        self,
        api: MemexAPIProtocol,
        config: HermesMemexConfig,
        vault_id: UUID | None,
        args: dict[str, Any],
    ) -> str: ...


class _AssetCacheHandler(Protocol):
    def __call__(
        self,
        api: MemexAPIProtocol,
        config: HermesMemexConfig,
        vault_id: UUID | None,
        args: dict[str, Any],
        *,
        asset_cache: SessionAssetCache | None = None,
    ) -> str: ...


HANDLERS: dict[str, _StdHandler | _AssetCacheHandler] = {
    # --- Vault-scoped (Stream 1) ---
    'memex_memory_search': handle_memory_search,
    'memex_note_search': handle_note_search,
    'memex_survey': handle_survey,
    'memex_add_note': handle_add_note,
    'memex_append_note': handle_append_note,
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
    'memex_resize_image': handle_resize_image,
    'memex_add_assets': handle_add_assets,
    # --- KV store (Stream 5) ---
    'memex_kv_write': handle_kv_write,
    'memex_kv_get': handle_kv_get,
    'memex_kv_search': handle_kv_search,
    'memex_kv_list': handle_kv_list,
    # --- Tier A handlers appended at module bottom (F4 / F5 / F8 / F9 / F20 / F32) ---
}


def dispatch(
    tool_name: str,
    args: dict[str, Any],
    *,
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    asset_cache: SessionAssetCache | None = None,
) -> str:
    handler = HANDLERS.get(tool_name)
    if handler is None:
        return tool_error(f'Unknown tool: {tool_name}')
    try:
        if 'asset_cache' in inspect.signature(handler).parameters:
            return cast(_AssetCacheHandler, handler)(
                api, config, vault_id, args, asset_cache=asset_cache
            )
        return cast(_StdHandler, handler)(api, config, vault_id, args)
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
    'TOOLS_MODE_SCHEMAS',
    'VaultResolutionError',
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
    'RESIZE_IMAGE_SCHEMA',
    # --- KV store (Stream 5) ---
    'KV_GET_SCHEMA',
    'KV_LIST_SCHEMA',
    'KV_SEARCH_SCHEMA',
    'KV_WRITE_SCHEMA',
    # --- F32 diagnostics ---
    'GET_DIAGNOSTICS_SUMMARY_SCHEMA',
    # --- F4 (WS-quick-wins) ---
    'MEMORY_DEPRIORITIZE_SCHEMA',
    'MEMORY_RESTORE_SCHEMA',
    # --- F14 / F29 record_outcome (WS-quick-wins) ---
    'RECORD_OUTCOME_SCHEMA',
    # --- F49 contradiction-graph timeline ---
    'GET_UNIT_HISTORY_SCHEMA',
]


# ============================================================
# Tier A — Hermes sync wrappers
# F4:  WS-quick-wins  (handle_memory_deprioritize, handle_memory_restore)
# F5:  WS-quick-wins  (handle_memory_summarize_node)
# F8:  WS-linter      (handle_get_lint_flags)
# F9:  WS-locks       (handle_memory_reconsolidate, handle_memory_consolidate)
# F32: WS-diagnostics (handle_get_diagnostics_summary)
# ============================================================

# --- F4 ---  (filled by WS-quick-wins)

MEMORY_DEPRIORITIZE_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_deprioritize',
    'description': (
        "Lower a memory unit's retrieval rank without deleting it (NON-DESTRUCTIVE). "
        'Use when a memory is misleading, outdated, or noise that contaminates retrieval. '
        'Companion to memex_memory_restore. Contrast with archive (destructive) — '
        'prefer deprioritize unless the unit must leave the entity graph entirely. '
        '\n\n'
        'When the user reports an issue resolved, follow the §3.5 5-step flow '
        '(see briefing): disambiguate first, route by info quality (Options A/B/C; '
        'Option B requires top_k>=30), mandatory LLM judgment over candidates, then '
        'PAIRED writes — memex_record_outcome(success=false) AND '
        'memex_memory_deprioritize against the LLM-judged-relevant subset only. '
        'The two verbs are orthogonal axes (MW gradient vs binary surface state); '
        'user-confirmed-fix is BOTH signals at once. Imperfect cross-note recall is '
        'by design — exploration is the safety net. For HOW-THINGS-CHANGED '
        'audit queries, route to memex_memory_search(apply_pre_filter=False) instead.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_id': {
                'type': 'string',
                'description': 'Memory unit UUID.',
            },
            'reason': {
                'type': 'string',
                'description': (
                    'Brief text explanation logged to audit_logs (e.g., '
                    "'user confirmed issue fixed', 'superseded by v2.3 release')."
                ),
            },
        },
        'required': ['unit_id', 'reason'],
    },
}
# Note: vault_id is sourced from the Hermes session binding (handler injects
# it from `vault_id` arg); not exposed in the schema since the agent never
# names a vault directly.

MEMORY_RESTORE_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_restore',
    'description': (
        'Restore a previously-deprioritized memory unit. Flips is_deprioritized '
        'back to false; the unit re-enters default-scope retrieval. Writes an '
        'audit_logs row.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_id': {
                'type': 'string',
                'description': 'Memory unit UUID.',
            },
        },
        'required': ['unit_id'],
    },
}


def handle_memory_deprioritize(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        raw_unit_id = _require(args, 'unit_id')
        reason = _require(args, 'reason')
    except ValueError as e:
        return tool_error(str(e))
    try:
        uuid_obj = UUID(str(raw_unit_id))
    except ValueError:
        return tool_error(f'Invalid memory unit UUID: {raw_unit_id}')
    if vault_id is None:
        return tool_error('No vault is bound to this Hermes session; cannot deprioritize.')
    try:
        result = run_sync(
            api.deprioritize_memory_unit(uuid_obj, reason=reason, vault_id=vault_id),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_memory_deprioritize failed: %s', e)
        return tool_error(f'Deprioritize failed: {e}')
    return json.dumps(
        {
            'unit_id': str(getattr(result, 'id', uuid_obj)),
            'is_deprioritized': True,
            'reason': reason,
        }
    )


def handle_memory_restore(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        raw_unit_id = _require(args, 'unit_id')
    except ValueError as e:
        return tool_error(str(e))
    try:
        uuid_obj = UUID(str(raw_unit_id))
    except ValueError:
        return tool_error(f'Invalid memory unit UUID: {raw_unit_id}')
    if vault_id is None:
        return tool_error('No vault is bound to this Hermes session; cannot restore.')
    try:
        result = run_sync(api.restore_memory_unit(uuid_obj, vault_id=vault_id), timeout=30.0)
    except Exception as e:
        logger.warning('memex_memory_restore failed: %s', e)
        return tool_error(f'Restore failed: {e}')
    return json.dumps({'unit_id': str(getattr(result, 'id', uuid_obj)), 'is_deprioritized': False})


HANDLERS['memex_memory_deprioritize'] = handle_memory_deprioritize
HANDLERS['memex_memory_restore'] = handle_memory_restore
ALL_SCHEMAS.extend([MEMORY_DEPRIORITIZE_SCHEMA, MEMORY_RESTORE_SCHEMA])


# --- F5 ---  (filled by WS-quick-wins)

MEMORY_SUMMARIZE_NODE_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_summarize_node',
    'description': (
        'Trigger reflection synchronously on an entity to consolidate scattered or '
        'conflicting memories into a coherent mental model BEFORE continuing. '
        'Synchronous in-session counterpart to background reflect (queued, scheduler-driven). '
        'Rate-limited per (entity, vault); on rejection the response carries '
        "'retry_after_seconds' — do not retry-loop. Use sparingly; reflection is LLM-intensive."
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_id': {
                'type': 'string',
                'description': 'Entity UUID to reflect on.',
            },
            'scope': {
                'type': 'string',
                'enum': ['incremental', 'full'],
                'description': (
                    "'incremental' (default — only new evidence) or 'full' "
                    '(re-evaluate all evidence; capped at 1000 most-recent units).'
                ),
            },
            'vault_id': {
                'type': 'string',
                'description': 'Vault UUID; defaults to the global vault when omitted.',
            },
        },
        'required': ['entity_id'],
    },
}


def handle_memory_summarize_node(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    from memex_common.client import RateLimitExceeded

    try:
        raw_entity_id = _require(args, 'entity_id')
    except ValueError as e:
        return tool_error(str(e))
    try:
        entity_uuid = UUID(str(raw_entity_id))
    except ValueError:
        return tool_error(f'Invalid entity UUID: {raw_entity_id}')

    scope = args.get('scope', 'incremental')
    if scope not in ('incremental', 'full'):
        return tool_error(f"scope must be 'incremental' or 'full', got {scope!r}")

    raw_vault = args.get('vault_id')
    target_vault: UUID | None
    if raw_vault is None:
        target_vault = vault_id
    else:
        try:
            target_vault = run_sync(api.resolve_vault_identifier(str(raw_vault)), timeout=10.0)
        except ValueError as e:
            # Malformed identifier — string failed to parse as a UUID or vault
            # name (typo, bad format). Distinct log line from the KeyError path
            # so operators can grep for "malformed" vs "not found" in
            # log aggregators. User-facing tool_error stays generic for UX.
            logger.warning(
                'memex_memory_summarize_node: vault identifier malformed (input=%r): %s',
                raw_vault,
                e,
            )
            return tool_error('Vault not found or invalid identifier')
        except KeyError as e:
            # Identifier parsed cleanly but no vault with that id/name exists.
            # Distinct log line from the ValueError path; same generic
            # user-facing tool_error message for UX consistency.
            logger.warning(
                'memex_memory_summarize_node: vault identifier parsed but vault '
                'not found (input=%r): %s',
                raw_vault,
                e,
            )
            return tool_error('Vault not found or invalid identifier')
        except TimeoutError:
            # `run_sync` raises `concurrent.futures.TimeoutError` (aliased to
            # the built-in `TimeoutError` in 3.11+) when the 10s budget is
            # exhausted. Surface a distinct message so a stuck backend doesn't
            # masquerade as a generic resolution failure.
            # ORDERING INVARIANT: `TimeoutError` is a subclass of `OSError`
            # (Python 3.3+). This handler MUST stay above any `except OSError:`
            # branch — otherwise OSError would silently intercept timeouts.
            logger.warning(
                'memex_memory_summarize_node: vault resolution timed out for identifier %r',
                raw_vault,
            )
            return tool_error('Vault resolution timed out')
        except Exception:
            # Catch-all for infrastructure failures (HTTP errors, network
            # errors, backend exceptions). Surface a distinct message so
            # genuine connectivity issues don't masquerade as missing-vault
            # errors.
            logger.exception(
                'memex_memory_summarize_node: vault resolution failed for identifier %r',
                raw_vault,
            )
            return tool_error('Failed to resolve vault identifier')

    # Narrow ``target_vault`` to ``UUID``. ``api.resolve_vault_identifier`` is
    # contractually typed ``-> UUID`` (see memex_core.api / memex_common.client),
    # but the protocol stub used here (line 110) types it as ``Any`` and the
    # protocol-level return is ``UUID | None``. ``None`` is not a valid downstream
    # value for this handler, and ``isinstance(None, UUID)`` is ``False`` — so a
    # single ``not isinstance`` check catches both ``None`` and any other unexpected
    # type. Keep the diagnostic detail in the operator log; return a generic
    # user-facing message that matches the obfuscation policy of the surrounding
    # ValueError / KeyError / TimeoutError / generic-Exception paths in this
    # handler instead of leaking the type name to the caller. A regular
    # ``if``/``return`` (not ``assert``) ensures the check survives ``python -O``.
    if not isinstance(target_vault, UUID):
        logger.error(
            'memex_memory_summarize_node: vault resolution returned unexpected '
            'type %s (expected UUID, raw_input=%r)',
            type(target_vault).__name__,
            raw_vault,
        )
        return tool_error('Internal error: vault resolution returned unexpected result')

    try:
        result = run_sync(
            api.summarize_node(entity_uuid, scope=scope, vault_id=target_vault),
            timeout=120.0,
        )
    except RateLimitExceeded as exc:
        return json.dumps(
            {
                'error': 'rate_limit_exceeded',
                'entity_id': str(entity_uuid),
                'retry_after_seconds': exc.retry_after_seconds,
                'message': str(exc),
            }
        )
    except Exception as e:
        logger.warning('memex_memory_summarize_node failed: %s', e)
        return tool_error(f'summarize_node failed: {e}')

    return json.dumps(
        {
            'entity_id': str(getattr(result, 'entity_id', entity_uuid)),
            'observation_count': len(getattr(result, 'new_observations', []) or []),
            'status': getattr(result, 'status', 'completed'),
            'scope': scope,
        }
    )


HANDLERS['memex_memory_summarize_node'] = handle_memory_summarize_node
ALL_SCHEMAS.append(MEMORY_SUMMARIZE_NODE_SCHEMA)


# --- F14 / F29 record_outcome --- (WS-quick-wins; fills the F14 ADD-2 Hermes gap)

RECORD_OUTCOME_SCHEMA: dict[str, Any] = {
    'name': 'memex_record_outcome',
    'description': (
        'Record whether previously retrieved memories or a stored procedure '
        'contributed to a successful outcome. Default mode increments MW '
        "counters on memory units (target_type='memory_unit', "
        "unit_ids=[...]); set target_type='kv_key' with kv_key="
        "'procedure:<verb>:<context-tag>' to score a stored procedure. Call "
        'after you actually used the retrieved memory or the procedure.\n\n'
        'Call generously. Silence provides no learning signal.\n\n'
        'When the user reports an issue resolved, follow the §3.5 5-step flow '
        '(see briefing): disambiguate first, route by info quality (Options '
        'A/B/C; Option B requires top_k>=30), mandatory LLM judgment over '
        'candidates, then PAIRED writes — memex_record_outcome(success=false) '
        'AND memex_memory_deprioritize against the LLM-judged-relevant subset '
        'only. The two verbs are orthogonal axes (MW gradient vs binary surface '
        'state); user-confirmed-fix is BOTH signals at once. Imperfect '
        'cross-note recall is by design — exploration is the safety net. '
        'For HOW-THINGS-CHANGED audit queries, route to '
        'memex_memory_search(apply_pre_filter=False) instead.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'success': {
                'type': 'boolean',
                'description': (
                    'True if the task succeeded using these memories, '
                    'false if they were misleading.'
                ),
            },
            'unit_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'memory_unit mode only. UUIDs of memory units you actually '
                    'used — not all retrieved units, only the ones that were '
                    "load-bearing in your reasoning. Required when target_type='memory_unit'."
                ),
            },
            'vault_id': {
                'type': 'string',
                'description': 'Vault UUID or name. Defaults to the session-bound vault.',
            },
            'outcome_confidence': {
                'type': 'number',
                'minimum': 0.0,
                'maximum': 1.0,
                'description': 'Weight for this outcome signal (0.0-1.0). Default 1.0.',
            },
            'reason': {
                'type': 'string',
                'description': 'Optional free-text reason (logged, not stored on units).',
            },
            'target_type': {
                'type': 'string',
                'enum': ['memory_unit', 'kv_key'],
                'description': (
                    "What the outcome scores. 'memory_unit' (default) increments "
                    "MW counters on memory units in unit_ids. 'kv_key' "
                    'increments counters on the procedure_outcomes row for kv_key.'
                ),
            },
            'kv_key': {
                'type': 'string',
                'description': (
                    'kv_key mode only. Procedure KV key '
                    "(procedure:<verb>:<context-tag>). Required when target_type='kv_key'."
                ),
            },
        },
        'required': ['success'],
    },
}


def handle_record_outcome(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Dual-mode dispatcher (memory_unit / kv_key).

    Preserves the ADD-2 invariant by passing ``unit_ids`` and ``success``
    positionally and ``target_type`` / ``kv_key`` keyword-only — same shape
    as MemexAPI.record_outcome and RemoteMemexAPI.record_outcome.
    """
    try:
        success = _require(args, 'success')
    except ValueError as e:
        return tool_error(str(e))
    if not isinstance(success, bool):
        return tool_error(f"'success' must be a boolean, got {type(success).__name__}")

    target_type = args.get('target_type', 'memory_unit')
    if target_type not in ('memory_unit', 'kv_key'):
        return tool_error(f"target_type must be 'memory_unit' or 'kv_key', got {target_type!r}")

    unit_ids = args.get('unit_ids')
    if unit_ids is not None and not isinstance(unit_ids, list):
        return tool_error("'unit_ids' must be a list of UUID strings")

    kv_key = args.get('kv_key')
    if kv_key is not None and not isinstance(kv_key, str):
        return tool_error("'kv_key' must be a string")

    raw_vault = args.get('vault_id')
    target_vault: str | None
    if raw_vault is None:
        target_vault = str(vault_id) if vault_id else None
    else:
        target_vault = str(raw_vault)

    outcome_confidence = args.get('outcome_confidence', 1.0)
    reason = args.get('reason')

    try:
        result = run_sync(
            api.record_outcome(
                unit_ids,
                success,
                target_vault,
                outcome_confidence,
                reason,
                target_type=target_type,
                kv_key=kv_key,
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_record_outcome failed: %s', e)
        return tool_error(f'record_outcome failed: {e}')

    return json.dumps(result)


HANDLERS['memex_record_outcome'] = handle_record_outcome
ALL_SCHEMAS.append(RECORD_OUTCOME_SCHEMA)


# --- F8 ---  (filled by WS-linter)

GET_LINT_FLAGS_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_lint_flags',
    'description': (
        'memex_get_lint_flags — List pending memory-hygiene findings the linter has detected.\n'
        'Use periodically (e.g., once per long session) or when the user asks about memory state.\n'
        '\n'
        '- vault_id (optional): scope to a single vault. Defaults to the active write vault '
        'when omitted (Wave 0 vault-scoping invariant — never falls through to a global '
        'all-vault view).\n'
        '- lint_type (optional): structural | quality | governance | schema\n'
        '- status (optional): pending | resolved | dismissed (default: pending)\n'
        '- limit (default 20)\n'
        '\n'
        'Each finding includes: target_id, lint_type, evidence (why detected), suggested_action.\n'
        'Most findings can be auto-resolved by calling the relevant tool (e.g., memory_deprioritize\n'
        'for low-MW units). Surface high-confidence findings to the user; act autonomously on\n'
        'low-risk ones (deprioritize, mark stale).'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_id': {
                'type': 'string',
                'description': (
                    'Vault UUID or name. Omit to default to the session active write vault.'
                ),
            },
            'lint_type': {
                'type': 'string',
                'enum': ['structural', 'quality', 'governance', 'schema'],
            },
            'status': {
                'type': 'string',
                'enum': ['pending', 'resolved', 'dismissed'],
                'default': 'pending',
            },
            'limit': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 200,
                'default': 20,
            },
            'cursor': {
                'type': 'string',
                'description': 'Opaque cursor from a prior page; omit on first call.',
            },
        },
        'required': [],
    },
}


def handle_get_lint_flags(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Sync wrapper around RemoteMemexAPI.lint_get_flags.

    HIGH-006: never falls through to an all-vault view. Either the agent
    supplies vault_id, or the Hermes session vault binding is used. If
    neither is available, the call is rejected.
    """
    raw_vault = args.get('vault_id')
    resolved: str | None = None
    try:
        if raw_vault:
            resolved_id = run_sync(api.resolve_vault_identifier(raw_vault), timeout=10.0)
            resolved = str(resolved_id) if resolved_id else None
        elif vault_id is not None:
            resolved = str(vault_id)
        if resolved is None:
            return tool_error(
                'memex_get_lint_flags requires a vault_id or an active session '
                'vault binding (Wave 0 vault-scoping invariant; refusing to '
                'fall through to a global all-vault view).'
            )
        result = run_sync(
            api.lint_get_flags(
                vault_id=resolved,
                lint_type=args.get('lint_type'),
                status=args.get('status', 'pending'),
                limit=int(args.get('limit', 20)),
                cursor=args.get('cursor'),
            ),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_get_lint_flags failed: %s', e)
        return tool_error(f'Lint flags query failed: {e}')

    return json.dumps(result, default=str)


HANDLERS['memex_get_lint_flags'] = handle_get_lint_flags
ALL_SCHEMAS.append(GET_LINT_FLAGS_SCHEMA)


LINT_APPLY_WINNER_SCHEMA: dict[str, Any] = {
    'name': 'memex_lint_apply_winner',
    'description': (
        'Apply the recommended action on a winner-proposal lint finding '
        "(rule_name=propose_contradiction_winner). The finding's "
        'evidence.action drives the mutation: mark a unit stale, mark a '
        'note superseded, rewrite a contradicts link as refines, or a '
        'no-op write when inconclusive. Captures prior_state so the change '
        'is reversible via memex_lint_reverse_winner.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'finding_id': {
                'type': 'string',
                'description': 'UUID of the pending winner-proposal finding.',
            },
        },
        'required': ['finding_id'],
    },
}


LINT_REVERSE_WINNER_SCHEMA: dict[str, Any] = {
    'name': 'memex_lint_reverse_winner',
    'description': (
        'Reverse a previously applied winner-proposal lint finding. Restores '
        'the row(s) recorded under evidence.resolution.prior_state and '
        'writes a paired audit row. The original finding stays resolved.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'finding_id': {
                'type': 'string',
                'description': (
                    'UUID of the previously applied (status=resolved) '
                    'winner-proposal finding to reverse.'
                ),
            },
        },
        'required': ['finding_id'],
    },
}


def handle_lint_apply_winner(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Sync wrapper around RemoteMemexAPI.lint_apply_winner."""
    finding_id = args.get('finding_id')
    if not finding_id:
        return tool_error('memex_lint_apply_winner requires finding_id')
    try:
        result = run_sync(api.lint_apply_winner(str(finding_id)), timeout=30.0)
    except Exception as e:
        logger.warning('memex_lint_apply_winner failed: %s', e)
        return tool_error(f'Lint apply failed: {e}')
    return json.dumps(result, default=str)


def handle_lint_reverse_winner(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Sync wrapper around RemoteMemexAPI.lint_reverse_winner."""
    finding_id = args.get('finding_id')
    if not finding_id:
        return tool_error('memex_lint_reverse_winner requires finding_id')
    try:
        result = run_sync(api.lint_reverse_winner(str(finding_id)), timeout=30.0)
    except Exception as e:
        logger.warning('memex_lint_reverse_winner failed: %s', e)
        return tool_error(f'Lint reverse failed: {e}')
    return json.dumps(result, default=str)


HANDLERS['memex_lint_apply_winner'] = handle_lint_apply_winner
HANDLERS['memex_lint_reverse_winner'] = handle_lint_reverse_winner
ALL_SCHEMAS.append(LINT_APPLY_WINNER_SCHEMA)
ALL_SCHEMAS.append(LINT_REVERSE_WINNER_SCHEMA)


# --- F9 ---  (filled by WS-locks)

MEMORY_RECONSOLIDATE_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_reconsolidate',
    'description': (
        'Re-evaluate memories for a specific entity, detecting contradictions and '
        'updating mental models. Use when retrieved facts about an entity disagree. '
        'Runs contradiction detection across all units linked to the entity, then '
        'triggers reflection. ENTITY-SCOPED counterpart to memex_memory_consolidate '
        '(vault-wide). LLM-intensive — use only on concrete evidence of conflict.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'entity_id': {
                'type': 'string',
                'description': 'Entity UUID to reconsolidate.',
            },
            'vault_id': {
                'type': 'string',
                'description': 'Vault UUID — required for vault-scoped unit resolution.',
            },
        },
        'required': ['entity_id', 'vault_id'],
    },
}


MEMORY_CONSOLIDATE_SCHEMA: dict[str, Any] = {
    'name': 'memex_memory_consolidate',
    'description': (
        'Vault-wide batch curation. Identifies low-MW + stale units and '
        'deprioritizes them; writes findings to the maintenance ledger. '
        'VAULT-SCOPED counterpart to memex_memory_reconsolidate (per-entity). '
        'Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer '
        'memex_memory_reconsolidate.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'vault_id': {
                'type': 'string',
                'description': 'Vault UUID to consolidate.',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'If true, preview without making changes.',
            },
        },
        'required': ['vault_id'],
    },
}


def handle_memory_reconsolidate(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        raw_entity = _require(args, 'entity_id')
        raw_vault = _require(args, 'vault_id')
    except ValueError as e:
        return tool_error(str(e))
    try:
        entity_uuid = UUID(str(raw_entity))
    except ValueError:
        return tool_error(f'Invalid entity UUID: {raw_entity}')
    try:
        vault_uuid = UUID(str(raw_vault))
    except ValueError:
        return tool_error(f'Invalid vault UUID: {raw_vault}')
    try:
        result = run_sync(api.reconsolidate_entity(entity_uuid, vault_uuid), timeout=120.0)
    except Exception as e:
        logger.warning('memex_memory_reconsolidate failed: %s', e)
        return tool_error(f'Reconsolidate failed: {e}')
    return json.dumps(result, default=str)


def handle_memory_consolidate(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        raw_vault = _require(args, 'vault_id')
    except ValueError as e:
        return tool_error(str(e))
    try:
        vault_uuid = UUID(str(raw_vault))
    except ValueError:
        return tool_error(f'Invalid vault UUID: {raw_vault}')
    dry_run = bool(args.get('dry_run', False))
    try:
        result = run_sync(api.consolidate_vault(vault_uuid, dry_run=dry_run), timeout=300.0)
    except Exception as e:
        logger.warning('memex_memory_consolidate failed: %s', e)
        return tool_error(f'Consolidate failed: {e}')
    return json.dumps(result, default=str)


HANDLERS['memex_memory_reconsolidate'] = handle_memory_reconsolidate
HANDLERS['memex_memory_consolidate'] = handle_memory_consolidate
ALL_SCHEMAS.extend([MEMORY_RECONSOLIDATE_SCHEMA, MEMORY_CONSOLIDATE_SCHEMA])


# --- F32 --- (filled by WS-diagnostics)
def handle_get_diagnostics_summary(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    """Sync wrapper around RemoteMemexAPI.get_diagnostics_summary."""
    raw = args.get('vault_id')
    if not raw and vault_id is None:
        return tool_error('No vault specified and no session-bound vault.')

    try:
        if raw:
            target = run_sync(api.resolve_vault_identifier(raw), timeout=10.0)
        else:
            target = vault_id
        summary = run_sync(api.get_diagnostics_summary(target), timeout=30.0)
    except Exception as e:
        logger.warning('memex_get_diagnostics_summary failed: %s', e)
        return tool_error(f'Diagnostics summary failed: {e}')

    return json.dumps(summary)


HANDLERS['memex_get_diagnostics_summary'] = handle_get_diagnostics_summary


# --- F49 contradiction-graph timeline ---

GET_UNIT_HISTORY_SCHEMA: dict[str, Any] = {
    'name': 'memex_get_unit_history',
    'description': (
        'Walk the contradiction graph backward (newer -> older) from a memory '
        'unit, returning its supersession history as a tree. Use for '
        '"how has my view on X evolved" / audit / lineage queries. v1 '
        'returns supersession history (negative-evidence path: contradicts / '
        'weakens links), NOT full confidence evolution. A future forward=True '
        'extension can walk reinforces separately. No reranker, no boosts, '
        'no quality filtering — graph walk is for completeness, not relevance.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'unit_id': {
                'type': 'string',
                'description': 'Memory unit UUID to start the walk from (root, depth=0).',
            },
            'max_depth': {
                'type': 'integer',
                'description': (
                    'Maximum recursion depth (default: 10). Nodes reached at '
                    'the cap are returned with truncated=True.'
                ),
            },
        },
        'required': ['unit_id'],
    },
}
# Note: vault_id is sourced from the Hermes session binding (handler injects
# it from the `vault_id` arg); not exposed in the schema since the agent never
# names a vault directly.


def handle_get_unit_history(
    api: MemexAPIProtocol,
    config: HermesMemexConfig,
    vault_id: UUID | None,
    args: dict[str, Any],
) -> str:
    try:
        raw_unit_id = _require(args, 'unit_id')
    except ValueError as e:
        return tool_error(str(e))
    try:
        uuid_obj = UUID(str(raw_unit_id))
    except (ValueError, TypeError):
        return tool_error(f'Invalid memory unit UUID: {raw_unit_id}')
    if vault_id is None:
        return tool_error('No vault is bound to this Hermes session; cannot walk history.')

    raw_depth = args.get('max_depth')
    try:
        max_depth = int(raw_depth) if raw_depth is not None else 10
    except (TypeError, ValueError):
        return tool_error(f'Invalid max_depth: {raw_depth!r}')
    if max_depth < 0:
        return tool_error('max_depth must be >= 0.')

    try:
        result = run_sync(
            api.get_unit_history(uuid_obj, vault_id=vault_id, max_depth=max_depth),
            timeout=30.0,
        )
    except Exception as e:
        logger.warning('memex_get_unit_history failed: %s', e)
        return tool_error(f'Get unit history failed: {e}')

    dump = getattr(result, 'model_dump', None)
    if callable(dump):
        return json.dumps(dump(mode='json'))
    return json.dumps(result)


HANDLERS['memex_get_unit_history'] = handle_get_unit_history
ALL_SCHEMAS.append(GET_UNIT_HISTORY_SCHEMA)
