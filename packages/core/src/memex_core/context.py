from contextvars import ContextVar
from uuid import uuid4

# ContextVar to store the current session ID
_session_id_ctx: ContextVar[str] = ContextVar('session_id', default='global')


def get_session_id() -> str:
    """Get the current session ID from context."""
    return _session_id_ctx.get()


def set_session_id(session_id: str | None = None) -> str:
    """Set the session ID for the current context. Generates a new one if None."""
    sid = session_id or str(uuid4())
    _session_id_ctx.set(sid)
    return sid
