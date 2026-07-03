"""Shared helpers for Alembic migration integration tests.

Used by ``test_int_alembic_021.py``, ``test_int_alembic_022.py``, and any
future ``test_int_alembic_NNN.py``. Each test module gets a `fresh_db_url`
fixture that yields a freshly created Postgres database (in the existing
session-scoped container) so ``alembic upgrade head`` runs from base.
"""

from __future__ import annotations

import asyncio
import pathlib as plb
import secrets
from contextlib import contextmanager
from typing import AsyncGenerator, Iterator
from urllib.parse import urlparse, urlunparse

from alembic import command
from alembic.config import Config
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer


def alembic_cfg_for(db_url: str) -> Config:
    """Build an Alembic Config rooted at ``memex_core/alembic`` targeting ``db_url``."""
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    cfg = Config(str(package_dir / 'alembic.ini'))
    cfg.set_main_option('script_location', str(package_dir / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', db_url)
    return cfg


def sync_url(asyncpg_url: str) -> str:
    """Convert ``postgresql+asyncpg://...`` → ``postgresql://...`` for sync use."""
    parsed = urlparse(asyncpg_url)
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


async def alembic_upgrade(db_url: str, target: str = '024_intent_risk_classifier') -> None:
    """Run ``alembic upgrade <target>`` against ``db_url``.

    Default pinned to ``024_intent_risk_classifier`` (Wave 1 head). Tier A
    seed PR introduced revisions 025-029 as NIE stubs; each Tier A feature
    test (``test_int_alembic_NNN.py``) bumps its own target when the matching
    stub body lands.
    """
    cfg = alembic_cfg_for(db_url)
    await asyncio.to_thread(command.upgrade, cfg, target)


async def alembic_downgrade(db_url: str, target: str) -> None:
    """Run ``alembic downgrade <target>`` against ``db_url``."""
    cfg = alembic_cfg_for(db_url)
    await asyncio.to_thread(command.downgrade, cfg, target)


async def make_fresh_db(
    postgres_container: PostgresContainer, db_prefix: str
) -> AsyncGenerator[str, None]:
    """Create a fresh Postgres DB inside the session container, yield its URL, drop it.

    Yields the asyncpg URL pointing at the new DB. The DB is dropped on
    teardown after terminating any other connections to it.
    """
    db_name = f'{db_prefix}_{secrets.token_hex(6)}'

    base_url = postgres_container.get_connection_url().replace('psycopg2', 'asyncpg')
    parsed = urlparse(base_url)
    admin_url = urlunparse(parsed._replace(path='/postgres'))
    new_url = urlunparse(parsed._replace(path=f'/{db_name}'))

    admin_engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level='AUTOCOMMIT')
    async with admin_engine.connect() as conn:
        # Identifiers cannot be parameterized in DDL. ``db_name`` is
        # ``<prefix>_<32 hex chars>`` — fixed prefix + secrets-generated hex,
        # so f-string interpolation is safe.
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()

    new_engine = create_async_engine(new_url, poolclass=NullPool)
    async with new_engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
    await new_engine.dispose()

    try:
        yield new_url
    finally:
        admin_engine = create_async_engine(
            admin_url, poolclass=NullPool, isolation_level='AUTOCOMMIT'
        )
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = :db AND pid <> pg_backend_pid()'
                ),
                {'db': db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


@contextmanager
def neutralized_stub_revisions(revision_ids: list[str]) -> Iterator[None]:
    """Replace ``upgrade``/``downgrade`` with no-ops on the named stub revisions.

    Used by Tier A migration tests whose new revision sits *downstream* of
    other workstreams' raising stubs. Alembic loads each revision file via
    ``importlib.util.spec_from_file_location`` + a fresh ``exec_module``
    (see ``alembic/util/pyfiles.py:load_module_py``), producing a brand-new
    module object each time — distinct from any module registered in
    ``sys.modules``. So we patch alembic's loader directly: after each
    revision module is loaded, we replace its ``upgrade``/``downgrade``
    with no-ops if the revision id matches one of ``revision_ids``.

    Example::

        with neutralized_stub_revisions(['025_maintenance_proposals',
                                         '026_revisit_columns',
                                         '027_consolidation_ticks']):
            await alembic_upgrade(db_url, target='028_procedure_outcomes')
    """
    from alembic.util import pyfiles  # type: ignore[import-not-found]

    target_module_ids = {f'{rev_id}_py' for rev_id in revision_ids}
    original_load = pyfiles.load_module_py

    def _patched_load_module_py(module_id: str, path):  # type: ignore[no-untyped-def]
        module = original_load(module_id, path)
        if module_id in target_module_ids:
            module.upgrade = lambda: None  # type: ignore[assignment]
            module.downgrade = lambda: None  # type: ignore[assignment]
        return module

    pyfiles.load_module_py = _patched_load_module_py
    try:
        yield
    finally:
        pyfiles.load_module_py = original_load
