from typing import Any
from uuid import UUID


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


class DeltaValidationError(MemexError, ValueError):
    """Raised when an append delta fails the shape/size/encoding rules.

    Inherits ``ValueError`` so Pydantic surfaces it as a 422 from inside
    model_validators.
    """

    pass


class KVKeyValidationError(MemexError, ValueError):
    """Raised when a KV key fails namespace/shape validation.

    A malformed key is caller-correctable bad input, so this maps to a 400
    (via ``_handle_error``'s generic ``MemexError`` branch) instead of an
    Internal Server Error. Inherits ``ValueError`` so existing call sites
    and tests that assert ``ValueError`` on invalid keys stay green.
    """

    pass


class ObservationReadOnlyError(MemexError):
    """Raised when ``memory_deprioritize`` is called with an ``Observation.id``.

    Mental-model observations are read-only projections of memory units. To
    suppress an observation, deprioritize one of its source MUs. Carries
    ``source_memory_units`` so the HTTP layer can return a structured 400
    body redirecting the caller.
    """

    def __init__(self, source_memory_units: list[UUID]):
        super().__init__(
            'observations are read-only',
            details={'source_memory_units': [str(u) for u in source_memory_units]},
        )
        self.source_memory_units = list(source_memory_units)

    def to_http_detail(self) -> dict[str, Any]:
        """Render the HTTP 400 ``detail`` payload for FastAPI / ASGI surfaces.

        Single source of truth for the response shape — the route handler in
        ``server/memories.py`` and the defensive clause in
        ``server/common.py:_handle_error`` both call this, so a future
        contract change updates one place.
        """
        return {
            'error': 'observations are read-only',
            'source_memory_units': [str(u) for u in self.source_memory_units],
        }
