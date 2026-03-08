"""NLP-based temporal constraint extraction from natural language queries."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger('memex.core.memory.retrieval.temporal_extraction')

# Patterns that look temporal but are too ambiguous or not date-like
_FALSE_POSITIVE_PATTERNS = re.compile(
    r'^(?:the|a|an|i|me|my|we|our|is|am|are|was|were|be|been|it|this|that|'
    r'about|tell|what|how|who|where|when|why|do|does|did|can|could|will|would|'
    r'shall|should|may|might|must|have|has|had|get|got|know|think|said|say|'
    r'find|give|let|make|go|come|see|look|want|need|use|try|ask|show|help|'
    r'call|work|keep|put|run|move|live|play|new|old|good|bad|big|small|'
    r'first|last|long|short|great|little|right|left|high|low|just|also|'
    r'very|much|more|most|some|any|all|each|every|no|not|only|than|too|'
    r'still|already|always|never|often|sometimes|now|then|here|there|'
    r'up|down|out|in|on|off|over|under|back|away|again|cats|dogs|'
    r'people|things|stuff|man|woman|day|time|way|life|world|part|place|'
    r'case|point|fact|hand|eye|room|home|end|side|head|house|water|'
    r'number|night|word|information)$',
    re.IGNORECASE,
)

# Temporal trigger phrases that signal a real temporal intent
_TEMPORAL_TRIGGERS = re.compile(
    r'\b(?:last\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|'
    r'saturday|sunday|spring|summer|fall|autumn|winter|quarter|semester|decade|century)|'
    r'(?:\d+|a|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
    r'(?:days?|weeks?|months?|years?|hours?|minutes?)\s+ago|'
    r'yesterday|today|tonight|'
    r'(?:in|during|since|before|after|from|until)\s+'
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december'
    r'|\d{4})|'
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
    r'\s+\d{4}|'
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
    r'\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?|'
    r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|'
    r'\d{4}[/-]\d{1,2}[/-]\d{1,2}|'
    r'this\s+(?:week|month|year|morning|afternoon|evening)|'
    r'past\s+(?:\d+\s+)?(?:days?|weeks?|months?|years?)|'
    r'(?:early|late|mid)\s*-?\s*'
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december'
    r'|\d{4})'
    r')\b',
    re.IGNORECASE,
)

# Pattern for "in <year>" specifically
_YEAR_PATTERN = re.compile(r'\bin\s+(\d{4})\b', re.IGNORECASE)

# Pattern for "in <Month>" or "in <Month> <Year>"
_MONTH_PATTERN = re.compile(
    r'\bin\s+(january|february|march|april|may|june|july|august|'
    r'september|october|november|december)(?:\s+(\d{4}))?\b',
    re.IGNORECASE,
)

# Pattern for "last week/month/year"
_LAST_PERIOD_PATTERN = re.compile(
    r'\blast\s+(week|month|year)\b',
    re.IGNORECASE,
)

# Pattern for "N days/weeks/months ago"
_AGO_PATTERN = re.compile(
    r'\b(\d+|a|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
    r'(days?|weeks?|months?|years?)\s+ago\b',
    re.IGNORECASE,
)

_WORD_TO_NUM = {
    'a': 1,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
}

_MONTH_NAMES = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last day of a given month/year."""
    return calendar.monthrange(year, month)[1]


