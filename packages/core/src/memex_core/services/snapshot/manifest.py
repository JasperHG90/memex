"""Snapshot manifest model.

The manifest declares snapshot version, source vault identity, alembic head,
embedding-model identity, and per-table row counts. Downstream consumers
read this to decide whether they can import the snapshot.

Snapshot SemVer:
- MAJOR: removed/renamed field on any export model, or restructured layout.
- MINOR: added field on an export model, or new file in the layout.
- PATCH: documentation-only changes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SNAPSHOT_VERSION = '1.2.0'
OBSERVATION_SCHEMA_VERSION = '1'


class SnapshotVersion(BaseModel):
    """Parsed SemVer for snapshot format compatibility checks."""

    model_config = ConfigDict(frozen=True)

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> SnapshotVersion:
        parts = value.split('.')
        if len(parts) != 3:
            raise ValueError(f'Invalid snapshot version {value!r}; expected MAJOR.MINOR.PATCH')
        try:
            return cls(major=int(parts[0]), minor=int(parts[1]), patch=int(parts[2]))
        except ValueError as e:
            raise ValueError(f'Invalid snapshot version {value!r}: {e}') from e

    def __str__(self) -> str:
        return f'{self.major}.{self.minor}.{self.patch}'


class EmbeddingModelIdentity(BaseModel):
    """Identifies the embedding model used at export time.

    On import (V12), this is checked against the importing server's
    configured embedding model. Mismatch refuses import — embeddings
    regenerated with a different model would not be comparable to the
    rest of the imported state.

    The ``hash`` is a content-hash of the model weights when available
    (e.g. fastembed local checkpoints expose a stable identifier);
    falls back to an empty string when the backend doesn't expose one.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Model identifier, e.g. ``all-MiniLM-L6-v2``.')
    dim: int = Field(description='Output embedding dimension.', gt=0)
    hash: str = Field(default='', description='Optional content hash of the model weights.')


class SnapshotManifest(BaseModel):
    """The manifest.json contents."""

    model_config = ConfigDict(extra='forbid')

    snapshot_version: str = Field(
        default=SNAPSHOT_VERSION,
        description='SemVer for the snapshot format.',
    )
    source_vault_id: UUID = Field(description='ID of the vault that was exported.')
    source_vault_name: str = Field(description='Name of the vault at export time.')
    exported_at: datetime = Field(
        description='UTC timestamp when the export was written. ISO-8601 with +00:00 offset.',
    )
    alembic_head: str = Field(
        description='Alembic migration head of the source DB at export time.',
    )
    embedding_model: EmbeddingModelIdentity = Field(
        description='Embedding-model identity (v1.1+).',
    )
    observation_schema_version: str = Field(
        default=OBSERVATION_SCHEMA_VERSION,
        description='Schema version of MentalModel.observations entries (v1.1+).',
    )
    table_counts: dict[str, int] = Field(
        default_factory=dict,
        description='Per-table row counts in this snapshot (post-filter).',
    )
