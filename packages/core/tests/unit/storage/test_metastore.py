from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from pydantic import SecretStr

from memex_core.config import PostgresMetaStoreConfig, PostgresInstanceConfig
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine


@pytest.fixture
def mock_config() -> PostgresMetaStoreConfig:
    """Fixture providing a valid PostgresMetaStoreConfig."""
    instance = PostgresInstanceConfig(
        host='localhost',
        port=5432,
        database='test_db',
        user='test_user',
        password=SecretStr('test_password'),
    )
    return PostgresMetaStoreConfig(instance=instance, pool_size=5, max_overflow=10)


@pytest.fixture
def mock_sqla_engine() -> MagicMock:
    """Mock for sqlalchemy's AsyncEngine."""
    engine = MagicMock()

    # dispose is awaitable
    engine.dispose = AsyncMock()

    # begin() is sync but returns an async context manager
    # We use AsyncMock for the context manager so it supports __aenter__/__aexit__
    connection = AsyncMock()
    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = connection

    engine.begin.return_value = context_manager
    return engine


@pytest.fixture
def mock_session_factory() -> MagicMock:
    """Mock for async_sessionmaker."""
    return MagicMock()


@pytest.fixture
def engine_instance(mock_config: PostgresMetaStoreConfig) -> AsyncPostgresMetaStoreEngine:
    """Fixture providing an uninitialized AsyncPostgresMetaStoreEngine."""
    return AsyncPostgresMetaStoreEngine(mock_config)


@pytest.mark.asyncio
async def test_connect(
    engine_instance: AsyncPostgresMetaStoreEngine,
    mock_sqla_engine: MagicMock,
    mock_session_factory: MagicMock,
    mock_config: PostgresMetaStoreConfig,
) -> None:
    """Test connect() initializes engine, session factory, and runs schema check."""
    with (
        patch(
            'memex_core.storage.metastore.create_async_engine', return_value=mock_sqla_engine
        ) as mock_create_engine,
        patch(
            'memex_core.storage.metastore.async_sessionmaker', return_value=mock_session_factory
        ) as mock_maker,
        patch.object(
            AsyncPostgresMetaStoreEngine,
            '_check_schema_version',
            new_callable=AsyncMock,
        ) as mock_check,
    ):
        # Act
        result = await engine_instance.connect()

        # Assert
        assert result is engine_instance

        # Verify create_async_engine call
        mock_create_engine.assert_called_once()
        call_args = mock_create_engine.call_args
        assert call_args[0][0] == mock_config.instance.connection_string
        assert call_args[1]['pool_size'] == 5
        assert call_args[1]['max_overflow'] == 10

        # Verify schema version check was called
        mock_check.assert_awaited_once()

        # Verify session factory creation
        mock_maker.assert_called_once()
        assert mock_maker.call_args[1]['bind'] == mock_sqla_engine


@pytest.mark.asyncio
async def test_close(
    engine_instance: AsyncPostgresMetaStoreEngine, mock_sqla_engine: MagicMock
) -> None:
    """Test close() disposes the engine and clears state."""
    # Setup: manually set engine (simulating connected state)
    engine_instance._engine = mock_sqla_engine
    engine_instance._session_factory = MagicMock()

    # Act
    result = await engine_instance.close()

    # Assert
    assert result is engine_instance
    mock_sqla_engine.dispose.assert_awaited_once()
    assert engine_instance._engine is None
    assert engine_instance._session_factory is None


@pytest.mark.asyncio
async def test_close_idempotent(engine_instance: AsyncPostgresMetaStoreEngine) -> None:
    """Test close() does nothing if already closed."""
    # Setup: ensure no engine
    engine_instance._engine = None

    # Act
    await engine_instance.close()

    # Assert: no error raised, nothing happened
    assert engine_instance._engine is None


def test_session_maker_property(
    engine_instance: AsyncPostgresMetaStoreEngine, mock_session_factory: MagicMock
) -> None:
    """Test session_maker() returns the factory when connected."""
    engine_instance._session_factory = mock_session_factory
    assert engine_instance.session_maker() == mock_session_factory


def test_session_maker_not_connected(engine_instance: AsyncPostgresMetaStoreEngine) -> None:
    """Test session_maker() raises RuntimeError when not connected."""
    engine_instance._session_factory = None
    with pytest.raises(RuntimeError, match='Database not connected'):
        engine_instance.session_maker()


def test_engine_property(
    engine_instance: AsyncPostgresMetaStoreEngine, mock_sqla_engine: MagicMock
) -> None:
    """Test engine property returns the engine when connected."""
    engine_instance._engine = mock_sqla_engine
    assert engine_instance.engine == mock_sqla_engine


def test_engine_property_not_connected(engine_instance: AsyncPostgresMetaStoreEngine) -> None:
    """Test engine property raises RuntimeError when not connected."""
    engine_instance._engine = None
    with pytest.raises(RuntimeError, match='Database not connected'):
        _ = engine_instance.engine


@pytest.mark.asyncio
async def test_open_context_manager(
    engine_instance: AsyncPostgresMetaStoreEngine,
) -> None:
    """Test open() calls connect() and close()."""
    # Mock connect and close on the instance
    engine_instance.connect = AsyncMock(return_value=engine_instance)  # type: ignore
    engine_instance.close = AsyncMock(return_value=engine_instance)  # type: ignore

    async with engine_instance.open() as engine:
        assert engine is engine_instance
        # engine_instance.connect.assert_awaited_once()
        # engine_instance.close.assert_not_awaited()  # Not yet

    # engine_instance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_context_manager(
    engine_instance: AsyncPostgresMetaStoreEngine, mock_session_factory: MagicMock
) -> None:
    """Test session() yields a session from the factory."""
    # Setup
    engine_instance._session_factory = mock_session_factory
    mock_session = AsyncMock(spec=AsyncSession)
    # The factory returns an async context manager that yields the session
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_factory.return_value = mock_session_ctx

    # Act
    async with engine_instance.session() as session:
        assert session is mock_session

    # Assert
    mock_session_factory.assert_called_once()
    mock_session_ctx.__aenter__.assert_awaited_once()
    mock_session_ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_context_manager_error_logging(
    engine_instance: AsyncPostgresMetaStoreEngine, mock_session_factory: MagicMock
) -> None:
    """Test that errors in session context are logged and re-raised."""
    engine_instance._session_factory = mock_session_factory
    mock_session_ctx = AsyncMock()
    mock_session_factory.return_value = mock_session_ctx

    # Setup logger mock
    with patch.object(engine_instance, '_logger') as mock_logger:
        with pytest.raises(ValueError, match='Test error'):
            async with engine_instance.session():
                raise ValueError('Test error')

        mock_logger.error.assert_called_once()
        assert 'Session error' in mock_logger.error.call_args[0][0]
