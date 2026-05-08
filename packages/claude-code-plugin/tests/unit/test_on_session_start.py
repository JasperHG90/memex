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
    """write_count, file_edits, capture_count_* must all be wiped fresh."""
    state_dir = mock_memex.plugin_data / 'memex'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'write_count').write_text('99')
    (state_dir / 'file_edits').mkdir(exist_ok=True)
    (state_dir / 'file_edits' / 'stale').write_text('5 stalefile')
    (state_dir / 'capture_count_oldsession').write_text('a\nb\n')

    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )

    assert not (state_dir / 'write_count').exists()
    assert not (state_dir / 'file_edits').exists()
    assert not (state_dir / 'capture_count_oldsession').exists()


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
