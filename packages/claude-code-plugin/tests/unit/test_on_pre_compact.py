"""Unit tests for ``scripts/on_pre_compact.sh`` — append discarded turns."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _helpers import MockMemex, run_script


def _precompact_payload(transcript_path: str, session_id: str = 'cc-sess-1') -> str:
    return json.dumps(
        {
            'session_id': session_id,
            'transcript_path': transcript_path,
            'cwd': '/tmp',
            'permission_mode': 'default',
            'hook_event_name': 'PreCompact',
        }
    )


def _seed_session_start_state(
    mock: MockMemex, *, note_key: str = 'session:2026-05-08T12:00:00.000'
) -> None:
    state = mock.plugin_data / 'memex'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'session_note_key').write_text(note_key)
    (state / 'project_id').write_text('github.com/acme/myapp')


def test_skips_when_session_note_key_missing(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    """If SessionStart never ran, there's nothing to anchor the append to."""
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert 'session note key is missing' in out['hookSpecificOutput']['additionalContext']
    # No memex calls
    assert mock_memex.calls() == []


def test_first_compaction_creates_note(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    _seed_session_start_state(mock_memex)
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert '✓' in out['hookSpecificOutput']['additionalContext']

    # Should have called `memex note add` (first call → create) with the note_key
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1, f'Expected 1 note add call, got {len(add_calls)}'
    argv = add_calls[0]['argv']
    assert '--key' in argv
    key_idx = argv.index('--key')
    assert argv[key_idx + 1] == 'session:2026-05-08T12:00:00.000'
    assert '--vault' in argv
    vault_idx = argv.index('--vault')
    assert argv[vault_idx + 1] == 'eng-vault'
    assert '--background' in argv

    # Created-flag file written
    flag = mock_memex.plugin_data / 'memex' / 'session_note_created_cc-sess-1'
    assert flag.exists()

    # Offset file updated to total transcript line count
    offset_file = mock_memex.plugin_data / 'memex' / 'session_note_offset_cc-sess-1'
    assert offset_file.exists()
    assert int(offset_file.read_text().strip()) == 4  # 4-line transcript fixture


def test_second_compaction_uses_append_not_add(
    mock_memex: MockMemex, transcript_jsonl: Path, tmp_path: Path
) -> None:
    _seed_session_start_state(mock_memex)
    # Simulate the first compaction having already created the note
    state = mock_memex.plugin_data / 'memex'
    (state / 'session_note_created_cc-sess-1').touch()
    (state / 'session_note_offset_cc-sess-1').write_text('4')

    # Add 2 more turns to the transcript
    transcript_jsonl.write_text(
        transcript_jsonl.read_text()
        + json.dumps({'role': 'user', 'content': 'follow-up question'})
        + '\n'
        + json.dumps({'role': 'assistant', 'content': 'follow-up answer'})
        + '\n'
    )
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    # Should have called `memex note append`
    append_calls = mock_memex.calls_matching('note', 'append')
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(append_calls) == 1, f'Expected 1 append call, got {len(append_calls)}'
    assert len(add_calls) == 0, 'Must not re-create the note on second compaction'
    # Offset advanced to 6 lines
    assert int((state / 'session_note_offset_cc-sess-1').read_text().strip()) == 6


def test_no_new_turns_skips_capture(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    _seed_session_start_state(mock_memex)
    state = mock_memex.plugin_data / 'memex'
    (state / 'session_note_created_cc-sess-1').touch()
    # Mark all 4 lines as already captured
    (state / 'session_note_offset_cc-sess-1').write_text('4')

    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
    )
    out = json.loads(result.stdout)
    assert 'no new turns' in out['hookSpecificOutput']['additionalContext']
    assert mock_memex.calls_matching('note', 'append') == []
    assert mock_memex.calls_matching('note', 'add') == []


def test_offset_not_updated_when_capture_fails(
    mock_memex: MockMemex, transcript_jsonl: Path
) -> None:
    """If memex returns non-zero, the offset must NOT advance — the next
    compaction must retry the same content."""
    _seed_session_start_state(mock_memex)
    mock_memex.force_fail('note add')

    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert (
        'failed' in out['hookSpecificOutput']['additionalContext'].lower()
        or 'skipped' in out['hookSpecificOutput']['additionalContext'].lower()
    )

    state = mock_memex.plugin_data / 'memex'
    assert not (state / 'session_note_offset_cc-sess-1').exists()
    assert not (state / 'session_note_created_cc-sess-1').exists()


def test_missing_transcript_path_skips_capture(mock_memex: MockMemex) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload('/nonexistent.jsonl'),
        env=mock_memex.env,
    )
    out = json.loads(result.stdout)
    assert 'transcript_path missing' in out['hookSpecificOutput']['additionalContext']
    assert mock_memex.calls_matching('note', 'add') == []


