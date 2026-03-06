from uuid import UUID
from typing import Any, Self
from pydantic import Field, model_validator
from sqlmodel import SQLModel

VALID_STRATEGIES = frozenset({'semantic', 'keyword', 'graph', 'temporal', 'mental_model'})


class RetrievalRequest(SQLModel):
    """
    Unified request object for memory retrieval.
    """

    query: str = Field(..., description='The search query or context string.')
    limit: int = Field(
        default=10,
        description='Maximum number of results to return. Ignored when token_budget is set.',
    )
    token_budget: int | None = Field(
        default=None,
        description=(
            'Maximum token budget for results (greedy packing). '
            'When set, token_budget is the sole constraint — limit is ignored.'
        ),
    )

    # Scoping
    vault_ids: list[UUID] | None = Field(
        default=None,
        description='List of specific vault IDs to search. If None or empty, searches ALL vaults (Global + All Projects).',
    )
    filters: dict[str, Any] = Field(
        default_factory=dict, description='Optional key-value filters (e.g. fact_type).'
    )

    # Advanced options
    rerank: bool = Field(
        default=True, description='Whether to apply neural reranking if available.'
    )
    min_score: float | None = Field(
        default=None, description='Minimum score threshold for results (post-reranking).'
    )
    strategy_weights: dict[str, float] | None = Field(
        default=None, description='Optional custom weights for RRF fusion.'
    )
    strategies: list[str] | None = Field(
        default=None,
        description=(
            'Inclusion list of strategies to run. '
            'Valid values: semantic, keyword, graph, temporal, mental_model. '
            'If None, all strategies are used.'
        ),
    )
    expand_query: bool = Field(
        default=False, description='Whether to expand the query using an LLM.'
    )
    fusion_strategy: str = Field(
        default='rrf', description='The fusion strategy to use (e.g., rrf, position_aware).'
    )
    include_vectors: bool = Field(
        default=False, description='Whether to include embeddings in the result (slower).'
    )
    include_stale: bool = Field(
        default=False, description='Whether to include stale memory units in results.'
    )
    include_superseded: bool = Field(
        default=False,
        description='Whether to include superseded (low-confidence) memory units in results.',
    )
    debug: bool = Field(
        default=False,
        description=(
            'When True, collect per-strategy attribution (name, rank, RRF score, timing).'
        ),
    )
    mmr_lambda: float | None = Field(
        default=None,
        description='Per-request MMR lambda override. None=use config default.',
    )

    @model_validator(mode='after')
    def validate_strategies(self) -> Self:
        if self.strategies is not None:
            if len(self.strategies) == 0:
                raise ValueError('strategies list must not be empty')
            invalid = set(self.strategies) - VALID_STRATEGIES
            if invalid:
                raise ValueError(
                    f'Invalid strategy names: {sorted(invalid)}. '
                    f'Valid strategies: {sorted(VALID_STRATEGIES)}'
                )
        return self
