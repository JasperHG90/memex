"""InboxRouterService — periodic triage of the ``inbox`` vault.

Orchestrates: refresh per-vault anchors → cache per-note features → score all
inbox notes (pairwise GaussianNB in SQL) → decide → auto-route the confident
ones (capped) and emit cockpit proposals for the rest. Learns online from
cockpit/auto decisions via SQL conjugate updates.

The model is "fit" entirely in Postgres; see ``sql.py``. This service is the
thin Python layer that drives those statements and applies the decision policy
in ``decisions.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from memex_core.services.base import BaseService
from memex_core.services.inbox_router import sql as _sql
from memex_core.services.inbox_router.decisions import (
    CandidateScore,
    DecisionKind,
    DecisionThresholds,
    RouterDecision,
    decide,
)
from memex_core.services.inbox_router.evidence import (
    CandidateEvidence,
    NoFitEvidence,
    RouteEvidence,
)

if TYPE_CHECKING:
    from memex_core.config import MemexConfig
    from memex_core.memory.models.embedding import EmbeddingsModel
    from memex_core.services.notes import NoteService
    from memex_core.services.vaults import VaultService
    from memex_core.storage.filestore import BaseAsyncFileStore
    from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger(__name__)

INBOX_VAULT_NAME = 'inbox'
ROUTE_RULE = 'inbox_vault_route'
NO_FIT_RULE = 'inbox_vault_no_fit'
ROUTER_ACTOR = 'system:inbox-router'
TOP_CANDIDATES = 3  # surfaced in route-proposal evidence
MAX_NOTES_PER_TICK = 500  # bound the scoring batch


@dataclass
class TriageResult:
    vault_id: UUID | None = None
    scored: int = 0
    auto_routed: int = 0
    proposed: int = 0
    no_fit: int = 0
    skipped_cap: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            'vault_id': str(self.vault_id) if self.vault_id else None,
            'scored': self.scored,
            'auto_routed': self.auto_routed,
            'proposed': self.proposed,
            'no_fit': self.no_fit,
            'skipped_cap': self.skipped_cap,
            'errors': self.errors,
        }


# Emit a pending proposal, idempotent on the pending tuple AND respecting the
# 30-day post-resolution/dismissal cooldown (mirrors LintService's guard) so a
# dismissed route isn't immediately re-proposed.
_EMIT_PENDING_SQL = """
INSERT INTO maintenance_proposals
    (vault_id, lint_type, target_type, target_id, rule_name, evidence,
     suggested_action, status, source)
SELECT :vault_id ::uuid, 'routing', 'note', :target_id, :rule_name,
       CAST(:evidence AS jsonb), :suggested_action, 'pending', 'rule'
WHERE NOT EXISTS (
    SELECT 1 FROM maintenance_proposals mp
     WHERE mp.rule_name = :rule_name
       AND mp.target_type = 'note'
       AND mp.target_id = :target_id
       AND mp.vault_id = :vault_id ::uuid
       AND mp.status IN ('resolved', 'dismissed')
       AND mp.resolved_at > now() - interval '30 days'
)
ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
DO NOTHING
"""

# No-fit backoff: advance the retry counter / next_retry_at on the existing
# pending row when it is due, otherwise insert a fresh one.
_UPSERT_NO_FIT_SQL = """
INSERT INTO maintenance_proposals
    (vault_id, lint_type, target_type, target_id, rule_name, evidence,
     suggested_action, status, source)
VALUES (:vault_id ::uuid, 'routing', 'note', :target_id, :rule_name,
        CAST(:evidence AS jsonb), :suggested_action, 'pending', 'rule')
ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
DO UPDATE SET evidence = CAST(:evidence AS jsonb)
WHERE (maintenance_proposals.evidence->>'next_retry_at') IS NULL
   OR (maintenance_proposals.evidence->>'next_retry_at')::timestamptz <= now()
"""

# Record an auto-applied route as a resolved proposal (audit trail).
_INSERT_RESOLVED_SQL = """
INSERT INTO maintenance_proposals
    (vault_id, lint_type, target_type, target_id, rule_name, evidence,
     suggested_action, status, source, resolved_at, resolved_by)
VALUES (:vault_id ::uuid, 'routing', 'note', :target_id, :rule_name,
        CAST(:evidence AS jsonb), :suggested_action, 'resolved', 'rule',
        now(), :actor)
