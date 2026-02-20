from unittest.mock import AsyncMock, Mock
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.storage.transaction import AsyncTransaction
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore


@pytest.fixture
def mock_meta() -> AsyncMock:
    """Mock for AsyncBaseMetaStoreEngine."""
    meta = Mock(spec=AsyncBaseMetaStoreEngine)
    # session_maker returns a factory, which returns a session
    session_factory = Mock()
    meta.session_maker.return_value = session_factory
    return meta


@pytest.fixture
def mock_session(mock_meta: Mock) -> AsyncMock:
    """Mock for AsyncSession, wired into mock_meta."""
    # Create an AsyncMock for the session
    session = AsyncMock(spec=AsyncSession)

    # Explicitly make sure lifecycle methods are AsyncMocks so they can be awaited
    session.begin = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()

    # Wire it up: meta.session_maker() -> factory; factory() -> session
    # mock_meta.session_maker is already a Mock from mock_meta fixture
    session_factory = mock_meta.session_maker.return_value
    session_factory.return_value = session

    return session


@pytest.fixture
def mock_fs() -> AsyncMock:
    """Mock for BaseAsyncFileStore."""
    fs = AsyncMock(spec=BaseAsyncFileStore)
    fs.begin_staging = Mock()  # Synchronous method
    fs.commit_staging = AsyncMock()
    fs.rollback_staging = AsyncMock()
    return fs


@pytest.fixture
def transaction(mock_meta: Mock, mock_fs: AsyncMock) -> AsyncTransaction:
    """Fixture providing an AsyncTransaction instance."""
    return AsyncTransaction(mock_meta, mock_fs, 'test-txn-id')


def test_init(transaction: AsyncTransaction, mock_meta: Mock, mock_fs: AsyncMock) -> None:
    """Test correct initialization of AsyncTransaction."""
    assert transaction.meta == mock_meta
    assert transaction.fs == mock_fs
    assert transaction.txn_id == 'test-txn-id'
    assert transaction._session is None


def test_access_session_before_start(transaction: AsyncTransaction) -> None:
    """Test accessing db_session before transaction start raises RuntimeError."""
    with pytest.raises(RuntimeError, match='Transaction not started'):
        _ = transaction.db_session


async def test_transaction_success(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test successful transaction flow (happy path)."""
    async with transaction as txn:
        assert txn is transaction
        assert txn.db_session == mock_session

        # Verify startup actions
        mock_session.begin.assert_awaited_once()
        mock_fs.begin_staging.assert_called_once_with('test-txn-id')

    # Verify commit actions on exit
    mock_session.commit.assert_awaited_once()
    mock_fs.commit_staging.assert_awaited_once()
    mock_session.close.assert_awaited_once()


async def test_transaction_staging_failure(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test failure during file staging initialization."""
    mock_fs.begin_staging.side_effect = ValueError('Staging failed')

    with pytest.raises(ValueError, match='Staging failed'):
        async with transaction:
            pass

    # Verify cleanup
    mock_session.begin.assert_awaited_once()
    mock_session.close.assert_awaited_once()
    # Should NOT commit or rollback if start failed
    mock_session.commit.assert_not_awaited()
    mock_fs.commit_staging.assert_not_awaited()


async def test_transaction_body_failure(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test rollback when exception occurs in the transaction body."""
    with pytest.raises(ValueError, match='Body error'):
        async with transaction:
            raise ValueError('Body error')

    # Verify rollback actions
    mock_session.rollback.assert_awaited_once()
    mock_fs.rollback_staging.assert_awaited_once()
    mock_session.close.assert_awaited_once()

    # Should NOT commit
    mock_session.commit.assert_not_awaited()
    mock_fs.commit_staging.assert_not_awaited()


async def test_transaction_db_commit_failure(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test rollback when DB commit fails."""
    mock_session.commit.side_effect = RuntimeError('DB Commit failed')

    with pytest.raises(RuntimeError, match='DB Commit failed'):
        async with transaction:
            pass

    # Verify rollback actions
    mock_session.commit.assert_awaited_once()
    mock_session.rollback.assert_awaited_once()
    mock_fs.rollback_staging.assert_awaited_once()
    mock_session.close.assert_awaited_once()

    # FS commit should be skipped
    mock_fs.commit_staging.assert_not_awaited()


async def test_transaction_file_commit_failure(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test rollback when FileStore commit fails."""
    mock_fs.commit_staging.side_effect = IOError('File Commit failed')

    with pytest.raises(IOError, match='File Commit failed'):
        async with transaction:
            pass

    # Verify rollback actions
    mock_session.commit.assert_awaited_once()
    # Since commit succeeded, rollback is called in exception handler
    mock_session.rollback.assert_awaited_once()
    mock_fs.rollback_staging.assert_awaited_once()
    mock_session.close.assert_awaited_once()


async def test_rollback_swallows_db_exception(
    transaction: AsyncTransaction, mock_session: AsyncMock, mock_fs: AsyncMock
) -> None:
    """Test that DB rollback failure doesn't block FS rollback."""
    # Setup: Body fails, triggering rollback. DB rollback also fails.
    mock_session.rollback.side_effect = Exception('Rollback failed')

    with pytest.raises(ValueError, match='Original error'):
        async with transaction:
            raise ValueError('Original error')

    # Verify correct call order
    mock_session.rollback.assert_awaited_once()
    mock_fs.rollback_staging.assert_awaited_once()
    mock_session.close.assert_awaited_once()
