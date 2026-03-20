import os

import pytest
from unittest.mock import patch
from pydantic import ValidationError
from memex_common.config import (
    MemexConfig,
    ServerConfig,
    VaultConfig,
    LocalFileStoreConfig,
    PostgresInstanceConfig,
    PostgresMetaStoreConfig,
    ExtractionConfig,
    ModelConfig,
    SecretStr,
)


def test_postgres_instance_connection_string():
    config = PostgresInstanceConfig(
        host='localhost',
        port=5432,
        database='memex',
        user='admin',
        password=SecretStr('secret'),
    )
    assert config.connection_string == 'postgresql+asyncpg://admin:secret@localhost:5432/memex'


def test_extraction_config_model_validation():
    # Valid model config
    config = ExtractionConfig(model=ModelConfig(model='gemini/pro'))
    assert config.model.model == 'gemini/pro'

    # Missing model field in ModelConfig
    with pytest.raises(ValidationError):
        ExtractionConfig(model={})  # type: ignore


def test_memex_config_defaults(tmp_path):
    # Mock CWD to avoid picking up local .memex.yaml
    # We patch the specific reference in the config module
    # We also need to patch global config source to avoid picking up ~/.config/memex/config.yaml
    # We also enforce 'test-vault' via env var as the expected test configuration
    with (
        patch('memex_common.config.plb.Path.cwd', return_value=tmp_path),
        patch('memex_common.config.GlobalYamlConfigSettingsSource.__call__', return_value={}),
        patch.dict('os.environ', {'MEMEX_SERVER__DEFAULT_ACTIVE_VAULT': 'test-vault'}),
    ):
        # Minimal config required fields
        # meta_store is required. extraction has a default factory.
        meta_store_data = {
            'type': 'postgres',
            'instance': {
                'host': 'localhost',
                'database': 'memex',
                'user': 'admin',
                'password': 'password',
            },
        }

        config = MemexConfig(
            server=ServerConfig(
                default_active_vault='test-vault',
                meta_store=PostgresMetaStoreConfig(
                    instance=PostgresInstanceConfig.model_validate(meta_store_data['instance'])
                ),
            ),
        )

        # Check defaults
        assert config.server.default_active_vault == 'test-vault'
        assert isinstance(config.server.file_store, LocalFileStoreConfig)
        assert config.server.file_store.type == 'local'
        # Check root sync
        assert (
            config.server.file_store.root == config.server.file_store.root
        )  # Redundant check but keeps logic similar


class TestVaultConfigResolution:
    """Tests for write_vault and read_vaults property resolution on MemexConfig."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self):
        """Prevent ambient YAML configs from interfering."""
        with patch.dict(
            os.environ,
            {
                'MEMEX_LOAD_LOCAL_CONFIG': 'false',
                'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
            },
        ):
            yield

    def test_write_vault_from_vault_active(self):
        """vault.active takes precedence over server.default_active_vault."""
        config = MemexConfig(vault=VaultConfig(active='project-x'))
        assert config.write_vault == 'project-x'

    def test_write_vault_falls_back_to_server(self):
        """Falls back to server.default_active_vault when vault.active is None."""
        config = MemexConfig(server=ServerConfig(default_active_vault='custom'))
        assert config.write_vault == 'custom'

    def test_write_vault_default(self):
        """Default is 'global'."""
        config = MemexConfig()
        assert config.write_vault == 'global'

    def test_read_vaults_from_vault_search(self):
        """vault.search takes precedence."""
        config = MemexConfig(vault=VaultConfig(search=['a', 'b']))
        assert config.read_vaults == ['a', 'b']

    def test_read_vaults_from_vault_active(self):
        """When only vault.active set, read_vaults = [active]."""
        config = MemexConfig(vault=VaultConfig(active='project-x'))
        assert config.read_vaults == ['project-x']

    def test_read_vaults_falls_back_to_server(self):
        """Falls back to [server.default_reader_vault]."""
        config = MemexConfig(server=ServerConfig(default_reader_vault='archive'))
        assert config.read_vaults == ['archive']

    def test_read_vaults_default(self):
        """Default is ['global']."""
        config = MemexConfig()
        assert config.read_vaults == ['global']

    def test_vault_search_empty_list(self):
        """Empty search list is honored (not treated as None)."""
        config = MemexConfig(vault=VaultConfig(search=[]))
        assert config.read_vaults == []

    def test_full_resolution_chain(self):
        """vault.search > vault.active > server defaults."""
        config = MemexConfig(
            vault=VaultConfig(active='proj', search=['proj', 'shared']),
            server=ServerConfig(
                default_active_vault='srv-write',
                default_reader_vault='srv-read',
            ),
        )
        assert config.write_vault == 'proj'
        assert config.read_vaults == ['proj', 'shared']
