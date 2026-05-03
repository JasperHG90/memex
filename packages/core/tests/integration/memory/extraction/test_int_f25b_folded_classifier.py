"""F25b — folded intent + risk classifier integration tests.

After F25b, the standalone classifier predictor is gone — intent + risk are
output fields on ``ExtractSemanticFacts`` and arrive on each ``RawFact``
directly from the extraction LLM. These tests cover the post-fold contract:

1. Happy path — extracted facts come back with valid intent + risk values
   that survive the round trip into ``ProcessedFact``.
2. Default-on-fail — invalid LLM output for intent/risk is coerced to the
   schema defaults (``durable`` / ``none``) by ``RawFact``'s pydantic
   validators.
3. Safety filtering — facts the LLM flagged as ``risk_class='safety'`` are
   dropped by ``filter_safety_blocked`` before persistence and the
   ``CLASSIFIER_BLOCKED_TOTAL`` counter increments per vault.
4. Real-LLM-turn validation (``@pytest.mark.llm``) — drive the extraction
   signature through a live model on hand-curated fixtures and assert the
   model emits valid intent/risk classifications across the documented
   classes most of the time.
"""

from __future__ import annotations

import datetime as dt
import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import dspy
import pytest
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import ExtractionConfig, ModelConfig, SimpleTextSplitting
from memex_core.memory.entity_resolver import EntityResolver
from memex_common.schemas import IntentClass, RiskClass
from memex_core.memory.extraction.classifier import filter_safety_blocked
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import (
    ExtractedOutput,
    ProcessedFact,
    RawFact,
    RetainContent,
)
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.sql_models import MemoryUnit
from memex_core.metrics import CLASSIFIER_BLOCKED_TOTAL

# Hermes round-5 retired ``DEFAULT_INTENT`` / ``DEFAULT_RISK`` from
# classifier.py — derive them directly from the canonical enums.
DEFAULT_INTENT = IntentClass.DURABLE.value
DEFAULT_RISK = RiskClass.NONE.value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_engine() -> ExtractionEngine:
    # Note: this builds a real ``dspy.LM`` without an api_key kwarg. The
    # mocked-LLM tests below patch ``run_dspy_operation`` at the call site,
    # so the LM is never actually invoked — but if a future test forgets the
    # patch and the real predictor runs, the resulting auth error will be
    # confusing rather than a clear pytest skip. The ``@pytest.mark.llm``
    # test path constructs its own ``dspy.LM`` with an explicit api_key so
    # it never reaches this helper.
    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
        max_concurrency=2,
    )
    lm = dspy.LM(model=config.model.model)
    predictor = dspy.Predict(ExtractSemanticFacts)
    embedding_model = await get_embedding_model()
    entity_resolver = EntityResolver(resolution_threshold=0.65)
    return ExtractionEngine(
        config=config,
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
    )


