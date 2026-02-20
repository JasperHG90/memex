from typing import Any


class MemexError(Exception):
    """Base class for all Memex exceptions."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ResourceNotFoundError(MemexError):
    """Raised when a requested resource is not found."""

    pass


class VaultNotFoundError(ResourceNotFoundError):
    """Raised when a vault is not found."""

    pass


class EntityNotFoundError(ResourceNotFoundError):
    """Raised when an entity is not found."""

    pass


class MemoryUnitNotFoundError(ResourceNotFoundError):
    """Raised when a memory unit is not found."""

    pass


class DocumentNotFoundError(ResourceNotFoundError):
    """Raised when a document is not found."""

    pass


class DuplicateResourceError(MemexError):
    """Raised when a resource already exists."""

    pass


class AmbiguousResourceError(MemexError):
    """Raised when a query for a resource returns multiple results but only one was expected."""

    pass
