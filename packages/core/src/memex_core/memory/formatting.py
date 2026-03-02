from datetime import datetime


def format_for_embedding(text: str, fact_type: str, context: str | None = None) -> str:
    """
    Formats text for embedding generation to match the model's training distribution.

    Training Format: "Type (Context): Text"
    Example: "Experience (Maintenance): I changed the oil."

    Args:
        text: The core narrative text.
        fact_type: The epistemic type (world, experience, observation).
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


def format_for_reranking(
    text: str, event_date: datetime, fact_type: str, context: str | None = None
) -> str:
    """
    Formats text for cross-encoder reranking to match the model's training distribution.

    Training Format: "[Date: Month DD, YYYY (YYYY-MM-DD)] [Type] Context: Text"
    Example: "[Date: January 14, 2026 (2026-01-14)] [Experience] Maintenance: I changed the oil."

    Args:
        text: The core narrative text.
        event_date: The date associated with the memory.
        fact_type: The epistemic type (world, experience, observation).
        context: Optional context string.

    Returns:
        Formatted string for the reranking model.
    """
    # Format: January 14, 2026
    date_readable = event_date.strftime('%B %d, %Y')
    # Format: 2026-01-14
    date_iso = event_date.strftime('%Y-%m-%d')

    f_type = fact_type.capitalize() if fact_type else 'Unknown'
    ctx_prefix = f'{context}: ' if context else ''

    return f'[Date: {date_readable} ({date_iso})] [{f_type}] {ctx_prefix}{text}'
