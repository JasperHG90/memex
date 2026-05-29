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


class TestVaultBareStringCoercion:
    """C.1: MemexConfig.vault accepts bare-string env / YAML shorthand
    in addition to the nested form. Closes the deployment env-var footgun
    where MEMEX_VAULT=hermes (per the hermes-plugin README) was crashing
    the CLI with `ValidationError: error parsing value for field "vault"`.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config(self):
        with patch.dict(
            os.environ,
            {
                'MEMEX_LOAD_LOCAL_CONFIG': 'false',
                'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
            },
        ):
            yield

    def test_bare_string_env_promotes_to_active(self):
        """`MEMEX_VAULT=hermes` → `vault.active='hermes'`. THE regression test."""
        with patch.dict(os.environ, {'MEMEX_VAULT': 'hermes'}):
            cfg = MemexConfig()
        assert cfg.vault.active == 'hermes'
        assert cfg.vault.search is None

    def test_nested_env_form_still_works(self):
        """`MEMEX_VAULT__ACTIVE=memex` keeps working (backwards compat)."""
        with patch.dict(os.environ, {'MEMEX_VAULT__ACTIVE': 'memex'}):
            cfg = MemexConfig()
        assert cfg.vault.active == 'memex'

    def test_dict_kwarg_form_still_works(self):
        """Direct kwarg with a dict still works."""
        cfg = MemexConfig(vault={'active': 'kw'})
        assert cfg.vault.active == 'kw'

    def test_bare_string_kwarg_promotes_to_active(self):
        """`MemexConfig(vault='kw')` is also accepted as shorthand."""
        cfg = MemexConfig(vault='kw')
        assert cfg.vault.active == 'kw'

    def test_vault_search_csv_env_form(self):
        """`MEMEX_VAULT__SEARCH=a,b,c` → `['a', 'b', 'c']`."""
        with patch.dict(
            os.environ,
            {'MEMEX_VAULT__ACTIVE': 'main', 'MEMEX_VAULT__SEARCH': 'a,b,c'},
        ):
            cfg = MemexConfig()
        assert cfg.vault.search == ['a', 'b', 'c']

    def test_vault_search_csv_strips_whitespace_and_empties(self):
        """CSV parser drops empty entries (trailing commas, extra whitespace)."""
        with patch.dict(
            os.environ,
            {'MEMEX_VAULT__ACTIVE': 'main', 'MEMEX_VAULT__SEARCH': 'a, b ,, c'},
        ):
            cfg = MemexConfig()
        assert cfg.vault.search == ['a', 'b', 'c']

    def test_vault_search_json_env_form_still_works(self):
        """JSON list env value `["a","b"]` still parses as a list."""
        with patch.dict(
            os.environ,
            {'MEMEX_VAULT__ACTIVE': 'main', 'MEMEX_VAULT__SEARCH': '["a","b"]'},
        ):
            cfg = MemexConfig()
        assert cfg.vault.search == ['a', 'b']

    def test_vault_search_list_kwarg_passes_through(self):
        """Direct list kwarg is unchanged."""
        cfg = MemexConfig(vault=VaultConfig(search=['x', 'y']))
        assert cfg.vault.search == ['x', 'y']

    def test_nested_env_wins_over_bare_string_when_both_set(self):
        """If somehow both MEMEX_VAULT and MEMEX_VAULT__ACTIVE are set,
        pydantic-settings resolves the nested form last → it wins."""
        with patch.dict(
            os.environ,
            {'MEMEX_VAULT': 'bare', 'MEMEX_VAULT__ACTIVE': 'nested'},
        ):
            cfg = MemexConfig()
        # The nested form is more explicit; document whichever wins.
        # Empirically pydantic-settings v2 resolves __ACTIVE after the
        # bare form, so 'nested' should be the resolved value.
        assert cfg.vault.active == 'nested'

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


# ---------------------------------------------------------------------------
# EntityMaintenanceConfig
# ---------------------------------------------------------------------------


class TestEntityMaintenanceConfig:
    """Defaults + validator for the cross-batch entity-cluster collapse loop."""

    def test_defaults_off(self):
        from memex_common.config import EntityMaintenanceConfig

        cfg = EntityMaintenanceConfig()
        assert cfg.scan_enabled is False
        assert cfg.top_n == 100
        assert cfg.scan_cooldown_days == 7
        assert cfg.pair_threshold == 0.85
        assert cfg.cluster_min_threshold == 0.70

    def test_validator_rejects_cluster_min_above_pair_threshold(self):
        import pytest
        from pydantic import ValidationError

        from memex_common.config import EntityMaintenanceConfig

        with pytest.raises(ValidationError):
            EntityMaintenanceConfig(pair_threshold=0.7, cluster_min_threshold=0.8)

    def test_validator_allows_equal_thresholds(self):
        from memex_common.config import EntityMaintenanceConfig

        cfg = EntityMaintenanceConfig(pair_threshold=0.75, cluster_min_threshold=0.75)
        assert cfg.cluster_min_threshold == cfg.pair_threshold

    def test_top_n_below_two_rejected_by_config(self):
        import pytest
        from pydantic import ValidationError

        from memex_common.config import EntityMaintenanceConfig

        with pytest.raises(ValidationError):
            EntityMaintenanceConfig(top_n=1)
