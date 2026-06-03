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
from dataclasses import dataclass, field
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
    from memex_common.config import InboxRouterConfig
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
# Session-level advisory lock key serialising live triage ticks (distinct from
# the scheduler leader lock).
_ROUTER_LOCK_ID = 5432789123456790


@dataclass
class TriageResult:
    """Per-tick triage counters.

    Every scored note increments at least one terminal-disposition bucket
    (``auto_routed`` / ``proposed`` / ``no_fit`` / ``blocked_cooldown`` /
    ``blocked_backoff`` / ``errors``). ``skipped_cap`` is NOT a terminal
    bucket but an orthogonal reason-tag: an over-budget AUTO_ROUTE increments
    ``skipped_cap`` AND its terminal bucket (``proposed`` or
    ``blocked_cooldown``). So the buckets are intentionally non-additive — do
    not assume they sum to ``scored``.
    """

    vault_id: UUID | None = None
    scored: int = 0
    auto_routed: int = 0
    proposed: int = 0
    no_fit: int = 0
    skipped_cap: int = 0
    # A route proposal the re-proposal cooldown suppressed (the note was routed/
    # dismissed recently). Previously these notes were silently uncounted, so a
    # tick that "scored 22" could show "proposed 0" with no explanation.
    blocked_cooldown: int = 0
    # A no-fit proposal whose backoff window is not yet due — also previously
    # silent.
    blocked_backoff: int = 0
    errors: int = 0
    # Populated only on a dry run: one entry per scored note with the would-be
    # decision + top candidate. Lets callers (e.g. the eval suite) read per-note
    # predictions without mutating anything.
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'vault_id': str(self.vault_id) if self.vault_id else None,
            'scored': self.scored,
            'auto_routed': self.auto_routed,
            'proposed': self.proposed,
            'no_fit': self.no_fit,
            'skipped_cap': self.skipped_cap,
            'blocked_cooldown': self.blocked_cooldown,
            'blocked_backoff': self.blocked_backoff,
            'errors': self.errors,
            'decisions': self.decisions,
        }


# Emit a pending proposal, idempotent on the pending tuple AND respecting the
# 30-day post-resolution/dismissal cooldown (mirrors LintService's guard) so a
# dismissed route isn't immediately re-proposed.
# The ON CONFLICT arbiter is the existing partial unique index
# ``uq_maintenance_proposals_pending`` on
# (rule_name, target_type, target_id, vault_id) WHERE status='pending'
# (MaintenanceProposal.__table_args__ in sql_models.py) — shared with the lint
# system, exercised by the integration tests.
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
       AND mp.resolved_at > now() - make_interval(days => :cooldown_days)
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

