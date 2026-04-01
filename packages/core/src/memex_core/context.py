from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

import structlog

# ContextVar to store the current session ID
_session_id_ctx: ContextVar[str] = ContextVar('session_id', default='global')

# ContextVar to store the current actor (who is performing the operation)
_actor_ctx: ContextVar[str] = ContextVar('actor', default='anonymous')

# Optional: bridge session IDs to OpenTelemetry spans (for Arize Phoenix sessions)
try:
    from openinference.instrumentation import using_session as _oi_using_session
except ImportError:
    _oi_using_session = None


def get_session_id() -> str:
    """Get the current session ID from context."""
    return _session_id_ctx.get()


def set_session_id(session_id: str | None = None) -> str:
    """Set the session ID for the current context. Generates a new one if None."""
    sid = session_id or str(uuid4())
    _session_id_ctx.set(sid)
    return sid


def get_actor() -> str:
    """Get the current actor from context."""
    return _actor_ctx.get()


def set_actor(actor: str) -> str:
    """Set the actor for the current context."""
    _actor_ctx.set(actor)
    return actor


@asynccontextmanager
async def background_session(
    label: str = 'background', actor: str = 'system'
) -> AsyncIterator[str]:
    """Establish session context for background (non-HTTP) tasks."""
    sid = set_session_id(f'{label}-{uuid4().hex[:12]}')
    set_actor(actor)
    structlog.contextvars.bind_contextvars(session_id=sid)
    if _oi_using_session:
        with _oi_using_session(sid):
            yield sid
    else:
        yield sid
