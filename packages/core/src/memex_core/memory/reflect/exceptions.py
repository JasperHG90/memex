"""Reflection-layer exception types shared across engine + service + surface layers."""


class AdvisoryLockTakenError(RuntimeError):
    """Raised by ``_refresh_observation`` when the per-entity advisory lock is held.

    The scheduler treats this as "re-claim later" — it resets the queue row to
    PENDING with a jittered ``last_queued_at`` in the near future and DOES NOT
    increment ``retry_count``. Internal sentinel; not surfaced via HTTP.
    """


class ReflectionAbandonedError(Exception):
    """Raised when a reflection attempt's Phase 5 CAS UPDATE abandoned
    because a concurrent worker advanced the ``mental_models.version``
    between our read and write.

    The entity is benignly contented, not failed; the queue layer
    handles re-enqueue (``mark_abandoned``, retry_count unchanged) so
    the next scheduler tick re-runs reflection on the fresher state.
    This exception lets synchronous on-demand reflection paths
    (summarize_node / HTTP / MCP / Hermes) propagate the abandon signal
    so surface adapters can translate to a structured retry envelope
    rather than misrepresenting the entity as observation-less.
    """
