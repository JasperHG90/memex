"""Tests for Alembic migration setup."""

import importlib.util
import os
import pathlib as plb
from unittest.mock import patch

import pytest


@pytest.fixture()
def core_root():
    import memex_core

    return plb.Path(memex_core.__file__).resolve().parent.parent.parent


class TestAlembicStructure:
    """Verify the Alembic directory structure exists and is valid."""

    def test_alembic_ini_exists(self, core_root):
        assert (core_root / 'alembic.ini').is_file()

    def test_env_py_exists(self, core_root):
        assert (core_root / 'alembic' / 'env.py').is_file()

    def test_versions_dir_exists(self, core_root):
        assert (core_root / 'alembic' / 'versions').is_dir()

    def test_script_mako_exists(self, core_root):
        assert (core_root / 'alembic' / 'script.py.mako').is_file()

    def test_baseline_migration_exists(self, core_root):
        versions = list((core_root / 'alembic' / 'versions').glob('001_*.py'))
        assert len(versions) == 1, f'Expected 1 baseline migration, found {len(versions)}'


class TestGetDatabaseUrl:
    """Tests for the get_database_url helper in memex_core.storage.db_url."""

    def test_memex_database_url_env(self):
        from memex_core.storage.db_url import get_database_url

        with patch.dict(os.environ, {'MEMEX_DATABASE_URL': 'postgresql+asyncpg://u:p@h/d'}):
            assert get_database_url() == 'postgresql+asyncpg://u:p@h/d'

    def test_memex_instance_env_vars(self):
        from memex_core.storage.db_url import get_database_url

        env = {
            'MEMEX_SERVER__META_STORE__INSTANCE__HOST': 'myhost',
            'MEMEX_SERVER__META_STORE__INSTANCE__PORT': '5433',
            'MEMEX_SERVER__META_STORE__INSTANCE__DATABASE': 'mydb',
            'MEMEX_SERVER__META_STORE__INSTANCE__USER': 'myuser',
            'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD': 'mypass',
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('MEMEX_DATABASE_URL', None)
            url = get_database_url()
            assert url == 'postgresql+asyncpg://myuser:mypass@myhost:5433/mydb'

    def test_instance_env_defaults(self):
        from memex_core.storage.db_url import get_database_url

        env = {
            'MEMEX_SERVER__META_STORE__INSTANCE__HOST': 'localhost',
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('MEMEX_DATABASE_URL', None)
            url = get_database_url()
            assert 'localhost:5432' in url
            assert 'postgres' in url

    def test_no_config_raises(self):
        from memex_core.storage.db_url import get_database_url

        clean = {k: v for k, v in os.environ.items() if not k.startswith('MEMEX_')}
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(RuntimeError, match='No database URL configured'):
                get_database_url()


class TestBaselineMigration:
    """Tests for the baseline migration script content."""

    def test_baseline_has_extension_creation(self, core_root):
        migration_path = core_root / 'alembic' / 'versions' / '001_baseline_extensions.py'

        spec = importlib.util.spec_from_file_location('baseline', migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == '001_baseline'
        assert module.down_revision is None

    def test_alembic_config_loads(self, core_root):
        """Verify alembic.ini can be parsed by Alembic."""
        from alembic.config import Config

        ini_path = core_root / 'alembic.ini'
        cfg = Config(str(ini_path))
        assert cfg.get_main_option('script_location') == 'alembic'
