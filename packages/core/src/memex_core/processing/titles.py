"""Document title extraction for content ingested without an explicit meaningful name.

Priority chain:
1. Caller-provided name (if meaningful — not None/empty/UUID/generic word).
2. First level-1 node from the page_index TOC (zero LLM cost, page_index path only).
3. First H1 header detected via regex (zero LLM cost).
4. LLM extraction from block summaries (page_index path) or raw text excerpt (simple path).
5. Fallback: provided name or 'Untitled'.
"""

import logging
import re
from typing import Any
from uuid import UUID

import dspy
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.llm import run_dspy_operation
from memex_core.memory.extraction.utils import detect_markdown_headers_regex

logger = logging.getLogger('memex.core.processing.titles')

_TITLE_CHAR_LIMIT = 1500

# Generic names that offer no useful information about document content
_GENERIC_NAMES = frozenset({'untitled', 'note', 'document', 'file', 'page', 'text'})

_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


class ExtractDocumentTitle(dspy.Signature):
    """Extract a concise, descriptive title for a document from its content summary.

    Return only the title text — no quotes, no punctuation at the end,
    no prefix like 'Title:'.  If the content has no clear topic, return an empty string.
    """

    content_summary: str = dspy.InputField(
        desc='A summary of the document content to analyse for a title.',
    )
    title: str = dspy.OutputField(
        desc='A concise title (5–10 words) capturing the document subject.  Empty if unclear.',
    )


def extract_title_from_markdown(text: str) -> str | None:
    """Return the clean text of the first H1 header found in *text*, or None."""
    headers = detect_markdown_headers_regex(text)
    for header in headers:
        if header.level_hint == 'h1' and header.clean_title:
            return header.clean_title.strip()
    return None


def extract_title_from_page_index_toc(toc: list[dict[str, Any]]) -> str | None:
    """Return the title of the first level-1 node with a non-empty title, or None.

    The thin tree is a list of dicts serialised by ``TOCNode.tree_without_text()``.
    Each dict has at least ``title`` (str) and ``level`` (int) keys.
    """
    for node in toc:
        if node.get('level') == 1:
            title = (node.get('title') or '').strip()
            if title:
                return title
    return None


def _build_summary_text_from_page_index(page_index: list[dict[str, Any]]) -> str:
    """Build a compact summary string from the page_index thin tree for LLM title inference.

    Walks the tree collecting (title, summary) pairs and formats them so the LLM
    gets the semantic gist of the document without the full raw text.
    """
    parts: list[str] = []

    def _walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            title = (node.get('title') or '').strip()
            summary = node.get('summary') or {}
            # BlockSummary-style: {topic, key_points} OR SectionSummary-style: {who, what, ...}
            if isinstance(summary, dict):
                topic = summary.get('topic', '')
                key_points: list[str] = summary.get('key_points') or []
                if topic:
                    parts.append(f'[{title}] {topic}')
                    if key_points:
                        parts.append('; '.join(key_points[:3]))
                else:
                    # SectionSummary fields
                    what = summary.get('what', '')
                    if what and title:
                        parts.append(f'[{title}] {what}')
                    elif title:
                        parts.append(title)
            elif title:
                parts.append(title)
            _walk(node.get('children') or [])

    _walk(page_index)
    return '\n'.join(parts)


async def extract_title_via_llm(
    content_summary: str,
    lm: dspy.LM,
    session: AsyncSession | None = None,
    vault_id: UUID | None = None,
) -> str | None:
    """Ask the LLM to infer a title from a content summary string.

    Args:
        content_summary: Compact text describing the document (summaries or raw excerpt).
        lm: The DSPy language model instance.
        session: Optional DB session for token-usage logging.
        vault_id: Optional vault ID for token-usage logging.

    Returns:
        A non-empty title string, or None if extraction failed or returned nothing.
    """
    excerpt = content_summary[:_TITLE_CHAR_LIMIT]
    if not excerpt.strip():
        return None

    predictor = dspy.Predict(ExtractDocumentTitle)

    try:
        prediction, _ = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={'content_summary': excerpt},
            session=session,
            context_metadata={'operation': 'title_extraction'},
            vault_id=vault_id,
        )
    except Exception:
        logger.warning('LLM title extraction failed', exc_info=True)
        return None

    raw: str = getattr(prediction, 'title', '') or ''
    title = raw.strip().strip('"\'').strip()
    return title if title else None


