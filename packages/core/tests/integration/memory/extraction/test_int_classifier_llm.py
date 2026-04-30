"""Real-LLM golden test for the F25 write-time intent + risk classifier.

Drives a real model through ``ClassifyMemoryUnit`` on hand-curated fixtures
that exercise each branch of the prompt. Validates that the model's
interpretation of the signature prose matches the documented semantics —
static schema assertions cannot prove this.

Skipped unless GOOGLE_API_KEY is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import dspy
import pytest

from memex_core.memory.extraction.classifier import (
    ClassifyMemoryUnit,
    classify_facts,
    filter_safety_blocked,
)
from memex_core.memory.extraction.models import FactTypes, ProcessedFact


def _fact(text: str, context: str = '') -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type=FactTypes.WORLD,
        embedding=[0.0] * 4,
        mentioned_at=datetime.now(timezone.utc),
        context=context,
        chunk_id=str(uuid.uuid4()),
    )


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_classifier_intent_distinguishes_permanent_durable_ephemeral():
    """Each documented intent class lands on its own bucket on representative input."""
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key)
    predictor = dspy.Predict(ClassifyMemoryUnit)

    facts = [
        _fact('User has a peanut allergy.'),
        _fact("User's home address is 123 Maple Street, Springfield."),
        _fact('User decided to migrate the auth service to Postgres for the Q3 rewrite.'),
        _fact("Tomorrow's standup is at 10am."),
        _fact('Bob is on vacation this week.'),
    ]

    semaphore = asyncio.Semaphore(3)
    await classify_facts(facts, lm=lm, predictor=predictor, semaphore=semaphore)

    intents = [f.intent_class for f in facts]
    risks = [f.risk_class for f in facts]

    # Permanent: identity / preferences / non-decaying personal facts.
    assert intents[0] == 'permanent', f'peanut allergy → permanent, got {intents[0]}'
    assert intents[1] == 'permanent', f'home address → permanent, got {intents[1]}'

    # Durable: project decisions, multi-week relevance.
    assert intents[2] == 'durable', f'auth migration decision → durable, got {intents[2]}'

    # Ephemeral: dated short-lived context.
    assert intents[3] == 'ephemeral', f"tomorrow's standup → ephemeral, got {intents[3]}"
    assert intents[4] == 'ephemeral', f"bob's vacation → ephemeral, got {intents[4]}"

    # None of these are remotely safety-class.
    assert all(r != 'safety' for r in risks), f'unexpected safety flag: {risks}'

    # The address is at minimum 'private' (PII).
    assert risks[1] in ('private', 'sensitive'), f'address risk → private/sensitive, got {risks[1]}'


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_classifier_risk_flags_pii_and_blocks_safety():
    """PII routes to private/sensitive; harm-planning gets safety; ordinary content is none."""
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key)
    predictor = dspy.Predict(ClassifyMemoryUnit)

    facts = [
        _fact('The user prefers ruff over black for Python formatting.'),
        _fact("User's social security number is 123-45-6789."),
        _fact('Detailed step-by-step plan for self-harm tonight including method choice.'),
    ]

    await classify_facts(facts, lm=lm, predictor=predictor)
    risks = [f.risk_class for f in facts]

    # Plain preference: no risk flag.
    assert risks[0] == 'none', f'tooling preference → none, got {risks[0]}'

    # SSN is canonical PII → private (preferred) or sensitive (acceptable).
    assert risks[1] in ('private', 'sensitive'), f'SSN risk → private/sensitive, got {risks[1]}'

    # Self-harm planning is the documented 'safety' trigger.
    assert risks[2] == 'safety', f'self-harm plan → safety, got {risks[2]}'

    # filter_safety_blocked must drop the safety unit and keep the rest.
    kept = filter_safety_blocked(facts)
    assert len(kept) == 2
    assert all(f.risk_class != 'safety' for f in kept)
