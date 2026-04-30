"""F32 Hermes briefing — diagnostics summary inlines into briefing block (Test 8).

Verifies that ``format_briefing_block`` accepts a diagnostics-summary dict and
renders a markdown block surfacing at least three documented fields
(manifold_status, unit_counts, top entities) — satisfying AC-F32-6.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import format_briefing_block


def test_diagnostic_summary_in_briefing_block():
    summary = {
        'vault_id': 'a-vault-id',
        'as_of': '2026-04-30T00:00:00Z',
        'manifold_status': 'pending',
        'unit_counts': {'active': 12, 'stale': 3, 'deprioritized': 1},
        'lint_pending_by_type': {},
        'cluster_count': None,
        'avg_mw_score': 0.61,
        'top_5_retrieved_entities': [
            {'entity_id': 'e1', 'name': 'Memex', 'volume': 9, 'avg_mw_score': 0.7},
            {'entity_id': 'e2', 'name': 'F32', 'volume': 4, 'avg_mw_score': 0.5},
        ],
    }

    block = format_briefing_block(
        briefing='# Briefing',
        vault_id='test-vault',
        project_id='proj-x',
        session_note_key='hermes:session:abc',
        kv_instructions_if_no_vault=False,
        diagnostics_summary=summary,
    )

    assert '### Memex diagnostics' in block

    # Field 1: manifold status (pending in this fixture; cluster_count null).
    assert 'pending' in block
    assert 'cluster_count' in block

    # Field 2: unit counts (active / stale / deprioritized).
    assert '12' in block and '3' in block and '1' in block
    assert 'active' in block and 'stale' in block and 'deprioritized' in block

    # Field 3: top entities.
    assert 'Memex' in block
    assert 'F32' in block

    # The verb is surfaced for agents.
    assert 'memex_get_diagnostics_summary' in block


def test_briefing_block_omits_diagnostics_when_summary_none():
    """No regression: omitting the summary param keeps the existing block shape."""
    block = format_briefing_block(
        briefing='# Briefing',
        vault_id='v',
        project_id='p',
        session_note_key='hermes:session:abc',
        kv_instructions_if_no_vault=False,
    )
    assert '### Memex diagnostics' not in block
    assert '# Briefing' in block
