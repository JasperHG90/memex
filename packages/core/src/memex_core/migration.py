"""Alembic migration helpers for runtime schema checks."""

import pathlib as plb

from alembic.config import Config
from alembic.script import ScriptDirectory

_PACKAGE_DIR = plb.Path(__file__).resolve().parent


def _alembic_cfg() -> Config:
    """Build an Alembic Config pointing at the memex_core alembic directory."""
    ini_path = _PACKAGE_DIR / 'alembic.ini'
    cfg = Config(str(ini_path))
    cfg.set_main_option('script_location', str(_PACKAGE_DIR / 'alembic'))
    return cfg


def get_expected_head() -> str:
    """Return the current head revision from the Alembic script directory."""
    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f'Expected exactly 1 Alembic head revision, found {len(heads)}: {heads}')
    return heads[0]
