"""Shared F22 confidence-gate primitives for lint and lint_llm.

Hermes round-10 MED: ``lint_llm`` previously reached into ``lint``'s private
symbols (``_gate_blocks_finding``, ``_bulk_load_confidence_map``,
``_confidence_map_blocks``) to share the gate predicate. Promote the gate
helpers to a third module so both call sites import the SAME public symbol
and there is no private-API coupling between the two services.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.confidence import mean_and_variance


def _clamp_confidence_pair(confidence: float, evidence_count: int) -> tuple[float, int]:
    """Clamp a (confidence, evidence_count) pair to the F22 valid ranges.

    Hermes round-16 MED: the three lint paths (``gate_blocks_finding``,
    ``bulk_load_confidence_map``, ``confidence_map_blocks``) all need
    the same ``confidence ∈ [0, 1]`` + ``evidence_count >= 0``
    belt-and-suspenders clamp before calling ``mean_and_variance``
    (which raises ``ValueError`` on out-of-range input). Centralise
    the formula here so a future tweak — e.g., logging a warning when
    the clamp engages, or delegating fully to
    ``extract_confidence_and_count`` — lands once instead of three
    times.
    """
    return max(0.0, min(1.0, confidence)), max(0, evidence_count)


# Hermes round-14 MED: explicit CAST to ``uuid`` so a non-UUID string
# parameter raises a clear, predictable Postgres ``invalid input syntax``
# error rather than a partial implicit-cast failure deep in asyncpg's
# prepared-statement machinery. The column is UUID-typed; matching the
# parameter type at the SQL boundary keeps the call site self-documenting.
_LOAD_UNIT_CONFIDENCE_SQL = text("""
    SELECT confidence, confidence_evidence_count
    FROM memory_units
    WHERE id = CAST(:unit_id AS uuid)
""")


# Hermes round-5 MED: ``expanding=True`` is the portable analogue of the
# prior ``CAST(:unit_ids AS uuid[]) + ANY``. SQLAlchemy expands ``:unit_ids``
# into ``(:p1, :p2, ...)`` at compile time and asyncpg infers the column
# type from the LHS of IN, so an explicit per-parameter cast is unnecessary.
_BULK_LOAD_UNIT_CONFIDENCE_SQL = text("""
    SELECT id::text AS unit_id, confidence, confidence_evidence_count
    FROM memory_units
    WHERE id IN :unit_ids
""").bindparams(bindparam('unit_ids', expanding=True))


async def gate_blocks_finding(
    session: AsyncSession,
    unit_id: str,
    confidence_min: float,
    variance_max: float,
) -> bool:
    """Return True iff (confidence, variance) violates the lint gate.

    Cold-start units (count=0, variance=1/12) are blocked when ``variance_max``
    is set below 1/12 — so freshly extracted units don't surface as
    "low-confidence" findings purely because they have no evidence yet.

    The per-row fetch is retained for tests and one-off callers; the
    rule-runner path uses :func:`bulk_load_confidence_map` to avoid the
    N+1 query that would otherwise fire on rules with many candidate rows.
    """
    row = (await session.execute(_LOAD_UNIT_CONFIDENCE_SQL, {'unit_id': unit_id})).first()
    if row is None:
        return False
    confidence = float(row[0]) if row[0] is not None else 1.0
    evidence_count = int(row[1]) if row[1] is not None else 0
    # Clamp parallels the retrieval path's ``extract_confidence_and_count``
    # (Hermes round-11 MED): the DB CHECK guards production writes, but a
    # stale/in-memory caller mustn't crash ``mean_and_variance`` on an
    # out-of-range confidence — defence-in-depth across both paths.
    confidence, evidence_count = _clamp_confidence_pair(confidence, evidence_count)
    _, variance = mean_and_variance(confidence, evidence_count)
    return confidence < confidence_min or variance > variance_max


async def bulk_load_confidence_map(
    session: AsyncSession,
    unit_ids: list[str],
) -> dict[str, tuple[float, int]]:
    """Bulk-fetch ``(confidence, confidence_evidence_count)`` for a set of units.

    Returns a map keyed by ``unit_id`` (text). IDs missing from the map mean
    the row was not found — callers should treat this as "do not block"
    (parity with :func:`gate_blocks_finding`'s ``row is None`` branch).

    F22: replaces the per-row SELECT inside the rule-runner loop so a rule
    that returns N candidates issues exactly one extra query (not N).
    """
    if not unit_ids:
        return {}
    result = await session.execute(_BULK_LOAD_UNIT_CONFIDENCE_SQL, {'unit_ids': unit_ids})
    out: dict[str, tuple[float, int]] = {}
    for row in result.mappings().all():
        confidence = float(row['confidence']) if row['confidence'] is not None else 1.0
        evidence_count = (
            int(row['confidence_evidence_count'])
            if row['confidence_evidence_count'] is not None
            else 0
        )
        # Hermes round-15 MED: clamp here so any new caller of this map
        # cannot pass an out-of-range confidence into ``mean_and_variance``
        # and trip its ``[0, 1]`` invariant. Parity with the inline clamp
        # already done in ``gate_blocks_finding`` and
        # ``confidence_map_blocks`` — the map's contract is "values are
        # safe to feed into the variance formula", not "raw DB values".
        out[row['unit_id']] = _clamp_confidence_pair(confidence, evidence_count)
    return out


def confidence_map_blocks(
    confidence_map: dict[str, tuple[float, int]],
    unit_id: str,
    confidence_min: float,
    variance_max: float,
) -> bool:
    """In-memory mirror of :func:`gate_blocks_finding` against a prefetched map.

    Missing entries do NOT block (parity: a missing row also does not block in
    the per-row SELECT path).
    """
    entry = confidence_map.get(unit_id)
    if entry is None:
        return False
    confidence, evidence_count = entry
    # See ``gate_blocks_finding`` for the clamp rationale (Hermes round-11 MED).
    confidence, evidence_count = _clamp_confidence_pair(confidence, evidence_count)
    _, variance = mean_and_variance(confidence, evidence_count)
    return confidence < confidence_min or variance > variance_max
