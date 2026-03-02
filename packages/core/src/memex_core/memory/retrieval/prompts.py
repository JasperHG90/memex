"""DSPy prompts and signatures for search result summarization."""

import dspy


class SearchSummarySignature(dspy.Signature):
    """Synthesize search results into a concise answer with citations.

    Given a user query and a numbered list of search results, produce a short summary
    that answers the query. Cite sources using bracket notation [0], [1], etc.,
    where the number corresponds to the zero-based index of the search result.
    Only cite results that directly support a claim. If no results are relevant,
    say so explicitly.

    When results conflict, prefer more recent information.
    """

    query: str = dspy.InputField(desc='The original user search query.')
    search_results: list[str] = dspy.InputField(
        desc='Numbered list of search result texts to synthesize.'
    )

    summary: str = dspy.OutputField(
        desc=(
            'A concise synthesis answering the query with bracket citations '
            '(e.g. [0], [1]) referencing the search results by index.'
        )
    )
