"""Tests for the Hermes-side config loader."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memex_hermes_plugin.memex.config import (
    HermesMemexConfig,
    load_config,
    save_config,
)


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    return tmp_path


def test_defaults_when_no_config_or_env(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for k in ('MEMEX_SERVER_URL', 'MEMEX_API_KEY', 'MEMEX_VAULT', 'MEMEX_HERMES_MODE'):
        monkeypatch.delenv(k, raising=False)
    with patch('memex_hermes_plugin.memex.config._apply_memex_fallback', side_effect=lambda d: d):
        cfg = load_config(tmp_home)
    assert cfg.server_url == 'http://127.0.0.1:8000'
    assert cfg.memory_mode == 'hybrid'
    assert cfg.vault_id is None


def test_env_overrides_file(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_home / 'memex' / 'config.json'
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        json.dumps({'server_url': 'http://file-host:8000', 'vault_id': 'file-vault'})
    )
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://env-host:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'env-vault')
    monkeypatch.setenv('MEMEX_HERMES_MODE', 'tools')

    cfg = load_config(tmp_home)

    assert cfg.server_url == 'http://env-host:8000'
    assert cfg.vault_id == 'env-vault'
    assert cfg.memory_mode == 'tools'


def test_save_config_writes_non_secret_keys(tmp_home: Path):
    save_config(
        {'server_url': 'http://x:9000', 'vault_id': 'v', 'api_key': None},
        tmp_home,
    )
    cfg_path = tmp_home / 'memex' / 'config.json'
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text())
    assert data['server_url'] == 'http://x:9000'
    assert data['vault_id'] == 'v'
    assert 'api_key' not in data


def test_save_config_merges(tmp_home: Path):
    save_config({'server_url': 'http://a'}, tmp_home)
    save_config({'vault_id': 'v'}, tmp_home)
    data = json.loads((tmp_home / 'memex' / 'config.json').read_text())
    assert data['server_url'] == 'http://a'
    assert data['vault_id'] == 'v'


def test_invalid_briefing_budget_rejected():
    with pytest.raises(Exception):
        HermesMemexConfig(briefing_budget=1500)


def test_server_url_trailing_slash_stripped():
    cfg = HermesMemexConfig(server_url='http://x:8000/')
    assert cfg.server_url == 'http://x:8000'


def test_invalid_strategy_rejected():
    """Strategies must match the server-side ``VALID_STRATEGIES`` frozenset."""
    from memex_hermes_plugin.memex.config import RecallConfig

    with pytest.raises(Exception):
        RecallConfig(strategies=['not-a-strategy'])
    # 'entity' was the old incorrect name — it must be 'graph'.
    with pytest.raises(Exception):
        RecallConfig(strategies=['entity'])


def test_default_strategies_match_server():
    """Server's ``VALID_STRATEGIES`` is mirrored in the plugin — keep them in sync."""
    from memex_core.memory.retrieval.models import VALID_STRATEGIES as SERVER_VALID

    from memex_hermes_plugin.memex.config import VALID_STRATEGIES, RecallConfig

    assert VALID_STRATEGIES == SERVER_VALID
    assert set(RecallConfig().strategies) <= SERVER_VALID


def test_memex_fallback_applied_when_file_and_env_miss(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    for k in ('MEMEX_SERVER_URL', 'MEMEX_API_KEY', 'MEMEX_VAULT'):
        monkeypatch.delenv(k, raising=False)

    class FakeSecret:
        def get_secret_value(self) -> str:
            return 'fallback-key'

    class FakeVault:
        active = 'fallback-vault'

    class FakeConfig:
        server_url = 'http://fallback:8000'
        api_key = FakeSecret()
        vault = FakeVault()

    with patch('memex_common.config.MemexConfig', return_value=FakeConfig()):
        cfg = load_config(tmp_home)
    assert cfg.server_url == 'http://fallback:8000'
    assert cfg.api_key == 'fallback-key'
    assert cfg.vault_id == 'fallback-vault'
