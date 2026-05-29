from sqlalchemy import Column, ForeignKey, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.types import Uuid as SA_UUID
from sqlmodel import Field
from memex_core.config import GLOBAL_VAULT_ID


def vault_id_field(*, primary_key: bool = False):
    """Returns a new Field for vault_id to avoid sharing Column objects.

    Pass ``primary_key=True`` for tables whose grain includes the vault (e.g.
    EntityCooccurrence, where the same entity pair is tracked per-vault).
    """
    return Field(
        default=GLOBAL_VAULT_ID,
        sa_column=Column(
            SA_UUID(),
            ForeignKey('vaults.id', ondelete='CASCADE'),
            primary_key=primary_key,
            index=True,
        ),
        description='The UUID of the vault this record belongs to. Defaults to Global Vault.',
    )


def created_at_field():
    """Returns a new Field for created_at."""
    return Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the record was created.',
    )


def updated_at_field():
    """Returns a new Field for updated_at."""
    return Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()),
        description='Timestamp when the record was last updated.',
    )
