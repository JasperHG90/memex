"""Per-section image-reference parser for the page-index node grain.

Pure, deterministic, stdlib-only. Walks a single node's markdown text and
returns structured metadata for every embedded image it references. Three
syntaxes are recognised:

- standard markdown ``![alt](path "optional title")``
- Obsidian-style wiki embeds ``![[path]]``
- inline HTML ``<img src=... alt=...>``

External references (``http://`` / ``https://``) are not Memex-stored assets
and are skipped. Images inside fenced or inline code spans are not real refs
and are skipped.
"""

from __future__ import annotations

import re
from os.path import basename
from typing import Any

# A fence runs to its matching closer OR end-of-text (unterminated fences,
# common in truncated/streamed content, still suppress their body).
_FENCED_CODE_RE = re.compile(r'(```|~~~).*?(?:\1|\Z)', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`[^`\n]*`')

_MARKDOWN_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_WIKI_IMG_RE = re.compile(r'!\[\[([^\]]+)\]\]')
_HTML_IMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_HTML_SRC_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_HTML_ALT_RE = re.compile(r'\balt\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)

# Not Memex-stored assets: remote references and inline data URIs.
_SKIP_PREFIXES = ('http://', 'https://', 'data:')


def _is_skippable(path: str) -> bool:
    return path.lower().startswith(_SKIP_PREFIXES)


def _strip_code(text: str) -> str:
    """Blank out code spans so image-like syntax inside them is not parsed."""
    without_fenced = _FENCED_CODE_RE.sub('', text)
    return _INLINE_CODE_RE.sub('', without_fenced)


def _norm_alt(alt: str | None) -> str | None:
    if alt is None:
        return None
    alt = alt.strip()
    return alt or None


def extract_image_refs(text: str) -> list[dict[str, Any]]:
    """Return structured metadata for every Memex-stored image reference.

    Each entry is ``{path, alt_text, filename, scope: 'node'}``. Order follows
    first appearance in the text; duplicate paths are collapsed (first alt-text
    wins). Returns ``[]`` for empty input.
    """
    if not text:
        return []

    scanned = _strip_code(text)

    # Collect (position, path, alt) across all three syntaxes, then order by
    # first appearance so the stored list matches document order.
    candidates: list[tuple[int, str, str | None]] = []
    for m in _MARKDOWN_IMG_RE.finditer(scanned):
        candidates.append((m.start(), m.group(2), m.group(1)))
    for m in _WIKI_IMG_RE.finditer(scanned):
        candidates.append((m.start(), m.group(1), None))
    for m in _HTML_IMG_RE.finditer(scanned):
        tag = m.group(0)
        src_match = _HTML_SRC_RE.search(tag)
        if src_match is None:
            continue
        alt_match = _HTML_ALT_RE.search(tag)
        candidates.append(
            (m.start(), src_match.group(1), alt_match.group(1) if alt_match else None)
        )

    candidates.sort(key=lambda c: c[0])

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _pos, raw_path, alt in candidates:
        path = raw_path.strip()
        if not path or _is_skippable(path) or path in seen:
            continue
        seen.add(path)
        refs.append(
            {
                'path': path,
                'alt_text': _norm_alt(alt),
                'filename': basename(path),
                'scope': 'node',
            }
        )

    return refs
