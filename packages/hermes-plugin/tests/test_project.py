"""Tests for project_id derivation and vault resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


from memex_hermes_plugin.memex.project import (
    _normalize_remote,
    derive_project_id,
    project_vault_kv_key,
    resolve_vault,
)


class TestNormalizeRemote:
    def test_https_with_git_suffix(self):
        assert _normalize_remote('https://github.com/acme/myapp.git') == 'github.com/acme/myapp'

    def test_https_with_basic_auth(self):
        assert (
            _normalize_remote('https://user:pass@github.com/acme/myapp.git')
            == 'github.com/acme/myapp'
        )

    def test_https_without_git_suffix(self):
        assert _normalize_remote('https://github.com/acme/myapp') == 'github.com/acme/myapp'

    def test_ssh_url_keeps_user_prefix(self):
        # Matches claude-code-plugin's shell normalization.
        assert _normalize_remote('git@github.com:acme/myapp.git') == 'git@github.com:acme/myapp'


class TestDeriveProjectId:
    def test_falls_back_to_cwd_relative_to_home(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path))
        sub = tmp_path / 'proj' / 'sub'
        sub.mkdir(parents=True)
        with patch('subprocess.run', side_effect=FileNotFoundError):
            assert derive_project_id(cwd=sub, home=tmp_path) == 'proj/sub'

    def test_falls_back_to_absolute_when_not_under_home(self, tmp_path: Path):
        home = tmp_path / 'home'
        elsewhere = tmp_path / 'elsewhere'
        home.mkdir()
        elsewhere.mkdir()
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = derive_project_id(cwd=elsewhere, home=home)
        assert result == str(elsewhere.resolve())

    def test_uses_git_remote_when_available(self, tmp_path: Path):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='https://github.com/acme/myapp.git\n', stderr=''
        )
        with patch('subprocess.run', return_value=completed):
            assert derive_project_id(cwd=tmp_path, home=tmp_path) == 'github.com/acme/myapp'

    def test_falls_back_when_git_errors(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path))
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout='', stderr='')
        with patch('subprocess.run', return_value=failed):
            assert derive_project_id(cwd=tmp_path, home=tmp_path) == '.'


def test_project_vault_kv_key():
    assert project_vault_kv_key('github.com/acme/x') == 'project:github.com/acme/x:vault'


def _fake_kv_entry(value: str) -> object:
    """Build a real ``KVEntryDTO`` so tests catch DTO-shape drift."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from memex_common.schemas import KVEntryDTO

    now = datetime.now(timezone.utc)
    return KVEntryDTO(
        id=uuid4(),
        key='irrelevant',
        value=value,
        created_at=now,
        updated_at=now,
    )


class TestResolveVault:
    def test_kv_hit_wins(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=_fake_kv_entry('kv-vault'))
        api.resolve_vault_identifier = AsyncMock()
        result = resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='u',
            config_vault='cfg',
        )
        assert result == 'kv-vault'
        api.resolve_vault_identifier.assert_not_called()

    def test_kv_dict_return_still_handled(self):
        """Defensive fallback: if the client ever returns a dict, still works."""
        api = Mock()
        api.kv_get = AsyncMock(return_value={'value': 'dict-vault'})
        api.resolve_vault_identifier = AsyncMock()
        result = resolve_vault(
            api,
            project_id='p',
            agent_identity=None,
            user_id=None,
            config_vault='cfg',
        )
        assert result == 'dict-vault'

    def test_user_id_vault_used_when_kv_misses(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)

        async def _resolve(name: str) -> object:
            if name == 'hermes:user:alice':
                return object()
            raise ValueError('not found')

        api.resolve_vault_identifier = AsyncMock(side_effect=_resolve)
        result = resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='alice',
            config_vault='cfg',
        )
        assert result == 'hermes:user:alice'

    def test_config_vault_when_everything_else_misses(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)
        api.resolve_vault_identifier = AsyncMock(side_effect=ValueError('not found'))
        result = resolve_vault(
            api,
            project_id='p',
            agent_identity=None,
            user_id=None,
            config_vault='fallback',
        )
        assert result == 'fallback'

    def test_returns_none_when_no_candidates(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)
        api.resolve_vault_identifier = AsyncMock(side_effect=ValueError('not found'))
        result = resolve_vault(
            api,
            project_id='p',
            agent_identity=None,
            user_id=None,
            config_vault=None,
        )
        assert result is None
