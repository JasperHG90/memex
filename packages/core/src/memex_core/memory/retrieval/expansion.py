import dspy
import logging
from typing import Any
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.llm import run_dspy_operation
from memex_core.memory.sql_models import TokenUsage

logger = logging.getLogger('memex.core.memory.retrieval.expansion')


class QueryExpansionSignature(dspy.Signature):
    """Expand a search query into semantic variations for better retrieval."""

    query: str = dspy.InputField(desc='The original search query.')
    variations: list[str] = dspy.OutputField(
        desc='A list of 1-2 semantic variations or expanded versions of the query.'
    )


class QueryExpander:
    """Uses LLM to expand queries for multi-channel search."""

    def __init__(self, lm: dspy.LM):
        self.lm = lm
        self.predictor = dspy.Predict(QueryExpansionSignature)

    async def expand(
        self, query: str, session: AsyncSession | None = None, vault_id: Any | None = None
    ) -> tuple[list[str], TokenUsage]:
        """
        Generates expansion variations for the query.
        Returns (variations, usage).
        """
        try:
            result, usage = await run_dspy_operation(
                lm=self.lm,
                predictor=self.predictor,
                input_kwargs={'query': query},
                session=session,
                context_metadata={'operation': 'query_expansion'},
                vault_id=vault_id,
            )

            # Ensure we have a list and it's not empty
            variations = result.variations if hasattr(result, 'variations') else []
            if not isinstance(variations, list):
                variations = [str(variations)]

            # Limit to 1-2 variations as per strategy
            return variations[:2], usage
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            logger.warning(f'Query expansion failed: {e}. Falling back to original query.')
            return [], TokenUsage()
