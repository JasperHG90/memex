"""Pydantic import models — mirror V3 export models with forward-compat MINOR.

V12 reads snapshots written by V3. The export models (export_models.py) use
``extra='forbid'`` to catch column drift on export. Import needs the inverse:
``extra='ignore'`` so a V3 v1.2 snapshot (with one new field) imports cleanly
on a V12 pinned to v1.x — the contract from manifest.py is "same MAJOR +
same-or-higher MINOR accepts; downstream consumer ignores unknown fields."

Mirrors export_models field-for-field; if a new field is added there, mirror
it here in the same MINOR bump.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# Pinned MAJOR. import_snapshot refuses on a different MAJOR; same MAJOR +
# higher MINOR is accepted via extra='ignore'; same MAJOR + lower MINOR is
# refused (older importer can't be sure newer fields aren't load-bearing).
PINNED_SNAPSHOT_MAJOR = 1
PINNED_SNAPSHOT_MINOR = 2


def _import_config() -> ConfigDict:
    return ConfigDict(extra='ignore')


class _ImportBase(BaseModel):
    model_config = _import_config()


class VaultImport(_ImportBase):
    id: UUID
    name: str
    description: str | None = None
    mw_mode: str
    created_at: datetime


class NoteImport(_ImportBase):
    id: UUID
    vault_id: UUID
    session_id: str
    title: str | None = None
    description: str | None = None
    page_index: dict[str, Any] | None = None
    content_hash: str | None = None
    filestore_path: str | None = None
    assets: list[str] = Field(default_factory=list)
    doc_metadata: dict[str, Any] = Field(default_factory=dict)
    publish_date: datetime | None = None
    status: str
    superseded_by: UUID | None = None
    appended_to: UUID | None = None
    archived_at: datetime | None = None
    summary_version_incorporated: int | None = None
    created_at: datetime
    updated_at: datetime


class NoteAppendImport(_ImportBase):
    append_id: UUID
    note_id: UUID
    delta_sha256: str
    delta_bytes: int
    joiner: str
    resulting_content_hash: str
    new_unit_ids: list[UUID] = Field(default_factory=list)
    applied_at: datetime


class ChunkImport(_ImportBase):
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


class NodeImport(_ImportBase):
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


class MemoryUnitImport(_ImportBase):
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


class MemoryLinkImport(_ImportBase):
    from_unit_id: UUID
    to_unit_id: UUID
    link_type: str
    vault_id: UUID
    entity_id: UUID | None = None
    link_metadata: dict[str, Any] = Field(default_factory=dict)
    weight: float
    created_at: datetime


class EntityImport(_ImportBase):
    id: UUID
    canonical_name: str
    phonetic_code: str | None = None
    entity_type: str | None = None
    first_seen: datetime
    last_seen: datetime
    mention_count: int
    retrieval_count: int
    last_retrieved_at: datetime | None = None


class EntityAliasImport(_ImportBase):
    id: UUID
    canonical_id: UUID
    name: str
    phonetic_code: str | None = None


class UnitEntityImport(_ImportBase):
    unit_id: UUID
    entity_id: UUID
    vault_id: UUID
    success_co_count: int
    failure_co_count: int


class EntityCooccurrenceImport(_ImportBase):
    entity_id_1: UUID
    entity_id_2: UUID
    vault_id: UUID
    cooccurrence_count: int
    last_cooccurred: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class MentalModelImport(_ImportBase):
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


class VaultSummaryImport(_ImportBase):
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


class MaintenanceProposalImport(_ImportBase):
    id: UUID
    vault_id: UUID
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


class EmbeddingModelIdentityImport(_ImportBase):
    """Mirror of ``EmbeddingModelIdentity`` with forward-compat."""

    name: str
    dim: int
    hash: str = ''


class SnapshotManifestImport(_ImportBase):
    """Mirror of ``SnapshotManifest`` with ``extra='ignore'``.

    The export-side ``SnapshotManifest`` declares ``extra='forbid'`` so a
    future MINOR-bumped manifest crashes when read by the V12 importer
    BEFORE ``_check_version`` ever runs — defeating the documented
    forward-compat contract. This import-side mirror ignores extra fields
    so MINOR-bumped snapshots remain importable on the current pinned importer.
    """

    snapshot_version: str
    source_vault_id: UUID
    source_vault_name: str
    exported_at: datetime
    alembic_head: str
    embedding_model: EmbeddingModelIdentityImport
    observation_schema_version: str
    table_counts: dict[str, int] = Field(default_factory=dict)


class ObservationV1(_ImportBase):
    """MentalModel.observations entry — frozen v1 shape (Decision 15).

    Mirrors ``memex_core.memory.sql_models.Observation``. Used as a
    post-import sanity check: with ``extra='forbid'`` any unexpected field
    is surfaced as a warning (not a refusal) so V3 v1.2+ snapshots remain
    importable.
    """

    model_config = ConfigDict(extra='forbid')

    id: UUID
    title: str
    content: str
    trend: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
