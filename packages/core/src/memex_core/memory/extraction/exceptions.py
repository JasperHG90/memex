from memex_common.exceptions import MemexError


class OutputTooLongException(Exception): ...


class ExtractionError(MemexError):
    """Raised when DSPy fact extraction fails (non-transient)."""

    ...
