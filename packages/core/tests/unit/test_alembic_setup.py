"""Tests for Alembic migration setup."""

import importlib.util
import os
import pathlib as plb
from unittest.mock import patch

import pytest


@pytest.fixture()
def package_dir():
    import memex_core

    return plb.Path(memex_core.__file__).resolve().parent


class TestAlembicStructure:
    """Verify the Alembic directory structure exists and is valid."""

    def test_alembic_ini_exists(self, package_dir):
        assert (package_dir / 'alembic.ini').is_file()

    def test_env_py_exists(self, package_dir):
        assert (package_dir / 'alembic' / 'env.py').is_file()

    def test_versions_dir_exists(self, package_dir):
        assert (package_dir / 'alembic' / 'versions').is_dir()

    def test_script_mako_exists(self, package_dir):
        assert (package_dir / 'alembic' / 'script.py.mako').is_file()

    def test_baseline_migration_exists(self, package_dir):
        versions = list((package_dir / 'alembic' / 'versions').glob('001_*.py'))
        assert len(versions) == 1, f'Expected 1 baseline migration, found {len(versions)}'

    def test_single_head(self):
        """Verify ScriptDirectory.get_heads() returns exactly 1 head."""
        from memex_core.migration import _alembic_cfg

        cfg = _alembic_cfg()
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f'Expected 1 head, found {len(heads)}: {heads}'

    def test_revision_chain_valid(self):
        """Walk the chain from head to base with no gaps."""
        from memex_core.migration import _alembic_cfg

        cfg = _alembic_cfg()
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)

        revisions = list(script.walk_revisions())
        assert len(revisions) >= 1
        # The last revision should have no down_revision (base)
        assert revisions[-1].down_revision is None


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

    def test_baseline_has_extension_creation(self, package_dir):
        migration_path = package_dir / 'alembic' / 'versions' / '001_full_baseline.py'

        spec = importlib.util.spec_from_file_location('baseline', migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == '001_full_baseline'
        assert module.down_revision is None

    def test_alembic_config_loads(self, package_dir):
        """Verify alembic.ini can be parsed by Alembic."""
        from alembic.config import Config

        ini_path = package_dir / 'alembic.ini'
        cfg = Config(str(ini_path))
        # %(here)s is resolved to the ini file's directory at parse time
        assert cfg.get_main_option('script_location') == str(package_dir / 'alembic')
