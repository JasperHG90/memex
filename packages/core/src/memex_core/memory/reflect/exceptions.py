"""Reflection-layer exception types shared across engine + service + surface layers."""


class AdvisoryLockTakenError(RuntimeError):
    """Raised when an in-flight refresh must yield to a concurrent writer.

    Two sources today:
      - CAS-UPDATE rowcount=0 in ``_refresh_observation`` Phase C (another
        writer advanced ``mental_models.version`` between Phase A read and
        Phase C write).
      - Live-evidence set changed between Phase A and Phase C (concurrent
        deprio's refresh enqueue was silently deduped against this row).

    The scheduler treats this as "re-claim later" — resets the queue row to
    PENDING with a jittered ``last_queued_at`` in the near future and DOES
    NOT increment ``retry_count``. Both subcases are transient contention,
    not failure. Internal sentinel; not surfaced via HTTP.

    Subclasses are interchangeable from the scheduler's point of view — the
    type system is the documentation: ``RefreshStaleReadError`` for
    Phase-A-vs-C inconsistency, ``RefreshCASAbandonedError`` for version
    contention.
    """


class RefreshStaleReadError(AdvisoryLockTakenError):
    """Phase A read no longer reflects current DB state.

    Live-evidence set changed (an MU was deprio'd / restored after Phase A
    snapshot). Committing the refresh would write observations consistent
    with the stale snapshot. Reclaim and re-run Phase A with current state.
    """


class RefreshCASAbandonedError(AdvisoryLockTakenError):
    """Phase C CAS UPDATE matched zero rows.

    Another writer (Phase 5 or another refresh) advanced
    ``mental_models.version`` between our Phase A read and Phase C commit.
    Reclaim and re-run with the fresher state.
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
