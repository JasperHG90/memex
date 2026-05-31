"""Transcript preprocessing pipeline for Hermes session-note capture.

Cleans, filters, and reformats raw turn buffers before they are ingested
as Memex notes.  All functions are pure (no I/O, no side-effects) so they
can be tested in isolation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A. System-prompt detector
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_ANCHORS: tuple[str, ...] = (
    'update the skill library',
    'consider saving to memory',
    'you can only call memory and skill management tools',
    'other tools will be denied at runtime',
    'do not attempt them',
    'nothing to save',
)

_SYSTEM_PROMPT_MIN_LENGTH = 200
_SYSTEM_PROMPT_MIN_ANCHORS = 2


def is_system_prompt(text: str) -> bool:
    """Detect injected system prompts using anchor-phrase matching.

    Robust to moderate rewording: requires *multiple* short structural
    anchors to match, not a single exact sentence.
    """
    if len(text) < _SYSTEM_PROMPT_MIN_LENGTH:
        return False
    lower = text.lower()
    hits = sum(1 for anchor in _SYSTEM_PROMPT_ANCHORS if anchor in lower)
    return hits >= _SYSTEM_PROMPT_MIN_ANCHORS


# ---------------------------------------------------------------------------
# A2. Context-compaction summary detector / stripper
# ---------------------------------------------------------------------------

_COMPACTION_START_ANCHOR = '[CONTEXT COMPACTION — REFERENCE ONLY]'
_COMPACTION_END_ANCHOR = (
    '--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'
)


def is_compaction_summary(text: str) -> bool:
    """Detect a Hermes context-compaction summary by its literal anchors.

    Requires BOTH anchors — a lone start anchor (truncated content) is not a
    complete, strippable block.
    """
    return _COMPACTION_START_ANCHOR in text and _COMPACTION_END_ANCHOR in text


def strip_compaction_summary(text: str) -> str:
    """Remove the anchored compaction-summary region, keeping surrounding text.

    No-ops (returns input unchanged) when either anchor is absent, so a future
    wording change fails safe rather than corrupting content.
    """
    start = text.find(_COMPACTION_START_ANCHOR)
    if start < 0:
        return text
    end = text.find(_COMPACTION_END_ANCHOR, start)
    if end < 0:
        return text
    return (text[:start] + text[end + len(_COMPACTION_END_ANCHOR) :]).strip()


# ---------------------------------------------------------------------------
# A3. Inline tool-output block stripper
# ---------------------------------------------------------------------------

_TOOL_OUTPUT_LINE_PREFIX = 'Tool: '


def is_tool_output_line(line: str) -> bool:
    """True only for ``Tool: `` lines whose payload is a structured object.

    Hermes serializes tool results as ``Tool: {...}`` / ``Tool: [...]``.
    Requiring the ``{``/``[`` opener avoids deleting organic prose that merely
    happens to begin a line with ``Tool: ``.
    """
    if not line.startswith(_TOOL_OUTPUT_LINE_PREFIX):
        return False
    payload = line[len(_TOOL_OUTPUT_LINE_PREFIX) :].lstrip()
    return payload[:1] in ('{', '[')


def _payload_end(text: str, open_idx: int) -> int:
    """Index just past the bracket-balanced JSON payload starting at *open_idx*.

    String-aware (braces inside quoted strings don't count). Returns
    ``len(text)`` for an unterminated payload.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(open_idx, len(text)):
        c = text[i]
        if esc:
            esc = False
        elif c == '\\':
            esc = True
        elif in_str:
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in '{[':
            depth += 1
        elif c in '}]':
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def strip_tool_output_blocks(text: str) -> tuple[str, int]:
    """Strip ``Tool: <structured-payload>`` blocks serialized inline as text.

    A block starts at a ``Tool: {`` / ``Tool: [`` line; only the
    bracket-balanced payload (which may span lines) plus the remainder of its
    final line is removed, so prose that follows the payload survives. Returns
    ``(stripped_text, dropped_block_count)``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    dropped = 0
    while i < n:
        nl = text.find('\n', i)
        line_end = n if nl == -1 else nl + 1
        if is_tool_output_line(text[i:line_end]):
            dropped += 1
            bracket = i + len(_TOOL_OUTPUT_LINE_PREFIX)
            while bracket < line_end and text[bracket] not in '{[':
                bracket += 1
            end = _payload_end(text, bracket)
            nl2 = text.find('\n', end)
            i = n if nl2 == -1 else nl2 + 1
        else:
            out.append(text[i:line_end])
            i = line_end
    return ''.join(out).rstrip(), dropped


# ---------------------------------------------------------------------------
# B. System-metadata detector
# ---------------------------------------------------------------------------

_SYSTEM_METADATA_RE = re.compile(
    r'^\s*\[(?:Note|System)\s*[:]\s*.+\]\s*$',
    re.DOTALL,
)


def is_system_metadata(text: str) -> bool:
    """Detect bracketed system-injected metadata lines."""
    return bool(_SYSTEM_METADATA_RE.match(text.strip()))


# ---------------------------------------------------------------------------
# C. HTML content detector / sanitizer
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r'<[^>]+>')

_HTML_INDICATORS: tuple[str, ...] = (
    '<!doctype',
    '<html',
    '<head',
    '<body',
    '<div',
    '<style',
    '<script',
    '<span',
    'class="',
    "class='",
    'style="',
    "style='",
)

_HTML_PLACEHOLDER = '[HTML content removed]'


def sanitize_html_content(text: str, threshold: int = 500) -> str:
    """Replace HTML-heavy content with a placeholder.

    Detection works in two modes:
    1. Tagged HTML — total chars inside ``<...>`` tags exceeds *threshold*.
    2. LLM-generated HTML — 3+ distinct HTML indicator patterns AND total
       text length > *threshold*.

    Returns the original text unchanged when neither mode triggers.
    """
    tag_chars = sum(len(m.group()) for m in _HTML_TAG_RE.finditer(text))
    if tag_chars > threshold:
        return _HTML_PLACEHOLDER

    lower = text.lower()
    indicator_hits = sum(1 for ind in _HTML_INDICATORS if ind in lower)
    if indicator_hits >= 3 and len(text) > threshold:
        return _HTML_PLACEHOLDER

    return text


# ---------------------------------------------------------------------------
# D. Turn preprocessor
# ---------------------------------------------------------------------------


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert Hermes ``{role, content}`` messages into ``{user, assistant}`` pairs."""
    pairs: list[dict[str, str]] = []
    pending_user: str | None = None

    for m in messages:
        if 'user' in m or 'assistant' in m:
            pairs.append(
                {
                    'user': m.get('user', ''),
                    'assistant': m.get('assistant', ''),
                }
            )
            continue

        role = str(m.get('role', '')).strip().lower()
        content = m.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(
                c.get('text', '') if isinstance(c, dict) else str(c)
                for c in content
                if not isinstance(c, dict) or c.get('type', 'text') == 'text'
            )

        if role == 'tool':
            continue

        if role == 'user':
            if pending_user is not None:
                pairs.append({'user': pending_user, 'assistant': ''})
            pending_user = content
        elif role == 'assistant':
            pairs.append({'user': pending_user or '', 'assistant': content})
            pending_user = None

    if pending_user is not None:
        pairs.append({'user': pending_user, 'assistant': ''})

    return pairs


def _strip_agent_artifacts(text: str, turn_index: int, field: str) -> str:
    """Remove compaction summaries and inline tool-output blocks from one field.

    Both strips run unconditionally — these are agent-internal artifacts that
    have no place in a durable session note. Emits a log line per strip so the
    volume removed is observable (and a sudden drop to zero signals that an
    upstream wording change has outrun the anchors).
    """
    if not text:
        return text

    if is_compaction_summary(text):
        stripped = strip_compaction_summary(text)
        logger.info(
            'hermes.transcript.compaction_stripped field=%s turn=%d bytes=%d',
            field,
            turn_index,
            len(text) - len(stripped),
        )
        text = stripped

    text, dropped = strip_tool_output_blocks(text)
    if dropped:
        logger.info(
            'hermes.transcript.tool_output_stripped field=%s turn=%d blocks=%d',
            field,
            turn_index,
            dropped,
        )

    return text


def preprocess_turns(
    turns: list[dict[str, Any]],
    *,
    strip_system_prompts: bool = True,
    strip_html_content: bool = True,
    html_content_threshold: int = 500,
) -> list[dict[str, str]]:
    """Clean raw turn dicts before formatting.

    Accepts both ``{user, assistant}`` pairs and ``{role, content}`` messages.
    Returns a new list of ``{user, assistant}`` dicts (never mutates input).
    """
    normalized = _normalize_messages(turns)
    cleaned: list[dict[str, str]] = []

    for i, turn in enumerate(normalized):
        user = turn.get('user', '')
        assistant = turn.get('assistant', '')

        if strip_system_prompts and user and is_system_prompt(user):
            logger.debug('Stripped system prompt from turn %d (%d chars)', i, len(user))
            user = '[system prompt omitted]'

        if user and is_system_metadata(user):
            logger.debug('Stripped system metadata from turn %d', i)
            user = ''

        user = _strip_agent_artifacts(user, i, 'user')
        assistant = _strip_agent_artifacts(assistant, i, 'assistant')

        if strip_html_content:
            if user:
                user = sanitize_html_content(user, threshold=html_content_threshold)
            if assistant:
                assistant = sanitize_html_content(assistant, threshold=html_content_threshold)

        cleaned.append({'user': user, 'assistant': assistant})

    return cleaned


# ---------------------------------------------------------------------------
# E. Quality gate
# ---------------------------------------------------------------------------

_PLACEHOLDER_STRINGS = frozenset(
    {
        '[system prompt omitted]',
        _HTML_PLACEHOLDER,
    }
)


def _content_chars(turns: list[dict[str, str]]) -> int:
    """Sum of non-placeholder content characters across all turns."""
    total = 0
    for turn in turns:
        for field in ('user', 'assistant'):
            text = turn.get(field, '').strip()
            if text and text not in _PLACEHOLDER_STRINGS:
                total += len(text)
    return total


def passes_quality_gate(
    turns: list[dict[str, str]],
    *,
    min_turns: int = 1,
    min_content_chars: int = 50,
) -> bool:
    """Return False when the preprocessed transcript is too thin to capture."""
    substantive_turns = sum(
        1
        for t in turns
        if t.get('assistant', '').strip() and t['assistant'].strip() not in _PLACEHOLDER_STRINGS
    )
    if substantive_turns < min_turns:
        return False

    if _content_chars(turns) < min_content_chars:
        return False

    return True


# ---------------------------------------------------------------------------
# F. Improved formatter
# ---------------------------------------------------------------------------


def format_transcript(messages: list[dict[str, Any]]) -> str:
    """Render turn dicts as a structured markdown transcript.

    Accepts both ``{user, assistant}`` pairs and ``{role, content}`` messages.
    Produces ``### Role`` headers with content on separate lines and ``---``
    horizontal rules between turns.
    """
    normalized = _normalize_messages(messages)
    blocks: list[str] = []

    for turn in normalized:
        parts: list[str] = []
        user = turn.get('user', '').strip()
        assistant = turn.get('assistant', '').strip()

        if user:
            parts.append(f'### User\n\n{user}')
        if assistant:
            parts.append(f'### Assistant\n\n{assistant}')

        if parts:
            blocks.append('\n\n'.join(parts))

    return '\n\n---\n\n'.join(blocks)


__all__ = [
    'format_transcript',
    'is_compaction_summary',
    'is_system_metadata',
    'is_system_prompt',
    'is_tool_output_line',
    'passes_quality_gate',
    'preprocess_turns',
    'sanitize_html_content',
    'strip_compaction_summary',
    'strip_tool_output_blocks',
]
