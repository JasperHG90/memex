"""Reflection-layer exception types shared across engine + service + surface layers."""


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
