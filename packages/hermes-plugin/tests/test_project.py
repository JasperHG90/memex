"""Tests for project_id derivation and KV-driven vault resolution."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from memex_common.schemas import KVEntryDTO

from memex_hermes_plugin.memex.cache import clear_vault_cache
from memex_hermes_plugin.memex.project import (
    KV_NAMESPACE,
    _normalize_remote,
    agent_vault_kv_key,
    derive_project_id,
    project_vault_kv_key,
    resolve_vault,
    user_vault_kv_key,
)


# ---------------------------------------------------------------------------
# Project-id derivation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# KV key helpers
# ---------------------------------------------------------------------------


class TestKvKeyHelpers:
    """All keys must live under app:hermes:* per the namespace contract."""

    def test_namespace_is_app_hermes(self):
        assert KV_NAMESPACE == 'app:hermes'

    def test_project_key(self):
        assert (
            project_vault_kv_key('github.com/acme/x')
            == 'app:hermes:project:github.com/acme/x:vault'
        )

    def test_user_key(self):
        assert user_vault_kv_key('10650075') == 'app:hermes:user:10650075:vault'

    def test_agent_key(self):
        assert agent_vault_kv_key('coder') == 'app:hermes:agent:coder:vault'


# ---------------------------------------------------------------------------
# KVEntryDTO factory
# ---------------------------------------------------------------------------


def _fake_kv_entry(value: str, key: str = 'irrelevant') -> KVEntryDTO:
    now = datetime.now(timezone.utc)
    return KVEntryDTO(id=uuid4(), key=key, value=value, created_at=now, updated_at=now)


# ---------------------------------------------------------------------------
# resolve_vault
# ---------------------------------------------------------------------------


class TestResolveVault:
    """The lookup chain: project KV → user KV → agent KV → config_vault → None.

    Synthetic vault names (e.g. literal ``hermes:user:alice``) are NOT
    consulted — that was the v0.1.12 behaviour and is gone.
    """

    def setup_method(self) -> None:
        clear_vault_cache()

    def test_project_kv_wins(self):
        api = Mock()

        async def _kv(key: str):
            return _fake_kv_entry('proj-vault') if 'project' in key else None

        api.kv_get = AsyncMock(side_effect=_kv)
        api.resolve_vault_identifier = AsyncMock()

        result = resolve_vault(
            api,
            project_id='github.com/acme/x',
            agent_identity='coder',
            user_id='alice',
            config_vault='cfg',
        )
        assert result == 'proj-vault'
        # Should not have attempted to resolve any vault name.
        api.resolve_vault_identifier.assert_not_called()
        # And only the project KV was needed (chain short-circuits).
        assert api.kv_get.await_count == 1
        api.kv_get.assert_awaited_with('app:hermes:project:github.com/acme/x:vault')

    def test_user_kv_used_when_project_missing(self):
        api = Mock()

        async def _kv(key: str):
            return _fake_kv_entry('user-vault') if 'user' in key else None

        api.kv_get = AsyncMock(side_effect=_kv)
        api.resolve_vault_identifier = AsyncMock()

        result = resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='alice',
            config_vault='cfg',
        )
        assert result == 'user-vault'
        # project KV was checked, then user KV.
        assert {c.args[0] for c in api.kv_get.await_args_list} == {
            'app:hermes:project:p:vault',
            'app:hermes:user:alice:vault',
        }

    def test_agent_kv_used_when_project_and_user_miss(self):
        api = Mock()

        async def _kv(key: str):
            return _fake_kv_entry('agent-vault') if 'agent' in key else None

        api.kv_get = AsyncMock(side_effect=_kv)
        api.resolve_vault_identifier = AsyncMock()

        result = resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='alice',
            config_vault='cfg',
        )
        assert result == 'agent-vault'

    def test_config_vault_when_everything_else_misses(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)
        api.resolve_vault_identifier = AsyncMock()

        result = resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='alice',
            config_vault='fallback',
        )
        assert result == 'fallback'

    def test_returns_none_when_no_candidates(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)
        api.resolve_vault_identifier = AsyncMock()

        result = resolve_vault(
            api,
            project_id='p',
            agent_identity=None,
            user_id=None,
            config_vault=None,
        )
        assert result is None

    def test_no_resolve_vault_identifier_calls_ever(self):
        """Regression test: v0.1.12 spammed Memex with vault-name lookups.

        The new design only ever does ``kv_get``; it never calls
        ``resolve_vault_identifier`` for synthetic names.
        """
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)
        api.resolve_vault_identifier = AsyncMock()

        resolve_vault(
            api,
            project_id='p',
            agent_identity='coder',
            user_id='alice',
            config_vault='cfg',
        )
        api.resolve_vault_identifier.assert_not_called()

    def test_kv_dict_return_still_handled(self):
        """Defensive fallback: if the client ever returns a plain dict."""
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

    def test_resolve_with_no_project_or_identity_uses_config(self):
        api = Mock()
        api.kv_get = AsyncMock()
        result = resolve_vault(
            api,
            project_id=None,
            agent_identity=None,
            user_id=None,
            config_vault='only-cfg',
        )
        assert result == 'only-cfg'
        api.kv_get.assert_not_called()
