"""Unit tests for ``scripts/on_session_start.sh`` — KV migration & state caching."""

from __future__ import annotations

import json
from pathlib import Path


from _helpers import MockMemex, run_script


def _session_start_payload(model: str = 'claude-opus-4-7') -> str:
    return json.dumps(
        {
            'session_id': 'cc-session-xyz',
            'transcript_path': '/tmp/x.jsonl',
            'cwd': '/tmp',
            'hook_event_name': 'SessionStart',
            'source': 'startup',
            'model': model,
        }
    )


def test_writes_session_note_key_state_file(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    state_dir = mock_memex.plugin_data / 'memex'
    note_key_file = state_dir / 'session_note_key'
    assert note_key_file.exists()
    note_key = note_key_file.read_text().strip()
    assert note_key.startswith('session:')
    # Must be a recognizable timestamp (YYYY-MM-DD prefix)
    assert note_key[8:18].count('-') == 2


def test_caches_model_from_payload(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(model='claude-haiku-4-5'),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    state_dir = mock_memex.plugin_data / 'memex'
    model_file = state_dir / 'model'
    assert model_file.exists()
    assert model_file.read_text().strip() == 'claude-haiku-4-5'


def test_caches_project_id_and_active_vault(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    state_dir = mock_memex.plugin_data / 'memex'
    assert (state_dir / 'project_id').read_text().strip() == 'github.com/acme/myapp'
    assert (state_dir / 'active_vault').read_text().strip() == 'eng-vault'


def test_legacy_kv_key_forward_migrates(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """SessionStart should auto-migrate the bare project: key into app:claude-code:*."""
    mock_memex.set_kv('project:github.com/acme/myapp:vault', 'old-vault')
    # New-namespace key intentionally absent
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    kv_text = mock_memex.kv_file.read_text()
    # Both keys now present with the same value
    assert 'project:github.com/acme/myapp:vault=old-vault' in kv_text
    assert 'app:claude-code:project:github.com/acme/myapp:vault=old-vault' in kv_text
    # And the resolved active vault matches
    assert (mock_memex.plugin_data / 'memex' / 'active_vault').read_text().strip() == 'old-vault'


def test_clears_stale_state_on_new_session(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """All per-session state files must be wiped fresh.

    Includes the PreCompact/SessionEnd `session_note_offset_*` /
    `session_note_created_*` family — otherwise they accumulate over
    hundreds of sessions and the offsets from previous sessions could
    nominally collide on the next session_id reuse.
    """
    state_dir = mock_memex.plugin_data / 'memex'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'write_count').write_text('99')
    (state_dir / 'file_edits').mkdir(exist_ok=True)
    (state_dir / 'file_edits' / 'stale').write_text('5 stalefile')
    (state_dir / 'capture_count_oldsession').write_text('a\nb\n')
    (state_dir / 'session_note_offset_oldsession').write_text('42')
    (state_dir / 'session_note_created_oldsession').write_text('')

    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )

    assert not (state_dir / 'write_count').exists()
    assert not (state_dir / 'file_edits').exists()
    assert not (state_dir / 'capture_count_oldsession').exists()
    assert not (state_dir / 'session_note_offset_oldsession').exists()
    assert not (state_dir / 'session_note_created_oldsession').exists()


def test_emits_additional_context_with_briefing(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    out = json.loads(result.stdout)
    assert 'hookSpecificOutput' in out
    ctx = out['hookSpecificOutput']['additionalContext']
    assert '(mock briefing content)' in ctx
    # Vault instruction surfaces the active vault
    assert 'eng-vault' in ctx
    # Auto-tag instruction must appear so the agent knows what's being injected
    assert 'Auto-injected metadata' in ctx
    assert 'session:' in ctx
    # The new namespaced KV key should appear in the "no vault set" branch
    # (not relevant here, but sanity-check the project_id is referenced)
    assert 'github.com/acme/myapp' in ctx


# ---------------------------------------------------------------------------
# Agent-surface composition (Phase 5 + round-2 hook fix).
# ---------------------------------------------------------------------------


def test_agent_surface_concatenates_before_briefing(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """The hook composes Tier 1b/2 (static, from `memex agent-surface`) +
    `---` separator + dynamic vault briefing (from `memex briefing`) in that
    order. Order matters — universal static content must sit in the
    cacheable prompt prefix, not after per-session state."""
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']

    # Both halves present
    surface_marker = '<mock-agent-surface target="claude-code"/>'
    briefing_marker = '(mock briefing content)'
    assert surface_marker in ctx, f'agent-surface block missing; ctx={ctx[:300]!r}'
    assert briefing_marker in ctx, f'briefing block missing; ctx={ctx[:300]!r}'

    # Order: agent-surface FIRST, then `---`, then briefing.
    surface_pos = ctx.index(surface_marker)
    briefing_pos = ctx.index(briefing_marker)
    sep_pos = ctx.find('\n---\n', surface_pos)
    assert surface_pos < sep_pos < briefing_pos, (
        f'wrong order: surface={surface_pos}, sep={sep_pos}, briefing={briefing_pos}'
    )

    # The hook invoked `memex agent-surface claude-code` (positional target form).
    surface_calls = mock_memex.calls_matching('agent-surface', 'claude-code')
    assert surface_calls, (
        f'expected `memex agent-surface claude-code` call; got calls='
        f'{[c.get("argv") for c in mock_memex.calls()]!r}'
    )


def test_agent_surface_failure_falls_back_to_briefing_only(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """When `memex agent-surface` fails (older plugin install pinned to a
    version that predates the subcommand), the hook degrades gracefully —
    no agent-surface block, but the briefing still lands in additionalContext."""
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    mock_memex.force_fail('agent-surface claude-code')

    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']

    # Briefing still lands
    assert '(mock briefing content)' in ctx
    # No agent-surface marker (the if-guard in the hook caught the failure)
    assert '<mock-agent-surface' not in ctx


def test_temp_files_cleaned_up_on_success(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """The hook's EXIT trap removes both `tmp_surface` and `tmp_briefing`
    on the success path. Verify by listing /tmp before/after and asserting
    no script-created temp files remain."""
    before = set(Path('/tmp').glob('tmp.*'))
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    after = set(Path('/tmp').glob('tmp.*'))
    leaked = after - before
    assert not leaked, f'hook leaked temp files: {leaked!r}'
