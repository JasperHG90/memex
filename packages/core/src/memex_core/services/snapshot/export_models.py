"""Pydantic export models — one per restored table.

These mirror the SQLModel column inventory at export time. Two reasons not
to dump SQLModels directly:

1. ``model_dump()`` on a SQLModel auto-leaks any new column added in a
   future migration, defeating the snapshot version contract. Adding a
   column here is an explicit decision that bumps the snapshot MINOR.
2. Several SQLModel columns are NOT exportable: vector embeddings (3-5KB
   of opaque float lists, regenerated on import) and Postgres GENERATED
   columns (e.g. ``MemoryUnit.search_tsvector`` — auto-recomputed).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def _model_config() -> ConfigDict:
    return ConfigDict(extra='forbid')


class _ExportBase(BaseModel):
    model_config = _model_config()


class VaultExport(_ExportBase):
    id: UUID
    name: str
    description: str | None = None
    mw_mode: str
    created_at: datetime


class NoteExport(_ExportBase):
    """Note metadata.

    ``filestore_path`` and ``assets`` are rewritten by the exporter to the
    relative paths inside the snapshot dir (``./assets/<filename>``).
    The exporter writes the actual asset bytes alongside under
    ``notes/<dir>/assets/<filename>``.

    The note's ``original_text`` is exported as ``note.md`` next to this
    metadata file rather than included inline; that keeps the JSON small
    and human-greppable.
    """

    id: UUID
    vault_id: UUID
    session_id: str
    title: str | None = None
    description: str | None = None
    page_index: dict[str, Any] | None = None
    content_hash: str | None = None
    filestore_path: str | None = Field(
        default=None,
        description='Relative path under the snapshot dir (e.g. ./content.bin) when the note has an associated file.',
    )
    assets: list[str] = Field(
        default_factory=list,
        description='Relative paths to assets under ./assets/.',
    )
    doc_metadata: dict[str, Any] = Field(default_factory=dict)
    publish_date: datetime | None = None
    status: str
    superseded_by: UUID | None = None
    appended_to: UUID | None = None
    archived_at: datetime | None = None
    summary_version_incorporated: int | None = None
    created_at: datetime
    updated_at: datetime


class NoteAppendExport(_ExportBase):
    append_id: UUID
    note_id: UUID
    delta_sha256: str
    delta_bytes: int
    joiner: str
    resulting_content_hash: str
    new_unit_ids: list[UUID] = Field(default_factory=list)
    applied_at: datetime


class ChunkExport(_ExportBase):
    """Chunks. Embedding column is regenerated on import; not exported."""

    id: UUID
    vault_id: UUID
    note_id: UUID
    text: str
    content_hash: str
    status: str
    chunk_index: int
    summary: dict[str, Any] | None = None
    summary_formatted: str | None = None
    created_at: datetime


class NodeExport(_ExportBase):
    id: UUID
    vault_id: UUID
    note_id: UUID
    block_id: UUID | None = None
    node_hash: str
    title: str
    text: str
    summary: dict[str, Any] | None = None
    summary_formatted: str | None = None
    level: int
    seq: int
    token_estimate: int
    status: str
    created_at: datetime


class MemoryUnitExport(_ExportBase):
    """MemoryUnit. Embedding + search_tsvector are NOT exported.

    MW counters (``is_deprioritized``, ``success_co_count``,
    ``failure_co_count``) are load-bearing for FSFM scoring; preserving
    them is the whole point of the snapshot. Confidence + evidence-count
    are similarly load-bearing for contradiction scoring.
    """

    id: UUID
    vault_id: UUID
    note_id: UUID | None = None
    chunk_id: UUID | None = None
    text: str
    fact_type: str
    status: str
    event_date: datetime
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None
    mentioned_at: datetime | None = None
    context: str | None = None
    is_deprioritized: bool
    success_co_count: int
    failure_co_count: int
    intent_class: str
    risk_class: str
    confidence: float
    confidence_evidence_count: int
    importance: float | None = None
    stability: float | None = None
    last_outcome_at: datetime | None = None
    unit_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MemoryLinkExport(_ExportBase):
    """MemoryLink. Composite PK ``(from_unit_id, to_unit_id, link_type)``."""

    from_unit_id: UUID
    to_unit_id: UUID
    link_type: str
    vault_id: UUID
    entity_id: UUID | None = None
    link_metadata: dict[str, Any] = Field(default_factory=dict)
    weight: float
    created_at: datetime


class EntityExport(_ExportBase):
    """GLOBAL table — no vault_id."""

    id: UUID
    canonical_name: str
    phonetic_code: str | None = None
    entity_type: str | None = None
    first_seen: datetime
    last_seen: datetime
    mention_count: int
    retrieval_count: int
    last_retrieved_at: datetime | None = None


class EntityAliasExport(_ExportBase):
    """GLOBAL table — no vault_id."""

    id: UUID
    canonical_id: UUID
    name: str
    phonetic_code: str | None = None


class UnitEntityExport(_ExportBase):
    unit_id: UUID
    entity_id: UUID
    vault_id: UUID
    success_co_count: int
    failure_co_count: int


class EntityCooccurrenceExport(_ExportBase):
    entity_id_1: UUID
    entity_id_2: UUID
    vault_id: UUID
    cooccurrence_count: int
    last_cooccurred: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class MentalModelExport(_ExportBase):
    """``observations`` is round-tripped through the frozen
    ``Observation`` v1 schema (see manifest ``observation_schema_version``).
    Embedding is regenerated on import.
    """

    id: UUID
    vault_id: UUID
    entity_id: UUID
    name: str
    observations: list[dict[str, Any]] = Field(default_factory=list)
    entity_metadata: dict[str, Any] = Field(default_factory=dict)
    last_refreshed: datetime
    version: int
    success_co_count: int
    failure_co_count: int


class VaultSummaryExport(_ExportBase):
    id: UUID
    vault_id: UUID
    narrative: str
    themes: list[dict[str, Any]] = Field(default_factory=list)
    inventory: dict[str, Any] = Field(default_factory=dict)
    key_entities: list[dict[str, Any]] = Field(default_factory=list)
    version: int
    notes_incorporated: int
    patch_log: list[dict[str, Any]] = Field(default_factory=list)
    needs_regeneration: bool
    created_at: datetime
    updated_at: datetime


class MaintenanceProposalExport(_ExportBase):
    id: UUID
    vault_id: UUID = Field(
        description='vault_id is nullable in the schema (NULL = global), but the per-vault export filter excludes NULLs; see Decision 13.',
    )
    lint_type: str
    target_type: str
    target_id: str
    rule_name: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_action: str
    status: str
    source: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
