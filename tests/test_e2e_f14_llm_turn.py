"""F14 — Real-LLM-turn validation (TC-F14-LLM, AC-F14-7).

Drives a real LLM via dspy through the F14 procedure-key surface to test
that, given the agent-facing description, the agent correctly chooses
the procedure-namespace verbs over alternatives.

Two cases:

1. **Capture flow** — user describes a how-to that worked → agent picks
   ``memex_kv_write`` against a ``procedure:<verb>:<context-tag>`` key
   over ``memex_add_note`` (the F14 verb-vs-note distinction in
   /remember SKILL.md).

2. **Recall + close-loop flow** — agent is about to perform a recurring
   task ("write a PR for this repo") → it picks
   ``memex_kv_get(key="procedure:write_pr:...")`` over
   ``memex_memory_search`` (procedure recall is shape-different from
   note recall) AND, after acting, it pairs with
   ``memex_record_outcome(target_type="kv_key", ...)`` to close the
   MW-counter loop.

GOOGLE_API_KEY skip is in the test body, NOT the decorator (canonical
Memex LLM-test pattern — see test_int_contradiction.py:212,
test_e2e_f5_llm_turn.py).
"""

from __future__ import annotations

import os

import pytest


async def _build_metastore_engine(postgres_url: str):
    """Construct + connect a metastore engine pointing at the testcontainers DB.

    Mirrors ``packages/core/tests/integration/conftest.py::metastore`` but
    inline so this E2E test doesn't need the package fixture. The full-loop
    test runs the OutcomeService against the same DB the FastAPI test
    client is reading via ``client``.
    """
    from urllib.parse import urlparse

    from memex_common.config import (
        PostgresInstanceConfig,
        PostgresMetaStoreConfig,
        SecretStr,
    )
    from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine

    # postgres_url comes in either postgresql+asyncpg:// or postgresql://
    # form; normalize so urlparse extracts user/host/port consistently.
    parsed = urlparse(postgres_url.replace('postgresql+asyncpg://', 'postgresql://'))
    config = PostgresMetaStoreConfig(
        instance=PostgresInstanceConfig(
            host=parsed.hostname or 'localhost',
            port=parsed.port or 5432,
            database=(parsed.path or '').lstrip('/'),
            user=parsed.username or 'postgres',
            password=SecretStr(parsed.password or 'postgres'),
        )
    )
    engine = AsyncPostgresMetaStoreEngine(config)
    await engine.connect()
    return engine


_PROCEDURE_SURFACE_DESCRIPTION = """
The Memex procedure: KV namespace (F14, RFC-007) is for compact, learned
how-tos owned by the agent. Keys MUST be shaped procedure:<verb>:<context-tag>
(e.g. procedure:write_pr:commit-style or procedure:run_tests:python-monorepo).
The agent owns the verb (the action it is taking); Memex stores observations
about how to ADAPT the verb to the specific context (the context-tag).

Tool surface available:

- memex_kv_write(value, key): WRITE-side. Use to capture a procedure that
  worked. The server stores a JSON envelope with active value + version
  + capped 5-version history.
- memex_kv_get(key, include_history=false): READ-side. Use to recall an
  active procedure value. Pass include_history=true to inspect the full
  envelope (active value + version + history).
- memex_record_outcome(target_type="kv_key", kv_key=..., success=...):
  CLOSE-LOOP after using a procedure. Increments per-(vault, key) Memory
  Worth counters so the procedure's MW score stays calibrated. Silence
  provides no learning signal.
- memex_add_note(title, markdown_content, ...): note-capture verb (not
  for procedures — notes are FACTS to recall, procedures are how-tos to
  EXECUTE).
- memex_memory_search(query): note/fact search (not procedure recall).
"""


