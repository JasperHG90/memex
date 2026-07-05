"""End-to-end proof that the Claude Code plugin's session-transcript capture
upserts into a SINGLE note across a resume — driven through the REAL `memex`
CLI against a live uvicorn server backed by the Postgres testcontainer.

Why this exists (beyond the unit tests in
``packages/claude-code-plugin/tests/unit/``): the unit tests drive the bash
hooks against a MOCK ``memex`` CLI, so they cannot prove the load-bearing
server semantics the fix depends on:

  1. ``memex note add --key session:<id>`` then ``memex note append --key
     session:<id>`` (the exact calls the hooks emit) produce ONE note whose
     body grows — not two notes.
  2. A resumed session (same CC ``session_id``) keeps appending to that one
     note rather than minting a duplicate.

This test runs the actual hook scripts as subprocesses invoking the real CLI
over HTTP, then asserts final state via the real API. The `session:<id>` tag
the hooks stamp is the anchor: exactly one tagged note ⇒ no duplication.

Marked ``llm_mock`` because ``note append`` runs synchronous block-diff
extraction; the ``mock_dspy_lm`` fixture makes that hermetic.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch
from urllib.parse import urlparse


import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.llm_mock]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / 'packages' / 'claude-code-plugin' / 'scripts'


# The hermetic-LLM helper lives under the core package's test tree, which isn't
# on the import path for root `tests/`. Reuse only its `MockDspyLM` class (the
# module's own `mock_dspy_lm` fixture is broken/shadowed) and define the working
# fixture locally — patching BOTH the definition site and the extraction import
# site, mirroring packages/core/tests/unit/conftest.py, so extraction on the
# in-process uvicorn server thread is intercepted (no network LLM).
class _Empty(str):
    """A permissive stand-in for any DSPy prediction result: reads as an empty
    string, an empty iterable, and returns itself for any missing attribute
    (so ``result.extracted_facts.extracted_facts``, ``pred.detected_headers``,
    ``pred.summary`` etc. all resolve to something empty/falsy). Lets the whole
    extraction pipeline COMPLETE with zero output — no network LLM — regardless
    of which ``operation_name`` is running."""

    def __new__(cls) -> '_Empty':
        return super().__new__(cls, '')

    def __getattr__(self, _name: str) -> '_Empty':
        return self

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __bool__(self) -> bool:
        return False


@pytest.fixture
def mock_dspy_lm() -> Iterator[None]:
    """Hermetic extraction: patch the real ``run_dspy_operation`` (definition +
    extraction import site) with a no-op returning an ``_Empty`` result, so
    background ingestion + append extraction COMPLETE (creating/growing the
    note) without any network LLM. We assert on the note body/identity, not on
    extracted facts (fact extraction is covered by dedicated suites)."""

    async def _fake_run(*_a: Any, **_kw: Any) -> _Empty:
        return _Empty()

    with (
        patch('memex_core.llm.run_dspy_operation', side_effect=_fake_run),
        patch('memex_core.memory.extraction.core.run_dspy_operation', side_effect=_fake_run),
    ):
        yield


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope='module')
def live_server(postgres_container: Any) -> Iterator[str]:
    """Run the Memex FastAPI app under uvicorn on a free port in a background
    thread, pointed at the session Postgres testcontainer. The real CLI (a
    subprocess) reaches it over HTTP via ``MEMEX_SERVER_URL``."""
    import uvicorn

    from memex_core.server import app

    parsed = urlparse(postgres_container.get_connection_url())
    os.environ.update(
        {
            'MEMEX_LOAD_LOCAL_CONFIG': 'false',
            'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
            'MEMEX_SERVER__META_STORE__TYPE': 'postgres',
            'MEMEX_SERVER__META_STORE__INSTANCE__HOST': parsed.hostname or 'localhost',
            'MEMEX_SERVER__META_STORE__INSTANCE__PORT': str(parsed.port or 5432),
            'MEMEX_SERVER__META_STORE__INSTANCE__DATABASE': parsed.path.lstrip('/'),
            'MEMEX_SERVER__META_STORE__INSTANCE__USER': parsed.username or 'test',
            'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD': parsed.password or 'test',
            'MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED': 'false',
        }
    )

    async def _noop(*_a: Any, **_kw: Any) -> None:
        import asyncio

        await asyncio.Event().wait()

    sched = patch('memex_core.server.run_scheduler_with_leader_election', side_effect=_noop)
    sched.start()

    port = _free_port()

    class _Server(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # type: ignore[override]
            pass

    server = _Server(
        uvicorn.Config(app=app, host='127.0.0.1', port=port, log_level='warning', lifespan='on')
    )
    thread = threading.Thread(target=server.run, daemon=True, name='memex-e2e-uvicorn')
    thread.start()
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        sched.stop()
        raise RuntimeError('Memex e2e server failed to start')
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        sched.stop()


@pytest.fixture
def hook_env(live_server: str, tmp_path: Path) -> dict[str, str]:
    """Env for running a hook script as a subprocess: real CLI on PATH, pointed
    at the live server, with an isolated plugin-state dir and HOME."""
    memex_bin = shutil.which('memex')
    assert memex_bin, 'real `memex` CLI must be on PATH for the e2e'
    state_home = tmp_path / 'home'
    state_home.mkdir()
    plugin_data = tmp_path / 'plugin_data'
    plugin_data.mkdir()
    return {
        'PATH': os.environ['PATH'],
        'HOME': str(state_home),
        'LANG': 'C.UTF-8',
        'MEMEX_SERVER_URL': live_server,
        'MEMEX_LOAD_LOCAL_CONFIG': 'false',
        'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
        'MEMEX_CC_SESSION_BRIEFING': 'off',
        'CLAUDE_PLUGIN_DATA': str(plugin_data),
        'CLAUDE_PLUGIN_ROOT': str(SCRIPTS.parent),
        'MEMEX_PLUGIN_VERSION': 'latest',
    }


def _run_hook(script: str, payload: dict[str, Any], env: dict[str, str], cwd: Path) -> None:
    r = subprocess.run(
        ['bash', str(SCRIPTS / script)],
        input=json.dumps(payload),
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f'{script} failed: rc={r.returncode}\nstderr={r.stderr}'


def _write_transcript(path: Path, turns: list[dict[str, Any]]) -> None:
    path.write_text('\n'.join(json.dumps(t) for t in turns) + '\n')


def _notes_tagged(server: str, tag: str) -> list[dict[str, Any]]:
    """Return notes carrying ``tag`` (NDJSON stream from GET /api/v1/notes)."""
    r = httpx.get(
        f'{server}/api/v1/notes',
        params={'tags': tag, 'limit': 200},
        timeout=30.0,
    )
    r.raise_for_status()
    out: list[dict[str, Any]] = []
    for line in r.text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _note_body(server: str, note_id: str) -> dict[str, Any]:
    r = httpx.get(f'{server}/api/v1/notes/{note_id}', timeout=30.0)
    r.raise_for_status()
    return r.json()


def _wait_for_notes(
    server: str, tag: str, count: int, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Poll until exactly ``count`` notes carry ``tag`` (``note add --background``
    is fire-and-forget, so the note appears only once the ingestion job runs).
    Returns the notes; raises with a diagnostic if the count never settles."""
    deadline = time.monotonic() + timeout
    seen: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        seen = _notes_tagged(server, tag)
        if len(seen) == count:
            return seen
        time.sleep(0.5)
    raise AssertionError(f'expected {count} note(s) tagged {tag!r}, got {len(seen)}: {seen}')


