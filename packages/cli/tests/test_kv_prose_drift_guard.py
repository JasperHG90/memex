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
# corresponds to a real OrangeHermes-class regression. Matched
# case-insensitively against the source so reverts that re-introduce the
# original capitalisation (e.g. "Fuzzy search facts by ...") are caught.
_BANNED_KV_PHRASES = (
    'fact store',
    'lightweight structured memory',
    'write a fact to the',
    'get a fact by',
    'fuzzy search facts',
    'list all stored facts',
    'list all facts',
    'delete a fact',
    'storing/retrieving structured facts',
    'store a user fact or preference',
    'persist facts and preferences across sessions',
    'the fact or preference text to store',
    'the fact/value to store',
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding='utf-8')


def _kv_section(text: str, start_marker: str) -> str:
    """Slice ``text`` to the KV-relevant region starting at ``start_marker``.

    Scoping the drift guard to the KV region (rather than the whole file)
    avoids false positives if a banned phrase legitimately appears in a
    contrastive non-KV context elsewhere in the file (e.g. a memory-unit
    description that says "this is NOT a fact store").
    """
    return text[text.index(start_marker) :]


def _assert_no_banned_in(haystack: str, *, where: str) -> None:
    """Run all banned-phrase checks against ``haystack`` (case-insensitive)."""
    lowered = haystack.lower()
    for banned in _BANNED_KV_PHRASES:
        assert banned not in lowered, f'banned KV-prose phrase {banned!r} reappeared in {where}'


def test_hermes_tools_py_no_kv_fact_muddle():
    # tools.py is a 3000-line module; scope to the KV schemas onward so a
    # banned phrase appearing in (say) a memory-unit description elsewhere
    # in the file doesn't flag.
    text = _read('packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py')
    _assert_no_banned_in(
        _kv_section(text, 'KV_WRITE_SCHEMA'), where='hermes-plugin tools.py KV section'
    )


def test_hermes_briefing_py_no_kv_fact_muddle():
    # briefing.py is short (~200 lines) and entirely agent-facing prose
    # about all three storage layers, so whole-file scope is fine here.
    text = _read('packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py')
    _assert_no_banned_in(text, where='hermes-plugin briefing.py')


def test_mcp_server_py_no_kv_fact_muddle():
    # server.py is large; scope to the KV tool definitions onward.
    text = _read('packages/mcp/src/memex_mcp/server.py')
    _assert_no_banned_in(
        _kv_section(text, "name='memex_kv_write'"), where='mcp server.py KV section'
    )


def test_mcp_server_py_kv_routing_block_is_clean():
    # Also scope-check the KV IF/THEN routing block in the system prompt.
    text = _read('packages/mcp/src/memex_mcp/server.py')
    routing_idx = text.index('→ KV STORE')
    # Take a generous window past the start marker — the bullet runs ~10 lines.
    routing_block = text[routing_idx : routing_idx + 1200]
    _assert_no_banned_in(routing_block, where='mcp server.py KV routing block')


def test_cli_kv_py_no_kv_fact_muddle():
    # kv.py is the dedicated KV CLI module — the entire file is KV-relevant,
    # so whole-file scope is fine (no contrastive non-KV regions exist here).
    text = _read('packages/cli/src/memex_cli/kv.py')
    _assert_no_banned_in(text, where='cli kv.py')


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
    ):
        assert marker in text, f'{marker!r} missing from MCP storage model'
