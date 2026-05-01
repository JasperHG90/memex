"""F9 — LLM-driven verb selection (entity-conflict / vault-audit).

Two real-LLM tests proving the Hermes-side schema descriptions actually steer
a tool-using model to pick the right verb for the right shape of work:

- Given an entity-conflict prompt ("two facts disagree about X"), the model
  should call `memex_memory_reconsolidate(entity_id, vault_id)` — NOT the
  vault-wide `memex_memory_consolidate` and NOT `memex_memory_summarize_node`.
- Given a vault-audit prompt ("clean up the vault, look for stale memory"),
  the model should call `memex_memory_consolidate(vault_id, dry_run=true)`.

Skipped when GEMINI_API_KEY/GOOGLE_API_KEY missing; uses the same model
pinning as the rest of the repo's LLM-gated tests (gemini-3-flash-preview).
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import uuid

import pytest

_HAS_GEMINI_KEY = bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))
_HAS_LITELLM = _ilu.find_spec('litellm') is not None


def _f9_tools() -> list[dict]:
    """Bundle reconsolidate + consolidate + summarize_node + deprioritize so the
    model has to actually pick by description, not by lonely-verb-in-list."""
    from memex_hermes_plugin.memex.tools import (
        MEMORY_CONSOLIDATE_SCHEMA,
        MEMORY_DEPRIORITIZE_SCHEMA,
        MEMORY_RECONSOLIDATE_SCHEMA,
        MEMORY_SUMMARIZE_NODE_SCHEMA,
    )

    return [
        {
            'type': 'function',
            'function': {
                'name': s['name'],
                'description': s['description'],
                'parameters': s['parameters'],
            },
        }
        for s in (
            MEMORY_RECONSOLIDATE_SCHEMA,
            MEMORY_CONSOLIDATE_SCHEMA,
            MEMORY_SUMMARIZE_NODE_SCHEMA,
            MEMORY_DEPRIORITIZE_SCHEMA,
        )
    ]


@pytest.mark.llm
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_entity_conflict_prompt_routes_to_reconsolidate():
    """Two-facts-disagree prompt should pick reconsolidate, not consolidate or summarize_node."""
    import litellm

    entity_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'user',
                    'content': (
                        f'I just noticed that two memories about entity {entity_id} '
                        'in vault '
                        f'{vault_id} disagree with each other. One says the '
                        'project deploys to AWS; another says GCP. The '
                        'contradiction is concrete and entity-specific. Resolve '
                        'this — re-evaluate the memories about that one entity '
                        'and detect the contradiction so the mental model gets '
                        "updated. Don't run a vault-wide audit."
                    ),
                }
            ],
            tools=_f9_tools(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    tool_calls = resp.choices[0].message.tool_calls or []
    assert tool_calls, f'model did not call a tool: {resp.choices[0].message!r}'
    chosen = tool_calls[0].function.name
    assert chosen == 'memex_memory_reconsolidate', (
        f'model picked {chosen!r}; expected memex_memory_reconsolidate. '
        'Description must distinguish entity-scoped (reconsolidate) from '
        'vault-scoped (consolidate) and from sync-reflect (summarize_node).'
    )
    args = json.loads(tool_calls[0].function.arguments)
    assert 'entity_id' in args and 'vault_id' in args, (
        f'reconsolidate call missing required args: {args!r}'
    )


@pytest.mark.llm
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_vault_audit_prompt_routes_to_consolidate_dry_run():
    """Periodic vault-audit prompt should pick consolidate with dry_run=true."""
    import litellm

    vault_id = str(uuid.uuid4())

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'user',
                    'content': (
                        f"It's time for the monthly vault hygiene pass on "
                        f'vault {vault_id}. I want to identify low-quality '
                        'memories across the entire vault that have accumulated '
                        'enough negative outcomes to be deprioritized — but '
                        'BEFORE making changes, give me a preview of what '
                        "would be deprioritized. Don't act on a single "
                        'unit; do the vault-wide batch review.'
                    ),
                }
            ],
            tools=_f9_tools(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    tool_calls = resp.choices[0].message.tool_calls or []
    assert tool_calls, f'model did not call a tool: {resp.choices[0].message!r}'
    chosen = tool_calls[0].function.name
    assert chosen == 'memex_memory_consolidate', (
        f'model picked {chosen!r}; expected memex_memory_consolidate. '
        'Description must steer vault-wide audit prompts to the batch verb.'
    )
    args = json.loads(tool_calls[0].function.arguments)
    assert 'vault_id' in args, f'consolidate call missing vault_id: {args!r}'
    # The user explicitly asked for a preview; dry_run should be true.
    assert args.get('dry_run') is True, (
        f'user asked for a preview; expected dry_run=true. Got args={args!r}'
    )
