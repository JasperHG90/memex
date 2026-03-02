from uuid import UUID

from pydantic import BaseModel, Field
from memex_core.memory.sql_models import MentalModel, Observation
from memex_core.config import GLOBAL_VAULT_ID


class ReflectionRequest(BaseModel):
    """
    Request to run the reflection loop on a specific entity.
    """

    entity_id: UUID = Field(description='The UUID of the entity to reflect upon.')
    limit_recent_memories: int = Field(
        default=20, description='Number of recent memories to consider.'
    )
    vault_id: UUID = Field(
        default=GLOBAL_VAULT_ID,
        description='The UUID of the vault this reflection is scoped to.',
    )


class ReflectionResult(BaseModel):
    """Result of a reflection cycle."""

    entity_id: UUID = Field(description='The UUID of the entity that was analyzed.')

    new_observations: list[Observation] = Field(
        description='List of new observations synthesized from recent memories.'
    )

    updated_model: MentalModel = Field(
        description='The updated mental model containing the new observations.'
    )
