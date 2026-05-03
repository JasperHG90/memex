"""Contradiction detection engine — Hindsight Retain-Time."""

import asyncio
import logging
from typing import Any
from uuid import UUID

import dspy
from sqlalchemy import func as sa_func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import ContradictionConfig
from memex_core.llm import run_dspy_operation
from memex_core.tracing import trace_span
from memex_core.memory.contradiction.candidates import get_candidates
from memex_core.memory.contradiction.signatures import (
    CandidateUnit,
    ClassifyRelationships,
    ContradictionRelationship,
    TriageNewUnits,
    TriageUnit,
)
from memex_core.memory.sql_models import MemoryLink, MemoryUnit, Note

logger = logging.getLogger('memex.core.memory.contradiction')


class ContradictionEngine:
    """Detects and records contradictions between memory units."""

    def __init__(self, lm: dspy.LM, config: ContradictionConfig):
        self.lm = lm
        self.config = config
        self.triage_predictor = dspy.Predict(TriageNewUnits)
        self.classify_predictor = dspy.Predict(ClassifyRelationships)

    async def detect_contradictions(
        self,
        session_factory: Any,
        document_id: str | None,
        unit_ids: list[UUID],
        vault_id: UUID,
    ) -> None:
        """
        Run contradiction detection as a background task.
        Creates its own DB session. Errors are logged, never raised.
        """
        try:
            async with session_factory() as session:
                await self._detect(session, unit_ids, vault_id)
                await session.commit()
        except Exception:
            logger.exception('Contradiction detection failed for document %s', document_id)

    async def _detect(
        self,
        session: AsyncSession,
        unit_ids: list[UUID],
        vault_id: UUID,
    ) -> None:
        """Core detection logic."""
        with trace_span(
            'memex.contradiction',
            'contradiction',
            {
                'contradiction.vault_id': str(vault_id),
                'contradiction.unit_count': str(len(unit_ids)),
            },
        ):
            new_units = await self._load_units(session, unit_ids)
            if not new_units:
                logger.info('No units found for IDs %s — already deleted?', unit_ids)
                return

            flagged_ids = await self._triage(new_units)
            if not flagged_ids:
                logger.info('Triage: no corrective units found among %d units', len(new_units))
                return

            flagged_units = [u for u in new_units if str(u.id) in flagged_ids]
            logger.info(
                'Triage: %d/%d units flagged for contradiction check',
                len(flagged_units),
                len(new_units),
            )

            all_links: list[MemoryLink] = []
            # Hermes round-4 HIGH (intra-batch) + round-5 HIGH (cross-batch):
            # accumulate signed alpha-step deltas per target so multiple
            # weakens against the same unit in one batch all land, AND apply
            # them via SQL-level arithmetic (``confidence = clamp(confidence +
            # :delta, 0, 1)``) so concurrent batches that touch overlapping
            # units cannot drift the column out of sync with the SQL-atomic
            # ``confidence_evidence_count`` increment.
            confidence_deltas: dict[UUID, float] = {}
            evidence_bumps: dict[UUID, int] = {}

            tasks = [self._process_flagged_unit(session, unit, vault_id) for unit in flagged_units]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, BaseException):
                    logger.error('Error processing flagged unit: %s', result)
                    continue
                links, deltas, bumps = result
                all_links.extend(links)
                for unit_id, delta in deltas.items():
                    confidence_deltas[unit_id] = confidence_deltas.get(unit_id, 0.0) + delta
                for unit_id, bump in bumps.items():
                    evidence_bumps[unit_id] = evidence_bumps.get(unit_id, 0) + bump

            if all_links:
                deduped: dict[tuple[UUID, UUID, str], MemoryLink] = {}
                for link in all_links:
                    deduped[(link.from_unit_id, link.to_unit_id, link.link_type)] = link
                rows = [
                    {
                        'from_unit_id': lk.from_unit_id,
                        'to_unit_id': lk.to_unit_id,
                        'vault_id': lk.vault_id,
                        'link_type': lk.link_type,
                        'entity_id': lk.entity_id,
                        'link_metadata': lk.link_metadata,
                        'weight': lk.weight,
                    }
                    for lk in deduped.values()
                ]
                insert_stmt = pg_insert(MemoryLink).values(rows)
                upsert_stmt = insert_stmt.on_conflict_do_update(
                    index_elements=['from_unit_id', 'to_unit_id', 'link_type'],
                    set_={
                        'weight': insert_stmt.excluded.weight,
                        'link_metadata': insert_stmt.excluded.link_metadata,
                    },
                )
                await session.exec(upsert_stmt)  # type: ignore[arg-type]
                all_links = list(deduped.values())

            # Apply confidence deltas via SQL-level arithmetic so concurrent
            # _detect calls on overlapping units cannot drift confidence
            # against the atomic confidence_evidence_count increment
            # (Hermes round-5 HIGH). ``LEAST/GREATEST`` clamp matches the
            # prior application-level ``max(0.0, min(1.0, ...))``.
            #
            # Round-4 HIGH semantic note (Hermes round-11): the deltas-and-
            # clamp form trades per-step clamping (old: confidence=0.15
            # weakened twice → 0.05, 0.0 with two zero-pinned steps) for
            # accumulated-then-clamped (new: total delta -0.2 → 0.15-0.2
            # clamped to 0.0 once). The endpoint value is identical for
            # any monotonic-decreasing sequence on a unit; only the
            # *number* of intermediate clamps changes — which is unobserved
            # because nothing reads the mid-batch state. The accumulation
            # is what makes round-4's concurrency fix correct: per-step
            # absolute writes raced; per-step deltas don't.
            #
            # TODO(perf, post-v1): one UPDATE per distinct unit_id is fine
            # for typical contradiction-batch sizes (≤ a few units), but a
            # single bulk ``UPDATE … FROM (VALUES …)`` or ``executemany``
            # would collapse this to one round-trip if/when batch fan-out
            # grows (Hermes round-7 MED — non-blocking).
            for unit_id, delta in confidence_deltas.items():
                values: dict[str, Any] = {
                    'confidence': sa_func.greatest(
                        0.0,
                        sa_func.least(1.0, MemoryUnit.confidence + delta),
                    ),
                }
                bump = evidence_bumps.get(unit_id, 0)
                if bump:
                    # GREATEST(0, ...) belt-and-suspenders parallels the
                    # confidence column's clamp (Hermes round-9 LOW).
                    # Bumps are always +1 today, so the GREATEST is a no-op
                    # — but a future code change that produces negative
                    # deltas would clamp gracefully here instead of
                    # tripping the DB CHECK constraint.
                    values['confidence_evidence_count'] = sa_func.greatest(
                        0,
                        MemoryUnit.confidence_evidence_count + bump,
                    )
                stmt = update(MemoryUnit).where(MemoryUnit.id == unit_id).values(**values)
                await session.execute(stmt)

            logger.info(
                'Contradiction detection: created %d links, updated %d confidences '
                '(evidence bumps: %d)',
                len(all_links),
                len(confidence_deltas),
                sum(evidence_bumps.values()),
            )

    async def _load_units(self, session: AsyncSession, unit_ids: list[UUID]) -> list[MemoryUnit]:
        """Load memory units by IDs."""
        if not unit_ids:
            return []
        stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(unit_ids))
        result = await session.exec(stmt)
        return list(result.all())

    async def _triage(self, units: list[MemoryUnit]) -> list[str]:
        """Single LLM call to identify corrective units."""
        triage_units = [TriageUnit(id=str(u.id), text=u.text) for u in units]

        result = await run_dspy_operation(
            lm=self.lm,
            predictor=self.triage_predictor,
            input_kwargs={'units': triage_units},
            operation_name='contradiction.triage',
        )

        flagged = result.flagged_ids
        return [str(fid) for fid in (flagged or [])]

    async def _process_flagged_unit(
        self,
        session: AsyncSession,
        unit: MemoryUnit,
        vault_id: UUID,
    ) -> tuple[list[MemoryLink], dict[UUID, float], dict[UUID, int]]:
        """Process a single flagged unit: get candidates, classify, adjust.

        Returns ``(links, confidence_deltas, evidence_bumps)``:

          - ``links``: link rows to upsert (deduped by the caller).
          - ``confidence_deltas``: signed alpha-step deltas keyed by unit_id.
            ``+alpha`` for reinforce, ``-alpha`` for weaken, ``-2*alpha`` for
            contradict. The caller sums deltas per-unit (Hermes round-4 HIGH:
            previously this dict was ``confidence_updates`` carrying the
            absolute new value, so concurrent batches that touched the same
            target would overwrite each other and drift the confidence column
            out of sync with ``confidence_evidence_count``).
          - ``evidence_bumps``: per-unit increments for
            ``confidence_evidence_count`` — summed by the caller.

        Both delta maps stay symmetric in the caller's accumulation step so
        ``confidence`` and ``confidence_evidence_count`` advance in lockstep.
        """
        candidates = await get_candidates(
            session,
            unit,
            vault_id,
            k=self.config.max_candidates_per_unit,
            threshold=self.config.similarity_threshold,
        )

        if not candidates:
            logger.info(
                'Unit %s: no candidates found (threshold=%.2f)',
                unit.id,
                self.config.similarity_threshold,
            )
            return [], {}, {}

        relationships = await self._classify(unit, candidates)

        links: list[MemoryLink] = []
        confidence_deltas: dict[UUID, float] = {}
        evidence_bumps: dict[UUID, int] = {}

        for rel in relationships:
            relation = rel.relation
            authoritative_hint = rel.authoritative
            reasoning = rel.reasoning

            existing_unit = next((c for c in candidates if str(c.id) == rel.existing_id), None)
            if existing_unit is None:
                continue

            authoritative, superseded = self._resolve_authority(
                unit, existing_unit, authoritative_hint
            )

            note_title = await self._get_note_title(session, authoritative.note_id)

            if relation == 'reinforce':
                # Symmetric reinforce: both endpoints gain +alpha. Reinforce
                # does NOT bump evidence_count (F22 v1 design — see BACKLOG
                # known-v1-limitation: forward/backfill symmetry).
                for u in [unit, existing_unit]:
                    confidence_deltas[u.id] = confidence_deltas.get(u.id, 0.0) + self.config.alpha
                link_type = 'reinforces'
            elif relation == 'weaken':
                confidence_deltas[superseded.id] = (
                    confidence_deltas.get(superseded.id, 0.0) - self.config.alpha
                )
                evidence_bumps[superseded.id] = evidence_bumps.get(superseded.id, 0) + 1
                link_type = 'weakens'
            elif relation == 'contradict':
                confidence_deltas[superseded.id] = (
                    confidence_deltas.get(superseded.id, 0.0) - 2 * self.config.alpha
                )
                evidence_bumps[superseded.id] = evidence_bumps.get(superseded.id, 0) + 1
                link_type = 'contradicts'
            else:
                continue

            link = MemoryLink(
                from_unit_id=authoritative.id,
                to_unit_id=superseded.id,
                link_type=link_type,
                vault_id=vault_id,
                weight=1.0,
                link_metadata={
                    'authoritative_unit_id': str(authoritative.id),
                    'superseded_unit_id': str(superseded.id),
                    'reasoning': reasoning,
                    'temporal_basis': (
                        'llm_override'
                        if authoritative_hint != self._temporal_default(unit, existing_unit)
                        else 'timestamp'
                    ),
                    'superseding_note_title': note_title,
                },
            )
            links.append(link)

        return links, confidence_deltas, evidence_bumps

    async def _classify(
        self, unit: MemoryUnit, candidates: list[MemoryUnit]
    ) -> list[ContradictionRelationship]:
        """Classify relationships between unit and candidates."""
        candidate_models = [
            CandidateUnit(
                id=str(c.id),
                text=c.text,
                date=c.event_date.isoformat() if c.event_date else 'unknown',
            )
            for c in candidates
        ]

        result = await run_dspy_operation(
            lm=self.lm,
            predictor=self.classify_predictor,
            input_kwargs={
                'new_unit_text': unit.text,
                'new_unit_date': (unit.event_date.isoformat() if unit.event_date else 'unknown'),
                'candidates': candidate_models,
            },
            operation_name='contradiction.classify',
        )

        relationships: list[ContradictionRelationship] = result.relationships or []
        valid_relations = {'reinforce', 'weaken', 'contradict'}
        return [r for r in relationships if r.relation in valid_relations]

    def _resolve_authority(
        self,
        new_unit: MemoryUnit,
        existing_unit: MemoryUnit,
        llm_hint: str,
    ) -> tuple[MemoryUnit, MemoryUnit]:
        """Determine which unit is authoritative (wins) and which is superseded."""
        temporal_default = self._temporal_default(new_unit, existing_unit)

        if llm_hint == temporal_default or llm_hint not in ('new', 'existing'):
            if new_unit.event_date and existing_unit.event_date:
                if new_unit.event_date >= existing_unit.event_date:
                    return new_unit, existing_unit
                return existing_unit, new_unit
            return new_unit, existing_unit

        if llm_hint == 'new':
            return new_unit, existing_unit
        return existing_unit, new_unit

    @staticmethod
    def _temporal_default(new_unit: MemoryUnit, existing_unit: MemoryUnit) -> str:
        """What temporal heuristic would say."""
        if new_unit.event_date and existing_unit.event_date:
            if new_unit.event_date >= existing_unit.event_date:
                return 'new'
            return 'existing'
        return 'new'

    @staticmethod
    async def _get_note_title(session: AsyncSession, note_id: UUID | None) -> str | None:
        """Get note title for provenance metadata."""
        if note_id is None:
            return None
        stmt = select(Note.title).where(Note.id == note_id)
        result = await session.exec(stmt)
        return result.first()
