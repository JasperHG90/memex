"""Tests for the Claude Code subagent answer module (M6 + M7).

These unit tests do NOT spawn a real ``claude`` process. They assert:

- Workspace setup writes the expected ``.mcp.json``, ``.memex.yaml``,
  and ``CLAUDE.md`` files with correct contents.
- Plugin resolution honours the override arg, then the env var, then
  the repo-default path.
- ``_run_claude_subagent`` assembles the right CLI (including
  ``--plugin-dir``) and parses JSONL output into the expected shape.
- ``answer_questions`` rejects unimplemented methods.
- ``answer_questions`` writes hypotheses via ``append_jsonl`` when the
  subagent subprocess is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memex_eval.external.longmemeval_answer import (
    _PLUGIN_DIR,
    AnswerMethod,
    _normalise_server_url,
    _parse_claude_json_output,
    _resolve_plugin_dir,
    _run_claude_subagent,
    _setup_subagent_workspace,
    answer_questions,
)
from memex_eval.external.longmemeval_common import DATASET_FILENAMES, append_jsonl


def test_normalise_server_url_strips_api_v1_and_trailing_slash() -> None:
    assert _normalise_server_url('http://localhost:8001/api/v1/') == 'http://localhost:8001'
    assert _normalise_server_url('http://localhost:8001/api/v1') == 'http://localhost:8001'
    assert _normalise_server_url('http://localhost:8001/') == 'http://localhost:8001'
    assert _normalise_server_url('http://localhost:8001') == 'http://localhost:8001'


def test_setup_subagent_workspace_writes_expected_files(tmp_path: Path) -> None:
    workdir = _setup_subagent_workspace(
        server_url='http://localhost:8001/api/v1/',
        vault_name='longmemeval_s_run-1',
    )
    try:
        w = Path(workdir)
        assert (w / '.mcp.json').exists()
        mcp = json.loads((w / '.mcp.json').read_text())
        assert 'memex' in mcp['mcpServers']
        assert mcp['mcpServers']['memex']['env']['MEMEX_SERVER_URL'] == 'http://localhost:8001'

        memex_yaml = (w / '.memex.yaml').read_text()
        assert 'longmemeval_s_run-1' in memex_yaml
        assert 'http://localhost:8001' in memex_yaml

        claude_md = (w / 'CLAUDE.md').read_text()
        assert 'LongMemEval' in claude_md
        assert 'I do not know based on the available memory.' in claude_md

        settings = json.loads((w / '.claude' / 'settings.local.json').read_text())
        assert 'mcp__memex__memex_memory_search' in settings['permissions']['allow']
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def test_resolve_plugin_dir_override_wins(tmp_path: Path) -> None:
    assert _resolve_plugin_dir(str(tmp_path)) == tmp_path


def test_resolve_plugin_dir_env_var_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('MEMEX_CLAUDE_PLUGIN_DIR', str(tmp_path))
    assert _resolve_plugin_dir(None) == tmp_path


def test_resolve_plugin_dir_defaults_to_repo_packages_path(monkeypatch) -> None:
    monkeypatch.delenv('MEMEX_CLAUDE_PLUGIN_DIR', raising=False)
    assert _resolve_plugin_dir(None) == _PLUGIN_DIR
    # And the repo default actually exists in this worktree — i.e. the
    # plugin IS installed and ready to be passed via --plugin-dir.
    assert _PLUGIN_DIR.exists()
    assert (_PLUGIN_DIR / '.claude-plugin' / 'plugin.json').exists()
    assert (_PLUGIN_DIR / 'skills').is_dir()


def test_parse_claude_json_output_extracts_answer_and_tool_calls() -> None:
    lines = [
        json.dumps(
            {
                'type': 'assistant',
                'message': {
                    'content': [
                        {
                            'type': 'tool_use',
                            'name': 'mcp__memex__memex_memory_search',
                            'input': {'query': 'foo'},
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                'type': 'result',
                'result': 'Tuesday at 3pm.',
                'usage': {
                    'input_tokens': 100,
                    'cache_read_input_tokens': 50,
                    'output_tokens': 20,
                },
                'num_turns': 3,
                'total_cost_usd': 0.0123,
                'model': 'claude-sonnet-4-6',
            }
        ),
    ]
    parsed = _parse_claude_json_output('\n'.join(lines), duration=12.5)
    assert parsed['answer'] == 'Tuesday at 3pm.'
    assert parsed['model'] == 'claude-sonnet-4-6'
    assert parsed['num_turns'] == 3
    assert parsed['cost_usd'] == 0.0123
    assert parsed['duration_s'] == 12.5
    assert parsed['tokens']['input'] == 150
    assert parsed['tokens']['output'] == 20
    assert parsed['tool_calls'] == [
        {'name': 'mcp__memex__memex_memory_search', 'input': {'query': 'foo'}}
    ]


def test_run_claude_subagent_invokes_correct_cli(tmp_path: Path) -> None:
    """The assembled argv must include ``--plugin-dir`` pointing at the
    resolved plugin directory — this is the M7 wiring."""
    captured: dict = {}

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({'type': 'result', 'result': 'ok', 'usage': {}, 'model': 'claude-opus'})
        stderr = ''

    def _fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return _FakeProc()

    with patch('memex_eval.external.longmemeval_answer.subprocess.run', _fake_run):
        result = _run_claude_subagent(
            'What did the user say?',
            workdir=str(tmp_path),
            plugin_dir=Path('/fake/plugin'),
            timeout_s=60.0,
        )

    assert result['answer'] == 'ok'
    cmd = captured['cmd']
    assert cmd[0] == 'claude'
    assert '--plugin-dir' in cmd
    assert cmd[cmd.index('--plugin-dir') + 1] == '/fake/plugin'
    assert '-p' in cmd
    assert '--output-format' in cmd and 'json' in cmd


def test_verify_plugin_installed_passes_for_repo_default() -> None:
    """The repo-default plugin is a valid install — verification passes."""
    from memex_eval.external.longmemeval_answer import _verify_plugin_installed

    _verify_plugin_installed(_PLUGIN_DIR)  # no raise


def test_verify_plugin_installed_raises_on_missing_dir(tmp_path: Path) -> None:
    from memex_eval.external.longmemeval_answer import _verify_plugin_installed

    with pytest.raises(FileNotFoundError, match='plugin'):
        _verify_plugin_installed(tmp_path / 'does-not-exist')


def test_verify_plugin_installed_raises_on_missing_manifest(tmp_path: Path) -> None:
    """A directory without ``.claude-plugin/plugin.json`` is rejected —
    this catches ``--plugin-dir`` pointing at the wrong folder."""
    from memex_eval.external.longmemeval_answer import _verify_plugin_installed

    (tmp_path / 'skills').mkdir()  # skills without a manifest
    with pytest.raises(FileNotFoundError, match='plugin.json'):
        _verify_plugin_installed(tmp_path)


@pytest.mark.asyncio
async def test_answer_questions_rejects_gemini_cli(tmp_path: Path) -> None:
    dataset = tmp_path / DATASET_FILENAMES['oracle']
    dataset.write_text(json.dumps([]))
    output = tmp_path / 'hypotheses.jsonl'

    with pytest.raises(NotImplementedError):
        await answer_questions(
            server_url='http://localhost:8001',
            dataset_path=str(dataset),
            variant='oracle',
            run_id='r1',
            output_path=str(output),
            method=AnswerMethod.GEMINI_CLI,
            allow_unpinned_checksum=True,
        )


@pytest.mark.asyncio
async def test_answer_questions_writes_hypotheses_via_mock_subagent(tmp_path: Path) -> None:
    """Full answer pipeline with subprocess mocked out — asserts the
    hypothesis JSONL carries the subagent's answer + model fingerprint."""
    dataset = tmp_path / DATASET_FILENAMES['oracle']
    dataset.write_text(
        json.dumps(
            [
                {
                    'question_id': 'q-001',
                    'question_type': 'single-session-user',
                    'question': 'What did the user say first?',
                    'answer': 'Hello.',
                    'haystack_sessions': [],
                }
            ]
        )
    )
    output = tmp_path / 'hypotheses.jsonl'

    def _fake_run(cmd, **kwargs):
        class _P:
            returncode = 0
            stdout = json.dumps(
                {
                    'type': 'result',
                    'result': 'Hello.',
                    'usage': {'input_tokens': 1, 'output_tokens': 2},
                    'total_cost_usd': 0.001,
                    'model': 'claude-sonnet-4-6',
                }
            )
            stderr = ''

        return _P()

    plugin_dir = tmp_path / 'plugin'
    (plugin_dir / '.claude-plugin').mkdir(parents=True)
    (plugin_dir / '.claude-plugin' / 'plugin.json').write_text('{}')

    with (
        patch('memex_eval.external.longmemeval_answer.subprocess.run', _fake_run),
    ):
        answered = await answer_questions(
            server_url='http://localhost:8001',
            dataset_path=str(dataset),
            variant='oracle',
            run_id='r1',
            output_path=str(output),
            method=AnswerMethod.CLAUDE_CODE,
            plugin_dir=str(plugin_dir),
            allow_unpinned_checksum=True,
        )

    assert answered == 1
    rows = [json.loads(line) for line in output.read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0]['hypothesis'] == 'Hello.'
    assert 'claude-code' in rows[0]['answer_model_fingerprint']
    assert 'claude-sonnet-4-6' in rows[0]['answer_model_fingerprint']


