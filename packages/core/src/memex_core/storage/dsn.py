"""Shared DSN derivation utility.

Centralises the conversion from the SQLAlchemy connection string used by
``MemexConfig.server.meta_store.instance.connection_string`` (which carries
the ``+asyncpg`` driver suffix) into the bare ``postgresql://`` DSN that
``asyncpg.connect`` / ``asyncpg.create_pool`` expects.

Previously inlined separately in ``services/locks.py`` and
``services/consolidation.py``; this single source of truth prevents drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.engine.url import make_url

if TYPE_CHECKING:
    from memex_core.config import MemexConfig


def dsn_from_config(config: 'MemexConfig') -> str:
    """Derive a plain ``postgresql://`` DSN for asyncpg from the meta_store config.

    Strips the ``+asyncpg`` driver suffix that SQLAlchemy uses, since asyncpg
    wants the bare scheme. Mirrors the pattern in ``scheduler.py``.
    """
    sa_url = make_url(config.server.meta_store.instance.connection_string)
    return sa_url.set(drivername='postgresql').render_as_string(hide_password=False)
