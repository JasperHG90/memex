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
# B. System-metadata detector
# ---------------------------------------------------------------------------

# Single-line only: no DOTALL (would let .+ span newlines) or MULTILINE.
_SYSTEM_METADATA_RE = re.compile(
    r'^\s*\[(?:Note|System)\s*[:]\s*.+\]\s*$',
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

HTML_PLACEHOLDER = '[HTML content removed]'
SYSTEM_PROMPT_PLACEHOLDER = '[system prompt omitted]'


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
        return HTML_PLACEHOLDER

    lower = text.lower()
    indicator_hits = sum(1 for ind in _HTML_INDICATORS if ind in lower)
    if indicator_hits >= 3 and len(text) > threshold:
        return HTML_PLACEHOLDER

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
            if pending_user is not None:
                pairs.append({'user': pending_user, 'assistant': ''})
                pending_user = None
            pairs.append(
                {
                    'user': m.get('user', ''),
                    'assistant': m.get('assistant', ''),
                }
            )
            continue

        role = str(m.get('role', 'user')).strip().lower() or 'user'
        content = m.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(
                c.get('text', '') if isinstance(c, dict) else str(c)
                for c in content
                if not isinstance(c, dict) or c.get('type', 'text') == 'text'
            )
        elif not isinstance(content, str):
            content = str(content)

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


def preprocess_turns(
    turns: list[dict[str, Any]],
    *,
    strip_system_prompts: bool = True,
    strip_system_metadata: bool = True,
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
            user = SYSTEM_PROMPT_PLACEHOLDER

        if strip_system_metadata and user and is_system_metadata(user):
            logger.debug('Stripped system metadata from turn %d', i)
            user = ''

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
        SYSTEM_PROMPT_PLACEHOLDER,
        HTML_PLACEHOLDER,
    }
)


def content_chars(turns: list[dict[str, str]]) -> int:
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
    min_capture_chars: int = 50,
) -> bool:
    """Return False when the preprocessed transcript is too thin to capture."""
    substantive_turns = sum(
        1
        for t in turns
        if t.get('assistant', '').strip() and t['assistant'].strip() not in _PLACEHOLDER_STRINGS
    )
    if substantive_turns < min_turns:
        return False

    if content_chars(turns) < min_capture_chars:
        return False

    return True


# ---------------------------------------------------------------------------
# F. Improved formatter
# ---------------------------------------------------------------------------


def _render_pairs(pairs: list[dict[str, str]]) -> str:
    """Render already-normalized ``{user, assistant}`` pairs as markdown."""
    blocks: list[str] = []
    for turn in pairs:
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


def format_transcript(messages: list[dict[str, Any]]) -> str:
    """Render turn dicts as a structured markdown transcript.

    Accepts both ``{user, assistant}`` pairs and ``{role, content}`` messages.
    Produces ``### Role`` headers with content on separate lines and ``---``
    horizontal rules between turns.
    """
    return _render_pairs(_normalize_messages(messages))


__all__ = [
    'HTML_PLACEHOLDER',
    'SYSTEM_PROMPT_PLACEHOLDER',
    'content_chars',
    'format_transcript',
    'is_system_metadata',
    'is_system_prompt',
    'passes_quality_gate',
    'preprocess_turns',
    'sanitize_html_content',
]
