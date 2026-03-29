from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from memex_cli.sync.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_TOML,
    NoteSyncConfig,
    WatchMode,
    load_config,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path


class TestDefaults:
    def test_default_config(self) -> None:
        cfg = NoteSyncConfig()
        assert cfg.vault_id is None
        assert cfg.sync.batch_size == 32
        assert cfg.sync.state_file == '.memex-sync.db'
        assert '.obsidian' in cfg.sync.exclude.base
        assert cfg.sync.assets.enabled is True
        assert cfg.sync.assets.max_size_mb == 50
        assert cfg.watch.mode == WatchMode.events
        assert cfg.watch.debounce_seconds == 5
        assert cfg.watch.poll_interval_seconds == 300

    def test_all_patterns_property(self) -> None:
        cfg = NoteSyncConfig(sync={'exclude': {'extends_exclude': ['templates/', '_archive/**']}})
        patterns = cfg.sync.exclude.all_patterns
        assert '.obsidian' in patterns
        assert 'templates/' in patterns
        assert '_archive/**' in patterns


class TestTomlLoading:
    def test_loads_from_vault_root(self, vault: Path) -> None:
        (vault / CONFIG_FILENAME).write_text('vault_id = "my-vault"\n\n[sync]\nbatch_size = 64\n')
        cfg = load_config(vault)
        assert cfg.vault_id == 'my-vault'
        assert cfg.sync.batch_size == 64

    def test_loads_from_explicit_path(self, vault: Path) -> None:
        custom = vault / 'custom-config.toml'
        custom.write_text('[sync]\nbatch_size = 64\n')
        cfg = load_config(vault, config_path=custom)
        assert cfg.sync.batch_size == 64

    def test_defaults_when_no_config(self, vault: Path) -> None:
        cfg = load_config(vault)
        assert cfg.vault_id is None
        assert cfg.sync.batch_size == 32

    def test_partial_toml_merges_with_defaults(self, vault: Path) -> None:
        (vault / CONFIG_FILENAME).write_text('[watch]\nmode = "poll"\n')
        cfg = load_config(vault)
        assert cfg.watch.mode == WatchMode.poll
        # Other defaults should still be present
        assert cfg.sync.batch_size == 32

    def test_nested_toml_sections(self, vault: Path) -> None:
        (vault / CONFIG_FILENAME).write_text(
            '[sync.exclude]\n'
            'extends_exclude = ["daily/", "templates/"]\n\n'
            '[sync.assets]\n'
            'max_size_mb = 10\n'
            'extends_include = [".mp3"]\n'
        )
        cfg = load_config(vault)
        assert cfg.sync.exclude.extends_exclude == ['daily/', 'templates/']
        assert cfg.sync.assets.max_size_mb == 10
        assert '.mp3' in cfg.sync.assets.extends_include

    def test_legacy_server_section_stripped(self, vault: Path) -> None:
        """Old TOMLs with [server] should work with deprecation warning."""
        (vault / 'obsidian-sync.toml').write_text(
            '[server]\nurl = "http://old:8321"\nvault_id = "old-vault"\n\n[sync]\nbatch_size = 16\n'
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            cfg = load_config(vault)
        assert cfg.vault_id == 'old-vault'  # promoted from [server]
        assert cfg.sync.batch_size == 16
        assert len(w) == 1
        assert 'deprecated' in str(w[0].message).lower()


class TestEnvVars:
    def test_batch_size_from_env(self, vault: Path) -> None:
        with patch.dict(os.environ, {'MEMEX_SYNC_SYNC__BATCH_SIZE': '16'}):
            cfg = load_config(vault)
        assert cfg.sync.batch_size == 16

    def test_vault_id_from_env(self, vault: Path) -> None:
        with patch.dict(os.environ, {'MEMEX_SYNC_VAULT_ID': 'env-vault'}):
            cfg = load_config(vault)
        assert cfg.vault_id == 'env-vault'


class TestDefaultConfigToml:
    def test_default_toml_is_valid(self, vault: Path) -> None:
        """Ensure the default config template parses without error."""
        (vault / CONFIG_FILENAME).write_text(DEFAULT_CONFIG_TOML)
        cfg = load_config(vault)
        assert cfg.sync.batch_size == 32
        assert cfg.watch.mode == WatchMode.events


class TestValidation:
    def test_rejects_invalid_batch_size(self) -> None:
        with pytest.raises(Exception):
            NoteSyncConfig(sync={'batch_size': 0})

    def test_rejects_batch_size_over_max(self) -> None:
        with pytest.raises(Exception):
            NoteSyncConfig(sync={'batch_size': 200})

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            NoteSyncConfig(unknown_field='value')
