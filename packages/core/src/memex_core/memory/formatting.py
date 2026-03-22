from datetime import datetime


def format_for_embedding(text: str, fact_type: str, context: str | None = None) -> str:
    """
    Formats text for embedding generation to match the model's training distribution.

    Training Format: "Type (Context): Text"
    Example: "Experience (Maintenance): I changed the oil."

    Args:
        text: The core narrative text.
        fact_type: The epistemic type (world, event, observation).
        context: Optional context string (e.g. "Maintenance").

    Returns:
        Formatted string for the embedding model.
    """
    # Normalize inputs
    f_type = fact_type.capitalize() if fact_type else 'Unknown'

    if context:
        return f'{f_type} ({context}): {text}'
    else:
        return f'{f_type}: {text}'


def _format_date(dt: datetime) -> str:
    """Format a datetime as 'Month DD, YYYY (YYYY-MM-DD)'."""
    return f'{dt.strftime("%B %d, %Y")} ({dt.strftime("%Y-%m-%d")})'


def format_for_reranking(
    text: str,
    fact_type: str,
    context: str | None = None,
    occurred_start: datetime | None = None,
    occurred_end: datetime | None = None,
) -> str:
    """
    Formats text for cross-encoder reranking to match the model's training distribution.

    Training Format: "[Start: Month DD, YYYY (YYYY-MM-DD)] [End: ongoing] [Type] Context: Text"

    Date range cases:
    - Start + no end (ongoing): [Start: ...] [End: ongoing]
    - Start + end (completed):  [Start: ...] [End: ...]
    - No start + no end:        No date prefix (undated facts are not penalized)
    - No start + end:           [End: ...]

    Args:
        text: The core narrative text.
        fact_type: The epistemic type (world, event, observation).
        context: Optional context string.
        occurred_start: When the fact started being true.
        occurred_end: When the fact stopped being true (None = ongoing/unknown).

    Returns:
        Formatted string for the reranking model.
    """
    f_type = fact_type.capitalize() if fact_type else 'Unknown'
    ctx_prefix = f'{context}: ' if context else ''

    # Build date prefix based on available range info
    date_parts: list[str] = []
    if occurred_start is not None:
        date_parts.append(f'[Start: {_format_date(occurred_start)}]')
    if occurred_start is not None or occurred_end is not None:
        if occurred_end is not None:
            date_parts.append(f'[End: {_format_date(occurred_end)}]')
        else:
            date_parts.append('[End: ongoing]')

    date_prefix = f'{" ".join(date_parts)} ' if date_parts else ''

    return f'{date_prefix}[{f_type}] {ctx_prefix}{text}'
