from uuid import UUID

from pydantic import BaseModel, Field
from memex_core.memory.sql_models import MentalModel, Observation, MemoryUnit
from memex_core.config import GLOBAL_VAULT_ID


class OpinionFormationRequest(BaseModel):
    """
    Request to form opinions based on an interaction.
    """

    query: str = Field(description="The user's original query.")
    context: list[MemoryUnit] = Field(
        description='Optional structured context with full memory units.'
    )
    answer: str = Field(description="The agent's final answer.")
    agent_name: str = Field(default='reasoning_agent', description='The identity of the agent.')
    vault_id: UUID = Field(
        default=GLOBAL_VAULT_ID,
        description='The UUID of the vault where this reasoning takes place.',
    )


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
