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
from sqlalchemy.exc import IntegrityError
from sqlmodel import text, SQLModel
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
            # Initialize extensions separately to handle race conditions in multi-worker setups.
            # We use separate transactions so that if one fails (e.g. UniqueViolation),
            # it doesn't abort the transaction for the others.

            # 1. Vector extension
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            except IntegrityError:
                self._logger.warning('Vector extension already exists (race condition handled).')
            except Exception as e:
                self._logger.warning(f'Error checking vector extension: {e}')

            # 2. pg_trgm extension
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
            except IntegrityError:
                self._logger.warning('pg_trgm extension already exists (race condition handled).')
            except Exception as e:
                self._logger.warning(f'Error checking pg_trgm extension: {e}')

            # 3. Create tables
            async with self.engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)

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


def get_metastore(config: PostgresMetaStoreConfig) -> AsyncBaseMetaStoreEngine:
    """Factory function to get the appropriate metastore engine."""
    if config.type == 'postgres':
        return AsyncPostgresMetaStoreEngine(config)
    raise ValueError(f'Unsupported metastore type: {config.type}')