def test_transcript_shrinkage_resets_offset(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """If the transcript file shrunk below the recorded offset (rotation,
    truncation), PreCompact must reset offset to 0 and re-capture from start
    rather than silently dropping the rotated content."""
    _seed_session_start_state(mock_memex)
    state = mock_memex.plugin_data / 'memex'
    (state / 'session_note_created_cc-sess-1').touch()
    # Pretend we previously captured 10 lines, then the file rotated to 4.
    (state / 'session_note_offset_cc-sess-1').write_text('10')

    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')

    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert '✓' in out['hookSpecificOutput']['additionalContext']
    # Offset re-anchored to actual line count
    assert int((state / 'session_note_offset_cc-sess-1').read_text().strip()) == 4
    # Append happened (note already existed)
    assert mock_memex.calls_matching('note', 'append'), 'Expected append after shrinkage reset'


def test_unreadable_transcript_skips_capture(mock_memex: MockMemex, tmp_path: Path) -> None:
    """A transcript_path that exists but isn't readable must be reported,
    not silently skipped under "no extractable text"."""
    import os

    _seed_session_start_state(mock_memex)
    transcript = tmp_path / 'unreadable.jsonl'
    transcript.write_text('{"role": "user", "content": "x"}\n')
    transcript.chmod(0o000)
    try:
        # Skip when running as root (chmod is bypassed)
        if os.getuid() == 0:
            pytest.skip('chmod 000 ineffective when running as root')
        result = run_script(
            'on_pre_compact.sh',
            stdin=_precompact_payload(str(transcript)),
            env=mock_memex.env,
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        # The hook must not silently drop the content with a misleading reason
        ctx = out['hookSpecificOutput']['additionalContext']
        assert 'not readable' in ctx or 'permissions' in ctx
    finally:
        transcript.chmod(0o644)


def test_null_content_jsonl_does_not_crash(
    mock_memex: MockMemex, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Lines with explicit ``"content": null`` must not crash the jq pipeline."""
    _seed_session_start_state(mock_memex)
    transcript = tmp_path / 'has_null.jsonl'
    lines = [
        {'role': 'user', 'content': 'real content'},
        {'role': 'assistant', 'content': None},
        {'role': 'user', 'content': 'after null'},
    ]
    transcript.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')

    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')

    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    # The non-null user turns must reach the captured note. Content is the
    # last positional argument after the `--` separator (introduced so
    # transcript lines starting with `-` aren't parsed as CLI flags).
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1
    argv = add_calls[0]['argv']
    assert '--' in argv, f'expected -- separator in argv, got {argv!r}'
    note_content = argv[argv.index('--') + 1]
    assert 'real content' in note_content
    assert 'after null' in note_content


def test_dash_prefixed_content_is_not_parsed_as_flag(
    mock_memex: MockMemex, tmp_path: Path, temp_git_repo: Path
) -> None:
    """Transcript turns starting with `-` (e.g. a bash log line) must not be
    misinterpreted as CLI flags by the underlying `memex note add` call. The
    `--` separator in `memex_persist_session_delta` is the load-bearing
    defense; assert it survives a dash-prefixed first turn.
    """
    transcript = tmp_path / 'transcript.jsonl'
    lines = [
        # First user turn starts with `--debug` — would shadow a real CLI
        # flag if injected directly.
        {'role': 'user', 'content': '--debug please diagnose the timeout'},
        {'role': 'assistant', 'content': 'Sure, here is what I see.'},
    ]
    transcript.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1, f'expected 1 note add call, got {add_calls!r}'
    argv = add_calls[0]['argv']
    # The `--` separator must come BEFORE any string carrying the dash-
    # prefixed turn; otherwise the CLI would have parsed `--debug` as a flag.
    assert '--' in argv
    sep_idx = argv.index('--')
    content = argv[sep_idx + 1]
    assert content.startswith('## Pre-compaction snapshot'), content
    assert '--debug please diagnose' in content


def test_session_stats_included_in_context(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    _seed_session_start_state(mock_memex)
    state = mock_memex.plugin_data / 'memex'
    (state / 'write_count').write_text('7')

    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
    )
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert '7 writes' in ctx


# ---------------------------------------------------------------------------
# V4: MEMEX_CC_TRANSCRIPT_CAPTURE opt-out
# ---------------------------------------------------------------------------


def test_pre_compact_skipped_when_MEMEX_CC_TRANSCRIPT_CAPTURE_off(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Disabled: no capture, but JSON output matches the existing skipped-path shape
    (additionalContext mentions reason + stats appendix)."""
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': 'off'},
    )
    assert result.returncode == 0, result.stderr
    assert not mock_memex.calls_matching('note', 'add')
    assert not mock_memex.calls_matching('note', 'append')

    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    # Shape parity: disabled-path uses the existing skipped-path output template.
    assert 'pre-compact capture skipped' in ctx
    assert 'MEMEX_CC_TRANSCRIPT_CAPTURE' in ctx
    # Stats appendix preserved (parity check)
    assert 'Session stats:' in ctx


@pytest.mark.parametrize('falsy', ['off', '0', 'false', 'no', 'disabled'])
def test_pre_compact_toggle_accepts_all_falsy_values(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path, falsy: str
) -> None:
    """All five falsy values suppress the capture — mirrors session-start parity test."""
    _seed_session_start_state(mock_memex)
    if mock_memex.calls_file.exists():
        mock_memex.calls_file.unlink()
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': falsy},
    )
    assert result.returncode == 0
    assert not mock_memex.calls_matching('note', 'add')
    assert not mock_memex.calls_matching('note', 'append')


@pytest.mark.parametrize('not_falsy', ['true', '1', 'yes', 'random-garbage', 'ON'])
def test_pre_compact_toggle_treats_unknown_value_as_on(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path, not_falsy: str
) -> None:
    """Anything not in the falsy set runs the capture — asymmetric one-way parser."""
    _seed_session_start_state(mock_memex)
    if mock_memex.calls_file.exists():
        mock_memex.calls_file.unlink()
    result = run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': not_falsy},
    )
    assert result.returncode == 0
    assert mock_memex.calls_matching('note', 'add'), (
        f'value {not_falsy!r} unexpectedly suppressed capture'
    )


