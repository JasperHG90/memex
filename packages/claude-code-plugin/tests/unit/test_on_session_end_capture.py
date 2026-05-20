"""Unit tests for ``scripts/on_session_end_capture.sh``.

Pins the SessionEnd safety-net contract:
- Gate on `reason`: skip clear/resume/bypass_permissions_disabled, capture
  prompt_input_exit/logout/other.
- Coordinate with PreCompact via the shared offset + created-flag files.
- Add when no prior compaction exists; append otherwise.
- Tag the capture with `session-end:<reason>`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _helpers import MockMemex, run_script


def _session_end_payload(
    *, reason: str = 'prompt_input_exit', transcript_path: str, session_id: str = 'cc-sess-1'
) -> str:
    return json.dumps(
        {
            'session_id': session_id,
            'transcript_path': transcript_path,
            'cwd': '/tmp',
            'hook_event_name': 'SessionEnd',
            'reason': reason,
        }
    )


def _seed_session_start_state(
    mock: MockMemex, *, note_key: str = 'session:2026-05-08T12:00:00.000'
) -> None:
    state = mock.plugin_data / 'memex'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'session_note_key').write_text(note_key)
    (state / 'project_id').write_text('github.com/acme/myapp')


@pytest.mark.parametrize('skipped_reason', ['clear', 'resume', 'bypass_permissions_disabled'])
def test_skipped_reasons_do_not_capture(
    mock_memex: MockMemex, transcript_jsonl: Path, skipped_reason: str
) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(reason=skipped_reason, transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    assert mock_memex.calls() == []  # No memex side-effects


@pytest.mark.parametrize('captured_reason', ['prompt_input_exit', 'logout', 'other'])
def test_captures_on_normal_exit_reasons(
    mock_memex: MockMemex,
    transcript_jsonl: Path,
    temp_git_repo: Path,
    captured_reason: str,
) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(reason=captured_reason, transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1
    argv = add_calls[0]['argv']
    # Tag includes session-end:<reason>
    tag_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == '--tag']
    assert f'session-end:{captured_reason}' in tag_pairs


def test_uses_append_when_compact_already_ran(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    _seed_session_start_state(mock_memex)
    state = mock_memex.plugin_data / 'memex'
    # Simulate PreCompact having created the note + advanced offset to line 4
    (state / 'session_note_created_cc-sess-1').touch()
    (state / 'session_note_offset_cc-sess-1').write_text('4')

    # Add 2 new turns post-compact
    transcript_jsonl.write_text(
        transcript_jsonl.read_text()
        + json.dumps({'role': 'user', 'content': 'late question'})
        + '\n'
        + json.dumps({'role': 'assistant', 'content': 'late answer'})
        + '\n'
    )
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    # Append, not add
    assert mock_memex.calls_matching('note', 'append'), 'Expected append call'
    assert not mock_memex.calls_matching('note', 'add'), (
        'Must not re-create the note when one already exists'
    )
    # Offset advanced to 6
    assert int((state / 'session_note_offset_cc-sess-1').read_text().strip()) == 6


def test_skips_when_no_new_turns_and_note_exists(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    _seed_session_start_state(mock_memex)
    state = mock_memex.plugin_data / 'memex'
    (state / 'session_note_created_cc-sess-1').touch()
    (state / 'session_note_offset_cc-sess-1').write_text('4')

    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    assert mock_memex.calls_matching('note', 'append') == []
    assert mock_memex.calls_matching('note', 'add') == []


def test_creates_fallback_session_note_key_when_state_missing(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Even if SessionStart never ran cleanly, SessionEnd should still capture."""
    # Don't seed any state — note_key file is missing
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1
    argv = add_calls[0]['argv']
    key_idx = argv.index('--key')
    # Must have generated a fallback session: key
    assert argv[key_idx + 1].startswith('session:')


def test_unknown_reason_captures_defensively(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(reason='made_up_reason', transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    assert len(mock_memex.calls_matching('note', 'add')) == 1


def test_missing_transcript_path_is_silent(mock_memex: MockMemex) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path='/nonexistent.jsonl'),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    assert mock_memex.calls() == []


def test_empty_payload_is_silent(mock_memex: MockMemex) -> None:
    _seed_session_start_state(mock_memex)
    result = run_script('on_session_end_capture.sh', stdin='', env=mock_memex.env)
    assert result.returncode == 0
    assert mock_memex.calls() == []


# ---------------------------------------------------------------------------
# V4: MEMEX_CC_TRANSCRIPT_CAPTURE opt-out
# ---------------------------------------------------------------------------


def test_capture_skipped_when_MEMEX_CC_TRANSCRIPT_CAPTURE_off(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Disabled: no `memex note add` / `note append` call, hook exits clean."""
    _seed_session_start_state(mock_memex)
    result = run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': 'off'},
    )
    assert result.returncode == 0
    assert not mock_memex.calls_matching('note', 'add')
    assert not mock_memex.calls_matching('note', 'append')


def test_session_end_disabled_does_not_advance_offset(
    mock_memex: MockMemex, transcript_jsonl: Path, temp_git_repo: Path
) -> None:
    """Offset file untouched under disabled capture — re-enabling resumes from prior offset."""
    _seed_session_start_state(mock_memex)
    state_dir = mock_memex.plugin_data / 'memex'
    offset_file = state_dir / 'session_note_offset_cc-sess-1'
    state_dir.mkdir(parents=True, exist_ok=True)
    offset_file.write_text('5')

    run_script(
        'on_session_end_capture.sh',
        stdin=_session_end_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_TRANSCRIPT_CAPTURE': 'off'},
    )
    assert offset_file.read_text().strip() == '5', (
        'offset advanced even though capture was disabled'
    )
