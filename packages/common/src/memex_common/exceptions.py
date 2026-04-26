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


class NoteNotFoundError(ResourceNotFoundError):
    """Raised when a note is not found."""

    pass


class DuplicateResourceError(MemexError):
    """Raised when a resource already exists."""

    pass


class AmbiguousResourceError(MemexError):
    """Raised when a query for a resource returns multiple results but only one was expected."""

    pass


class NoteNotAppendableError(MemexError):
    """Raised when an append targets a note whose status forbids further appends.

    Notes in 'archived' or 'superseded' state are immutable; callers must
    target an active note.
    """

    pass


class AppendIdConflictError(MemexError):
    """Raised when the same append_id has been used for a different operation.

    Replay semantics require the (note_id, delta) pair to be identical to the
    first call. A different parent or a different delta with the same append_id
    indicates a caller bug, not a retry.
    """

    pass


class FeatureDisabledError(MemexError):
    """Raised when a feature has been administratively disabled via config."""

    pass


class AppendLockTimeoutError(MemexError):
    """Raised when the per-parent append lock could not be acquired in time.

    Indicates contention on a hot parent — typically because a long-running
    extraction is in flight ahead of us. Callers should retry with backoff.
    """

    pass
