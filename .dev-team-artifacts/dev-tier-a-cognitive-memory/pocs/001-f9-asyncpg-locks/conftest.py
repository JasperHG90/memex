"""POC-001 conftest — testcontainer fixture for asyncpg advisory-lock scenarios.

Stand-alone testcontainer (NOT shared with the project's session-scoped
postgres_container fixture) so the POC runs in isolation when invoked
explicitly via:

    uv run pytest .dev-team-artifacts/.../pocs/001-f9-asyncpg-locks/
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from testcontainers.postgres import PostgresContainer

# Make `_helpers` resolvable from the POC dir.
_POC_DIR = Path(__file__).resolve().parent
if str(_POC_DIR) not in sys.path:
    sys.path.insert(0, str(_POC_DIR))


@pytest.fixture(scope='session')
def pg_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer('pgvector/pgvector:pg18-trixie') as ctr:
        yield ctr


@pytest.fixture(scope='session')
def asyncpg_dsn(pg_container: PostgresContainer) -> str:
    """Plain `postgresql://` DSN suitable for `asyncpg.connect(dsn)`.

    PostgresContainer.get_connection_url() returns a `psycopg2`-flavored URL;
    we strip the dialect suffix.
    """
    url = pg_container.get_connection_url()
    parsed = urlparse(url.replace('postgresql+psycopg2://', 'postgresql://'))
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


@pytest.fixture(scope='session')
def sqla_async_url(pg_container: PostgresContainer) -> str:
    """`postgresql+asyncpg://` DSN for SQLAlchemy AsyncSession scenarios."""
    base = pg_container.get_connection_url().replace('psycopg2', 'asyncpg')
    return base
