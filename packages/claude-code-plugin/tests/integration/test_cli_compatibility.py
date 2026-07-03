"""Integration tests: verify the CLI invocations the plugin scripts emit
parse cleanly through the real Memex Typer app (without spinning up a server).

This catches: renamed flags, missing required args, type mismatches — everything
short of actual server semantics. End-to-end server behavior is covered by
``test_e2e_lifecycle.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Existing project-wide CLI testing pattern (see tests/test_cli_note_search.py)
from typer.testing import CliRunner
from memex_cli.notes import app as notes_app
from memex_cli.kv import app as kv_app
from memex_common.config import MemexConfig
from memex_common.schemas import IngestResponse, NoteAppendResponse, KVEntryDTO

from _helpers import MockMemex, run_script

runner = CliRunner()


@pytest.fixture
def cli_config() -> MemexConfig:
    with patch.dict(
        os.environ, {'MEMEX_LOAD_LOCAL_CONFIG': 'false', 'MEMEX_LOAD_GLOBAL_CONFIG': 'false'}
    ):
        return MemexConfig()


def _mock_api_context(api_mock: AsyncMock):
    """Patch ``get_api_context`` so the CLI uses our mock instead of HTTP."""
    cm = AsyncMock()
    cm.__aenter__.return_value = api_mock
    cm.__aexit__.return_value = None
    notes_patch = patch('memex_cli.notes.get_api_context', return_value=cm)
    kv_patch = patch('memex_cli.kv.get_api_context', return_value=cm)
    return notes_patch, kv_patch


def test_session_end_capture_emits_valid_note_add_argv(
    mock_memex: MockMemex,
    transcript_jsonl: Path,
    temp_git_repo: Path,
    cli_config: MemexConfig,
) -> None:
    """SessionEnd hook → captures argv → real CLI parses without error."""
    state = mock_memex.plugin_data / 'memex'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'session_note_key').write_text('session:2026-05-08T12:00:00.000')
    (state / 'project_id').write_text('github.com/acme/myapp')

    import json

    payload = json.dumps(
        {
            'session_id': 'cc-int-1',
            'transcript_path': str(transcript_jsonl),
            'cwd': str(temp_git_repo),
            'hook_event_name': 'SessionEnd',
            'reason': 'prompt_input_exit',
        }
    )
    run_script(
        'on_session_end_capture.sh',
        stdin=payload,
        env=mock_memex.env,
        cwd=temp_git_repo,
    )

    add_calls = mock_memex.calls_matching('note', 'add')
    assert len(add_calls) == 1
    argv = add_calls[0]['argv']
    # First arg after `note add` is the positional content; CLI accepts it.
    assert argv[0:2] == ['note', 'add']

    # Drive the real Typer CLI with the same argv (skipping `note add` since
    # `notes_app` is the `note` Typer subgroup; CliRunner expects the
    # subcommand name as the first arg).
    cli_argv = argv[1:]  # drop the leading 'note', keep 'add' + the rest

    api = AsyncMock()
    api.ingest.return_value = AsyncMock(
        spec=IngestResponse,
        status='success',
        note_id='00000000-0000-0000-0000-000000000000',
        unit_ids=[],
        overlapping_notes=[],
    )
    notes_patch, _ = _mock_api_context(api)
    with notes_patch:
        result = runner.invoke(notes_app, cli_argv, obj=cli_config)
    assert result.exit_code == 0, (
        f'CLI rejected the argv emitted by the SessionEnd hook:\n'
        f'argv: {cli_argv}\nstdout: {result.stdout}\nexc: {result.exception}'
    )
    assert api.ingest.called


def test_pre_compact_emits_valid_append_argv_after_first_run(
    mock_memex: MockMemex,
    transcript_jsonl: Path,
    temp_git_repo: Path,
    cli_config: MemexConfig,
) -> None:
    """After the first compaction, subsequent compactions use ``note append``
    — which has stricter requirements (``--key`` requires ``--vault``)."""
    state = mock_memex.plugin_data / 'memex'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'session_note_key').write_text('session:2026-05-08T12:00:00.000')
    (state / 'project_id').write_text('github.com/acme/myapp')
    (state / 'session_note_created_cc-int-2').touch()
    (state / 'session_note_offset_cc-int-2').write_text('4')

    # Vault binding must exist so `--vault` is passed (CLI rejects --key without it).
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')

    # Add a new turn to the transcript
    import json

    transcript_jsonl.write_text(
        transcript_jsonl.read_text()
        + json.dumps({'role': 'user', 'content': 'second compact turn'})
        + '\n'
    )

    payload = json.dumps(
        {
            'session_id': 'cc-int-2',
            'transcript_path': str(transcript_jsonl),
            'cwd': str(temp_git_repo),
            'hook_event_name': 'PreCompact',
        }
    )
    run_script('on_pre_compact.sh', stdin=payload, env=mock_memex.env, cwd=temp_git_repo)

    append_calls = mock_memex.calls_matching('note', 'append')
    assert len(append_calls) == 1
    argv = append_calls[0]['argv']
    cli_argv = argv[1:]  # drop leading 'note'

    from uuid import UUID

    api = AsyncMock()
    api.append_to_note.return_value = NoteAppendResponse(
        status='success',
        note_id=UUID(int=0),
        append_id=UUID(int=1),
        content_hash='abc123',
        delta_bytes=10,
        new_unit_ids=[],
    )
    notes_patch, _ = _mock_api_context(api)
    with notes_patch:
        # The append CLI reads delta from stdin when --delta is omitted
        result = runner.invoke(notes_app, cli_argv, obj=cli_config, input=append_calls[0]['stdin'])
    assert result.exit_code == 0, (
        f'CLI rejected the argv emitted by PreCompact:\n'
        f'argv: {cli_argv}\nstdout: {result.stdout}\nexc: {result.exception}'
    )
    assert api.append_to_note.called


def test_kv_get_argv_is_compatible(cli_config: MemexConfig) -> None:
    """The shape used in resolve_config.sh: `memex kv get <key> --value-only`."""
    api = AsyncMock()
    import datetime as dt
    from uuid import uuid4

    api.kv_get.return_value = KVEntryDTO(
        id=uuid4(),
        key='app:claude-code:project:test:vault',
        value='vault-x',
        created_at=dt.datetime(2026, 1, 1),
        updated_at=dt.datetime(2026, 1, 1),
        expires_at=None,
    )
    _, kv_patch = _mock_api_context(api)
    with kv_patch:
        result = runner.invoke(
            kv_app,
            ['get', 'app:claude-code:project:test:vault', '--value-only'],
            obj=cli_config,
        )
    assert result.exit_code == 0
    assert result.stdout.strip() == 'vault-x'


def test_kv_put_argv_is_compatible(cli_config: MemexConfig) -> None:
    """The shape used by the migration helper: `memex kv put <key> <value>`."""
    api = AsyncMock()
    import datetime as dt
    from uuid import uuid4

    api.kv_put.return_value = KVEntryDTO(
        id=uuid4(),
        key='app:claude-code:project:test:vault',
        value='legacy-vault',
        created_at=dt.datetime(2026, 1, 1),
        updated_at=dt.datetime(2026, 1, 1),
        expires_at=None,
    )
    _, kv_patch = _mock_api_context(api)
    with kv_patch:
        result = runner.invoke(
            kv_app,
            ['put', 'app:claude-code:project:test:vault', 'legacy-vault'],
            obj=cli_config,
        )
    assert result.exit_code == 0
    assert api.kv_put.called
    call = api.kv_put.call_args
    # Confirm key and value reach the API as named args
    assert call.kwargs.get('key') == 'app:claude-code:project:test:vault'
    assert call.kwargs.get('value') == 'legacy-vault'


def test_note_add_accepts_repeated_tag_flags(cli_config: MemexConfig) -> None:
    """memex_persist_session_delta passes one ``--tag`` per tag. CLI must accept N tags."""
    api = AsyncMock()
    api.ingest.return_value = AsyncMock(
        spec=IngestResponse,
        status='success',
        note_id='00000000-0000-0000-0000-000000000000',
        unit_ids=[],
        overlapping_notes=[],
    )
    notes_patch, _ = _mock_api_context(api)
    cli_argv = [
        'add',
        'content',
        '--key',
        'session:test',
        '--background',
        '--title',
        'T',
        '--description',
        'D',
        '--author',
        'claude-code',
        '--tag',
        'surface:claude-code',
        '--tag',
        'auto-capture',
        '--tag',
        'session-transcript',
        '--tag',
        'session:test',
        '--tag',
        'project:my/repo',
        '--tag',
        'session-end:prompt_input_exit',
    ]
    with notes_patch:
        result = runner.invoke(notes_app, cli_argv, obj=cli_config)
    assert result.exit_code == 0, result.stdout
    note = api.ingest.call_args.args[0]
    assert 'surface:claude-code' in note.tags
    assert 'session-end:prompt_input_exit' in note.tags