def test_resume_upserts_into_single_growing_note(
    live_server: str, hook_env: dict[str, str], tmp_path: Path, mock_dspy_lm: Any
) -> None:
    """The money test: two captures under the same session_id (a resume) yield
    ONE note keyed by session:<id> whose body grew — proven through the real
    CLI + server, not a mock."""
    session_id = 'e2e-sess-aaaa-1111'
    note_key = f'session:{session_id}'
    transcript = tmp_path / 'transcript.jsonl'

    # --- SessionStart: derive + persist the note key from the session id ---
    _run_hook(
        'on_session_start.sh',
        {
            'session_id': session_id,
            'transcript_path': str(transcript),
            'hook_event_name': 'SessionStart',
            'source': 'startup',
            'model': 'claude-opus-4-8',
        },
        hook_env,
        tmp_path,
    )
    state_dir = Path(hook_env['CLAUDE_PLUGIN_DATA']) / 'memex'
    assert (state_dir / 'session_note_key').read_text().strip() == note_key

    # --- First capture (PreCompact): creates the note via `memex note add` ---
    _write_transcript(
        transcript,
        [
            {'role': 'user', 'content': 'Investigate the loop harness'},
            {'type': 'ai-title', 'aiTitle': 'Loop harness investigation', 'sessionId': session_id},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'FIRST_SNAPSHOT_MARKER'}]},
        ],
    )
    _run_hook(
        'on_pre_compact.sh',
        {
            'session_id': session_id,
            'transcript_path': str(transcript),
            'hook_event_name': 'PreCompact',
        },
        hook_env,
        tmp_path,
    )

    notes = _wait_for_notes(live_server, note_key, 1)
    note_id = str(notes[0]['id'])
    # Title came from the CC ai-title line (in `title` or `name`, whichever the
    # DTO carries it in).
    titles = {notes[0].get('title'), notes[0].get('name')}
    assert 'Session: Loop harness investigation' in titles, notes[0]
    body1 = _note_body(live_server, note_id)['original_text']
    assert 'FIRST_SNAPSHOT_MARKER' in body1

    # --- Resume: SessionStart fires again with the SAME id; cursor preserved ---
    _run_hook(
        'on_session_start.sh',
        {
            'session_id': session_id,
            'transcript_path': str(transcript),
            'hook_event_name': 'SessionStart',
            'source': 'resume',
            'model': 'claude-opus-4-8',
        },
        hook_env,
        tmp_path,
    )
    assert (state_dir / 'session_note_key').read_text().strip() == note_key
    assert (state_dir / f'session_note_offset_{session_id}').exists(), 'resume wiped the cursor'

    # --- Second capture (SessionEnd) after more turns: appends to the SAME note ---
    _write_transcript(
        transcript,
        [
            {'role': 'user', 'content': 'Investigate the loop harness'},
            {'type': 'ai-title', 'aiTitle': 'Loop harness investigation', 'sessionId': session_id},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'FIRST_SNAPSHOT_MARKER'}]},
            {'role': 'user', 'content': 'Now record the outcome'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'SECOND_SNAPSHOT_MARKER'}]},
        ],
    )
    _run_hook(
        'on_session_end_capture.sh',
        {
            'session_id': session_id,
            'transcript_path': str(transcript),
            'hook_event_name': 'SessionEnd',
            'reason': 'prompt_input_exit',
        },
        hook_env,
        tmp_path,
    )

    # THE ASSERTION: still exactly one note, and it grew.
    notes_after = _notes_tagged(live_server, note_key)
    assert len(notes_after) == 1, f'resume duplicated the note: {notes_after}'
    assert str(notes_after[0]['id']) == note_id, 'append created a new note instead of growing'
    body2 = _note_body(live_server, note_id)['original_text']
    assert 'FIRST_SNAPSHOT_MARKER' in body2
    assert 'SECOND_SNAPSHOT_MARKER' in body2
    assert body2.index('FIRST_SNAPSHOT_MARKER') < body2.index('SECOND_SNAPSHOT_MARKER')


