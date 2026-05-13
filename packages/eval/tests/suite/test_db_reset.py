"""``memex_eval.suite.db_reset`` — DSN resolution + orchestration.

Schema-level effects (drop_all, create_all, alembic stamp) live against
a live Postgres; they're exercised by integration tests, not here. This
file asserts:
- DSN resolution honours ``MEMEX_DATABASE_URL`` over ``MemexConfig``.
- DSN resolution falls back to ``MemexConfig`` (which itself reads
  ``MEMEX_SERVER__META_STORE__INSTANCE__*`` env vars / YAML).
- ``drop_and_recreate_schema`` restores the prior ``MEMEX_DATABASE_URL``
  env var after stamping Alembic, so the call is side-effect-free at
  the env level.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memex_eval.suite import db_reset


class TestResolveDbDsn:
    def test_direct_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('MEMEX_DATABASE_URL', 'postgresql+asyncpg://u:p@host:5432/db')
        assert db_reset._resolve_db_dsn() == 'postgresql+asyncpg://u:p@host:5432/db'

    def test_falls_back_to_memex_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``MEMEX_DATABASE_URL``, we read MemexConfig — which
        itself reads the per-instance env vars."""
        monkeypatch.delenv('MEMEX_DATABASE_URL', raising=False)
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__TYPE', 'postgres')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__HOST', 'cfg-host')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__PORT', '6543')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__DATABASE', 'cfg_db')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__USER', 'cfg_user')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD', 'cfg_pass')
        assert db_reset._resolve_db_dsn() == (
            'postgresql+asyncpg://cfg_user:cfg_pass@cfg-host:6543/cfg_db'
        )


class TestDropAndRecreateSchemaOrchestration:
    """We patch the engine + alembic command so the test never touches
    Postgres. The point is to assert orchestration order and env-var
    hygiene, not DDL semantics."""

    @pytest.mark.asyncio
    async def test_passes_resolved_dsn_to_engine_and_alembic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target_dsn = 'postgresql+asyncpg://u:p@host:5432/db'
        monkeypatch.setenv('MEMEX_DATABASE_URL', target_dsn)

        engine_mock = MagicMock()
        engine_mock.dispose = AsyncMock()
        conn_mock = MagicMock()
        conn_mock.execute = AsyncMock()
        conn_mock.run_sync = AsyncMock()
        # ``async with engine.begin()`` — make begin() a context manager.
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=conn_mock)
        async_cm.__aexit__ = AsyncMock(return_value=None)
        engine_mock.begin = MagicMock(return_value=async_cm)

        captured_dsn: list[str] = []

        def fake_create_engine(dsn: str, **_kwargs: object):
            captured_dsn.append(dsn)
            return engine_mock

        stamp_mock = MagicMock()
        monkeypatch.setattr(
            'sqlalchemy.ext.asyncio.create_async_engine',
            fake_create_engine,
        )
        with patch('alembic.command.stamp', stamp_mock):
            await db_reset.drop_and_recreate_schema()

        assert captured_dsn == [target_dsn]
        # stamp called with (cfg, 'head'); cfg has our DSN injected.
        assert stamp_mock.call_count == 1
        cfg_arg, rev_arg = stamp_mock.call_args.args
        assert rev_arg == 'head'
        assert cfg_arg.get_main_option('sqlalchemy.url') == target_dsn

    @pytest.mark.asyncio
    async def test_restores_prior_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``MEMEX_DATABASE_URL`` may already be set; we must restore the
        exact prior value (or absence) after stamping."""
        monkeypatch.setenv('MEMEX_DATABASE_URL', 'prior://original')

        engine_mock = MagicMock()
        engine_mock.dispose = AsyncMock()
        conn_mock = MagicMock()
        conn_mock.execute = AsyncMock()
        conn_mock.run_sync = AsyncMock()
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=conn_mock)
        async_cm.__aexit__ = AsyncMock(return_value=None)
        engine_mock.begin = MagicMock(return_value=async_cm)

        monkeypatch.setattr(
            'sqlalchemy.ext.asyncio.create_async_engine',
            lambda *_a, **_k: engine_mock,
        )
        with patch('alembic.command.stamp', MagicMock()):
            await db_reset.drop_and_recreate_schema(dsn='override://called')

        assert os.environ.get('MEMEX_DATABASE_URL') == 'prior://original'

    @pytest.mark.asyncio
    async def test_clears_env_when_no_prior_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('MEMEX_DATABASE_URL', raising=False)
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__TYPE', 'postgres')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__HOST', 'h')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__DATABASE', 'd')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__USER', 'u')
        monkeypatch.setenv('MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD', 'p')

        engine_mock = MagicMock()
        engine_mock.dispose = AsyncMock()
        conn_mock = MagicMock()
        conn_mock.execute = AsyncMock()
        conn_mock.run_sync = AsyncMock()
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=conn_mock)
        async_cm.__aexit__ = AsyncMock(return_value=None)
        engine_mock.begin = MagicMock(return_value=async_cm)

        monkeypatch.setattr(
            'sqlalchemy.ext.asyncio.create_async_engine',
            lambda *_a, **_k: engine_mock,
        )
        with patch('alembic.command.stamp', MagicMock()):
            await db_reset.drop_and_recreate_schema()

        assert 'MEMEX_DATABASE_URL' not in os.environ
