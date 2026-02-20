from typing import Self
import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine


class AsyncTransaction:
    """Manages an atomic transaction across both SQLModel (DB) and the file store.

    Uses a two-phase commit strategy:
    1. Open DB Transaction & Stage Files.
    2. If success: Commit DB -> Commit Files.
    3. If fail: Rollback DB -> Rollback Files.
    """

    def __init__(self, meta: AsyncBaseMetaStoreEngine, fs: BaseAsyncFileStore, transaction_id: str):
        self.meta = meta
        self.fs = fs
        self.txn_id = transaction_id
        self._logger = logging.getLogger('memex_core.storage.transaction.AsyncTransaction')

        self._session: AsyncSession | None = None
        self._transaction_ctx = None  # The generic Helper object from session.begin()

    @property
    def db_session(self) -> AsyncSession:
        """The active SQLModel session for this transaction."""
        if self._session is None:
            raise RuntimeError("Transaction not started. Use 'async with' to start.")
        return self._session

    async def __aenter__(self) -> Self:
        """Enter the transaction context."""
        session_factory = self.meta.session_maker()
        self._session = session_factory()
        await self._session.begin()
        try:
            self.fs.begin_staging(self.txn_id)
        except Exception as e:
            self._logger.error(f'Failed to begin file staging: {e}.')
            await self._session.close()
            self._session = None
            raise e
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the transaction context."""
        try:
            if exc_type:
                await self._rollback()
                return False
            await self.db_session.commit()
            await self.fs.commit_staging()
        except Exception as e:
            await self._rollback()
            raise e
        finally:
            if self._session:
                await self._session.close()
                self._session = None

    async def _rollback(self):
        """Rollback both DB and File operations."""
        # Rollback DB
        if self._session:
            try:
                await self._session.rollback()
            except Exception as e:
                self._logger.error(f'Failed to roll back DB transaction: {e}.')
                pass

        # Rollback Files
        await self.fs.rollback_staging()
