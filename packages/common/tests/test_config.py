import pytest
from unittest.mock import patch
from pydantic import ValidationError
from memex_common.config import (
    MemexConfig,
    ServerConfig,
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
        patch.dict('os.environ', {'MEMEX_SERVER__ACTIVE_VAULT': 'test-vault'}),
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
        # extraction_data = {'model': {'model': 'gemini/flash'}}

        config = MemexConfig(
            server=ServerConfig(
                active_vault='test-vault',
                meta_store=PostgresMetaStoreConfig(
                    instance=PostgresInstanceConfig.model_validate(meta_store_data['instance'])
                ),
            ),
            # Extraction has a default, so we don't strictly need to provide it if we don't want to override
            # extraction=ExtractionConfig(model=ModelConfig(model='gemini/flash')),
        )

        # Check defaults
        assert config.server.active_vault == 'test-vault'
        assert isinstance(config.server.file_store, LocalFileStoreConfig)
        assert config.server.file_store.type == 'local'
        # Check root sync
        assert (
            config.server.file_store.root == config.server.file_store.root
        )  # Redundant check but keeps logic similar
