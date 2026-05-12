"""Real-LM smoke test for ProposeContradictionWinner.

Drives one golden contradiction pair through the DSPy signature
against a live model. Gated by ``@pytest.mark.llm`` so normal CI
(without LLM credentials) skips it. Asserts the model returns a
parseable schema and a non-empty rationale.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.llm, pytest.mark.integration]


@pytest.mark.asyncio
async def test_real_lm_returns_parseable_schema() -> None:
    if not os.getenv('ANTHROPIC_API_KEY') and not os.getenv('OPENAI_API_KEY'):
        pytest.skip('no LLM credentials in env')

    import dspy

    from memex_core.memory.lint_llm.signatures import ProposeContradictionWinner

    lm = dspy.LM(
        model=os.getenv('MEMEX_TEST_LM_MODEL', 'openai/gpt-4o-mini'),
        cache=False,
    )
    predictor = dspy.Predict(ProposeContradictionWinner)

    with dspy.context(lm=lm):
        prediction = await predictor.acall(
            unit_a_text='The deploy runs at 09:00 UTC daily.',
            unit_b_text='Deploys happen at 17:00 UTC on weekdays.',
            unit_a_created_at='2026-04-01T00:00:00+00:00',
            unit_b_created_at='2026-05-10T00:00:00+00:00',
            unit_a_source_credibility=0.5,
            unit_b_source_credibility=0.85,
            unit_a_source_authority='chat-log',
            unit_b_source_authority='official-doc',
            fsfm_evidence={'flag_reason': 'low_credibility_contradiction_only'},
        )

    assert prediction.winner_id in ('unit_a', 'unit_b', 'inconclusive')
    assert prediction.loser_id in ('unit_a', 'unit_b', 'none')
    assert prediction.action in (
        'mark_loser_stale',
        'supersede_loser_note',
        'refine_not_contradict',
        'inconclusive',
    )
    assert 0.0 <= float(prediction.confidence) <= 1.0
    assert prediction.rationale.strip() != ''