# Inbox notes due for triage: in the inbox vault, with chunks, NOT already
# awaiting a cockpit decision on a pending route proposal, and not currently
# parked behind a not-yet-due no-fit backoff window. Skipping notes that already
# have a pending route proposal avoids re-scoring (and re-proposing with stale
# evidence) a note the user simply hasn't actioned yet.
_SELECT_INBOX_NOTES_SQL = """
SELECT n.id
  FROM notes n
 WHERE n.vault_id = :inbox_id ::uuid
   -- Require at least one EMBEDDED chunk: a note still awaiting embeddings has
   -- no centroid to score, and must be deferred rather than scored as no-fit.
   AND EXISTS (SELECT 1 FROM chunks c WHERE c.note_id = n.id AND c.embedding IS NOT NULL)
   AND NOT EXISTS (
        SELECT 1 FROM maintenance_proposals mp
         WHERE mp.rule_name = :route_rule
           AND mp.target_type = 'note'
           AND mp.target_id = n.id::text
           AND mp.status = 'pending'
   )
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
    def _cfg(self) -> InboxRouterConfig:
        return self.config.server.memory.inbox_router

    # ------------------------------------------------------------------ bootstrap
    async def ensure_inbox_vault(self) -> UUID | None:
        """Create the inbox vault if missing; return its id (None on failure).

        Query-first: only attempt creation when the vault is genuinely absent,
        so the ValueError/IntegrityError catch is scoped to the create-time race
        window (another worker created it between our check and ours) rather than
        masking validation failures.
        """
        from sqlalchemy.exc import IntegrityError

        existing = await self._inbox_vault_id()
        if existing is not None:
            return existing
        try:
            await self._vaults.create_vault(
                INBOX_VAULT_NAME,
                description='Holding vault for notes awaiting routing by the inbox router.',
            )
        except (ValueError, IntegrityError):
            pass  # created concurrently between the check above and here
        except Exception:
            logger.exception('inbox_router: failed to bootstrap inbox vault')
        return await self._inbox_vault_id()

    async def _inbox_vault_id(self) -> UUID | None:
        async with self.metastore.session() as session:
            row = (
                await session.execute(
                    # NB: the vaults table has no soft-delete column — only
                    # mental_models/notes carry archived_at. Do NOT filter on
                    # vaults.archived_at (it does not exist).
                    text('SELECT id FROM vaults WHERE name = :name ORDER BY created_at LIMIT 1'),
                    {'name': INBOX_VAULT_NAME},
                )
            ).first()
        return row[0] if row else None

    async def ensure_prior_seeded(self) -> None:
        """Seed the NB sufficient-statistics prior if absent (idempotent).

        Migration 055 seeds DBs upgraded via Alembic; this covers
        create_all-provisioned DBs (fresh servers, the eval harness, tests)
        where the migration body never runs. ``ON CONFLICT DO NOTHING`` makes
        it a no-op once seeded.
        """
        async with self.metastore.session() as session:
            await session.execute(text(_sql.SEED_NB_STATS_SQL))
            await session.execute(text(_sql.SEED_NB_CLASS_COUNTS_SQL))
            await session.commit()

    # ------------------------------------------------------------------ anchors
    async def refresh_anchors(self) -> int:
        """Refresh per-vault anchors for every candidate (non-inbox) vault.

        Embeds each vault's narrative in Python (for ``summary_embedding``); the
        remaining anchors are computed in SQL. Returns the number refreshed.
        """
        await self.ensure_prior_seeded()
        excluded = set(self._excluded_vault_names())
        async with self.metastore.session() as session:
            rows = (
                await session.execute(
                    # vaults has no archived_at column (no soft-delete); select all.
                    text(
                        "SELECT v.id, v.name, COALESCE(vs.narrative, v.description, '') "
                        'FROM vaults v '
                        'LEFT JOIN vault_summaries vs ON vs.vault_id = v.id'
                    )
                )
            ).all()
        targets = [
            (vid, name, narrative) for (vid, name, narrative) in rows if name not in excluded
        ]
        if not targets:
            return 0

        narratives = [narrative or '(empty)' for (_, _, narrative) in targets]
        # Shared embedding cap across api.py + document_search.py + retrieval/engine.py
        # — one model, one capacity budget. Thread keeps running on timeout.
        from memex_core.memory.retrieval._offload import (
            get_embedding_call_timeout,
            get_embedding_semaphore,
        )

        async with get_embedding_semaphore():
            vecs = await asyncio.wait_for(
                asyncio.to_thread(self.embedding_model.encode, narratives),
                timeout=get_embedding_call_timeout(),
            )

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
        await self.populate_note_caches([note_id])

    async def populate_note_caches(self, note_ids: list[UUID]) -> None:
        """(Re)compute cached features for many notes in one statement."""
        if not note_ids:
            return
        async with self.metastore.session() as session:
            await session.execute(
                text(_sql.POPULATE_NOTE_CACHE_SQL),
                {'note_ids': [str(n) for n in note_ids]},
            )
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
        for row in rows:
            m = row._mapping  # access by column name, not position
            nid = m['note_id']
            note_id = nid if isinstance(nid, UUID) else UUID(str(nid))
            vid = m['vault_id']
            out.setdefault(note_id, []).append(
                CandidateScore(
                    vault_id=vid if isinstance(vid, UUID) else UUID(str(vid)),
                    vault_name=m['vault_name'],
                    p_match=float(m['p_match']) if m['p_match'] is not None else 0.0,
                    p_match_raw=float(m['p_match_raw']) if m['p_match_raw'] is not None else 0.0,
                    ci_half_width=float(m['ci_half_width'])
                    if m['ci_half_width'] is not None
                    else 0.0,
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
        """Run one full triage pass over the inbox vault.

        Live ticks take a session-level advisory lock so a manual
        ``memex inbox triage`` racing the scheduler can't run concurrently and
        double the daily auto-apply budget. Dry runs don't mutate, so they skip
        the lock. If the lock is held, this tick is a no-op.
        """
        if dry_run:
            return await self._run_tick(dry_run=True)

        async with self.metastore.session() as lock_session:
            got = (
                await lock_session.execute(
                    text('SELECT pg_try_advisory_lock(:k)'), {'k': _ROUTER_LOCK_ID}
                )
            ).scalar()
            if not got:
                logger.info('inbox_router: another triage holds the lock; skipping tick')
                return TriageResult()
            try:
                return await self._run_tick(dry_run=False)
            finally:
                await lock_session.execute(
                    text('SELECT pg_advisory_unlock(:k)'), {'k': _ROUTER_LOCK_ID}
                )
                await lock_session.commit()

    async def _run_tick(self, *, dry_run: bool) -> TriageResult:
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
                        'route_rule': ROUTE_RULE,
                        'no_fit_rule': NO_FIT_RULE,
                        'limit': MAX_NOTES_PER_TICK,
                    },
                )
            ).all()
        note_ids = [r[0] if isinstance(r[0], UUID) else UUID(str(r[0])) for r in note_rows]
        if not note_ids:
            return result

        await self.populate_note_caches(note_ids)

        scored = await self.score_notes(note_ids)
        result.scored = len(scored)

        warmed_up = await self._is_warmed_up()
        thresholds = DecisionThresholds(
            auto_apply_enabled=self._cfg.auto_apply_enabled,
            auto_apply_min_p_match=self._cfg.auto_apply_min_p_match,
            t_margin=self._cfg.t_margin,
            t_low=self._cfg.t_low,
        )

        # Auto-apply budget is a per-day-per-vault ceiling, counted from the
        # already-resolved router routes today so concurrent invocations (a
        # manual `memex inbox triage` racing the scheduler) share one cap rather
        # than each getting a fresh allowance.
        remaining_budget = max(
            0, self._cfg.max_auto_applies_per_day - await self._auto_applied_today(inbox_id)
        )

        for nid in note_ids:
            decision = decide(nid, scored.get(nid, []), thresholds=thresholds, warmed_up=warmed_up)
            if dry_run:
                top = decision.top
                result.decisions.append(
                    {
                        'note_id': str(nid),
                        'kind': decision.kind.value,
                        'routing_state': decision.routing_state.value,
                        'margin': decision.margin,
                        'top_vault_id': str(top.vault_id) if top else None,
                        'top_vault_name': top.vault_name if top else None,
                        'top_p_match': top.p_match if top else None,
                    }
                )
            try:
                if decision.kind == DecisionKind.AUTO_ROUTE:
                    # The daily cap applies to the count in both modes so a dry run
                    # previews the same auto/skipped split a live tick would produce.
                    if remaining_budget <= 0:
                        # skipped_cap is a reason-tag (hit the daily auto-apply
                        # cap), orthogonal to the terminal disposition below: the
                        # note still falls through to a proposal, which either
                        # lands (proposed) or is suppressed by the cooldown
                        # (blocked_cooldown) — never silently dropped.
                        result.skipped_cap += 1
                        if dry_run:
                            result.proposed += 1
                        elif await self._emit_route(inbox_vault_id=inbox_id, decision=decision):
                            result.proposed += 1
                        else:
                            result.blocked_cooldown += 1
                    else:
                        if not dry_run:
                            await self._auto_apply(inbox_id, decision)
                        result.auto_routed += 1
                        remaining_budget -= 1
                elif decision.kind == DecisionKind.PROPOSE_CANDIDATES:
                    # Count only proposals that actually landed (the cooldown guard
                    # in _EMIT_PENDING_SQL can skip the insert). A suppressed insert
                    # is counted as blocked_cooldown rather than silently dropped.
                    if dry_run:
                        result.proposed += 1
                    elif await self._emit_route(inbox_vault_id=inbox_id, decision=decision):
                        result.proposed += 1
                    else:
                        result.blocked_cooldown += 1
                else:  # PROPOSE_NO_FIT
                    if dry_run:
                        result.no_fit += 1
                    elif await self._emit_no_fit(inbox_id, decision):
                        result.no_fit += 1
                    else:
                        result.blocked_backoff += 1
            except Exception:
                logger.exception('inbox_router: failed to act on note %s', nid)
                result.errors += 1

        return result

    # ------------------------------------------------------------------ status
    async def status(self) -> dict[str, Any]:
        """Router readiness + pending routing-proposal counts (CLI + HTTP share this)."""
        match_count = await self._match_count()
        async with self.metastore.session() as session:
            rows = (
                await session.execute(
                    text(
                        'SELECT rule_name, COUNT(*) FROM maintenance_proposals '
                        "WHERE lint_type = 'routing' AND status = 'pending' GROUP BY rule_name"
                    )
                )
            ).all()
        pending = {r[0]: int(r[1]) for r in rows}
        return {
            'enabled': self._cfg.enabled,
            'auto_apply_enabled': self._cfg.auto_apply_enabled,
            'warmed_up': match_count >= self._cfg.min_decisions_before_auto_apply,
            'match_observations': match_count,
            'min_decisions_before_auto_apply': self._cfg.min_decisions_before_auto_apply,
            'pending_route': pending.get(ROUTE_RULE, 0),
            'pending_no_fit': pending.get(NO_FIT_RULE, 0),
        }

    # ------------------------------------------------------------------ helpers
    async def _match_count(self) -> float:
        async with self.metastore.session() as session:
            n = (
                await session.execute(
                    text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1')
                )
            ).scalar()
        return float(n or 0.0)

    async def _is_warmed_up(self) -> bool:
        return await self._match_count() >= self._cfg.min_decisions_before_auto_apply

    async def _auto_applied_today(self, inbox_id: UUID) -> int:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.metastore.session() as session:
            n = (
                await session.execute(
                    text(_sql.COUNT_AUTO_APPLIED_TODAY_SQL),
                    {'vault_id': str(inbox_id), 'today_start': today_start},
                )
            ).scalar()
        return int(n or 0)

    def _excluded_vault_names(self) -> list[str]:
        # The inbox is always the source; the rest is operator-configurable
        # (defaults to the catch-all 'global' vault, which dilutes routing).
        return [INBOX_VAULT_NAME, *self._cfg.excluded_vaults]

    async def _auto_apply(self, inbox_id: UUID, decision: RouterDecision) -> None:
        top = decision.top
        if top is None:  # AUTO_ROUTE always has a top; defensive, not assert (-O strips asserts).
            return
        await self._notes.migrate_note(decision.note_id, top.vault_id)
        # Serialize through Pydantic's JSON serializer (consistent with _emit_route)
        # then graft the resolution block on the parsed dict.
        evidence = json.loads(self._route_evidence(inbox_id, decision).model_dump_json())
        evidence['resolution'] = {
            'verb': 'auto_route',
            'target_vault_id': str(top.vault_id),
            'p_match': top.p_match,
            'margin': decision.margin,
            'applied_at': datetime.now(timezone.utc).isoformat(),
        }
        # migrate_note has already committed (its own transaction). This audit
        # write runs in a separate transaction; on failure the note is correctly
        # moved but the audit row is missing. We log a structured reconciliation
        # record and re-raise (the tick counts it as an error) rather than losing
        # it silently. The note is out of the inbox, so it won't be re-triaged.
        try:
            async with self.metastore.session() as session:
                # Dismiss any pending routing proposal for this note — the note has
                # moved, so a leftover cockpit action would be stale.
                await session.execute(
                    text(
                        'UPDATE maintenance_proposals '
                        "SET status = 'dismissed', resolved_at = now(), resolved_by = :actor "
                        "WHERE lint_type = 'routing' AND target_type = 'note' "
                        "AND target_id = :target_id AND status = 'pending'"
                    ),
                    {'target_id': str(decision.note_id), 'actor': ROUTER_ACTOR},
                )
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
        except Exception:
            logger.exception(
                'auto_route.audit_write_failed: note %s migrated to %s without audit row',
                decision.note_id,
                top.vault_id,
            )
            raise
        # Learn: the chosen vault is a positive, the other candidates negatives.
        # Best-effort and per-call (matching the cockpit path) — the route is
        # already applied + audited, so a learning hiccup must not surface as a
        # routing failure, and one failed update must not drop the rest.
        await self._record_feedback_safe(decision.note_id, top.vault_id, 1)
        for cand in decision.candidates[1:TOP_CANDIDATES]:
            await self._record_feedback_safe(decision.note_id, cand.vault_id, 0)

    async def _record_feedback_safe(self, note_id: UUID, vault_id: UUID, label: int) -> None:
        try:
            await self.record_feedback(note_id, vault_id, label)
        except Exception:
            logger.warning(
                'inbox_router: record_feedback failed (note=%s vault=%s label=%s)',
                note_id,
                vault_id,
                label,
                exc_info=True,
            )

    async def _emit_route(self, *, inbox_vault_id: UUID, decision: RouterDecision) -> bool:
        """Emit a pending route proposal. Returns True iff a row was inserted
        (the cooldown guard can skip it)."""
        top = decision.top
        if top is None:
            return False
        evidence = self._route_evidence(inbox_vault_id, decision)
        async with self.metastore.session() as session:
            res = await session.execute(
                text(_EMIT_PENDING_SQL),
                {
                    'vault_id': str(inbox_vault_id),
                    'target_id': str(decision.note_id),
                    'rule_name': ROUTE_RULE,
                    'evidence': evidence.model_dump_json(),
                    'suggested_action': f'Route to {top.vault_name}?',
                    'cooldown_days': self._cfg.reproposal_cooldown_days,
                },
            )
            await session.commit()
        return res.rowcount > 0

    async def _emit_no_fit(self, inbox_id: UUID, decision: RouterDecision) -> bool:
        """Emit / refresh a no-fit proposal. Returns True iff a row landed."""
        best = decision.candidates[0].p_match_raw if decision.candidates else 0.0
        now = datetime.now(timezone.utc)
        # Read the existing retry counter and upsert in ONE transaction. The
        # SELECT ... FOR UPDATE locks any existing pending row so a concurrent
        # writer can't read the same retry_n and clobber the increment.
        async with self.metastore.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT (evidence->>'retry_n')::int FROM maintenance_proposals "
                        "WHERE rule_name = :rule AND target_type = 'note' "
                        'AND target_id = :tid AND vault_id = :vid ::uuid '
                        "AND status = 'pending' FOR UPDATE"
                    ),
                    {'rule': NO_FIT_RULE, 'tid': str(decision.note_id), 'vid': str(inbox_id)},
                )
            ).first()
            prev = int(row[0]) if row and row[0] is not None else -1
            retry_n = prev + 1
            delay_days = min(self._cfg.backoff_base_days * (2**retry_n), self._cfg.backoff_cap_days)
            next_retry = now + timedelta(days=delay_days)
            evidence = NoFitEvidence(
                routing_state=decision.routing_state.value,
                best_p_match_raw=best,
                retry_n=retry_n,
                next_retry_at=next_retry.isoformat(),
                last_evaluated_at=now.isoformat(),
            )
            upsert = await session.execute(
                text(_UPSERT_NO_FIT_SQL),
                {
                    'vault_id': str(inbox_id),
                    'target_id': str(decision.note_id),
                    'rule_name': NO_FIT_RULE,
                    'evidence': evidence.model_dump_json(),
                    'suggested_action': (
                        'No vault fits this note; leave in inbox or migrate manually.'
                    ),
                },
            )
            await session.commit()
            if upsert.rowcount == 0 and row is not None:
                # An existing pending no-fit wasn't due yet, so the UPSERT's WHERE
                # guard skipped the evidence refresh. Expected only if a manual
                # tick raced the backoff window (the SELECT guard normally filters
                # these out); log for visibility rather than silently dropping.
                logger.debug(
                    'inbox_router: no-fit upsert skipped (backoff not due) for note %s',
                    decision.note_id,
                )
        return upsert.rowcount > 0

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