def test_distinct_sessions_get_distinct_notes(
    live_server: str, hook_env: dict[str, str], tmp_path: Path, mock_dspy_lm: Any
) -> None:
    """Control: two genuinely different session ids produce two notes (keying
    on session_id must not collapse unrelated conversations)."""
    for suffix in ('bbbb-2222', 'cccc-3333'):
        session_id = f'e2e-sess-{suffix}'
        transcript = tmp_path / f'transcript_{suffix}.jsonl'
        _write_transcript(
            transcript,
            [
                {'role': 'user', 'content': f'work item {suffix}'},
                {'role': 'assistant', 'content': [{'type': 'text', 'text': f'MARKER_{suffix}'}]},
            ],
        )
        # Fresh plugin-state dir per session (mirrors a separate launch).
        _run_hook(
            'on_session_start.sh',
            {
                'session_id': session_id,
                'transcript_path': str(transcript),
                'hook_event_name': 'SessionStart',
                'source': 'startup',
                'model': 'claude-opus-4-8',
            },
            hook_env,
            tmp_path,
        )
        _run_hook(
            'on_session_end_capture.sh',
            {
                'session_id': session_id,
                'transcript_path': str(transcript),
                'hook_event_name': 'SessionEnd',
                'reason': 'prompt_input_exit',
            },
            hook_env,
            tmp_path,
        )
        notes = _notes_tagged(live_server, f'session:{session_id}')
        assert len(notes) == 1, f'session {session_id}: expected 1 note, got {notes}'
