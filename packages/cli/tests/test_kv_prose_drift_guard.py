"""Cross-client prose drift guard for KV-store wording.

The OrangeHermes-class bug originates in agent-facing prose that overloads
the word ``fact`` across two storage layers:
- Memory units: extracted facts/observations/events. Append-only.
- KV store: namespaced operational state. Mutable upsert.

When a KV tool's description, help text, or routing block calls KV "a fact
store" or invites the agent to ``store a fact``, agents conflate the two
and try to write learned-fact summaries into KV instead of retaining notes.
The fix is to reserve the bare word ``fact`` for memory-unit contexts and
to use ``operational pointer`` / ``preference`` / ``binding`` /
``convention`` / ``KV entry`` for KV.

This test reads the Hermes plugin's ``tools.py``, the MCP server's
``server.py``, and the CLI's ``kv.py`` as plain text — it does not import
the modules — and asserts the regression-fence wordings are not present in
KV-related regions of any client.

If you legitimately need to introduce ``fact`` wording in a KV surface
(e.g. *contrast* with memory units), explicitly add a phrase containing
"NOT for facts" or similar disambiguation in the same paragraph; the guard
checks the *banned* phrases, not the bare word, so contrastive uses are fine.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


# Phrases that must NOT appear anywhere in agent-facing KV prose. Each phrase
# corresponds to a real OrangeHermes-class regression.
_BANNED_KV_PHRASES = (
    'fact store',
    'lightweight structured memory',
    'Write a fact to the',
    'Get a fact by',
    'fuzzy search facts',
    'list all stored facts',
    'List all facts',
    'Delete a fact',
    'storing/retrieving structured facts',
    'store a user fact or preference',
    'persist facts and preferences across sessions',
    'The fact or preference text to store',
    'The fact/value to store',
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding='utf-8')


def test_hermes_tools_py_no_kv_fact_muddle():
    text = _read('packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py')
    for banned in _BANNED_KV_PHRASES:
        assert banned not in text, (
            f'banned KV-prose phrase {banned!r} reappeared in hermes-plugin tools.py'
        )


def test_hermes_briefing_py_no_kv_fact_muddle():
    text = _read('packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py')
    for banned in _BANNED_KV_PHRASES:
        assert banned not in text, (
            f'banned KV-prose phrase {banned!r} reappeared in hermes-plugin briefing.py'
        )


def test_mcp_server_py_no_kv_fact_muddle():
    text = _read('packages/mcp/src/memex_mcp/server.py')
    for banned in _BANNED_KV_PHRASES:
        assert banned not in text, f'banned KV-prose phrase {banned!r} reappeared in mcp server.py'


def test_cli_kv_py_no_kv_fact_muddle():
    text = _read('packages/cli/src/memex_cli/kv.py')
    for banned in _BANNED_KV_PHRASES:
        assert banned not in text, f'banned KV-prose phrase {banned!r} reappeared in cli kv.py'


def test_storage_layer_terminology_is_consistent_in_hermes_briefing():
    """Hermes briefing must teach the three-layer storage model up front."""
    text = _read('packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py')
    assert '_STORAGE_MODEL_PRIMER' in text
    # Primer must contain the three layers and the append-only invariant.
    for marker in (
        '**Notes**',
        '**Memory units**',
        '**KV store**',
        'Append-only',
    ):
        assert marker in text, f'{marker!r} missing from Hermes storage primer'


def test_storage_layer_terminology_is_consistent_in_mcp_instructions():
    """MCP instructions must teach the three-layer storage model up front."""
    text = _read('packages/mcp/src/memex_mcp/server.py')
    assert 'STORAGE MODEL' in text
    # Storage model section must contain the three layers and append-only.
    for marker in (
        '**Notes**',
        '**Memory units**',
        '**KV store**',
        'Append-only',
        'never mark memory units',
    ):
        assert marker in text, f'{marker!r} missing from MCP storage model'
