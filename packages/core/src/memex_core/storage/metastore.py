import logging
from typing import Generic, TypeVar, AsyncGenerator, Self
from abc import abstractmethod, ABCMeta
from contextlib import asynccontextmanager

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    async_sessionmaker,
)
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import PostgresMetaStoreConfig

T = TypeVar('T', bound=BaseModel)


class AsyncBaseMetaStoreEngine(Generic[T], metaclass=ABCMeta):
    """Abstract base class for asynchronous metadata store engines using SQLModel.

    This class defines the interface for interacting with a metadata storage backend.
    It handles engine lifecycle and session factory management.
    """

    def __init__(self, config: T):
        self._logger = logging.getLogger(
            f'memex.core.storage.metastore.engine.{self.__class__.__name__}'
        )
        self._config: T = config
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @abstractmethod
    async def connect(self, create_schema: bool = True) -> Self:
        """Initialize the engine and connection pool."""
        ...

    @abstractmethod
    async def close(self) -> Self:
        """Dispose of the engine and close all connections."""
        ...

    @abstractmethod
    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory."""
        ...

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[Self, None]:
        """Context manager to initialize and teardown the engine."""
        await self.connect()
        try:
            yield self
        finally:
            await self.close()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide a transactional scope around a series of operations.

        Yields:
            AsyncSession: A SQLModel async session.
        """
        session_factory = self.session_maker()
        async with session_factory() as session:
            try:
                yield session
                # NB: user should commit/rollback explicitly
            except Exception as e:
                self._logger.error(f'Session error: {e}.')
                raise

    @property
    def engine(self) -> AsyncEngine:
        """Expose the engine so callers can perform DDL/maintenance."""
        if self._engine is None:
            raise RuntimeError('Database not connected. Call connect() first.')
        return self._engine


class AsyncPostgresMetaStoreEngine(AsyncBaseMetaStoreEngine[PostgresMetaStoreConfig]):
    """PostgreSQL implementation using SQLModel + AsyncPG."""

    async def connect(self, create_schema: bool = True) -> Self:
        database_url = self._config.instance.connection_string

        self._engine = create_async_engine(
            database_url,
            echo=False,
            future=True,
            pool_size=self._config.pool_size,
            max_overflow=self._config.max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_timeout=30,  # Wait up to 30s for a connection
            connect_args={
                'server_settings': {
                    'timezone': 'UTC',
                    'statement_timeout': str(self._config.statement_timeout_ms),
                },
            },
        )

        self._session_factory = async_sessionmaker(
            bind=self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

        if create_schema:
            await self._check_schema_version()

        self._logger.debug('PostgreSQL async engine initialized.')
        return self

    async def close(self) -> Self:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._logger.debug('PostgreSQL async engine disposed.')
        return self

    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError('Database not connected. Call connect() first.')
        return self._session_factory

    async def _check_schema_version(self) -> None:
        """Verify the database schema matches the expected Alembic head revision."""
        from memex_core.migration import get_expected_head

        expected = get_expected_head()

        async with self.engine.begin() as conn:
            # Check if alembic_version table exists
            result = await conn.execute(
                text(
                    'SELECT EXISTS ('
                    '  SELECT 1 FROM information_schema.tables '
                    "  WHERE table_name = 'alembic_version'"
                    ')'
                )
            )
            has_table = result.scalar()

            if not has_table:
                raise RuntimeError(
                    'Database schema not initialized (alembic_version table missing). '
                    'Run: memex database upgrade'
                )

            result = await conn.execute(text('SELECT version_num FROM alembic_version'))
            row = result.first()

            if row is None:
                raise RuntimeError(
                    'Database schema not initialized (no version stamp). '
                    'Run: memex database upgrade'
                )

            current = row[0]
            if current != expected:
                raise RuntimeError(
                    f'Database schema version mismatch: '
                    f'database is at {current!r}, expected {expected!r}. '
                    'Run: memex database upgrade'
                )

        self._logger.debug('Schema version check passed (revision %s).', expected)


def get_metastore(config: PostgresMetaStoreConfig) -> AsyncBaseMetaStoreEngine:
    """Factory function to get the appropriate metastore engine."""
    if config.type == 'postgres':
        return AsyncPostgresMetaStoreEngine(config)
    raise ValueError(f'Unsupported metastore type: {config.type}')
