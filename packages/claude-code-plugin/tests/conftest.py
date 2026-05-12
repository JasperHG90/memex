"""Pytest fixtures for the Claude Code plugin's bash hook scripts.

Each test gets:
  - A temp dir as ``CLAUDE_PLUGIN_DATA`` so state files don't leak between tests.
  - A PATH-prefixed ``uvx`` shim that intercepts every ``uvx --from <pkg> memex``
    call and routes it to ``mock_memex`` — the production scripts run unchanged.
  - A ``MockMemex`` handle for setting KV state, asserting on calls, and
    forcing failures.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from _helpers import FIXTURES_DIR, MockMemex, PLUGIN_ROOT


@pytest.fixture
def mock_memex(tmp_path: Path) -> Iterator[MockMemex]:
    """Per-test PATH shim for the Memex CLI."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'uvx').symlink_to(FIXTURES_DIR / 'mock_uvx')

    calls_file = tmp_path / 'calls.jsonl'
    kv_file = tmp_path / 'kv.txt'
    notes_dir = tmp_path / 'notes'
    plugin_data = tmp_path / 'plugin_data'
    plugin_data.mkdir()

    handle = MockMemex(
        bin_dir=bin_dir,
        calls_file=calls_file,
        kv_file=kv_file,
        notes_dir=notes_dir,
        plugin_data=plugin_data,
        env={
            'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'MOCK_MEMEX_CALLS': str(calls_file),
            'MOCK_MEMEX_KV_FILE': str(kv_file),
            'MOCK_MEMEX_NOTES_DIR': str(notes_dir),
            'CLAUDE_PLUGIN_DATA': str(plugin_data),
            'CLAUDE_PLUGIN_ROOT': str(PLUGIN_ROOT),
            'MEMEX_PLUGIN_VERSION': 'latest',
        },
    )
    kv_file.write_text('')
    yield handle


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo at a known tmp path; return the path."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo, check=True)
    (repo / 'README.md').write_text('test\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'initial'], cwd=repo, check=True)
    subprocess.run(
        ['git', 'remote', 'add', 'origin', 'https://github.com/acme/myapp.git'],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture
def transcript_jsonl(tmp_path: Path) -> Path:
    """Write a small but realistic JSONL transcript file and return its path."""
    import json

    path = tmp_path / 'transcript.jsonl'
    lines = [
        {'role': 'user', 'content': 'Hello, can you help me debug an auth issue?'},
        {
            'role': 'assistant',
            'content': [
                {'type': 'text', 'text': 'Sure, what errors are you seeing?'},
                {'type': 'tool_use', 'id': 'x', 'name': 'Bash', 'input': {}},
            ],
        },
        {'role': 'user', 'content': 'I get "Invalid credentials" on every login.'},
        {
            'role': 'assistant',
            'content': [
                {
                    'type': 'text',
                    'text': 'Let me check the auth middleware. Could be a token TTL issue.',
                }
            ],
        },
    ]
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')
    return path


def is_jq_available() -> bool:
    return shutil.which('jq') is not None
