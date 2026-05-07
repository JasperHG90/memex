"""Contradiction detection engine — Hindsight Retain-Time."""

import asyncio
import logging
import re
from typing import Any
from uuid import UUID

import dspy
from sqlalchemy import func as sa_func, text as sa_text, update
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
from memex_core.memory.sql_models import (
    LintSource,
    LintStatus,
    LintType,
    MaintenanceProposal,
    MemoryLink,
    MemoryUnit,
    Note,
)

logger = logging.getLogger('memex.core.memory.contradiction')


_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]')


def _sanitise_evidence_text(value: Any, *, max_len: int) -> str | None:
    """Defensive sanitisation for free-text payloads stored in lint
    ``evidence`` JSONB. Strips ASCII C0 controls (``0x00–0x1F`` except
    ``\\n``/``\\t``), DEL (``0x7F``) and the C1 control range
    (``0x80–0x9F``); truncates to ``max_len`` characters; returns None
    for empty/None input.

    Pre-truncates the input to ``max_len * 4`` chars before the regex pass
    so megabyte-scale pathological payloads (e.g., a runaway LLM that
    emits 1 MB of NULs) cannot turn this defensive helper into an O(N)
    bottleneck on the contradiction emit path. ``max_len * 4`` leaves
    enough headroom for inputs that are 75 % control chars and still
    yield ``max_len`` survivors after stripping.

    Downstream renderers must still escape per their target format
    (HTML, Markdown, terminal); this only protects the storage layer
    from payload bombs and terminal-hostile content.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if len(value) > max_len * 4:
        value = value[: max_len * 4]
    cleaned = _CONTROL_CHAR_RE.sub('', value).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + '…'
    return cleaned


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
            # Accumulate signed alpha-step deltas per target; apply via
            # SQL-level ``clamp(confidence + :delta, 0, 1)`` so concurrent
            # batches on overlapping units stay in sync with the atomic
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

                await self._emit_contradiction_findings(session, all_links, vault_id)

            # Clamp per-unit deltas via SQL LEAST/GREATEST to prevent races on
            # overlapping units (mirrors application-level max(0, min(1, ...))).
            for unit_id, delta in confidence_deltas.items():
                values: dict[str, Any] = {
                    'confidence': sa_func.greatest(
                        0.0,
                        sa_func.least(1.0, MemoryUnit.confidence + delta),
                    ),
                }
                bump = evidence_bumps.get(unit_id, 0)
                if bump:
                    # GREATEST(0, ...) guards against future negative deltas
                    # (always +1 today, so currently a no-op).
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

    async def _emit_contradiction_findings(
        self,
        session: AsyncSession,
        links: list[MemoryLink],
        vault_id: UUID,
    ) -> None:
        """Surface ``contradicts`` links as ``maintenance_proposals`` rows so
        ``memex lint findings`` shows them immediately, without waiting on the
        periodic LLM-lint tick.

        Idempotency: the partial unique index on
        ``(rule_name, target_type, target_id, vault_id) WHERE status='pending'``
        prevents a second pending row for the same superseded unit while a
        prior finding is still pending. Once that finding is resolved or
        dismissed it leaves the index, so a subsequent contradiction against
        the same unit will create a new pending row — by design: a flagged
        unit that gets contradicted again after triage deserves a new alert.

        ``evidence`` carries free-text ``reasoning`` and ``superseding_note_title``
        sourced from upstream (LLM output and note titles). The values are
        sanitised at write time (control-chars stripped, length-capped) so
        the JSONB column cannot store arbitrarily large or terminal-hostile
        payloads. Downstream renderers MUST still escape per their target
        format (HTML, Markdown, etc.); do not assume the values are safe
        for direct rendering.

        Within a single call, multiple contradicts links may share the same
        superseded ``target_id`` (e.g. two new units contradicting the same
        old fact in one ingestion batch). Postgres rejects duplicate target
        rows in a single ``ON CONFLICT DO NOTHING`` statement with a
        ``cardinality_violation`` because the index arbiter cannot resolve
        them in one pass. We dedupe by ``target_id`` first; ``links`` is
        sorted by ``(target_id, from_unit_id)`` so the kept finding is
        deterministic across retries even when upstream LLM ordering shifts.
        """
        # Stable sort so the kept link per target is deterministic across
        # retries — without this, the LLM's flagged_ids ordering decides
        # which authoritative_unit_id ends up in evidence.
        sorted_links = sorted(
            (lk for lk in links if lk.link_type == 'contradicts'),
            key=lambda lk: (str(lk.to_unit_id), str(lk.from_unit_id)),
        )
        seen_targets: set[str] = set()
        rows: list[dict[str, Any]] = []
        for link in sorted_links:
            target_id = str(link.to_unit_id)
            if target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            metadata = link.link_metadata or {}
            evidence = {
                'authoritative_unit_id': metadata.get('authoritative_unit_id'),
                'superseded_unit_id': target_id,
                'reasoning': _sanitise_evidence_text(metadata.get('reasoning'), max_len=1000),
                'superseding_note_title': _sanitise_evidence_text(
                    metadata.get('superseding_note_title'), max_len=200
                ),
            }
            rows.append(
                {
                    'vault_id': vault_id,
                    'lint_type': LintType.QUALITY.value,
                    'target_type': 'memory_unit',
                    'target_id': target_id,
                    'rule_name': 'semantic_contradiction',
                    'evidence': evidence,
                    'suggested_action': (
                        'Review the contradiction and decide whether the '
                        'superseded unit should be revised or removed.'
                    ),
                    'status': LintStatus.PENDING.value,
                    'source': LintSource.LLM.value,
                }
            )
        if not rows:
            return
        insert_stmt = pg_insert(MaintenanceProposal).values(rows)
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=['rule_name', 'target_type', 'target_id', 'vault_id'],
            index_where=sa_text("status = 'pending'"),
        )
        await session.exec(insert_stmt)  # type: ignore[arg-type]

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

        Returns (links, confidence_deltas, evidence_bumps). Deltas are signed
        alpha-steps so the caller can sum per-unit without overwrite races.
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
        # Per-target confidence deltas and evidence bumps (+1 per
        # weaken/contradict link). ``evidence_bumps`` counts only
        # negative-evidence links; reinforces are excluded for
        # backfill/forward symmetry. Weaken applies a -alpha step;
        # contradict applies a one-shot supersession penalty
        # (1 - superseded_threshold + alpha) so the unit lands strictly
        # below the retrieval-side superseded threshold without
        # depending on repeated hits.
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
                # Symmetric reinforce: both endpoints gain +alpha (no evidence
                # bump — forward/backfill symmetry).
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
                # Push confidence below superseded_threshold so contradicted
                # units are immediately recognized as superseded by retrieval.
                penalty = 1.0 - self.config.superseded_threshold + self.config.alpha
                confidence_deltas[superseded.id] = (
                    confidence_deltas.get(superseded.id, 0.0) - penalty
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
