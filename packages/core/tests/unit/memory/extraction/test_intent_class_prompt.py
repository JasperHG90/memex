"""P9 + round-6 H5: prompt cue-word taxonomy guards.

These are not LLM tests — they're static prompt-content guards. The
``intent_class`` field description and the ``ExtractSemanticFacts``
signature docstring are the two surfaces DSPy emits to the LLM. If
either drifts away from the cue-word taxonomy without an explicit
update, this test catches it. A real LLM regression test belongs in
``packages/core/tests/integration/`` and depends on credentials; the
guard here keeps the prompt structure stable in the meantime.
"""

from __future__ import annotations

from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.memory.extraction.models import RawFact


def test_rawfact_intent_class_description_carries_cue_taxonomy() -> None:
    desc = RawFact.model_fields['intent_class'].description or ''
    # The "4+ weeks" decision rule is the durable signal that survives
    # prompt rephrasing. Removing it would silently revert to the legacy
    # 1-line "permanent / durable / ephemeral" prompt.
    assert '4+ WEEKS' in desc.upper(), (
        'intent_class description must reference the "4+ weeks" decision rule '
        '— the durability question that disambiguates cue words from semantic '
        'content.'
    )
    # Each class must be named with at least one cue-word example so the
    # LLM can pattern-match.
    for tag in ('permanent', 'durable', 'ephemeral'):
        assert f"'{tag}'" in desc, f'intent_class taxonomy missing {tag!r}'
    # At least one of the canonical cue phrases per class.
    for cue in ('I am', 'we decided', 'by EOD'):
        assert cue in desc, f'intent_class description missing cue phrase {cue!r}'


def test_extract_semantic_facts_doc_carries_cue_taxonomy() -> None:
    doc = ExtractSemanticFacts.__doc__ or ''
    assert '4+ WEEKS' in doc.upper(), (
        'ExtractSemanticFacts docstring must reference the "4+ weeks" decision rule.'
    )
    for tag in ('permanent', 'durable', 'ephemeral'):
        assert f"'{tag}'" in doc
    # The H5 fix specifically softened "today" to require semantic
    # content to win — the override-clause assertion lives in the
    # next test (``test_..._warns_against_today_overfit``) where it
    # uses an OR check across acceptable phrasings. Asserting "identity"
    # here is enough to anchor the durable-vs-ephemeral semantic axis.
    assert 'identity' in doc


def test_extract_semantic_facts_doc_warns_against_today_overfit() -> None:
    """H5: a "today" prefix on a durable announcement must NOT push the
    fact to ephemeral. The docstring must explicitly warn about this."""
    doc = ExtractSemanticFacts.__doc__ or ''
    # Either explicit "transient prefix" warning OR "today we announced" example.
    has_transient_warning = 'transient prefix' in doc or 'transient cue' in doc
    has_today_example = 'today we announced' in doc
    assert has_transient_warning or has_today_example, (
        'ExtractSemanticFacts docstring must guard against the "today" '
        'cue-word overfitting durable announcements to ephemeral.'
    )
