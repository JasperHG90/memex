"""Contradiction detection engine — Hindsight Retain-Time."""

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import dspy
from sqlalchemy import update
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import ContradictionConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.contradiction.candidates import get_candidates
from memex_core.memory.contradiction.signatures import (
    ClassifyRelationships,
    TriageNewUnits,
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
        new_units = await self._load_units(session, unit_ids)
        if not new_units:
            return

        flagged_ids = await self._triage(new_units)
        if not flagged_ids:
            logger.debug('Triage: no corrective units found among %d units', len(new_units))
            return

        flagged_units = [u for u in new_units if str(u.id) in flagged_ids]
        logger.info(
            'Triage: %d/%d units flagged for contradiction check',
            len(flagged_units),
            len(new_units),
        )

        all_links: list[MemoryLink] = []
        confidence_updates: dict[UUID, float] = {}

        tasks = [self._process_flagged_unit(session, unit, vault_id) for unit in flagged_units]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logger.error('Error processing flagged unit: %s', result)
                continue
            links, updates = result
            all_links.extend(links)
            confidence_updates.update(updates)

        for link in all_links:
            session.add(link)

        for unit_id, new_confidence in confidence_updates.items():
            stmt = (
                update(MemoryUnit).where(MemoryUnit.id == unit_id).values(confidence=new_confidence)
            )
            await session.execute(stmt)

        logger.info(
            'Contradiction detection: created %d links, updated %d confidences',
            len(all_links),
            len(confidence_updates),
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
        units_json = json.dumps([{'id': str(u.id), 'text': u.text} for u in units])

        result = await run_dspy_operation(
            lm=self.lm,
            predictor=self.triage_predictor,
            input_kwargs={'units': units_json},
        )

        flagged = result.flagged_ids
        if isinstance(flagged, str):
            try:
                flagged = json.loads(flagged)
            except (json.JSONDecodeError, TypeError):
                flagged = []
        return [str(fid) for fid in (flagged or [])]

    async def _process_flagged_unit(
        self,
        session: AsyncSession,
        unit: MemoryUnit,
        vault_id: UUID,
    ) -> tuple[list[MemoryLink], dict[UUID, float]]:
        """Process a single flagged unit: get candidates, classify, adjust."""
        candidates = await get_candidates(
            session,
            unit,
            vault_id,
            k=self.config.max_candidates_per_unit,
            threshold=self.config.similarity_threshold,
        )

        if not candidates:
            return [], {}

        relationships = await self._classify(unit, candidates)

        links: list[MemoryLink] = []
        confidence_updates: dict[UUID, float] = {}

        for rel in relationships:
            relation = rel['relation']
            authoritative_hint = rel.get('authoritative', 'new')
            reasoning = rel.get('reasoning', '')

            existing_unit = next((c for c in candidates if str(c.id) == rel['existing_id']), None)
            if existing_unit is None:
                continue

            authoritative, superseded = self._resolve_authority(
                unit, existing_unit, authoritative_hint
            )

            note_title = await self._get_note_title(session, authoritative.note_id)

            if relation == 'reinforce':
                for u in [unit, existing_unit]:
                    new_conf = min(u.confidence + self.config.alpha, 1.0)
                    confidence_updates[u.id] = new_conf
                link_type = 'reinforces'
            elif relation == 'weaken':
                new_conf = max(superseded.confidence - self.config.alpha, 0.0)
                confidence_updates[superseded.id] = new_conf
                link_type = 'weakens'
            elif relation == 'contradict':
                new_conf = max(superseded.confidence - 2 * self.config.alpha, 0.0)
                confidence_updates[superseded.id] = new_conf
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

        return links, confidence_updates

    async def _classify(
        self, unit: MemoryUnit, candidates: list[MemoryUnit]
    ) -> list[dict[str, Any]]:
        """Classify relationships between unit and candidates."""
        candidates_json = json.dumps(
            [
                {
                    'id': str(c.id),
                    'text': c.text,
                    'date': c.event_date.isoformat() if c.event_date else 'unknown',
                }
                for c in candidates
            ]
        )

        result = await run_dspy_operation(
            lm=self.lm,
            predictor=self.classify_predictor,
            input_kwargs={
                'new_unit_text': unit.text,
                'new_unit_date': (unit.event_date.isoformat() if unit.event_date else 'unknown'),
                'candidates': candidates_json,
            },
        )

        relationships = result.relationships
        if isinstance(relationships, str):
            try:
                relationships = json.loads(relationships)
            except (json.JSONDecodeError, TypeError):
                relationships = []

        valid_relations = {'reinforce', 'weaken', 'contradict'}
        return [r for r in (relationships or []) if r.get('relation') in valid_relations]

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