def _build_predictor():
    """Build the dspy verb-selection predictor + LM. Lazy import to keep
    pytest collection cheap when GOOGLE_API_KEY is absent.
    """
    import dspy

    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key, timeout=120)

    _signature_doc = (
        'Decide which Memex tool the agent should call.\n\n'
        + _PROCEDURE_SURFACE_DESCRIPTION
        + '\n\nOutput the bare verb name only (one of: memex_kv_write, '
        'memex_kv_get, memex_record_outcome, memex_add_note, '
        'memex_memory_search) — no prose, no arguments.'
    )

    class VerbSelection(dspy.Signature):
        __doc__ = _signature_doc

        user_signal: str = dspy.InputField(
            desc='Description of what the user said and the agent state.'
        )
        verb: str = dspy.OutputField(
            desc=(
                'Which verb to call: memex_kv_write OR memex_kv_get OR '
                'memex_record_outcome OR memex_add_note OR memex_memory_search.'
            )
        )

    return dspy.Predict(VerbSelection), lm


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_picks_kv_write_for_procedure_capture():
    """TC-F14-LLM-1: user describes a how-to that worked → agent picks
    ``memex_kv_write`` (procedure-namespace) over ``memex_add_note``.

    The /remember SKILL.md F14 block says: "Use a procedure: key when the
    content is a how-to that you (or a future agent) will ACTUALLY EXECUTE;
    use memex_add_note when the content is a fact, decision, or piece of
    context to recall." This test asserts the LLM honors that distinction.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    predictor, lm = _build_predictor()

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'The user just walked through how to run the test matrix on '
                'this Python monorepo: "uv run pytest -m integration tests/, '
                'then check just docs-build, then prek for lint." It worked. '
                'Save this how-to so future sessions can repeat it.'
            )
        )

    verb = response.verb.lower()
    assert 'kv_write' in verb, (
        f'Agent picked the wrong verb: {response.verb!r}. '
        'Expected memex_kv_write — a how-to that the agent will EXECUTE '
        'belongs in the procedure: KV namespace, not as a note.'
    )
    assert 'add_note' not in verb, (
        f'Agent suggested memex_add_note: {response.verb!r}. '
        'Procedures are how-tos to execute; notes are facts to recall. '
        'F14 verb-vs-note distinction breached.'
    )


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_picks_kv_get_for_procedure_recall():
    """TC-F14-LLM-2: agent about to perform a recurring task → picks
    ``memex_kv_get`` against a ``procedure:`` key over ``memex_memory_search``.

    The /recall SKILL.md F14 step says to check the procedure: KV namespace
    BEFORE other surfaces when the user query is "how do I X?" or the agent
    is about to perform a recurring task.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    predictor, lm = _build_predictor()

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'The user just said: "Open a PR for the bug fix we just '
                'finished." The agent is about to write the PR title and '
                'body. The agent KNOWS this team has captured a procedure '
                'for PR commit-message style at procedure:write_pr:commit-style '
                'previously. The agent should look up that procedure before '
                'drafting the PR.'
            )
        )

    verb = response.verb.lower()
    assert 'kv_get' in verb, (
        f'Agent picked the wrong verb: {response.verb!r}. '
        'Expected memex_kv_get — procedure recall is shape-different from '
        'note recall (the procedure: namespace is the right surface for '
        '"how do I X?" queries).'
    )
    assert 'memory_search' not in verb, (
        f'Agent suggested memex_memory_search: {response.verb!r}. '
        'Notes/facts come from memory_search; procedures come from kv_get '
        'on procedure:<verb>:<context-tag>. F14 read-side breached.'
    )


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_closes_loop_with_record_outcome():
    """TC-F14-LLM-3: after USING a procedure key, agent picks
    ``memex_record_outcome(target_type="kv_key", ...)`` to close the loop.

    The /remember SKILL.md F14 block + the Hermes routing guide both say
    "After actually USING a procedure key in a turn (you read it via
    memex_kv_get and then performed the action), close the loop with
    memex_record_outcome(target_type='kv_key', kv_key=..., success=...)."
    Silence provides no learning signal.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    predictor, lm = _build_predictor()

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'The agent just READ procedure:write_pr:commit-style via '
                'memex_kv_get and used the procedure to draft a PR. The PR '
                'was approved by the user with no edits. The agent should '
                'now record that the procedure worked, so its Memory Worth '
                "counters reflect what's working."
            )
        )

    verb = response.verb.lower()
    assert 'record_outcome' in verb, (
        f'Agent picked the wrong verb: {response.verb!r}. '
        'Expected memex_record_outcome — close-loop after using a procedure '
        'is REQUIRED to keep MW counters calibrated. Silence is the F14 '
        'failure mode F1c is built to detect.'
    )


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_full_loop_briefing_to_mw_counter_increment(client, postgres_url):
    """TC-F14-LLM-FULL-LOOP: end-to-end agent loop that closes the F14 contract.

    Drives the full briefing → kv_get → act → record_outcome → counter
    increment → next-turn briefing reflects updated mw_score loop:

    1. Seed a procedure: KV key (the how-to content).
    2. Seed prior outcomes for that key (3s/1f → mw_score ≈ 0.667 baseline).
    3. Render the briefing data once (HTTP /procedure-observations) — the
       surface the next-turn agent sees. Capture pre-state mw_score.
    4. Real LLM (turn 1): given the briefing surface, agent picks
       ``memex_kv_get`` against the procedure key.
    5. Drive kv_get via HTTP — confirm the procedure body returns.
    6. Real LLM (turn 2): given post-action context (procedure used,
       outcome successful), agent picks ``memex_record_outcome``.
    7. Drive record_outcome via the service (no HTTP surface — service-only
       seam in v1, mirroring F4 walkthrough).
    8. Re-render the briefing data — assert mw_score moved upward by the
       expected delta (4s/1f → ≈0.7143, +0.0476).

    The PRE→POST mw_score delta is the load-bearing assertion — it proves
    the full loop closes and the next-turn briefing surface actually
    reflects the agent's prior outcome.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import asyncio
    from uuid import uuid4

    import dspy

    from memex_common.config import GLOBAL_VAULT_ID
    from memex_core.services.outcomes import OutcomeService, compute_mw_score

    proc_key = f'procedure:write_pr:full-loop-{uuid4().hex[:8]}'
    proc_body = (
        'Write the PR title in conventional-commit format (feat:/fix:/refactor:); '
        'open with a one-line summary; bullet the test plan; cite the issue.'
    )

    # --- Step 1: seed the procedure key via the kv put route. -----------
    put_resp = client.put('/api/v1/kv', json={'key': proc_key, 'value': proc_body})
    assert put_resp.status_code == 200, put_resp.text

    # --- Step 2: seed prior outcomes (3s / 1f → mw ≈ 0.667). ------------
    # No HTTP surface for record_outcome (service-only seam in v1, mirroring
    # F4 walkthrough at test_e2e_f4_llm_turn.py:92); drive the service
    # directly. Same path used to close the loop after the agent's turn.
    async def _seed_outcomes():
        engine = await _build_metastore_engine(postgres_url)
        try:
            outcomes = OutcomeService()
            async with engine.session() as session:
                for _ in range(3):
                    await outcomes.record_outcome(
                        session=session,
                        unit_ids=None,
                        success=True,
                        vault_id=str(GLOBAL_VAULT_ID),
                        target_type='kv_key',
                        kv_key=proc_key,
                    )
                await outcomes.record_outcome(
                    session=session,
                    unit_ids=None,
                    success=False,
                    vault_id=str(GLOBAL_VAULT_ID),
                    target_type='kv_key',
                    kv_key=proc_key,
                )
        finally:
            await engine.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed_outcomes())
    finally:
        loop.close()

    # --- Step 3: render briefing pre-state via /procedure-observations. ---
    pre_resp = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': str(GLOBAL_VAULT_ID), 'limit': 20},
    )
    assert pre_resp.status_code == 200, pre_resp.text
    pre_rows = [r for r in pre_resp.json() if r['kv_key'] == proc_key]
    assert len(pre_rows) == 1, (
        f'expected procedure to surface in briefing data; got {pre_resp.json()}'
    )
    pre_mw = pre_rows[0]['mw_score']
    expected_pre_mw = compute_mw_score(3, 1)  # 4/6 ≈ 0.6667
    assert pre_mw == pytest.approx(expected_pre_mw, abs=1e-6), (
        f'briefing pre-state mw_score={pre_mw} did not match seed (3s/1f → '
        f'{expected_pre_mw}); the briefing surface is not reflecting the '
        'procedure_outcomes table.'
    )

    # --- Steps 4 + 6: real LLM verb selection — two turns. -------------
    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key, timeout=120)

    predictor, _ = _build_predictor()
    with dspy.context(lm=lm):
        # Turn 1: agent observes the briefing → picks kv_get.
        turn1 = predictor(
            user_signal=(
                f'The next-turn agent briefing surfaces this procedure: '
                f'"{proc_key}" (mw_score={pre_mw:.3f}). The user just said: '
                '"Open a PR for the bug fix we just finished." The agent should '
                'first look up the procedure body before drafting.'
            )
        )

    assert 'kv_get' in turn1.verb.lower(), (
        f'Turn 1: agent picked {turn1.verb!r}. Expected memex_kv_get to read '
        'the procedure body before acting on the user request.'
    )

    # --- Step 5: drive kv_get HTTP — confirm body returns. -----------
    get_resp = client.get('/api/v1/kv/get', params={'key': proc_key})
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()['value'] == proc_body, (
        'kv_get did not return the seeded procedure body — briefing → read path is broken.'
    )

    # Turn 2: agent has used the procedure successfully → close the loop.
    with dspy.context(lm=lm):
        turn2 = predictor(
            user_signal=(
                f'The agent just READ "{proc_key}" via memex_kv_get, drafted '
                'the PR using that procedure body, and the PR was approved '
                "with no edits. To keep the procedure's Memory Worth "
                'calibrated, the agent should now record the outcome.'
            )
        )

    assert 'record_outcome' in turn2.verb.lower(), (
        f'Turn 2: agent picked {turn2.verb!r}. Expected memex_record_outcome '
        'to close the loop after a successful use of the procedure.'
    )

    # --- Step 7: drive record_outcome via the service (no HTTP route). --
    async def _close_loop_success():
        engine = await _build_metastore_engine(postgres_url)
        try:
            outcomes = OutcomeService()
            async with engine.session() as session:
                await outcomes.record_outcome(
                    session=session,
                    unit_ids=None,
                    success=True,
                    vault_id=str(GLOBAL_VAULT_ID),
                    target_type='kv_key',
                    kv_key=proc_key,
                )
        finally:
            await engine.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close_loop_success())
    finally:
        loop.close()

    # --- Step 8: re-render briefing — assert mw_score increased. ------
    post_resp = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': str(GLOBAL_VAULT_ID), 'limit': 20},
    )
    assert post_resp.status_code == 200, post_resp.text
    post_rows = [r for r in post_resp.json() if r['kv_key'] == proc_key]
    assert len(post_rows) == 1
    post_mw = post_rows[0]['mw_score']
    expected_post_mw = compute_mw_score(4, 1)  # 5/7 ≈ 0.7143
    assert post_mw == pytest.approx(expected_post_mw, abs=1e-6), (
        f'briefing post-state mw_score={post_mw} did not match expected '
        f"4s/1f → {expected_post_mw}; the agent's record_outcome did not "
        'propagate to the briefing surface.'
    )
    assert post_mw > pre_mw, (
        f'mw_score did not move upward: pre={pre_mw}, post={post_mw}. '
        'The full F14 loop (briefing → read → act → record_outcome → '
        'counter increment → next-turn briefing reflects new score) is '
        'broken — the load-bearing assertion of TC-F14-LLM-FULL-LOOP.'
    )
    # Visible delta for QA's stamp + paper trail.
    print(
        f'\n[TC-F14-LLM-FULL-LOOP] mw_score delta: pre={pre_mw:.4f} → '
        f'post={post_mw:.4f} (Δ=+{post_mw - pre_mw:.4f}); '
        f'success_co_count: 3 → 4, failure_co_count: 1 (unchanged).'
    )
