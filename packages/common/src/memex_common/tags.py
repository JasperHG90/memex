"""Protected-tag registry.

Some tags carry *system* meaning rather than describing topic. They fall in two
groups:

* **Reserved semantic tags** — exact literals that gate a behaviour (e.g.
  ``handoff`` is how the Claude Code ``/continue`` skill discovers handoff notes;
  ``case`` marks ``role='case'`` notes; ``file-import`` is ingestion provenance).
* **Provenance tags** — namespaced literals (``surface:*``, ``project:*``,
  ``session:*``, ``git:*``, …) stamped by ambient capture (the
  ``inject_memex_tags`` hook and the session-capture scripts). They record an
  origin, not a subject.

A *caller* may legitimately supply such a tag (the ``/handoff`` skill passes
``tags=["handoff"]``); those are authoritative. The danger is the content
*inference* path: the PageIndex LLM, summarising a transcript that happens to be
about handoff skills, will freely emit ``handoff`` as a topical tag — which then
poisons every tag-scoped query that relies on the tag's reserved meaning.

``is_protected_tag`` lets the extraction layer strip protected tags from the
*inferred* set only, while leaving caller-supplied tags untouched.
"""

from __future__ import annotations

# Exact-match reserved semantic tags. Each gates a system behaviour:
#   handoff            -> /continue handoff discovery (claude-code-plugin)
#   case               -> case notes (role='case'); see services/case_service.py
#   file-import        -> file-ingestion provenance; see services/ingestion.py
#   session-transcript -> auto-captured session-transcript marker (capture script)
#   auto-capture       -> auto-capture provenance marker (capture script)
RESERVED_SEMANTIC_TAGS: frozenset[str] = frozenset(
    {
        'handoff',
        'case',
        'file-import',
        'session-transcript',
        'auto-capture',
    }
)

# Provenance namespaces. A tag of the form ``<namespace>:...`` (e.g.
# ``project:github.com/...``, ``git:branch=...``, ``claude:model=...``) is
# stamped by ambient capture to record origin. The inference LLM must never mint
# one. Matched on the segment before the first ``:`` so bare topical words that
# merely coincide with a namespace (e.g. a lone ``project`` tag) are NOT treated
# as provenance.
PROVENANCE_TAG_NAMESPACES: frozenset[str] = frozenset(
    {
        'surface',
        'project',
        'session',
        'session-end',
        'git',
        'claude',
        'app',
    }
)


def is_protected_tag(tag: str) -> bool:
    """Return True if ``tag`` is reserved (semantic) or provenance-namespaced.

    Case- and whitespace-insensitive. Protected tags may be supplied verbatim by
    a caller (authoritative), but MUST be stripped from LLM-inferred tags so that
    content *about* a reserved concept does not mint the reserved tag.
    """
    normalized = tag.strip().lower()
    if not normalized:
        return False
    if normalized in RESERVED_SEMANTIC_TAGS:
        return True
    if ':' in normalized:
        namespace = normalized.split(':', 1)[0]
        if namespace in PROVENANCE_TAG_NAMESPACES:
            return True
    return False