"""

# Inbox notes due for triage: in the inbox vault, with chunks, and not currently
# parked behind a not-yet-due no-fit proposal.
_SELECT_INBOX_NOTES_SQL = """
SELECT n.id
  FROM notes n
 WHERE n.vault_id = :inbox_id ::uuid
   AND EXISTS (SELECT 1 FROM chunks c WHERE c.note_id = n.id)
   AND NOT EXISTS (
        SELECT 1 FROM maintenance_proposals mp
         WHERE mp.rule_name = :no_fit_rule
           AND mp.target_type = 'note'
           AND mp.target_id = n.id::text
           AND mp.status = 'pending'
           AND (mp.evidence->>'next_retry_at')::timestamptz > now()
   )
 LIMIT :limit
"""


class InboxRouterService(BaseService):
    """Drives inbox-note scoring, auto-routing, and proposal emission."""

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
        *,
        embedding_model: EmbeddingsModel,
        notes: NoteService,
        vaults: VaultService,
    ) -> None:
        super().__init__(metastore, filestore, config)
        self.embedding_model = embedding_model
        self._notes = notes
        self._vaults = vaults

    @property
    def _cfg(self):  # noqa: ANN202 - InboxRouterConfig
        return self.config.server.memory.inbox_router

    # ------------------------------------------------------------------ bootstrap
    async def ensure_inbox_vault(self) -> UUID | None:
        """Create the inbox vault if missing; return its id (None on failure)."""
        from sqlalchemy.exc import IntegrityError

        try:
            await self._vaults.create_vault(
                INBOX_VAULT_NAME,
                description='Holding vault for notes awaiting routing by the inbox router.',
            )
        except (ValueError, IntegrityError):
            pass  # already exists (non-locking SELECT race or prior creation)
        except Exception:
            logger.exception('inbox_router: failed to bootstrap inbox vault')
        return await self._inbox_vault_id()

    async def _inbox_vault_id(self) -> UUID | None:
        async with self.metastore.session() as session:
            row = (
                await session.execute(
                    text(
                        'SELECT id FROM vaults WHERE name = :name AND archived_at IS NULL '
                        'ORDER BY created_at LIMIT 1'
                    ),
                    {'name': INBOX_VAULT_NAME},
                )
            ).first()
        return row[0] if row else None

    # ------------------------------------------------------------------ anchors
    async def refresh_anchors(self) -> int:
        """Refresh per-vault anchors for every candidate (non-inbox) vault.

        Embeds each vault's narrative in Python (for ``summary_embedding``); the
        remaining anchors are computed in SQL. Returns the number refreshed.
        """
        excluded = set(self._excluded_vault_names())
        async with self.metastore.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT v.id, v.name, COALESCE(vs.narrative, v.description, '') "
                        'FROM vaults v '
                        'LEFT JOIN vault_summaries vs ON vs.vault_id = v.id '
                        'WHERE v.archived_at IS NULL'
                    )
                )
            ).all()
        targets = [
            (vid, name, narrative) for (vid, name, narrative) in rows if name not in excluded
        ]
        if not targets:
            return 0

        narratives = [narrative or '(empty)' for (_, _, narrative) in targets]
        vecs = await asyncio.to_thread(self.embedding_model.encode, narratives)

        refreshed = 0
        async with self.metastore.session() as session:
            for (vid, _name, _narr), vec in zip(targets, vecs, strict=True):
                summary_vec = '[' + ','.join(f'{float(x):.8f}' for x in vec) + ']'
                await session.execute(
                    text(_sql.REFRESH_VAULT_ANCHOR_SQL),
                    {
                        'vault_id': str(vid),
                        'summary_embedding': summary_vec,
                        'top_k': self._cfg.top_k_entities,
                    },
                )
                refreshed += 1
            await session.commit()
        return refreshed

    async def populate_note_cache(self, note_id: UUID) -> None:
        """(Re)compute the cached features for one note. Idempotent upsert."""
        async with self.metastore.session() as session:
            await session.execute(text(_sql.POPULATE_NOTE_CACHE_SQL), {'note_id': str(note_id)})
            await session.commit()

    # ------------------------------------------------------------------ scoring
    async def score_notes(self, note_ids: list[UUID]) -> dict[UUID, list[CandidateScore]]:
        if not note_ids:
            return {}
        excluded = self._excluded_vault_names()
        async with self.metastore.session() as session:
            rows = (
                await session.execute(
                    text(_sql.SCORE_NOTES_SQL),
                    {'note_ids': [str(n) for n in note_ids], 'excluded': excluded},
                )
            ).all()
        out: dict[UUID, list[CandidateScore]] = {}
        for r in rows:
            note_id = r[0] if isinstance(r[0], UUID) else UUID(str(r[0]))
            out.setdefault(note_id, []).append(
                CandidateScore(
                    vault_id=r[1] if isinstance(r[1], UUID) else UUID(str(r[1])),
                    vault_name=r[2],
                    p_match=float(r[3]) if r[3] is not None else 0.0,
                    p_match_raw=float(r[4]) if r[4] is not None else 0.0,
                    ci_half_width=float(r[6]) if r[6] is not None else 0.0,
                )
            )
        return out

    # ------------------------------------------------------------------ feedback
    async def record_feedback(self, note_id: UUID, vault_id: UUID, label: int) -> None:
        """Apply one online conjugate update for a (note, vault, match?) pair."""
        async with self.metastore.session() as session:
            await session.execute(
                text(_sql.ONLINE_UPDATE_SQL),
                {
                    'note_id': str(note_id),
                    'vault_id': str(vault_id),
                    'label': int(label),
                    'gamma': float(self._cfg.ewma_gamma),
                },
            )
            await session.commit()

    # ------------------------------------------------------------------ tick
    async def triage_tick(self, *, dry_run: bool = False) -> TriageResult:
        """Run one full triage pass over the inbox vault."""
        result = TriageResult()
        inbox_id = await self._inbox_vault_id()
        if inbox_id is None:
            logger.info('inbox_router: no inbox vault; skipping tick')
            return result
        result.vault_id = inbox_id

        await self.refresh_anchors()

        async with self.metastore.session() as session:
            note_rows = (
                await session.execute(
                    text(_SELECT_INBOX_NOTES_SQL),
                    {
                        'inbox_id': str(inbox_id),
                        'no_fit_rule': NO_FIT_RULE,
                        'limit': MAX_NOTES_PER_TICK,
                    },
                )
            ).all()
        note_ids = [r[0] if isinstance(r[0], UUID) else UUID(str(r[0])) for r in note_rows]
        if not note_ids:
            return result

        for nid in note_ids:
            await self.populate_note_cache(nid)

        scored = await self.score_notes(note_ids)
        result.scored = len(scored)

        warmed_up = await self._is_warmed_up()
        thresholds = DecisionThresholds(
            auto_apply_enabled=self._cfg.auto_apply_enabled,
            auto_apply_min_p_match=self._cfg.auto_apply_min_p_match,
            t_margin=self._cfg.t_margin,
            t_low=self._cfg.t_low,
        )

        for nid in note_ids:
            decision = decide(nid, scored.get(nid, []), thresholds=thresholds, warmed_up=warmed_up)
            try:
                if decision.kind is DecisionKind.AUTO_ROUTE:
                    if dry_run:
                        result.auto_routed += 1
                    elif result.auto_routed >= self._cfg.max_auto_applies_per_tick:
                        # Over the per-tick budget — fall through to a proposal.
                        await self._emit_route(session_vault=inbox_id, decision=decision)
                        result.skipped_cap += 1
                        result.proposed += 1
                    else:
                        await self._auto_apply(inbox_id, decision)
                        result.auto_routed += 1
                elif decision.kind is DecisionKind.PROPOSE_CANDIDATES:
                    if not dry_run:
                        await self._emit_route(session_vault=inbox_id, decision=decision)
                    result.proposed += 1
                else:  # PROPOSE_NO_FIT
                    if not dry_run:
                        await self._emit_no_fit(inbox_id, decision)
                    result.no_fit += 1
            except Exception:
                logger.exception('inbox_router: failed to act on note %s', nid)
                result.errors += 1

        return result

    # ------------------------------------------------------------------ helpers
    async def _is_warmed_up(self) -> bool:
        async with self.metastore.session() as session:
            n = (
                await session.execute(
                    text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1')
                )
            ).scalar()
        return float(n or 0.0) >= self._cfg.min_decisions_before_auto_apply

    def _excluded_vault_names(self) -> list[str]:
        # The inbox is the source; 'global' is a catch-all that dilutes routing.
        return [INBOX_VAULT_NAME, 'global']

    async def _auto_apply(self, inbox_id: UUID, decision: RouterDecision) -> None:
        top = decision.top
        assert top is not None
        await self._notes.migrate_note(decision.note_id, top.vault_id)
        evidence = self._route_evidence(inbox_id, decision).model_dump()
        evidence['resolution'] = {
            'verb': 'auto_route',
            'target_vault_id': str(top.vault_id),
            'p_match': top.p_match,
            'margin': decision.margin,
            'applied_at': datetime.now(timezone.utc).isoformat(),
        }
        async with self.metastore.session() as session:
            await session.execute(
                text(_INSERT_RESOLVED_SQL),
                {
                    'vault_id': str(inbox_id),
                    'target_id': str(decision.note_id),
                    'rule_name': ROUTE_RULE,
                    'evidence': json.dumps(evidence),
                    'suggested_action': f'Auto-routed to {top.vault_name}',
                    'actor': ROUTER_ACTOR,
                },
            )
            await session.commit()
        # Learn: the chosen vault is a positive, the other candidates negatives.
        await self.record_feedback(decision.note_id, top.vault_id, 1)
        for cand in decision.candidates[1:TOP_CANDIDATES]:
            await self.record_feedback(decision.note_id, cand.vault_id, 0)

    async def _emit_route(self, *, session_vault: UUID, decision: RouterDecision) -> None:
        top = decision.top
        assert top is not None
        evidence = self._route_evidence(session_vault, decision)
        async with self.metastore.session() as session:
            await session.execute(
                text(_EMIT_PENDING_SQL),
                {
                    'vault_id': str(session_vault),
                    'target_id': str(decision.note_id),
                    'rule_name': ROUTE_RULE,
                    'evidence': evidence.model_dump_json(),
                    'suggested_action': f'Route to {top.vault_name}?',
                },
            )
            await session.commit()

    async def _emit_no_fit(self, inbox_id: UUID, decision: RouterDecision) -> None:
        best = decision.candidates[0].p_match_raw if decision.candidates else 0.0
        now = datetime.now(timezone.utc)
        retry_n, next_retry = await self._next_backoff(inbox_id, decision.note_id, now)
        evidence = NoFitEvidence(
            routing_state=decision.routing_state.value,
            best_p_match_raw=best,
            retry_n=retry_n,
            next_retry_at=next_retry.isoformat(),
            last_evaluated_at=now.isoformat(),
        )
        async with self.metastore.session() as session:
            await session.execute(
                text(_UPSERT_NO_FIT_SQL),
                {
                    'vault_id': str(inbox_id),
                    'target_id': str(decision.note_id),
                    'rule_name': NO_FIT_RULE,
                    'evidence': evidence.model_dump_json(),
                    'suggested_action': 'No vault fits this note; leave in inbox or migrate manually.',
                },
            )
            await session.commit()

    async def _next_backoff(
        self, inbox_id: UUID, note_id: UUID, now: datetime
    ) -> tuple[int, datetime]:
        """Compute the next (retry_n, next_retry_at) from any existing no-fit row."""
        async with self.metastore.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT (evidence->>'retry_n')::int FROM maintenance_proposals "
                        "WHERE rule_name = :rule AND target_type = 'note' "
                        'AND target_id = :tid AND vault_id = :vid ::uuid '
                        "AND status = 'pending'"
                    ),
                    {'rule': NO_FIT_RULE, 'tid': str(note_id), 'vid': str(inbox_id)},
                )
            ).first()
        prev = int(row[0]) if row and row[0] is not None else -1
        retry_n = prev + 1
        delay_days = min(self._cfg.backoff_base_days * (2**retry_n), self._cfg.backoff_cap_days)
        return retry_n, now + timedelta(days=delay_days)

    def _route_evidence(self, inbox_id: UUID, decision: RouterDecision) -> RouteEvidence:
        return RouteEvidence(
            routing_state=decision.routing_state.value,
            margin=decision.margin,
            source_vault_id=str(inbox_id),
            top_candidates=[
                CandidateEvidence(
                    vault_id=str(c.vault_id),
                    vault_name=c.vault_name,
                    p_match=c.p_match,
                    p_match_raw=c.p_match_raw,
                    ci_half_width=c.ci_half_width,
                )
                for c in decision.candidates[:TOP_CANDIDATES]
            ],
        )
