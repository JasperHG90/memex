"""Surprise score — anisotropy-corrected per-unit surprise.

Mirrors production semantics from ``memory/contradiction/candidates.py`` and
``memory/retrieval/engine.py`` (``_compute_pairwise_cosine``)::

  surprise(unit, vault) = 1 - mean( normalize( top_k_cosine_sim(unit, vault) ) )

where ``normalize`` is the shared :class:`AnisotropyCorrector` (Z-score →
sigmoid) and ``top_k_cosine_sim`` is computed in pgvector with
``1 - (a.embedding <=> b.embedding)``.

A unit that fits its vault returns high corrected similarities → low surprise.
A unit that is anomalous (figure-skating in a Python-frameworks vault, or a
contradiction to the corpus consensus) returns low corrected similarities →
high surprise.

Validated by POC (`pocs/002-f10-surprise-threshold/result.md`):
- PASS topical-anomaly recall @ 0.7 threshold.
- Polarity-inversion recall is bridged by the NLI classifier: see ``polarity.py`` and
  :func:`gate_passes` — cosine surprise OR'd with NLI contradiction-probability.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.lint_llm.polarity import (
    DEFAULT_POLARITY_THRESHOLD,
    gate_passes as _polarity_gate_passes,
)
from memex_core.memory.models.anisotropy import (
    AnisotropyCorrector,
    get_shared_corrector,
)


DEFAULT_K = 8


def gate_passes(
    cosine_surprise: float,
    polarity_contra_prob: float | None,
    *,
    surprise_threshold: float,
    polarity_threshold: float = DEFAULT_POLARITY_THRESHOLD,
) -> bool:
    """Composed cosine + NLI gate.

    Returns ``True`` when the cosine surprise alone clears ``surprise_threshold``
    OR (when cosine is below threshold) the supplied NLI contradiction-probability
    crosses ``polarity_threshold``.

    Callers MUST pass ``polarity_contra_prob=None`` when cosine surprise is
    already above the threshold so NLI is not invoked unnecessarily — the
    short-circuit on the cosine branch enforces this.
    """
    return _polarity_gate_passes(
        cosine_surprise,
        polarity_contra_prob,
        surprise_threshold=surprise_threshold,
        polarity_threshold=polarity_threshold,
    )


async def compute_unit_surprise(
    unit_id: UUID,
    vault_id: UUID,
    session: AsyncSession,
    *,
    k: int = DEFAULT_K,
    corrector: AnisotropyCorrector | None = None,
) -> float:
    """Anisotropy-corrected surprise score for ``unit_id`` against its vault.

    Returns a value in [0, 1] where 1.0 is maximally surprising. Computes
    top-k cosine similarities to other ACTIVE units in the same vault via
    pgvector, routes them through the anisotropy corrector, then returns
    ``1 - mean(corrected)``.

    A vault with fewer than ``k`` peer units returns 1.0 (no neighborhood to
    fit into → maximally surprising by definition). Same response for a unit
    whose embedding is missing.
    """
    corrector = corrector or get_shared_corrector()

    stmt = text("""
        WITH self AS (
            SELECT embedding
            FROM memory_units
            WHERE id = :unit_id
        )
        SELECT 1 - (m.embedding <=> self.embedding) AS sim
        FROM memory_units m, self
        WHERE m.vault_id = :vault_id
          AND m.id != :unit_id
          AND m.status = 'active'
          AND m.embedding IS NOT NULL
          AND self.embedding IS NOT NULL
        ORDER BY (m.embedding <=> self.embedding)
        LIMIT :k
    """)
    conn = await session.connection()
    result = await conn.execute(
        stmt,
        {'unit_id': str(unit_id), 'vault_id': str(vault_id), 'k': k},
    )
    raw_sims = [float(row.sim) for row in result]

    if len(raw_sims) < k:
        return 1.0

    corrected = [corrector.normalize(s) for s in raw_sims]
    return 1.0 - (sum(corrected) / len(corrected))


async def warm_corrector(
    session: AsyncSession,
    vault_id: UUID,
    *,
    target_observations: int = 256,
    corrector: AnisotropyCorrector | None = None,
) -> int:
    """Warm the shared corrector with random pairwise similarities from a vault.

    The corrector is cold-start passthrough until it has ``min_samples``
    (default 32) observations in its window; the corrected output only
    diverges from the raw similarity once the window is populated. To make
    the surprise score meaningful on the first call, pre-feed at least
    ``target_observations`` similarities drawn from random pairs of units in
    the vault.

    Returns the number of similarities fed to the corrector.
    """
    corrector = corrector or get_shared_corrector()

    stmt = text("""
        SELECT 1 - (a.embedding <=> b.embedding) AS sim
        FROM memory_units a, memory_units b
        WHERE a.vault_id = :vault_id
          AND b.vault_id = :vault_id
          AND a.id < b.id
          AND a.status = 'active'
          AND b.status = 'active'
          AND a.embedding IS NOT NULL
          AND b.embedding IS NOT NULL
        ORDER BY random()
        LIMIT :limit
    """)
    conn = await session.connection()
    result = await conn.execute(stmt, {'vault_id': str(vault_id), 'limit': target_observations})
    fed = 0
    for row in result:
        corrector.normalize(float(row.sim))
        fed += 1
    return fed
