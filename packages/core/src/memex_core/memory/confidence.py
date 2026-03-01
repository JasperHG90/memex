import logging
import math
from uuid import UUID
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, col

from memex_core.types import EvidenceType
from memex_core.memory.sql_models import MemoryUnit, EvidenceLog

logger = logging.getLogger('memex.core.memory.confidence')


def confidence_weight(confidence_score: float | None, floor: float = 0.3) -> float:
    """Continuous confidence weighting factor for ranking.

    Returns floor + (1-floor) * confidence for opinions, 1.0 for non-opinions (None).
    """
    if confidence_score is None:
        return 1.0
    return floor + (1.0 - floor) * confidence_score


class ConfidenceEngine:
    """
    Engine for calculating and updating confidence scores using Bayesian inference
     and informative priors (semantic inheritance).
    """

    def __init__(
        self,
        damping_factor: float = 0.1,
        max_inherited_mass: float = 10.0,
        similarity_threshold: float = 0.8,
    ):
        self.damping_factor = damping_factor
        self.max_inherited_mass = max_inherited_mass
        self.similarity_threshold = similarity_threshold

    async def adjust_belief(
        self,
        session: AsyncSession,
        unit_uuid: str | UUID,
        evidence_type_key: str,
        description: str | None = None,
    ) -> dict[str, float]:
        """
        Adjust the confidence of a memory unit based on new evidence (Bayesian update).
        """
        # Map key to EvidenceType
        try:
            # Match by key (e.g. 'user_validation')
            evidence = next(e for e in EvidenceType if e.key == evidence_type_key)
        except StopIteration:
            raise ValueError(f'Invalid evidence type key: {evidence_type_key}')

        # 1. Fetch MemoryUnit
        if isinstance(unit_uuid, str):
            unit_id = UUID(unit_uuid)
        else:
            unit_id = unit_uuid

        statement = select(MemoryUnit).where(col(MemoryUnit.id) == unit_id)
        result = await session.exec(statement)
        unit = result.first()

        if not unit:
            raise ValueError(f'Memory unit not found: {unit_id}')

        if unit.fact_type != 'opinion':
            raise ValueError(
                f'Cannot adjust belief on non-opinion unit (type={unit.fact_type}). '
                f'Only opinion-type memory units have confidence scores.'
            )

        # 2. Capture state before update
        alpha_before = unit.confidence_alpha if unit.confidence_alpha is not None else 1.0
        beta_before = unit.confidence_beta if unit.confidence_beta is not None else 1.0

        # 3. Apply Update
        if evidence.is_success:
            unit.confidence_alpha = alpha_before + evidence.weight
            unit.confidence_beta = beta_before
        else:
            unit.confidence_alpha = alpha_before
            unit.confidence_beta = beta_before + evidence.weight

        # 4. Log EvidenceTrail
        log_entry = EvidenceLog(
            unit_id=unit_id,
            evidence_type=evidence.key,
            description=description,
            alpha_before=alpha_before,
            beta_before=beta_before,
            alpha_after=unit.confidence_alpha,
            beta_after=unit.confidence_beta,
        )
        session.add(log_entry)
        session.add(unit)
        await session.flush()

        return {
            'confidence_before': alpha_before / (alpha_before + beta_before),
            'confidence_after': unit.confidence_alpha
            / (unit.confidence_alpha + unit.confidence_beta),
            'alpha': unit.confidence_alpha,
            'beta': unit.confidence_beta,
        }

    async def apply_custom_update(
        self,
        session: AsyncSession,
        unit_uuid: str | UUID,
        alpha_delta: float,
        beta_delta: float,
        evidence_type: str,
        description: str | None = None,
    ) -> None:
        """
        Apply a custom update to confidence scores and log it.
        Useful for automated processes (e.g. merging opinions) where weights are calculated dynamically.
        """
        # 1. Fetch MemoryUnit
        if isinstance(unit_uuid, str):
            unit_id = UUID(unit_uuid)
        else:
            unit_id = unit_uuid

        statement = select(MemoryUnit).where(col(MemoryUnit.id) == unit_id)
        result = await session.exec(statement)
        unit = result.first()

        if not unit:
            raise ValueError(f'Memory unit not found: {unit_id}')

        # Capture state before update
        alpha_before = unit.confidence_alpha if unit.confidence_alpha is not None else 1.0
        beta_before = unit.confidence_beta if unit.confidence_beta is not None else 1.0

        # 2. Update stats
        unit.confidence_alpha = alpha_before + alpha_delta
        unit.confidence_beta = beta_before + beta_delta

        # Also update access stats (mimicking storage.update_fact_confidence)
        unit.access_count += 1
        from datetime import datetime, timezone

        unit.mentioned_at = datetime.now(timezone.utc)

        # 4. Log EvidenceTrail
        log_entry = EvidenceLog(
            unit_id=unit_id,
            evidence_type=evidence_type,
            description=description,
            alpha_before=alpha_before,
            beta_before=beta_before,
            alpha_after=unit.confidence_alpha,
            beta_after=unit.confidence_beta,
        )
        session.add(log_entry)
        session.add(unit)
        await session.flush()

    async def calculate_informative_prior(
        self,
        session: AsyncSession,
        embedding: list[float],
        exclude_ids: list[UUID] | None = None,
    ) -> tuple[float, float]:
        """
        Calculates starting alpha and beta values based on semantic neighbors.

        Logic:
        1. Find top-5 semantic neighbors.
        2. If neighbors have confidence scores, inherit a damped portion of their mass.
        3. If neighbors are 'world' facts (no explicit confidence), treat them as
           moderately high confidence (alpha=5.0, beta=1.0) for inheritance purposes.
        """
        # Guard: If embedding is a zero-vector (or near zero), it has no semantic location.
        # Return uninformative prior immediately to avoid NaN in cosine similarity.
        if not any(embedding) or sum(x * x for x in embedding) < 1e-9:
            return 1.0, 1.0

        if exclude_ids is None:
            exclude_ids = []

        from typing import Any, cast

        # 1 - cosine_distance = cosine_similarity
        similarity = 1 - cast(Any, col(MemoryUnit.embedding)).cosine_distance(embedding)

        # Search for neighbors
        statement = (
            select(
                MemoryUnit.confidence_alpha,
                MemoryUnit.confidence_beta,
                MemoryUnit.fact_type,
                similarity,
            )
            .where(similarity >= self.similarity_threshold)
            .order_by(similarity.desc())
            .limit(5)
        )

        if exclude_ids:
            statement = statement.where(col(MemoryUnit.id).not_in(exclude_ids))

        results = await session.exec(statement)
        neighbors = results.all()

        inherited_alpha = 0.0
        inherited_beta = 0.0

        for alpha, beta, fact_type, sim in neighbors:
            if sim is None or math.isnan(sim):
                continue

            # Determine effective alpha/beta for inheritance
            eff_alpha = alpha
            eff_beta = beta

            # If it's a world fact without explicit scores, assume high-confidence prior
            if eff_alpha is None or eff_beta is None:
                if fact_type in ('world', 'experience'):
                    eff_alpha = 5.0
                    eff_beta = 1.0
                else:
                    # For other types without scores (shouldn't happen for opinions), use neutral prior
                    eff_alpha = 1.0
                    eff_beta = 1.0

            # Apply similarity weighting and damping
            inherited_alpha += eff_alpha * float(sim) * self.damping_factor
            inherited_beta += eff_beta * float(sim) * self.damping_factor

        # Apply cap to inherited mass to prevent echo chambers
        total_inherited = inherited_alpha + inherited_beta
        if total_inherited > self.max_inherited_mass:
            scale = self.max_inherited_mass / total_inherited
            inherited_alpha *= scale
            inherited_beta *= scale

        # Start with uniform prior (1.0, 1.0) + inherited mass
        return (1.0 + inherited_alpha, 1.0 + inherited_beta)
