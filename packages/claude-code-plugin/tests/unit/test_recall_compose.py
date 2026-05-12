"""Unit tests for ``scripts/recall_compose.sh`` — the /recall transcript fallback."""

from __future__ import annotations

import json
from pathlib import Path


from _helpers import MockMemex, run_script


def _expansion_payload(
    *,
    command_name: str = 'recall',
    command_args: str = '',
    transcript_path: str = '',
) -> str:
    return json.dumps(
        {
            'session_id': 'sess-abc',
            'transcript_path': transcript_path,
            'cwd': '/tmp',
            'permission_mode': 'default',
            'hook_event_name': 'UserPromptExpansion',
            'expansion_type': 'slash_command',
            'command_name': command_name,
            'command_args': command_args,
            'command_source': 'plugin',
            'prompt': f'/{command_name} {command_args}',
        }
    )


def _parse_output(stdout: str) -> dict:
    return json.loads(stdout)


def test_skips_unrelated_commands(mock_memex: MockMemex) -> None:
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(command_name='remember'),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    assert _parse_output(result.stdout) == {}


def test_skips_when_explicit_args(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(
            command_args='token TTL issue', transcript_path=str(transcript_jsonl)
        ),
        env=mock_memex.env,
    )
    assert _parse_output(result.stdout) == {}


def test_skips_when_args_are_only_whitespace(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    """A query that's just spaces should still trigger the fallback."""
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(command_args='   \t  ', transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    assert 'hookSpecificOutput' in out
    ctx = out['hookSpecificOutput']['additionalContext']
    assert 'Composed query' in ctx


def test_composes_from_last_n_turns(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript_jsonl)),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    out = _parse_output(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    # Default N=3 — should see content from at least one user + one assistant turn
    assert '[user]' in ctx
    assert '[assistant]' in ctx
    # Content from the most recent turns should appear
    assert 'Invalid credentials' in ctx
    assert 'auth middleware' in ctx
    # Visible announcement
    assert 'No `/recall` query supplied' in ctx
    # Composed-query markers (parseable by the skill)
    assert '--- Composed query (last 3 turns) ---' in ctx
    assert '--- End composed query ---' in ctx


def test_respects_recall_turns_env(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    env = {**mock_memex.env, 'MEMEX_CC_RECALL_TURNS': '1'}
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript_jsonl)),
        env=env,
    )
    out = _parse_output(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert 'last 1 turns' in ctx
    # With N=1, only the most recent turn appears — that's the assistant's response.
    assert 'auth middleware' in ctx


def test_clamps_extreme_recall_turns_value(mock_memex: MockMemex, transcript_jsonl: Path) -> None:
    """Out-of-range MEMEX_CC_RECALL_TURNS clamps into [1,10]."""
    for raw, expected in [('0', '1'), ('99', '10'), ('-3', '3'), ('abc', '3')]:
        env = {**mock_memex.env, 'MEMEX_CC_RECALL_TURNS': raw}
        result = run_script(
            'recall_compose.sh',
            stdin=_expansion_payload(transcript_path=str(transcript_jsonl)),
            env=env,
        )
        out = _parse_output(result.stdout)
        if 'hookSpecificOutput' in out:
            ctx = out['hookSpecificOutput']['additionalContext']
            assert f'last {expected} turns' in ctx, (
                f'For input {raw!r}, expected clamp to {expected}'
            )


def test_missing_transcript_returns_passthrough(mock_memex: MockMemex) -> None:
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path='/nonexistent/path.jsonl'),
        env=mock_memex.env,
    )
    assert _parse_output(result.stdout) == {}


def test_handles_assistant_array_content_format(mock_memex: MockMemex, tmp_path: Path) -> None:
    """Some CC versions wrap content in a {message: {role, content}} envelope."""
    transcript = tmp_path / 'wrapped.jsonl'
    lines = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'first turn'}},
        {
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'content': [
                    {'type': 'text', 'text': 'second turn answer'},
                    {'type': 'tool_use', 'id': 'a', 'name': 'X', 'input': {}},
                ],
            },
        },
    ]
    transcript.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript)),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert 'first turn' in ctx
    assert 'second turn answer' in ctx


def test_skips_tool_use_blocks_only(mock_memex: MockMemex, tmp_path: Path) -> None:
    """A turn whose content is *exclusively* tool-use must not appear in the query."""
    transcript = tmp_path / 'tool_only.jsonl'
    lines = [
        {'role': 'user', 'content': 'real user question'},
        {
            'role': 'assistant',
            'content': [
                {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {}},
            ],
        },
    ]
    transcript.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript)),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    if 'hookSpecificOutput' in out:
        ctx = out['hookSpecificOutput']['additionalContext']
        assert 'real user question' in ctx
        # No empty assistant entry (content was all tool_use)
        assert ctx.count('[assistant]') == 0


def test_truncates_long_content_per_turn(mock_memex: MockMemex, tmp_path: Path) -> None:
    """Each turn's text is capped at 200 chars to bound the composed query."""
    transcript = tmp_path / 'long.jsonl'
    big_text = 'x' * 5000
    transcript.write_text(json.dumps({'role': 'user', 'content': big_text}) + '\n')
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript)),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    # Final composed-query block is capped at 800 chars; the per-turn cap is
    # 200, so a single 5000-char input should not blow up the output.
    composed = ctx.split('--- Composed query', 1)[1]
    composed = composed.split('--- End composed query', 1)[0]
    # 200 chars per turn + ~10 chars role prefix per turn = well under 800
    assert len(composed) < 1000


def test_skips_malformed_jsonl_lines(mock_memex: MockMemex, tmp_path: Path) -> None:
    """A garbage line in the transcript shouldn't crash the hook."""
    transcript = tmp_path / 'mixed.jsonl'
    transcript.write_text(
        json.dumps({'role': 'user', 'content': 'good line'})
        + '\n'
        + 'this is not json\n'
        + json.dumps({'role': 'assistant', 'content': 'after garbage'})
        + '\n'
    )
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript)),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert 'good line' in ctx
    assert 'after garbage' in ctx


def test_empty_transcript_returns_passthrough(mock_memex: MockMemex, tmp_path: Path) -> None:
    transcript = tmp_path / 'empty.jsonl'
    transcript.write_text('')
    result = run_script(
        'recall_compose.sh',
        stdin=_expansion_payload(transcript_path=str(transcript)),
        env=mock_memex.env,
    )
    assert _parse_output(result.stdout) == {}
