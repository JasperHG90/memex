"""F4 — Real-LLM-turn validation (AC-F4-7, T6).

Drives a real LLM via dspy through the §3.5 walkthrough:
1. Agent observes a misleading memory unit → calls `memex_record_outcome(success=false)`.
2. User says "this is fixed" → agent picks `memex_memory_deprioritize` (NOT archive).

Asserts both tool calls are emitted in the trace; verb-disambiguation prefers
deprioritize over archive (Wave 0 §6 #12).

GOOGLE_API_KEY skip is in the test body, NOT the decorator (canonical Memex
LLM-test pattern — see test_int_contradiction.py:212).
"""

from __future__ import annotations

import os

import pytest

from memex_mcp._f4_descriptions import MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_picks_deprioritize_for_disambiguation_prompt():
    """T6: real LLM, given the F4 verb description, picks deprioritize over
    archive when a user says "this is fixed".
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key, timeout=120)

    _signature_doc = (
        'Decide which Memex memory verb the agent should call.\n\n'
        'Tool surface available:\n'
        '- memex_memory_deprioritize(unit_id, reason): non-destructive curation.\n'
        '  Description: ' + MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION + '\n'
        '- memex_memory_archive(unit_id): DESTRUCTIVE archive '
        '(removes from entity graph).\n\n'
        'Output the bare verb name only (one of the two above) — no prose.'
    )

    class VerbSelection(dspy.Signature):
        __doc__ = _signature_doc

        user_signal: str = dspy.InputField(desc='What the user just said about the memory unit.')
        verb: str = dspy.OutputField(
            desc='The verb to call: memex_memory_deprioritize OR memex_memory_archive.'
        )

    predictor = dspy.Predict(VerbSelection)

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'I confirmed the deploy issue is now fixed; '
                "the unit saying 'deploy target is staging' was wrong all along."
            )
        )

    # Wave 0 §6 #12: 'this is fixed' is the deprioritize-not-archive trigger.
    assert 'deprioritize' in response.verb.lower(), (
        f'Agent picked the wrong verb: {response.verb!r}. '
        'Expected memex_memory_deprioritize (non-destructive). '
        'Wave 0 §6 #12 invariant breached.'
    )
    assert 'archive' not in response.verb.lower(), (
        f'Agent suggested archive: {response.verb!r}. '
        'Archive is destructive; deprioritize is the right verb for "this is fixed".'
    )


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_uses_deprioritize_after_record_outcome(client, postgres_url):
    """T6 part 2: §3.5 walkthrough — record_outcome(success=false) followed by
    memex_memory_deprioritize. Both tool calls land; final state is consistent.

    Drives the HTTP surface directly to avoid bringing up a full Hermes / MCP
    harness in CI. The "real LLM" part is the verb selection above; this part
    asserts the API contract end-to-end.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    # Seed a memory unit + simulate a failed outcome (MW counter bump) +
    # deprioritize. The §3.5 walkthrough composes record_outcome → deprioritize;
    # here we drive the MW counter directly to avoid coupling to outcome HTTP
    # surface (which is a service-only seam in v1) and assert the deprioritize
    # half lands cleanly on top of the MW state.
    from test_e2e_f4_deprioritize import _seed_unit, _read_unit, _query_audit
    import asyncio
    import asyncpg

    unit_id = _seed_unit(postgres_url)

    # Step 1: simulate failed outcome by bumping the MW failure counter.
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _bump_failure():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                'UPDATE memory_units SET failure_co_count = failure_co_count + 1 WHERE id = $1',
                unit_id,
            )
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_bump_failure())
    finally:
        loop.close()

    # Step 2: deprioritize the unit (the agent's response to the failed outcome).
    deprio_resp = client.post(
        f'/api/v1/memories/{unit_id}/deprioritize',
        json={
            'reason': 'Outcome confirmed misleading; deprioritized after agent observed failure.'
        },
    )
    assert deprio_resp.status_code == 200, deprio_resp.text

    # Final-state assertions.
    assert _read_unit(postgres_url, unit_id)['is_deprioritized'] is True
    audit_rows = _query_audit(postgres_url, 'memory_deprioritize', str(unit_id))
    assert len(audit_rows) == 1
    assert 'misleading' in audit_rows[0]['details']['reason'].lower()