def _is_meaningful_name(name: str | None) -> bool:
    """Return True only when *name* contains actionable document-specific information.

    A name is **not** meaningful when it is:
    - None or empty.
    - A plain UUID (e.g. ``"3f2504e0-4f89-11d3-9a0c-0305e82c3301"``).
    - One of the generic placeholder words (case-insensitive).
    """
    if not name or not name.strip():
        return False
    stripped = name.strip()
    if _UUID_PATTERN.match(stripped):
        return False
    if stripped.lower() in _GENERIC_NAMES:
        return False
    return True


async def resolve_document_title(
    content_text: str,
    provided_name: str | None,
    lm: dspy.LM,
    session: AsyncSession | None = None,
    vault_id: UUID | None = None,
) -> str:
    """Resolve the best available title for a document being ingested (simple path).

    Priority:
    1. ``provided_name`` — if it is meaningful.
    2. First H1 header extracted via regex from *content_text*.
    3. LLM inference from the first ~1500 characters of *content_text*.
    4. ``provided_name`` as-is, or ``'Untitled'`` as the final fallback.

    Args:
        content_text: Full decoded document content.
        provided_name: The caller-supplied ``note._metadata.name`` (may be None).
        lm: DSPy language model used for LLM fallback.
        session: Optional DB session for token-usage logging.
        vault_id: Optional vault ID for token-usage logging.

    Returns:
        A non-empty title string.
    """
    # Priority 1: meaningful caller-provided name
    if _is_meaningful_name(provided_name):
        return provided_name  # type: ignore[return-value]

    # Priority 2: regex H1 header (zero cost)
    if md_title := extract_title_from_markdown(content_text):
        logger.debug(f'Title resolved from H1 header: {md_title!r}')
        return md_title

    # Priority 3: LLM inference from raw text excerpt
    if llm_title := await extract_title_via_llm(
        content_text[:_TITLE_CHAR_LIMIT], lm, session=session, vault_id=vault_id
    ):
        logger.debug(f'Title resolved via LLM (raw text): {llm_title!r}')
        return llm_title

    # Priority 4: fallback
    fallback = (provided_name or '').strip() or 'Untitled'
    logger.debug(f'Title fell back to: {fallback!r}')
    return fallback


async def resolve_title_from_page_index(
    page_index_toc: list[dict[str, Any]],
    provided_name: str | None,
    lm: dspy.LM,
    session: AsyncSession | None = None,
    vault_id: UUID | None = None,
) -> str:
    """Resolve the best title using the page_index thin tree (page_index extraction path).

    Priority:
    1. ``provided_name`` — if it is meaningful.
    2. First level-1 TOC node title (zero LLM cost).
    3. LLM inference from block summaries collected from the thin tree.
    4. ``provided_name`` as-is, or ``'Untitled'`` as the final fallback.

    Args:
        page_index_toc: Thin-tree list from ``TOCNode.tree_without_text()``.
        provided_name: The caller-supplied name (may be None or a generic fallback).
        lm: DSPy language model used for LLM fallback.
        session: Optional DB session for token-usage logging.
        vault_id: Optional vault ID for token-usage logging.

    Returns:
        A non-empty title string.
    """
    # Priority 1: meaningful caller-provided name
    if _is_meaningful_name(provided_name):
        return provided_name  # type: ignore[return-value]

    # Priority 2: first level-1 node in the TOC (zero cost)
    if toc_title := extract_title_from_page_index_toc(page_index_toc):
        logger.debug(f'Title resolved from page_index TOC: {toc_title!r}')
        return toc_title

    # Priority 3: LLM from block summaries
    summary_text = _build_summary_text_from_page_index(page_index_toc)
    if summary_text.strip():
        if llm_title := await extract_title_via_llm(
            summary_text, lm, session=session, vault_id=vault_id
        ):
            logger.debug(f'Title resolved via LLM (page_index summaries): {llm_title!r}')
            return llm_title

    # Priority 4: fallback
    fallback = (provided_name or '').strip() or 'Untitled'
    logger.debug(f'Title fell back to: {fallback!r}')
    return fallback
