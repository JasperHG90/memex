from uuid import UUID
from pydantic import BaseModel, Field


class VaultMixin(BaseModel):
    """Mixin to add vault_id to a Pydantic model."""

    vault_id: UUID | str | None = Field(
        default=None,
        description='The UUID or name of the vault this record belongs to.',
    )
