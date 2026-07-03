"""ClaudeCodeBackend wires the right files + flags for plugin parity.

Snapshots what gets handed to the ``claude`` subprocess by mocking
``subprocess.run`` and inspecting the workspace and command.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from memex_eval.suite.agents import (
    ClaudeCodeBackend,
    _MEMEX_TOOL_ALLOWLIST,
)


def _scenario():
    from memex_eval.suite import KeywordsPresent, Scenario

    return Scenario(
        id='ws_test',
        description='d',
        query='How do we use the vault?',
        expected=KeywordsPresent(type='keywords_present', keywords=['vault']),
    )


def _api(vault_id):
    api = SimpleNamespace()
    api.list_vaults = AsyncMock(return_value=[SimpleNamespace(id=vault_id, name='eval-test')])
    return api


def _fake_proc(stdout: str = '', returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=returncode)


@pytest.mark.asyncio
async def test_workspace_files_and_command_with_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the plugin resolved, all four files + plugin-dir flag are present."""
    monkeypatch.delenv('MEMEX_CLAUDE_PLUGIN_DIR', raising=False)
    backend = ClaudeCodeBackend(claude_bin='claude')
    assert backend.plugin_dir is not None, 'monorepo default should resolve'

    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == 'git':
            return _fake_proc()  # git init — let it pass
        captured['cmd'] = list(cmd)
        captured['cwd'] = Path(kwargs['cwd'])
        # Snapshot workspace contents BEFORE the temp dir is removed.
        cwd = Path(kwargs['cwd'])
        captured['mcp_json'] = (cwd / '.mcp.json').read_text()
        captured['claude_md'] = (cwd / 'CLAUDE.md').read_text()
        captured['settings'] = json.loads((cwd / '.claude' / 'settings.local.json').read_text())
        captured['has_git'] = (cwd / '.git').exists() or (cwd / '.git').is_dir()
        return _fake_proc(stdout='')

    vault_id = uuid4()
    with (
        patch.object(subprocess, 'run', side_effect=fake_run),
        patch('shutil.which', return_value='/usr/bin/claude'),
    ):
        await backend.answer(
            _scenario(),
            api=_api(vault_id),
            vault_id=vault_id,
            server_url='http://localhost:8000/api/v1/',
        )

    cmd = captured['cmd']
    assert cmd[0] == 'claude'
    assert '--model' in cmd and cmd[cmd.index('--model') + 1] == 'claude-sonnet-4-6'
    assert '--plugin-dir' in cmd
    pi_arg = cmd[cmd.index('--plugin-dir') + 1]
    assert Path(pi_arg).name == 'claude-code-plugin'
    assert '--output-format' in cmd and cmd[cmd.index('--output-format') + 1] == 'stream-json'
    assert '--permission-mode' in cmd
    assert '-p' in cmd

    assert 'memex' in captured['mcp_json']
    assert 'MEMEX_SERVER_URL' in captured['mcp_json']

    # Plugin variant of the CLAUDE.md is used.
    assert 'plugin' in captured['claude_md'].lower()
    assert 'eval-test' in captured['claude_md']

    allowed = captured['settings']['permissions']['allow']
    assert 'mcp__memex__memex_kv_put' in allowed
    assert 'mcp__memex__memex_memory_search' in allowed
    assert 'mcp__memex__memex_append_note' in allowed
    assert set(allowed) == set(_MEMEX_TOOL_ALLOWLIST)


@pytest.mark.asyncio
async def test_command_without_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Plugin unresolved → no --plugin-dir flag and the no-plugin CLAUDE.md is used."""
    # Point env at a nonexistent dir AND blank out the repo-default fallback.
    monkeypatch.setenv('MEMEX_CLAUDE_PLUGIN_DIR', str(tmp_path / 'nope'))
    import memex_eval.suite.agents as agents_mod

    fake_file = tmp_path / 'fake_workspace' / 'agents.py'
    fake_file.parent.mkdir()
    fake_file.write_text('')
    monkeypatch.setattr(agents_mod, '__file__', str(fake_file))

    backend = ClaudeCodeBackend(claude_bin='claude')
    assert backend.plugin_dir is None

    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == 'git':
            return _fake_proc()
        captured['cmd'] = list(cmd)
        cwd = Path(kwargs['cwd'])
        captured['claude_md'] = (cwd / 'CLAUDE.md').read_text()
        return _fake_proc(stdout='')

    # MEMEX_PROJECT_DIR must point somewhere valid so the answer() path can
    # locate a workspace root for the MCP server. Use the real worktree.
    monkeypatch.setenv(
        'MEMEX_PROJECT_DIR',
        str(Path(__file__).resolve().parents[3]),
    )

    vault_id = uuid4()
    with (
        patch.object(subprocess, 'run', side_effect=fake_run),
        patch('shutil.which', return_value='/usr/bin/claude'),
    ):
        await backend.answer(
            _scenario(),
            api=_api(vault_id),
            vault_id=vault_id,
            server_url='http://localhost:8000/api/v1/',
        )

    cmd = captured['cmd']
    assert '--plugin-dir' not in cmd, 'plugin-dir must not appear when plugin is unresolved'
    assert '--model' in cmd, 'model pin still applies in the no-plugin path'
    # No-plugin variant of CLAUDE.md is the older "Memex memory retrieval" template.
    assert 'Memex memory retrieval' in captured['claude_md']
