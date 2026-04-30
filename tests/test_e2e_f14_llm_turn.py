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
