import dspy
import logging
from memex_core.llm import run_dspy_operation

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

    async def expand(self, query: str) -> list[str]:
        """
        Generates expansion variations for the query.
        Returns variations.
        """
        try:
            result = await run_dspy_operation(
                lm=self.lm,
                predictor=self.predictor,
                input_kwargs={'query': query},
            )

            # Ensure we have a list and it's not empty
            variations = result.variations if hasattr(result, 'variations') else []
            if not isinstance(variations, list):
                variations = [str(variations)]

            # Limit to 1-2 variations as per strategy
            return variations[:2]
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            logger.warning(f'Query expansion failed: {e}. Falling back to original query.')
            return []