@pytest.mark.asyncio
async def test_answer_questions_raises_when_plugin_dir_missing(tmp_path: Path) -> None:
    dataset = tmp_path / DATASET_FILENAMES['oracle']
    dataset.write_text(
        json.dumps(
            [
                {
                    'question_id': 'q-001',
                    'question_type': 'single-session-user',
                    'question': '?',
                    'answer': 'x',
                    'haystack_sessions': [],
                }
            ]
        )
    )
    output = tmp_path / 'hypotheses.jsonl'

    with pytest.raises(FileNotFoundError, match='plugin'):
        await answer_questions(
            server_url='http://localhost:8001',
            dataset_path=str(dataset),
            variant='oracle',
            run_id='r1',
            output_path=str(output),
            method=AnswerMethod.CLAUDE_CODE,
            plugin_dir=str(tmp_path / 'does-not-exist'),
            allow_unpinned_checksum=True,
        )


@pytest.mark.asyncio
async def test_answer_questions_resume_skips_completed(tmp_path: Path) -> None:
    dataset = tmp_path / DATASET_FILENAMES['oracle']
    dataset.write_text(
        json.dumps(
            [
                {
                    'question_id': 'q-001',
                    'question_type': 'single-session-user',
                    'question': '?',
                    'answer': 'x',
                    'haystack_sessions': [],
                }
            ]
        )
    )
    output = tmp_path / 'hypotheses.jsonl'
    append_jsonl(
        output,
        {
            'question_id': 'q-001',
            'hypothesis': 'prev',
            'retrieved_unit_ids': [],
            'answer_model_fingerprint': 'x',
        },
    )

    # With the only question already in output, nothing runs — the
    # subprocess is never invoked, so if it IS invoked the test would
    # fail against a missing ``claude`` binary.
    plugin_dir = tmp_path / 'plugin'
    (plugin_dir / '.claude-plugin').mkdir(parents=True)

    answered = await answer_questions(
        server_url='http://localhost:8001',
        dataset_path=str(dataset),
        variant='oracle',
        run_id='r1',
        output_path=str(output),
        method=AnswerMethod.CLAUDE_CODE,
        plugin_dir=str(plugin_dir),
        allow_unpinned_checksum=True,
    )
    assert answered == 0
