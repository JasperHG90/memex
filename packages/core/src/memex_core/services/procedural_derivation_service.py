"""Derivation worker — drains the queue, distils, writes derived entries.

This is the consumer half of the §9 dirty-cluster loop (the LLM passes
live in ``memory/procedural_distillation.py``). For each claimed queue row:

* **procedure** — gather the case cluster behind the target anchor (its
  ``role='provenance'`` source notes), and — when the cluster has reached
  N≥3 (§9) — distil the procedure and write the steps/trigger/summary onto
  the existing draft anchor (a version bump; the anchor was created empty
  by assignment). Then, once ``(scope, verb)`` has ≥2 procedures, enqueue a
  strategy derivation (§9: a new case "feeds the shared strategy").
* **strategy** — gather the sibling procedures sharing ``(scope, verb)`` and
  distil the heuristic *above* them, upserting the strategy entry.

Derived entries stay ``status='draft'`` (§9: draft → confirm → active —
only confirmed entries are retrievable). The queue's claim / complete /
fail-with-retry methods (``procedural_repository``) own the transactional
state; this service is the work between claim and complete.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

import dspy
from sqlmodel import col, select

from memex_common.procedural_schemas import (
    ProceduralEntryCreate,
    ProceduralEntryUpdate,
)
from memex_core.memory.procedural_distillation import (
    MIN_CASES_FOR_DISTILLATION,
    distill_procedure,
    distill_strategy,
    render_cases_markdown,
    render_procedures_markdown,
)

if TYPE_CHECKING:
    from memex_common.procedural_schemas import ProceduralDerivationQueueClaim
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.services.procedural_derivation')

# §9: a strategy "emerges above multiple procedures" — don't synthesize a
# heuristic over a single procedure (that's just the procedure).
MIN_PROCEDURES_FOR_STRATEGY = 2

# rule_name on the §18.6.1 confirmation proposals. NOT llm_-prefixed —
# that namespace is reserved for the internal lint validator.
DISTILLATION_RULE_NAME = 'procedural_distillation'


class ProceduralDerivationError(RuntimeError):
    """A derivation task failed in a way the worker maps to retry/fail."""


class ProceduralDerivationService:
    """Claims and processes ``procedural_derivation_queue`` rows."""

    def __init__(self, api: 'MemexAPI') -> None:
        self._api = api

    # -- LM wiring (same source as the assignment judge) ----------------

    def _lm(self) -> dspy.LM:
        model_config = self._api.config.server.memory.extraction.model
        if model_config is None:
            raise ProceduralDerivationError('no LLM configured for the derivation pass')
        return dspy.LM(
            model=model_config.model,
            api_base=str(model_config.base_url) if model_config.base_url else None,
            api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
            timeout=model_config.timeout,
            num_retries=model_config.num_retries,
        )

    # -- Public entry points -------------------------------------------

    async def process_pending(self, *, limit: int = 1) -> list[UUID]:
        """Claim up to ``limit`` pending tasks and process each.

        Returns the queue ids that completed. A task that raises is routed
        to ``mark_derivation_failed`` (retry then fail) and excluded from
        the return — one poisoned row never blocks the others.
        """
        claims = await self._api._procedural_repo.claim_derivation_tasks(limit=limit)
        completed: list[UUID] = []
        for claim in claims:
            try:
                result_entry_id = await self._process_one(claim)
                await self._api._procedural_repo.mark_derivation_completed(
                    claim.queue_id, result_entry_id
                )
                completed.append(claim.queue_id)
            except Exception as exc:  # noqa: BLE001 — worker boundary
                logger.exception('derivation task %s failed', claim.queue_id)
                await self._api._procedural_repo.mark_derivation_failed(
                    claim.queue_id, last_error=str(exc)
                )
        return completed

    async def _process_one(self, claim: 'ProceduralDerivationQueueClaim') -> UUID:
        if claim.target_kind == 'strategy':
            return await self._derive_strategy(claim)
        return await self._derive_procedure(claim)

    # -- Procedure derivation ------------------------------------------

    async def _derive_procedure(self, claim: 'ProceduralDerivationQueueClaim') -> UUID:
        """Distil the case cluster behind the target procedure anchor and
        write it onto the existing draft entry. Returns the entry id."""
        if not claim.source_entry_ids:
            raise ProceduralDerivationError('procedure derivation task carries no target entry')
        entry_id = claim.source_entry_ids[0]

        cases = await self._gather_cases(entry_id)
        anchor = f'{claim.target_scope} / {claim.target_verb} / {claim.target_context}'

        # §9 amended (JG 2026-06-11): a single case is enough to derive a
        # procedure (MIN_CASES_FOR_DISTILLATION == 1). This guard only
        # trips when an anchor somehow has zero provenance cases — then the
        # draft stays a stub until a case is assigned and re-enqueues it.
        if len(cases) < MIN_CASES_FOR_DISTILLATION:
            logger.info(
                'procedure %s has %d case(s) (< %d) — leaving draft stub',
                entry_id,
                len(cases),
                MIN_CASES_FOR_DISTILLATION,
            )
            return entry_id

        distilled = await distill_procedure(
            self._lm(),
            cases_markdown=render_cases_markdown(cases),
            anchor=anchor,
        )

        # §18.6.4: NEVER auto-update a hand-edited (authored) entry — file a
        # proposal carrying the diff instead. ``origin='derived'`` entries
        # keep §9's silent version bump.
        current = await self._api.procedural.get(entry_id)
        if current.origin == 'authored':
            await self._file_apply_derivation_proposal(
                entry_id=entry_id,
                vault_id=claim.vault_id,
                distilled=distilled,
                source_case_ids=[cid for cid, _ in cases],
            )
            await self._maybe_enqueue_strategy(claim)
            return entry_id

        await self._api.procedural.update(
            entry_id,
            ProceduralEntryUpdate(
                title=distilled.title,
                summary=distilled.summary,
                body=distilled.body,
                trigger=distilled.trigger,
                edited_by='system:derivation',
                edit_reason=f'Distilled from {len(cases)} cases (§9).',
            ),
        )

        # §18.6.1: the distilled draft rides the lint surface for confirmation.
        await self._file_activation_proposal(
            entry_id=entry_id,
            vault_id=claim.vault_id,
            kind='procedure',
            title=distilled.title,
            summary=distilled.summary,
            source_case_ids=[cid for cid, _ in cases],
        )

        # §9 dirty event: a (re-)derived procedure feeds its parent strategy.
        await self._maybe_enqueue_strategy(claim)
        return entry_id

    async def _file_apply_derivation_proposal(
        self,
        *,
        entry_id: UUID,
        vault_id: UUID,
        distilled,
        source_case_ids: list[str],
    ) -> None:
        """File an apply_derivation proposal for an authored entry (§18.6.4):
        derivation never overwrites a hand-edit — it proposes the diff,
        resolved by applying a new version (origin stays authored) or
        dismissed. Pending-dedup → one open proposal per entry."""
        from memex_core.services.lint_external import (
            ExternalProposalRequest,
            insert_external_proposal,
        )

        try:
            req = ExternalProposalRequest(
                rule_name=DISTILLATION_RULE_NAME,
                lint_type='governance',
                target_type='procedural_entry',
                target_id=str(entry_id),
                description=f'Derivation proposes an update to authored entry: {distilled.title[:120]}',
                suggested_action=(
                    'Review the distilled diff and apply_derivation (new version, '
                    'origin stays authored) or dismiss.'
                ),
                vault_id=str(vault_id),
                evidence={'summary': distilled.summary, 'source_cases': source_case_ids},
                proposed_action={
                    'action_name': 'apply_derivation',
                    'params': {
                        'title': distilled.title,
                        'summary': distilled.summary,
                        'body': distilled.body,
                        'trigger': distilled.trigger,
                    },
                },
            )
            status, finding_id = await insert_external_proposal(
                self._api, req, vault_id=vault_id, actor='system:derivation'
            )
            logger.info('derivation filed apply_derivation proposal: %s (%s)', status, finding_id)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning('derivation: apply_derivation proposal failed for %s: %s', entry_id, exc)

    async def _file_activation_proposal(
        self,
        *,
        entry_id: UUID,
        vault_id: UUID,
        kind: str,
        title: str,
        summary: str,
        source_case_ids: list[str],
    ) -> None:
        """File a governance lint proposal to confirm a distilled draft
        (§18.6.1). The `activate_procedural_entry` action is pre-selected;
        the lint surface's pending-dedup means re-derivation won't re-nag.
        A filing failure is logged, not raised — the entry is already a
        valid draft, retrievable once confirmed by any other path."""
        from memex_core.services.lint_external import (
            ExternalProposalRequest,
            insert_external_proposal,
        )

        try:
            req = ExternalProposalRequest(
                rule_name=DISTILLATION_RULE_NAME,
                lint_type='governance',
                target_type='procedural_entry',
                target_id=str(entry_id),
                description=f'Distilled {kind} draft ready to activate: {title[:140]}',
                suggested_action=(
                    'Review the distilled draft and activate it (draft → published) '
                    'via activate_procedural_entry.'
                ),
                vault_id=str(vault_id),
                evidence={'summary': summary, 'source_cases': source_case_ids},
                proposed_action={'action_name': 'activate_procedural_entry', 'params': {}},
            )
            status, finding_id = await insert_external_proposal(
                self._api, req, vault_id=vault_id, actor='system:derivation'
            )
            logger.info('derivation filed activation proposal: %s (finding=%s)', status, finding_id)
        except Exception as exc:  # noqa: BLE001 — proposal filing is best-effort
            logger.warning(
                'derivation: activation proposal filing failed for %s: %s', entry_id, exc
            )

    async def _maybe_enqueue_strategy(self, claim: 'ProceduralDerivationQueueClaim') -> None:
        """Enqueue a strategy derivation for ``(scope, verb)`` once it has
        ≥2 procedures and no strategy task is already pending for it."""
        from memex_core.memory.sql_models import (
            DerivationQueueStatus,
            ProceduralDerivationQueue,
            ProceduralEntry,
        )

        scope, verb = claim.target_scope, claim.target_verb
        async with self._api.metastore.session() as session:
            proc_count = (
                await session.exec(
                    select(col(ProceduralEntry.id))
                    .where(col(ProceduralEntry.kind) == 'procedure')
                    .where(col(ProceduralEntry.scope) == scope)
                    .where(col(ProceduralEntry.verb) == verb)
                    .where(col(ProceduralEntry.status) != 'deprecated')
                )
            ).all()
            if len(proc_count) < MIN_PROCEDURES_FOR_STRATEGY:
                return
            # Dedup: skip if a pending/in-progress strategy task already exists.
            existing = (
                await session.exec(
                    select(col(ProceduralDerivationQueue.id))
                    .where(col(ProceduralDerivationQueue.target_kind) == 'strategy')
                    .where(col(ProceduralDerivationQueue.target_scope) == scope)
                    .where(col(ProceduralDerivationQueue.target_verb) == verb)
                    .where(
                        col(ProceduralDerivationQueue.status).in_(
                            [DerivationQueueStatus.PENDING, DerivationQueueStatus.IN_PROGRESS]
                        )
                    )
                )
            ).first()
            if existing is not None:
                return

        await self._api._procedural_repo.enqueue_derivation(
            vault_id=claim.vault_id,
            source_entry_ids=list(claim.source_entry_ids),
            target_kind='strategy',
            target_scope=scope,
            target_verb=verb,
            target_context=None,
        )

    # -- Strategy derivation -------------------------------------------

    async def _derive_strategy(self, claim: 'ProceduralDerivationQueueClaim') -> UUID:
        """Distil the heuristic above the sibling procedures sharing
        ``(scope, verb)`` and upsert the strategy entry. Returns its id."""
        procedures = await self._gather_sibling_procedures(claim.target_scope, claim.target_verb)
        if len(procedures) < MIN_PROCEDURES_FOR_STRATEGY:
            raise ProceduralDerivationError(
                f'strategy ({claim.target_scope}, {claim.target_verb}) has '
                f'{len(procedures)} procedure(s) (< {MIN_PROCEDURES_FOR_STRATEGY})'
            )

        anchor = f'{claim.target_scope} / {claim.target_verb}'
        distilled = await distill_strategy(
            self._lm(),
            procedures_markdown=render_procedures_markdown(procedures),
            anchor=anchor,
        )

        dto = await self._api.procedural.upsert(
            ProceduralEntryCreate(
                vault_id=claim.vault_id,
                kind='strategy',
                scope=claim.target_scope,
                verb=claim.target_verb,
                context=None,
                title=distilled.title,
                summary=distilled.summary,
                body=distilled.body,
                trigger=distilled.trigger,
                status='draft',
                origin='derived',
            )
        )

        # §18.6.1: the distilled strategy draft also rides the lint surface.
        await self._file_activation_proposal(
            entry_id=dto.id,
            vault_id=claim.vault_id,
            kind='strategy',
            title=distilled.title,
            summary=distilled.summary,
            source_case_ids=[p.get('title', '') for p in procedures],
        )
        return dto.id

    # -- Cluster gathering ---------------------------------------------

    async def _gather_cases(self, entry_id: UUID) -> list[tuple[str, str]]:
        """Return ``[(case_id, case_text), …]`` for an entry's provenance
        cases (``role='provenance'`` source notes)."""
        from memex_core.memory.sql_models import Note

        sources = await self._api._procedural_repo.list_sources_for_entry(
            entry_id, role='provenance'
        )
        note_ids = [s.source_note_id for s in sources if s.source_note_id is not None]
        cases: list[tuple[str, str]] = []
        if not note_ids:
            return cases
        async with self._api.metastore.session() as session:
            for note_id in note_ids:
                note = await session.get(Note, note_id)
                if note is not None and note.original_text:
                    cases.append((str(note_id), note.original_text))
        return cases

    async def _gather_sibling_procedures(
        self, scope: str, verb: str | None
    ) -> list[dict[str, str]]:
        """Return the non-deprecated procedures sharing ``(scope, verb)``."""
        from memex_core.memory.sql_models import ProceduralEntry

        async with self._api.metastore.session() as session:
            rows = (
                await session.exec(
                    select(ProceduralEntry)
                    .where(col(ProceduralEntry.kind) == 'procedure')
                    .where(col(ProceduralEntry.scope) == scope)
                    .where(col(ProceduralEntry.verb) == verb)
                    .where(col(ProceduralEntry.status) != 'deprecated')
                )
            ).all()
        return [
            {
                'title': r.title or '',
                'trigger': r.trigger or '',
                'summary': r.summary or '',
            }
            for r in rows
        ]


__all__ = ['ProceduralDerivationService', 'ProceduralDerivationError']
