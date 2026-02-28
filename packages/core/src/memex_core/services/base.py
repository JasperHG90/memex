"""Base class for all Memex domain services."""

from __future__ import annotations

from memex_core.config import MemexConfig
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore


class BaseService:
    """Base for all Memex domain services.

    Consolidates the shared dependencies every service needs:
    metastore (async DB), filestore (file backend), and config.
    """

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
    ) -> None:
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
