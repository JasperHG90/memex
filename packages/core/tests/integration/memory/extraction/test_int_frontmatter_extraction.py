"""Integration test for frontmatter extraction with real LLM."""

import datetime as dt

import dspy
import pytest

from memex_core.memory.extraction.core import extract_facts_from_frontmatter


@pytest.mark.llm
@pytest.mark.asyncio
async def test_frontmatter_extraction_with_real_llm():
    """Verify frontmatter extraction produces Person entity for author."""
    lm = dspy.LM('gemini/gemini-3-flash-preview')

    frontmatter_text = (
        '---\ncreated_by: Jasper Ginn\ncreated_date: 2025-05-24\ntitle: Test Document\n---\n'
    )

    facts = await extract_facts_from_frontmatter(
        frontmatter_text=frontmatter_text,
        event_date=dt.datetime(2025, 5, 24),
        lm=lm,
    )

    assert len(facts) >= 1, f'Expected at least one fact, got {len(facts)}'

    # At least one fact should mention Jasper Ginn
    jasper_facts = [f for f in facts if 'Jasper Ginn' in f.what]
    assert len(jasper_facts) >= 1, (
        f'Expected at least one fact mentioning Jasper Ginn, got: {[f.what for f in facts]}'
    )

    # At least one fact should have a Person entity
    all_entities = [e for f in facts for e in f.entities]
    person_entities = [
        e for e in all_entities if e.entity_type and 'person' in e.entity_type.lower()
    ]
    assert len(person_entities) >= 1, (
        f'Expected at least one Person entity, got: '
        f'{[(e.text, e.entity_type) for e in all_entities]}'
    )