def test_pre_compact_disabled_does_not_advance_offset(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Offset file untouched when capture is disabled. Pre-seed `5`; verify no advance."""
    _seed_session_start_state(mock_memex)
    state_dir = mock_memex.plugin_data / 'memex'
    offset_file = state_dir / 'session_note_offset_cc-sess-1'
    state_dir.mkdir(parents=True, exist_ok=True)
    offset_file.write_text('5')

    run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': 'off'},
    )
    assert offset_file.read_text().strip() == '5', (
        'offset advanced even though capture was disabled'
    )


def test_pre_compact_disabled_then_enabled_resumes_from_prior_offset(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Disabled run leaves offset alone; subsequent enabled run captures from the prior offset
    (no turns lost). Total lines in transcript_jsonl fixture is small (3 turns); pre-seed
    offset=1 and verify enabled capture happens after the disabled run."""
    _seed_session_start_state(mock_memex)
    state_dir = mock_memex.plugin_data / 'memex'
    offset_file = state_dir / 'session_note_offset_cc-sess-1'
    state_dir.mkdir(parents=True, exist_ok=True)
    offset_file.write_text('1')

    # Disabled run
    run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': 'off'},
    )
    assert offset_file.read_text().strip() == '1'
    assert not mock_memex.calls_matching('note', 'add')

    # Enabled run — should capture the remaining lines.
    run_script(
        'on_pre_compact.sh',
        stdin=_precompact_payload(str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1, 'enabled run after disabled should add the note'
    # Offset advanced past the seeded `1`.
    assert int(offset_file.read_text().strip()) > 1
