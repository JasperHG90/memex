"""Base class for all Memex domain services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memex_core.config import MemexConfig
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore

if TYPE_CHECKING:
    from memex_core.services.audit import AuditService


class BaseService:
    """Base for all Memex domain services.

    Consolidates the shared dependencies every service needs:
    metastore (async DB), filestore (file backend), and config.
    """

    _audit_service: AuditService | None = None

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
    ) -> None:
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