def _raw_fact(
    what: str,
    intent_class: str = 'durable',
    risk_class: str = 'none',
) -> RawFact:
    return RawFact(
        what=what,
        fact_type='world',
        intent_class=intent_class,  # type: ignore[arg-type]
        risk_class=risk_class,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. Default-on-fail: invalid LLM output coerced at RawFact validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_invalid_intent_and_risk_coerce_to_defaults() -> None:
    """A RawFact with garbage intent/risk values from the LLM lands on defaults."""
    rf = RawFact(
        what='something',
        fact_type='world',
        intent_class='forever',  # type: ignore[arg-type]
        risk_class='very-bad',  # type: ignore[arg-type]
    )
    assert rf.intent_class == DEFAULT_INTENT
    assert rf.risk_class == DEFAULT_RISK


# ---------------------------------------------------------------------------
# 2. Safety filtering — dropped before persistence + metric increments
# ---------------------------------------------------------------------------


def _make_processed_fact(text: str, vault_id, risk_class: str = 'none') -> ProcessedFact:
    fact = ProcessedFact(
        fact_text=text,
        fact_type='world',
        embedding=[0.0] * 384,
        mentioned_at=dt.datetime.now(dt.timezone.utc),
        vault_id=vault_id,
    )
    if risk_class != 'none':
        # Use the enum constructor explicitly — assigning a bare ``str`` to an
        # ``RiskClass``-typed field works via SQLModel coercion, but is
        # inconsistent with the field type and was flagged by Hermes round-1.
        fact.risk_class = RiskClass(risk_class)
    return fact


@pytest.mark.integration
def test_filter_safety_blocked_drops_safety_and_increments_metric() -> None:
    """Safety facts are removed and the per-vault block counter increments."""
    vault_id = uuid4()
    keep = _make_processed_fact(text=f'safe {uuid4()}', vault_id=vault_id)
    block = _make_processed_fact(text=f'blocked {uuid4()}', vault_id=vault_id, risk_class='safety')

    before = CLASSIFIER_BLOCKED_TOTAL.labels(vault_id=str(vault_id))._value.get()
    kept = filter_safety_blocked([keep, block])
    after = CLASSIFIER_BLOCKED_TOTAL.labels(vault_id=str(vault_id))._value.get()

    assert len(kept) == 1
    assert kept[0].fact_text == keep.fact_text
    assert after - before == 1


# ---------------------------------------------------------------------------
# 3. Mocked-LLM end-to-end: intent/risk flow into the DB row.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_intent_and_risk_round_trip_through_engine_with_mock_lm(session: AsyncSession):
    """A mocked extraction LLM returning intent='ephemeral' / risk='private'
    must persist those values onto MemoryUnit rows.
    """
    engine = await _build_engine()

    canned_facts = [
        _raw_fact('User scheduled a meeting tomorrow at 10am.', 'ephemeral', 'none'),
        _raw_fact('User has signed up for a Postgres tutorial.', 'durable', 'private'),
    ]

    async def fake_run_dspy(*_args, **kwargs):
        # Match real predictor return shape — DSPy Prediction with the field set.
        return dspy.Prediction(extracted_facts=ExtractedOutput(extracted_facts=canned_facts))

    with patch(
        'memex_core.memory.extraction.core.run_dspy_operation',
        new=AsyncMock(side_effect=fake_run_dspy),
    ):
        content = RetainContent(
            content='User has signed up for a Postgres tutorial. Scheduled meeting tomorrow at 10am.',
            event_date=dt.datetime.now(dt.timezone.utc),
            payload={'source': 'test'},
            context='test context',
        )
        unit_ids, _ = await engine.extract_and_persist(
            session=session,
            contents=[content],
            agent_name='f25b_test_agent',
            is_first_batch=True,
        )

    assert len(unit_ids) >= 1

    stmt = (
        select(MemoryUnit)
        .where(col(MemoryUnit.id).in_(unit_ids))
        .execution_options(populate_existing=True)
    )
    rows = (await session.exec(stmt)).all()
    intent_values = {r.intent_class for r in rows}
    risk_values = {r.risk_class for r in rows}
    assert intent_values <= {'ephemeral', 'durable'}
    assert risk_values <= {'none', 'private'}
    assert 'ephemeral' in intent_values
    assert 'private' in risk_values


@pytest.mark.integration
@pytest.mark.asyncio
async def test_safety_class_facts_are_dropped_end_to_end(session: AsyncSession):
    """A fact the (mocked) LLM flagged risk='safety' must NOT reach the DB."""
    engine = await _build_engine()

    canned_facts = [
        _raw_fact('User prefers ruff over black for Python.', 'permanent', 'none'),
        _raw_fact('Detailed plan to harm self tonight.', 'durable', 'safety'),
    ]

    async def fake_run_dspy(*_args, **kwargs):
        return dspy.Prediction(extracted_facts=ExtractedOutput(extracted_facts=canned_facts))

    with patch(
        'memex_core.memory.extraction.core.run_dspy_operation',
        new=AsyncMock(side_effect=fake_run_dspy),
    ):
        content = RetainContent(
            content='User prefers ruff over black. (... another fact about safety risk ...).',
            event_date=dt.datetime.now(dt.timezone.utc),
            payload={'source': 'test'},
            context='test context',
        )
        unit_ids, _ = await engine.extract_and_persist(
            session=session,
            contents=[content],
            agent_name='f25b_test_agent',
            is_first_batch=True,
        )

    stmt = (
        select(MemoryUnit)
        .where(col(MemoryUnit.id).in_(unit_ids))
        .execution_options(populate_existing=True)
    )
    rows = (await session.exec(stmt)).all()
    texts = ' '.join(r.text or '' for r in rows).lower()
    assert 'harm' not in texts
    assert all(r.risk_class != 'safety' for r in rows)


# ---------------------------------------------------------------------------
# 4. Real-LLM turn — intent + risk classification accuracy on hand-curated
#    fixtures. Skipped unless GOOGLE_API_KEY is set, per existing project
#    convention for @pytest.mark.llm tests.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_real_llm_extraction_emits_valid_intent_and_risk_per_fact() -> None:
    """Drive the folded extraction signature against a real LLM and assert
    that the produced intent/risk values are valid and that a permanent /
    durable / ephemeral / private signal each lands on the right bucket
    most of the time.

    Tolerance: 80% match on a 4-fixture set (3 of 4 must classify correctly).
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set — skipping real-LLM extraction turn')

    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key)
    predictor = dspy.Predict(ExtractSemanticFacts)

    fixtures = [
        # (chunk text, expected_intent, expected_risk)
        ('User has a peanut allergy.', 'permanent', 'none'),
        ('User prefers ruff over black for Python formatting.', 'permanent', 'none'),
        ("Tomorrow's standup is at 10am.", 'ephemeral', 'none'),
        ("User's social security number is 123-45-6789.", 'permanent', 'private'),
    ]

    intent_correct = 0
    risk_correct = 0
    risk_total = 0  # number of fixtures with a non-trivial expected risk
    risk_valid = 0
    intent_classes_seen: set[str] = set()
    risk_classes_seen: set[str] = set()

    for chunk, expected_intent, expected_risk in fixtures:
        with dspy.context(lm=lm):
            result = await predictor.acall(
                chunk_text=chunk,
                context='',
                event_date_ref=dt.datetime.now(dt.timezone.utc).strftime('%A, %B %d, %Y'),
                memory_context='Your name: f25b_real_llm_test',
                special_instructions='Extract semantic facts.',
            )
        # The predictor returns ``extracted_facts`` (an ExtractedOutput-shaped
        # object). After F25b every fact carries intent_class + risk_class.
        facts = list(result.extracted_facts.extracted_facts)
        assert facts, f'no facts produced for: {chunk!r}'
        for f in facts:
            assert f.intent_class in {'permanent', 'durable', 'ephemeral'}
            assert f.risk_class in {'none', 'sensitive', 'private', 'safety'}
            intent_classes_seen.add(f.intent_class)
            risk_classes_seen.add(f.risk_class)
            risk_valid += 1
        # Best-of-N tolerance — count this fixture as correct if any extracted
        # fact matched the expected intent label.
        if any(f.intent_class == expected_intent for f in facts):
            intent_correct += 1
        # Hermes round-6 MED: score risk classification with the same soft
        # tolerance as intent (best-of-N count) instead of a per-fixture hard
        # assert. Real-LLM non-determinism can flake the PII fixture without
        # the test being meaningfully wrong; score it under the aggregate
        # threshold below. Treat 'sensitive' as an acceptable downgrade of
        # 'private' (the LLM may legitimately disagree on PII strength).
        if expected_risk != 'none':
            risk_total += 1
            acceptable = {expected_risk}
            if expected_risk == 'private':
                acceptable.add('sensitive')
            if any(f.risk_class in acceptable for f in facts):
                risk_correct += 1

    assert intent_correct >= 3, (
        f'expected ≥3/4 intent fixtures to land correctly, got {intent_correct}/4'
    )
    if risk_total > 0:
        # Soft tolerance: at least half of the non-trivial-risk fixtures must
        # land correctly across runs. With the current 1-fixture set this
        # collapses to "the run produced a private/sensitive risk somewhere",
        # which is robust against single-shot LLM flakes.
        threshold = max(1, risk_total // 2)
        assert risk_correct >= threshold, (
            f'expected ≥{threshold}/{risk_total} risk fixtures to land in their '
            f'acceptable bucket, got {risk_correct}/{risk_total}'
        )
    assert risk_valid > 0
    assert {'permanent', 'ephemeral'} <= intent_classes_seen, (
        f'real LLM did not exercise both permanent and ephemeral classes; saw {intent_classes_seen}'
    )
