import dspy
import logging
from memex_core.llm import run_dspy_operation

logger = logging.getLogger('memex.core.memory.retrieval.expansion')


class SurveyDecompositionSignature(dspy.Signature):
    """Decompose a broad topic into focused sub-questions for comprehensive retrieval."""

    topic: str = dspy.InputField(desc='A broad topic or panoramic query to decompose.')
    sub_questions: list[str] = dspy.OutputField(
        desc='A list of 3-5 focused, specific sub-questions that together cover the topic.'
    )


class SurveyDecomposer:
    """Uses LLM to decompose broad topics into focused sub-questions."""

    def __init__(self, lm: dspy.LM):
        self.lm = lm
        self.predictor = dspy.Predict(SurveyDecompositionSignature)

    async def decompose(self, topic: str) -> list[str]:
        """
        Decompose a broad topic into 3-5 focused sub-questions.
        Clamps output to [3, 5] range: pads with rephrases if <3, truncates if >5.
        Falls back to [topic] on LLM failure.
        """
        try:
            result = await run_dspy_operation(
                lm=self.lm,
                predictor=self.predictor,
                input_kwargs={'topic': topic},
                operation_name='survey.decomposition',
            )

            sub_questions = result.sub_questions if hasattr(result, 'sub_questions') else []
            if not isinstance(sub_questions, list):
                sub_questions = [str(sub_questions)]

            # Filter empty strings
            sub_questions = [q.strip() for q in sub_questions if q and q.strip()]

            if not sub_questions:
                return [topic]

            # Clamp to [3, 5]
            if len(sub_questions) > 5:
                sub_questions = sub_questions[:5]
            elif len(sub_questions) < 3:
                # Pad with rephrases cycling through existing questions
                _prefixes = ['aspects of', 'details about', 'context for']
                original_len = len(sub_questions)
                for i in range(original_len, 3):
                    idx = i % original_len
                    prefix = _prefixes[i % len(_prefixes)]
                    sub_questions.append(f'{prefix} {topic} — {sub_questions[idx]}')

            return sub_questions
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            logger.warning(f'Survey decomposition failed: {e}. Falling back to original topic.')
            return [topic]


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
                operation_name='retrieval.expansion',
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