def extract_temporal_constraint(
    query: str,
    reference_date: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """Parse natural language temporal expressions from a query.

    Returns (start_date, end_date) tuple or None if no temporal expression found.
    Handles: 'last week', 'in March', 'yesterday', '3 days ago',
    'last month', 'in 2024', etc.

    Args:
        query: The natural language query string.
        reference_date: Reference point for relative dates. Defaults to now (UTC).

    Returns:
        A tuple of (start_date, end_date) with timezone-aware datetimes, or None.
    """
    if not query or not query.strip():
        return None

    if reference_date is None:
        reference_date = datetime.now(timezone.utc)
    elif reference_date.tzinfo is None:
        reference_date = reference_date.replace(tzinfo=timezone.utc)

    # Check for temporal trigger phrases first — skip dateparser if no trigger found
    if not _TEMPORAL_TRIGGERS.search(query):
        return None

    # Try structured regex patterns first for reliability
    result = _try_regex_patterns(query, reference_date)
    if result is not None:
        return result

    # Fall back to dateparser for complex expressions
    return _try_dateparser(query, reference_date)


def _try_regex_patterns(
    query: str,
    reference_date: datetime,
) -> tuple[datetime, datetime] | None:
    """Try to extract temporal constraints using regex patterns."""

    # "yesterday"
    if re.search(r'\byesterday\b', query, re.IGNORECASE):
        yesterday = reference_date - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (start, end)

    # "today"
    if re.search(r'\btoday\b', query, re.IGNORECASE):
        start = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = reference_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (start, end)

    # "last week/month/year"
    m = _LAST_PERIOD_PATTERN.search(query)
    if m:
        period = m.group(1).lower()
        if period == 'week':
            end = reference_date
            start = end - timedelta(days=7)
            return (_start_of_day(start), _end_of_day(end))
        elif period == 'month':
            end = reference_date
            start = end - timedelta(days=30)
            return (_start_of_day(start), _end_of_day(end))
        elif period == 'year':
            end = reference_date
            start = end - timedelta(days=365)
            return (_start_of_day(start), _end_of_day(end))

    # "N days/weeks/months ago"
    m = _AGO_PATTERN.search(query)
    if m:
        num_str = m.group(1).lower()
        unit = m.group(2).lower().rstrip('s')
        num = _WORD_TO_NUM.get(num_str)
        if num is None:
            num = int(num_str)

        if unit == 'day':
            target = reference_date - timedelta(days=num)
            return (_start_of_day(target), _end_of_day(target))
        elif unit == 'week':
            end = reference_date - timedelta(weeks=num)
            start = end - timedelta(days=6)
            return (_start_of_day(start), _end_of_day(end))
        elif unit == 'month':
            # Single calendar month window: go back num months
            target_month = reference_date.month - num
            target_year = reference_date.year
            while target_month <= 0:
                target_month += 12
                target_year -= 1
            tz = reference_date.tzinfo
            start = datetime(target_year, target_month, 1, 0, 0, 0, tzinfo=tz)
            last_day = _last_day_of_month(target_year, target_month)
            end = datetime(target_year, target_month, last_day, 23, 59, 59, 999999, tzinfo=tz)
            return (start, end)
        elif unit == 'year':
            # Quarter (3 months) centered on the target date
            target_year = reference_date.year - num
            target_month = reference_date.month
            tz = reference_date.tzinfo
            # 1.5 months before and after the target month
            start_month = target_month - 1
            start_year = target_year
            if start_month <= 0:
                start_month += 12
                start_year -= 1
            end_month = target_month + 1
            end_year = target_year
            if end_month > 12:
                end_month -= 12
                end_year += 1
            start = datetime(start_year, start_month, 1, 0, 0, 0, tzinfo=tz)
            last_day = _last_day_of_month(end_year, end_month)
            end = datetime(end_year, end_month, last_day, 23, 59, 59, 999999, tzinfo=tz)
            return (start, end)

    # "in March 2024" or "in March"
    m = _MONTH_PATTERN.search(query)
    if m:
        month_name = m.group(1).lower()
        year_str = m.group(2)
        month = _MONTH_NAMES[month_name]
        year = int(year_str) if year_str else reference_date.year
        tz = reference_date.tzinfo
        start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
        last_day = _last_day_of_month(year, month)
        end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=tz)
        return (start, end)

    # "in 2024"
    m = _YEAR_PATTERN.search(query)
    if m:
        year = int(m.group(1))
        tz = reference_date.tzinfo
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=tz)
        end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=tz)
        return (start, end)

    return None


def _try_dateparser(
    query: str,
    reference_date: datetime,
) -> tuple[datetime, datetime] | None:
    """Fall back to dateparser.search for complex temporal expressions."""
    try:
        import dateparser.search  # type: ignore[import-untyped]
    except ImportError:
        logger.warning('dateparser not installed; temporal extraction disabled')
        return None

    try:
        settings = {
            'PREFER_DATES_FROM': 'past',
            'RELATIVE_BASE': reference_date.replace(tzinfo=None),
            'RETURN_AS_TIMEZONE_AWARE': False,
        }
        results = dateparser.search.search_dates(query, settings=settings)
    except Exception:
        logger.debug('dateparser.search failed for query: %s', query, exc_info=True)
        return None

    if not results:
        return None

    # Filter out false positives: matched text that is just a common word
    filtered = []
    for matched_text, parsed_date in results:
        cleaned = matched_text.strip().strip('.,;:!?')
        if _FALSE_POSITIVE_PATTERNS.match(cleaned):
            continue
        filtered.append((matched_text, parsed_date))

    if not filtered:
        return None

    # Use the first valid result
    _matched_text, parsed_date = filtered[0]

    # Ensure timezone-aware
    tz = reference_date.tzinfo
    if parsed_date.tzinfo is None:
        parsed_date = parsed_date.replace(tzinfo=tz)

    # Reject future dates (more than 1 day ahead)
    if parsed_date > reference_date + timedelta(days=1):
        return None

    # Create a reasonable range around the parsed date (full day)
    start = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = parsed_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    return (start, end)


def _start_of_day(dt: datetime) -> datetime:
    """Return the start of the day for a datetime."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    """Return the end of the day for a datetime."""
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
