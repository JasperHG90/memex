from datetime import datetime, timezone
import re
import logging

from metaphone import doublemetaphone

logger = logging.getLogger('memex.core.memory.utils')

# Common stopwords to strip from entity names
STOPWORDS = {
    'the',
    'a',
    'an',
    'and',
    'or',
    'but',
    'if',
    'then',
    'else',
    'of',
    'at',
    'by',
    'for',
    'with',
    'about',
    'against',
    'between',
    'into',
    'through',
    'during',
    'before',
    'after',
    'above',
    'below',
    'to',
    'from',
    'up',
    'down',
    'in',
    'out',
    'on',
    'off',
    'over',
    'under',
    'again',
    'further',
    'then',
    'once',
    'inc',
    'llc',
    'corp',
    'limited',
    'ltd',
}


def normalize_name(name: str) -> str:
    """
    Standardize entity names by lowering, stripping, and removing common stopwords.
    """
    if not name:
        return ''

    # Lowercase and strip whitespace
    name = name.lower().strip()

    # Remove special characters except spaces and hyphens
    name = re.sub(r'[^a-z0-9\s\-]', '', name)

    # Strip stopwords from beginning and end
    words = name.split()
    while words and words[0] in STOPWORDS:
        words.pop(0)
    while words and words[-1] in STOPWORDS:
        words.pop(-1)

    return ' '.join(words)


def calculate_temporal_score(
    last_seen: datetime, current_time: datetime, half_life_days: float = 30.0
) -> float:
    """
    Calculate a temporal recency score using exponential half-life decay.
    Formula: Score = 2^(-days / half_life)

    This provides a more natural decay than linear, keeping memories relevant
    for much longer while still prioritizing the very recent.
    """
    if not last_seen or not current_time:
        return 0.0

    # Ensure both are offset-aware
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    diff_days = (current_time - last_seen).total_seconds() / 86400.0

    # Prevent future dates from causing > 1.0 scores
    if diff_days < 0:
        return 1.0

    return 2.0 ** (-diff_days / half_life_days)


def get_phonetic_code(name: str) -> str | None:
    """
    Generate a phonetic code for the given name.

    Uses Double Metaphone algorithm for robust matching.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None

    # Use Double Metaphone from 'metaphone' library
    # returns (primary, secondary)
    primary, _ = doublemetaphone(normalized)

    # Ensure it's not None or empty
    return primary if primary else None
