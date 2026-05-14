"""F14 briefing tests (TC-F14-4): procedural-observations block + routing guide.

The Hermes briefing surface must:

* render a "Learned procedures (recent)" section when ``procedural_observations``
  is provided, exposing each procedure's success/failure counters and an
  actionability cue (record_outcome pairing);
* gracefully render an empty-state hint when the list is empty;
* document the ``procedure:`` namespace and the ``record_outcome``
  pairing in the routing guide so an agent can act without prior context.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import (
    _render_procedural_block,
    format_briefing_block,
)


def test_kv_write_description_documents_procedure_namespace_and_record_outcome_pairing():
    """The procedure: namespace and outcome pairing now live in the MCP
    ``memex_kv_write`` description (authoritative source) and in the hermes-
    plugin KV_WRITE_SCHEMA description (in-process mirror)."""
    from memex_hermes_plugin.memex.tools import KV_WRITE_SCHEMA
    from memex_mcp.server import mcp
    import asyncio

    # Hermes-side schema description (what hermes' agent loop sees).
    hermes_desc = KV_WRITE_SCHEMA['description']
    assert 'procedure:<verb>:<context-tag>' in hermes_desc
    assert 'memex_record_outcome' in hermes_desc
    assert 'target_type="kv_key"' in hermes_desc

    # MCP-side description (what MCP clients like Claude Code see).
    tool = asyncio.get_event_loop().run_until_complete(mcp.get_tool('memex_kv_write'))
    mcp_desc = tool.description
    assert 'procedure:<verb>:<context-tag>' in mcp_desc
    assert 'memex_record_outcome' in mcp_desc


def test_render_procedural_block_lists_top_observations_with_counters():
    """Each observation lifts its kv_key + counters into the rendered block."""
    rendered = _render_procedural_block(
        [
            {
                'kv_key': 'procedure:write_pr:commit-style',
                'success_co_count': 4,
                'failure_co_count': 1,
                'last_outcome_at': '2026-04-30T14:00:00Z',
            },
            {
                'kv_key': 'procedure:run_tests:python-monorepo',
                'success_co_count': 7,
                'failure_co_count': 0,
                'last_outcome_at': '2026-04-29T10:00:00Z',
            },
        ]
    )
    assert '### Learned procedures (recent)' in rendered
    assert '`procedure:write_pr:commit-style`' in rendered
    assert '4 success / 1 failure' in rendered
    assert '`procedure:run_tests:python-monorepo`' in rendered
    assert '7 success / 0 failure' in rendered
    # Actionability cue + outcome pairing must be present (RFC-007 §155-185).
    assert 'memex_record_outcome' in rendered
    assert 'target_type="kv_key"' in rendered
    assert 'include_history=true' in rendered


def test_render_procedural_block_caps_at_five_entries():
    """Renderer caps to 5 even if the caller passes more."""
    obs = [
        {
            'kv_key': f'procedure:verb_{i}:tag-{i}',
            'success_co_count': i,
            'failure_co_count': 0,
            'last_outcome_at': None,
        }
        for i in range(8)
    ]
    rendered = _render_procedural_block(obs)
    for i in range(5):
        assert f'verb_{i}:tag-{i}' in rendered, f'first 5 must be rendered (i={i})'
    for i in range(5, 8):
        assert f'verb_{i}:tag-{i}' not in rendered, f'overflow must be dropped (i={i})'


def test_render_procedural_block_empty_state_provides_onboarding_hint():
    """Empty list still emits a hint that procedure: keys exist + how to seed one."""
    rendered = _render_procedural_block([])
    assert '### Learned procedures (recent)' in rendered
    assert 'procedure:<verb>:<context-tag>' in rendered
    # Empty-state must NOT pretend any procedures exist
    assert ' success ' not in rendered


def test_format_briefing_block_includes_procedural_section_when_provided():
    """The procedural-observations block must appear in the formatted briefing."""
    block = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='my-project',
        session_note_key='session/2026-04-30',
        kv_instructions_if_no_vault=False,
        procedural_observations=[
            {
                'kv_key': 'procedure:edit_yaml:ci-config',
                'success_co_count': 2,
                'failure_co_count': 0,
                'last_outcome_at': '2026-04-30T15:00:00Z',
            }
        ],
    )
    assert '### Learned procedures (recent)' in block
    assert '`procedure:edit_yaml:ci-config`' in block
    assert '2 success / 0 failure' in block


def test_format_briefing_block_omits_procedural_section_when_none():
    """No section is emitted when procedural_observations is None or [].

    Default path stays unchanged so existing F32/F4/F5 briefing tests do not
    pick up an unexpected section.
    """
    block_none = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='my-project',
        session_note_key='session/2026-04-30',
        kv_instructions_if_no_vault=False,
    )
    assert '### Learned procedures (recent)' not in block_none

    block_empty = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='my-project',
        session_note_key='session/2026-04-30',
        kv_instructions_if_no_vault=False,
        procedural_observations=[],
    )
    assert '### Learned procedures (recent)' not in block_empty
