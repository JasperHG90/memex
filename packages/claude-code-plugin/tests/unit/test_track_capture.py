"""Unit tests for ``scripts/track_capture.sh``.

The script is meant to be cheap and concurrency-safe — these tests pin both:
appending one marker line per invocation, atomic under parallel writes, and
gated to ``mcp__memex__memex_add_note`` so unrelated PostToolUse events do
not bump the counter.
"""

from __future__ import annotations

import json
import threading


from _helpers import MockMemex, run_script


def _payload(tool_name: str = 'mcp__memex__memex_add_note', session_id: str = 'sess-abc') -> str:
    return json.dumps(
        {
            'session_id': session_id,
            'transcript_path': '/tmp/x.jsonl',
            'cwd': '/tmp',
            'permission_mode': 'default',
            'hook_event_name': 'PostToolUse',
            'tool_name': tool_name,
            'tool_input': {},
            'tool_use_id': 'toolu_x',
            'tool_result': 'ok',
        }
    )


def test_increments_counter_on_memex_add_note(mock_memex: MockMemex) -> None:
    result = run_script('track_capture.sh', stdin=_payload(), env=mock_memex.env)
    assert result.returncode == 0
    assert result.stdout.strip() == '{}'
    counter_dir = mock_memex.plugin_data / 'memex'
    files = list(counter_dir.glob('capture_count_*'))
    assert len(files) == 1
    assert files[0].read_text().count('\n') == 1


def test_three_invocations_yield_three_lines(mock_memex: MockMemex) -> None:
    for _ in range(3):
        run_script('track_capture.sh', stdin=_payload(), env=mock_memex.env)
    files = list((mock_memex.plugin_data / 'memex').glob('capture_count_*'))
    assert len(files) == 1
    assert files[0].read_text().count('\n') == 3


def test_skips_unrelated_tool(mock_memex: MockMemex) -> None:
    """PostToolUse hook may be configured globally; defend against drift."""
    result = run_script(
        'track_capture.sh',
        stdin=_payload(tool_name='Bash'),
        env=mock_memex.env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == '{}'
    files = list((mock_memex.plugin_data / 'memex').glob('capture_count_*'))
    assert files == []


def test_separate_sessions_get_separate_files(mock_memex: MockMemex) -> None:
    run_script('track_capture.sh', stdin=_payload(session_id='alpha'), env=mock_memex.env)
    run_script('track_capture.sh', stdin=_payload(session_id='beta'), env=mock_memex.env)
    files = sorted(p.name for p in (mock_memex.plugin_data / 'memex').glob('capture_count_*'))
    assert files == ['capture_count_alpha', 'capture_count_beta']


def test_session_id_with_unsafe_chars_is_sanitized(mock_memex: MockMemex) -> None:
    """Slashes in session_id must not escape the state dir."""
    run_script(
        'track_capture.sh',
        stdin=_payload(session_id='../etc/passwd'),
        env=mock_memex.env,
    )
    counter_dir = mock_memex.plugin_data / 'memex'
    files = list(counter_dir.glob('capture_count_*'))
    assert len(files) == 1
    # Path-traversal requires '/'; with slashes sanitized the filename is
    # confined to the counter dir even if dots remain.
    assert '/' not in files[0].name
    # The file must actually live inside the state dir.
    assert files[0].resolve().parent == counter_dir.resolve()


def test_empty_stdin_is_graceful(mock_memex: MockMemex) -> None:
    result = run_script('track_capture.sh', stdin='', env=mock_memex.env)
    assert result.returncode == 0
    assert result.stdout.strip() == '{}'


def test_malformed_json_does_not_crash(mock_memex: MockMemex) -> None:
    result = run_script('track_capture.sh', stdin='{not-json', env=mock_memex.env)
    assert result.returncode == 0
    # No counter file because tool_name extraction returns empty → matcher gate trips.
    files = list((mock_memex.plugin_data / 'memex').glob('capture_count_*'))
    assert files == []


def test_concurrent_invocations_do_not_lose_appends(mock_memex: MockMemex) -> None:
    """Parallel hook fires must each produce exactly one marker line.

    O_APPEND on POSIX local filesystems is atomic for writes <= PIPE_BUF;
    we depend on that. Run 20 concurrent invocations and require exactly
    20 lines in the counter file.
    """
    threads = []
    payload = _payload(session_id='concurrent')

    def go() -> None:
        run_script('track_capture.sh', stdin=payload, env=mock_memex.env)

    for _ in range(20):
        t = threading.Thread(target=go)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    counter = mock_memex.plugin_data / 'memex' / 'capture_count_concurrent'
    assert counter.exists()
    line_count = counter.read_text().count('\n')
    assert line_count == 20, f'Expected 20 lines under concurrent fires, got {line_count}'
