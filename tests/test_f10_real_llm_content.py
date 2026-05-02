"""F10 — real-LLM content-quality test for ``CheckSemanticContradiction`` (Task #35).

F10 ships unit tests with a mocked DSPy LM (deterministic golden responses)
and integration tests that inject a stub ``run_llm_check``. This file closes
the loop with a single ``@pytest.mark.llm`` test that drives the actual DSPy
signature F10 ships (:class:`memex_core.memory.lint_llm.signatures.\
CheckSemanticContradiction`) through a real LLM round-trip.

Why this matters: lint-quality regressions hide behind static schema
assertions — the signature can compile cleanly while a wording change in the
docstring causes the model to systematically miss contradictions. A real-LLM
turn is the only check that catches that class of bug.

Skip semantics:
  * No ``ANTHROPIC_API_KEY`` (and no ``GOOGLE_API_KEY`` fallback) → skipped.
  * The CI default ``-m "not llm"`` excludes this test, so secret-less runs
    are safe by construction.

Token budget:
  * Two short notes (~30 words each) — well under any minimum-cost ceiling.
  * Single signature call. No iteration, no chain-of-thought wrapper.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.llm
@pytest.mark.asyncio
async def test_check_semantic_contradiction_flags_known_contradiction():
    """Real LLM round-trip: F10's ``CheckSemanticContradiction`` must flag a
    known contradiction between two short notes about the same project.

    Sanity asserts (not just "≥1 finding emitted"):
      * ``has_contradiction`` is True.
      * ``contradiction_with_unit_indices`` cites the correct peer index.
      * ``explanation`` references the contradiction concretely (mentions the
        databases by name).
    """
    anthropic_key = os.environ.get('ANTHROPIC_API_KEY')
    google_key = os.environ.get('GOOGLE_API_KEY')
    if not anthropic_key and not google_key:
        pytest.skip('Neither ANTHROPIC_API_KEY nor GOOGLE_API_KEY is set')

    import dspy

    from memex_core.llm import run_dspy_operation
    from memex_core.memory.lint_llm.signatures import CheckSemanticContradiction

    if anthropic_key:
        lm = dspy.LM(
            model='anthropic/claude-haiku-4-5',
            api_key=anthropic_key,
            timeout=120.0,
        )
    else:
        lm = dspy.LM(
            model='gemini/gemini-3-flash-preview',
            api_key=google_key,
            timeout=120.0,
        )

    audited_unit = 'Project X uses Postgres as its primary database.'
    related_units = [
        'Project X migrated from Postgres to MySQL last quarter; Postgres is no longer in use.',
        'Project Y stores telemetry in S3.',
    ]

    predictor = dspy.Predict(CheckSemanticContradiction)

    try:
        result = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'unit_text': audited_unit,
                'related_units_text': related_units,
            },
            operation_name='test_f10_semantic_contradiction',
        )
    except Exception as exc:
        msg = str(exc)
        if (
            '429' in msg
            or 'RESOURCE_EXHAUSTED' in msg
            or 'rate' in msg.lower()
            or 'quota' in msg.lower()
            or 'overloaded' in msg.lower()
        ):
            pytest.skip(f'LLM provider rate-limited / overloaded; retry later: {exc}')
        raise

    has_contradiction = bool(getattr(result, 'has_contradiction', False))
    assert has_contradiction is True, (
        f'Expected the model to detect the Postgres↔MySQL inversion. Got: '
        f'has_contradiction={has_contradiction!r}, '
        f'explanation={getattr(result, "explanation", "")!r}'
    )

    indices = list(getattr(result, 'contradiction_with_unit_indices', []) or [])
    assert 0 in indices, (
        f'Expected index 0 (the Postgres→MySQL migration unit) in '
        f'contradiction_with_unit_indices, got: {indices!r}'
    )

    explanation = str(getattr(result, 'explanation', '') or '')
    assert explanation.strip(), 'Expected a non-empty explanation when has_contradiction is True'
    lower = explanation.lower()
    assert 'postgres' in lower and 'mysql' in lower, (
        f'Explanation must reference both databases by name. Got: {explanation!r}'
    )


@pytest.mark.llm
@pytest.mark.asyncio
async def test_check_semantic_contradiction_clears_compatible_facts():
    """Negative-case sanity: the signature's STRICT RULE says compatible
    facts (different aspects of the same topic) must NOT trip the contradiction
    flag. This guards against drift toward over-eager contradiction calls,
    which would flood F10's MaintenanceProposal queue.
    """
    anthropic_key = os.environ.get('ANTHROPIC_API_KEY')
    google_key = os.environ.get('GOOGLE_API_KEY')
    if not anthropic_key and not google_key:
        pytest.skip('Neither ANTHROPIC_API_KEY nor GOOGLE_API_KEY is set')

    import dspy

    from memex_core.llm import run_dspy_operation
    from memex_core.memory.lint_llm.signatures import CheckSemanticContradiction

    if anthropic_key:
        lm = dspy.LM(
            model='anthropic/claude-haiku-4-5',
            api_key=anthropic_key,
            timeout=120.0,
        )
    else:
        lm = dspy.LM(
            model='gemini/gemini-3-flash-preview',
            api_key=google_key,
            timeout=120.0,
        )

    audited_unit = 'Project X uses Postgres as its primary database.'
    related_units = [
        'Project X runs daily backups of Postgres to S3.',
        'Project X exposes a REST API on port 8080.',
    ]

    predictor = dspy.Predict(CheckSemanticContradiction)
    try:
        result = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'unit_text': audited_unit,
                'related_units_text': related_units,
            },
            operation_name='test_f10_semantic_contradiction_negative',
        )
    except Exception as exc:
        msg = str(exc)
        if (
            '429' in msg
            or 'RESOURCE_EXHAUSTED' in msg
            or 'rate' in msg.lower()
            or 'quota' in msg.lower()
            or 'overloaded' in msg.lower()
        ):
            pytest.skip(f'LLM provider rate-limited / overloaded; retry later: {exc}')
        raise

    has_contradiction = bool(getattr(result, 'has_contradiction', False))
    explanation = str(getattr(result, 'explanation', '') or '')
    assert has_contradiction is False, (
        f'Compatible facts must not trip has_contradiction. Got '
        f'has_contradiction={has_contradiction!r}, explanation={explanation!r}'
    )
