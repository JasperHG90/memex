"""Procedural Plane — repository.

The repository owns CRUD + identity-anchor upsert for the ``procedural_*``
tables, plus the source-edge, pin, and derivation-queue mutations. The
search layer (see ``procedural_search_service.py``) is a separate
service that composes the repository with the embedding model and
asynchronous RRF.

Identity anchor
---------------

Every entry has a stable identity: ``(kind, scope, verb, context)`` is
unique (``uq_procedural_identity``, NULLS NOT DISTINCT). Procedure ≡
(scope, verb, context); strategy ≡ (scope, verb, NULL) — §18.1.
``upsert_by_identity`` exploits the anchor: a re-write of the same
procedure (same scope, same verb, same context) is one UPDATE, not a
new row. Cases are NOT on this plane (they are notes with
``role='case'``); they connect to entries via ``procedural_sources``.

Embeddings
----------

The repository does **not** compute embeddings — the *caller* does
(design §18.7: embeddings are computed by the caller and passed in;
the facade embeds the trigger via
``ProceduralSearchService.embed_trigger`` and threads the vector into
``create`` / ``update`` / ``upsert_by_identity``). When no embedding
is supplied alongside a trigger change, the stale vector is nulled so
retrieval can never serve a vector for text that no longer exists
(the §19.4 stale-embedding bug class).

Audit
-----

When an ``AuditService`` is wired onto ``self._audit_service`` (the V11
pattern — see ``MemexAPI.__init__``), the repository emits fire-and-
forget audit events on every mutation. The attribute is optional so unit
tests can run the repository in isolation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.exceptions import MemexError
from memex_common.procedural_schemas import (
    DerivationQueueStatus,
    ProceduralDerivationQueueClaim,
    ProceduralDerivationQueueDTO,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralEntryVersionDTO,
    ProceduralPinCreate,
    ProceduralPinDTO,
    ProceduralSourceCreate,
    ProceduralSourceDTO,
    ShortLabel,
)
from memex_core.memory.sql_models import (
    DerivationQueueStatus as DBDerivationQueueStatus,
)
from memex_core.memory.sql_models import (
    ProceduralDerivationQueue as DBProceduralDerivationQueue,
)
from memex_core.memory.sql_models import (
    ProceduralEntry as DBProceduralEntry,
)
from memex_core.memory.sql_models import (
    ProceduralEntryVersion as DBProceduralEntryVersion,
)
from memex_core.memory.sql_models import (
    ProceduralKind as DBProceduralKind,
)
from memex_core.memory.sql_models import (
    ProceduralOrigin as DBProceduralOrigin,
)
from memex_core.memory.sql_models import (
    ProceduralPin as DBProceduralPin,
)
from memex_core.memory.sql_models import (
    ProceduralSource as DBProceduralSource,
)
from memex_core.memory.sql_models import (
    ProceduralSourceRole as DBProceduralSourceRole,
)
from memex_core.memory.sql_models import (
    ProceduralStatus as DBProceduralStatus,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.procedural_repository')

# Bumped per claim — used by the audit surface to tell create vs update.
_VERSION_BUMP = 1

# Hard cap on pins per context key (§19.8 — 10 per assembled chain fits
# the §19.2 card budget). Enforced at pin time so the briefing never has
# to truncate a curated chain silently.
PIN_CAP_PER_CONTEXT = 10

# Actions logged to the audit service.
_AUDIT_CREATE = 'procedural_entry_create'
_AUDIT_UPDATE = 'procedural_entry_update'
_AUDIT_DEPRECATE = 'procedural_entry_deprecate'
_AUDIT_SOURCE_ADD = 'procedural_source_add'
_AUDIT_PIN_ADD = 'procedural_pin_add'
_AUDIT_PIN_REMOVE = 'procedural_pin_remove'
_AUDIT_QUEUE_ENQUEUE = 'procedural_queue_enqueue'
_AUDIT_QUEUE_CLAIM = 'procedural_queue_claim'
_AUDIT_QUEUE_COMPLETE = 'procedural_queue_complete'


class ProceduralRepositoryError(MemexError):
    """Base error for the procedural repository.

    Inherits from :class:`MemexError` so the procedural HTTP route's
    ``except (MemexError, ...)`` clause catches it and the
    ``_handle_error`` translator in ``server/common.py`` can map the
    specific subclass to its status code (409 for
    :class:`ProceduralIdentityConflict`, 404 for
    :class:`ProceduralEntryNotFound`). A regression to plain
    ``Exception`` would let the exception bubble up to FastAPI as a 500
    and look indistinguishable from a real internal failure.
    """


class ProceduralEntryNotFound(ProceduralRepositoryError):
    """Raised when an entry lookup misses."""


class ProceduralIdentityConflict(ProceduralRepositoryError):
    """Raised when an upsert collides with an existing row that does NOT
    match the (kind, scope, verb, context) anchor of the input — the only
    case where the unique constraint should re-raise to the caller."""


class ProceduralConstraintViolation(ProceduralRepositoryError):
    """Raised when a non-anchor DB constraint rejects a write.

    A check (e.g. ``ck_strategy_anchor``) or foreign-key violation
    surfaces as ``IntegrityError`` from asyncpg. The repository's
    identity-anchor contract is the *one* unique constraint that
    should re-raise as 409 (``ProceduralIdentityConflict``) — every
    other constraint failure is a *caller-correctable* input error
    and maps to 422 so the agent's loop surfaces the actual cause
    instead of a misleading "concurrent upsert, retry" message.

    The ``constraint`` attribute is the asyncpg
    ``diag.constraint_name`` (e.g. ``ck_strategy_context``,
    ``procedural_entries_vault_id_fkey``) so the agent's log
    surface can tell which rule fired.
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class ProceduralRepository:
    """Async CRUD for the procedural plane.

    Construction is cheap: the repository is a thin wrapper around the
    metastore's session factory. All methods take an explicit ``vault_id``
    wherever multi-tenancy is at risk.
    """

    # The partial unique index that backs the identity anchor — the only
    # constraint that re-raises as :class:`ProceduralIdentityConflict` (409,
    # "retry as upsert"). Every OTHER constraint (a CHECK, an FK, a non-anchor
    # UNIQUE like a pin) maps to :class:`ProceduralConstraintViolation` (422),
    # not 409 — see ``_translate_integrity_error``.
    _ANCHOR_CONSTRAINTS = frozenset({'uq_procedural_identity'})

    def __init__(self, metastore: AsyncBaseMetaStoreEngine) -> None:
        self._metastore = metastore
        # Optional — set by MemexAPI after construction (see V11 pattern).
        self._audit_service: Any | None = None

    @staticmethod
    def _translate_integrity_error(
        exc: IntegrityError,
        *,
        anchor_label: str,
    ) -> ProceduralRepositoryError:
        """Translate a raw asyncpg ``IntegrityError`` into the right
        domain error.

        The identity-anchor UNIQUE is the only case that maps to
        :class:`ProceduralIdentityConflict` (409, "retry"). Every
        other constraint — a CHECK, an FK, a sibling-table UNIQUE
        tripped by a parallel writer — maps to
        :class:`ProceduralConstraintViolation` (422) so the agent's
        retry loop can surface the actual cause instead of a
        misleading "concurrent upsert" message.

        SQLAlchemy wraps the asyncpg error in its own
        ``IntegrityError`` (``exc.orig``); the underlying asyncpg
        exception — with the rich ``diag`` (``constraint_name``,
        ``sqlstate``, ``message_primary``) — is the ``__cause__``.
        The wrapping strips the diag, so we have to walk one
        level down.
        """
        asyncpg_exc = getattr(getattr(exc, 'orig', None), '__cause__', None)
        if asyncpg_exc is None:
            asyncpg_exc = exc.orig
        constraint_name = getattr(asyncpg_exc, 'constraint_name', None)
        sqlstate = getattr(asyncpg_exc, 'sqlstate', None)

        if constraint_name in ProceduralRepository._ANCHOR_CONSTRAINTS:
            return ProceduralIdentityConflict(anchor_label)
        message = anchor_label
        if constraint_name:
            message = f'{anchor_label} (constraint={constraint_name!r})'
        elif sqlstate:
            # CHECK / FK without a named constraint (rare — most of
            # ours are named) still leaves a SQLSTATE the agent's
            # log surface can pivot on.
            message = f'{anchor_label} (sqlstate={sqlstate!r})'
        return ProceduralConstraintViolation(message, constraint=constraint_name)

    # ------------------------------------------------------------------
    # Entry CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        payload: ProceduralEntryCreate,
        *,
        trigger_embedding: list[float] | None = None,
    ) -> ProceduralEntryDTO:
        """Insert a new procedural entry.

        The (kind, scope, verb, context) anchor must be unique; a
        collision raises :class:`ProceduralIdentityConflict`. Anchor
        shape (procedure: verb+context; strategy: verb only, context
        forbidden — §18.1) is enforced by the
        :class:`ProceduralEntryCreate` DTO validator.

        ``trigger_embedding`` is the caller-computed vector for
        ``payload.trigger`` (§18.7) — the facade supplies it.
        """
        # Auto-stamp ``published_at`` when the create request lands
        # directly in the published lifecycle state. ``update()`` does
        # the same on a draft→published transition; a regression
        # that drops this branch would let search/briefing return
        # rows with no published_at, which the briefing's
        # "freshness" sort relies on.
        published_at = datetime.now(timezone.utc) if payload.status == 'published' else None

        entry = DBProceduralEntry(
            vault_id=payload.vault_id,
            kind=DBProceduralKind(payload.kind),
            scope=payload.scope,
            verb=payload.verb,
            context=payload.context,
            title=payload.title,
            summary=payload.summary,
            body=payload.body,
            trigger=payload.trigger,
            trigger_embedding=trigger_embedding or None,
            tags=payload.tags,
            extra_metadata=payload.extra_metadata,
            skill_hints=payload.skill_hints,
            status=DBProceduralStatus(payload.status),
            origin=DBProceduralOrigin(payload.origin),
            supersedes_id=payload.supersedes_id,
            published_at=published_at,
        )

        try:
            async with self._metastore.session() as session:
                session.add(entry)
                await session.commit()
                await session.refresh(entry)
        except IntegrityError as exc:
            logger.info(
                'procedural.create: integrity error for kind=%s scope=%s verb=%r context=%r: %s',
                payload.kind,
                payload.scope,
                payload.verb,
                payload.context,
                exc,
            )
            raise self._translate_integrity_error(
                exc,
                anchor_label=(
                    f'an entry with kind={payload.kind!r} scope={payload.scope!r} '
                    f'verb={payload.verb!r} context={payload.context!r} already exists'
                ),
            ) from exc

        await self._enqueue_version_audit(_AUDIT_CREATE, entry, payload.extra_metadata)
        return self._to_dto(entry)

    async def get(
        self,
        entry_id: UUID,
        *,
        vault_id: UUID | None = None,
    ) -> ProceduralEntryDTO:
        """Look up a single entry by id. Raises if missing or vault-mismatched."""
        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)
        return self._to_dto(entry)

    async def get_many(
        self,
        entry_ids: list[UUID],
        *,
        vault_id: UUID | None = None,
    ) -> list[ProceduralEntryDTO]:
        """Bulk lookup. Missing ids are skipped (not raised)."""
        if not entry_ids:
            return []
        async with self._metastore.session() as session:
            stmt = select(DBProceduralEntry).where(col(DBProceduralEntry.id).in_(entry_ids))
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)
            rows = (await session.exec(stmt)).all()
        return [self._to_dto(r) for r in rows]

    async def list_by_status(
        self,
        *,
        status: str | None = None,
        scope: ShortLabel | None = None,
        kind: str | None = None,
        vault_id: UUID | None = None,
        limit: int = 50,
        sort: Literal['-created_at', 'created_at'] | None = None,
    ) -> list[ProceduralEntryDTO]:
        """List entries by lifecycle status, newest first.

        The enumeration surface for curation/governance — e.g. the
        drafts the derivation pipeline produced that await confirmation
        (``status='draft'``). Distinct from :meth:`search`, which ranks
        by relevance and short-circuits to empty without query text or a
        pin context, so it cannot enumerate a queue.

        This is a plain filtered SELECT: no query, no embeddings.
        ``status=None`` lists every lifecycle state; ``scope`` / ``kind``
        narrow further. Ordered by ``created_at`` descending by default
        (drafts have no ``published_at`` to sort on). Pass
        ``sort='created_at'`` for oldest first.
        """
        async with self._metastore.session() as session:
            stmt = select(DBProceduralEntry)
            if status is not None:
                stmt = stmt.where(col(DBProceduralEntry.status) == DBProceduralStatus(status))
            if scope is not None:
                stmt = stmt.where(col(DBProceduralEntry.scope) == scope)
            if kind is not None:
                stmt = stmt.where(col(DBProceduralEntry.kind) == DBProceduralKind(kind))
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)
            order = (
                col(DBProceduralEntry.created_at).asc()
                if sort == 'created_at'
                else col(DBProceduralEntry.created_at).desc()
            )
            stmt = stmt.order_by(order).limit(limit)
            rows = (await session.exec(stmt)).all()
        return [self._to_dto(r) for r in rows]

    async def get_by_identity(
        self,
        *,
        kind: str,
        scope: ShortLabel,
        verb: str | None,
        context: str | None,
        vault_id: UUID | None = None,
        status: str | None = 'published',
    ) -> ProceduralEntryDTO | None:
        """Look up a single entry by its identity anchor.

        Returns ``None`` on a miss (does NOT raise :class:`ProceduralEntryNotFound`).
        The route uses this as the "have we learned this?" probe; a 404
        would be a contract violation because an unbound anchor is a
        valid, expected answer.

        The query uses ``IS NOT DISTINCT FROM`` so a NULL context
        matches the ``UNIQUE NULLS NOT DISTINCT`` index exactly: a
        strategy anchor ``(scope, verb, NULL)`` and a procedure anchor
        ``(scope, verb, context)`` never collide, and a strategy lookup
        with ``context=None`` distinguishes "no row" from "row with
        NULL context".

        ``status`` defaults to ``'published'`` so the agent's read-
        before-write loop matches the lifecycle state it would actually
        get back from search. Pass ``status=None`` to ignore lifecycle
        state and surface drafts (operator-only path).
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralEntry)
                .where(col(DBProceduralEntry.kind) == DBProceduralKind(kind))
                .where(col(DBProceduralEntry.scope) == scope)
                .where(col(DBProceduralEntry.verb).is_not_distinct_from(verb))
                .where(col(DBProceduralEntry.context).is_not_distinct_from(context))
            )
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)
            if status is not None:
                stmt = stmt.where(col(DBProceduralEntry.status) == DBProceduralStatus(status))
            entry = (await session.exec(stmt.limit(1))).first()
        if entry is None:
            return None
        return self._to_dto(entry)

    async def update(
        self,
        entry_id: UUID,
        payload: ProceduralEntryUpdate,
        *,
        vault_id: UUID | None = None,
        trigger_embedding: list[float] | None = None,
    ) -> ProceduralEntryDTO:
        """Mutate an existing entry in place.

        Always appends a new ``procedural_entry_versions`` row carrying
        the post-update body snapshot. Bumps ``updated_at`` on the entry.
        Triggers a status→published transition sets ``published_at`` on
        the entry.
        """
        if not payload.model_fields_set:
            raise ValueError('update called with no fields set')

        async with self._metastore.session() as session:
            # Lock the entry row for the read-modify-version-write
            # cycle so two parallel updaters can't both compute the
            # same ``max(version)+1`` and race the UNIQUE
            # ``(entry_id, version)`` constraint on the versions table.
            # The lock is released at the transaction's end (commit/
            # rollback). Sequential single-writer workloads pay a
            # no-op cost here; the only thing the lock prevents is
            # ``IntegrityError → 500`` under concurrent edits, which
            # would look indistinguishable from a real DB failure to
            # the agent.
            stmt = select(DBProceduralEntry).where(col(DBProceduralEntry.id) == entry_id)
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)
            stmt = stmt.with_for_update()
            entry = (await session.exec(stmt)).first()
            if entry is None:
                raise ProceduralEntryNotFound(str(entry_id))

            pre_status = entry.status
            if payload.title is not None:
                entry.title = payload.title
            if payload.summary is not None:
                entry.summary = payload.summary
            if payload.body is not None:
                entry.body = payload.body
            if payload.trigger is not None:
                entry.trigger = payload.trigger
                # Caller-computed embedding follows the trigger; absent
                # it, null the stale vector (§19.4 bug class) — the row
                # stays reachable via BM25 until re-embedded.
                entry.trigger_embedding = trigger_embedding or None
            if payload.tags is not None:
                entry.tags = payload.tags
            if payload.extra_metadata is not None:
                entry.extra_metadata = payload.extra_metadata
            if payload.skill_hints is not None:
                entry.skill_hints = payload.skill_hints
            if payload.status is not None:
                entry.status = DBProceduralStatus(payload.status)
            if payload.supersedes_id is not None:
                entry.supersedes_id = payload.supersedes_id

            # §18.6.4: a human/agent CONTENT edit makes the entry 'authored'
            # (sticky — once authored, always authored). Derivation then
            # PROPOSES updates instead of auto-applying. The derivation
            # worker (edited_by='system:derivation') and the activate action
            # (status-only, no content) never trip this.
            _content_edited = any(
                getattr(payload, f) is not None for f in ('title', 'summary', 'body', 'trigger')
            )
            _by = (payload.edited_by or '').strip()
            _origin = entry.origin.value if hasattr(entry.origin, 'value') else str(entry.origin)
            if _content_edited and not _by.startswith('system:') and _origin != 'authored':
                entry.origin = DBProceduralOrigin.AUTHORED

            # published_at transitions
            if (
                entry.status == DBProceduralStatus.PUBLISHED
                and pre_status != DBProceduralStatus.PUBLISHED
                and entry.published_at is None
            ):
                entry.published_at = datetime.now(timezone.utc)

            entry.updated_at = datetime.now(timezone.utc)
            session.add(entry)
            await session.flush()

            # Append a version row. The UNIQUE (entry_id, version)
            # constraint catches a parallel worker's race that slipped
            # through the row lock above.
            await self._append_version_row(
                session,
                entry,
                edited_by=payload.edited_by,
                edit_reason=payload.edit_reason,
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                # Belt-and-suspenders for the row-lock above: if a
                # parallel worker slipped through (e.g. because the
                # caller's transaction isolation is
                # READ COMMITTED + skip-locked on a different
                # transaction), the UNIQUE (entry_id, version)
                # constraint will reject the second version insert.
                # Translate via the constraint-name router so a
                # version-row collision surfaces as 409 (retry) but
                # an unrelated constraint (FK, CHECK) surfaces as
                # 422 (caller-correctable).
                raise self._translate_integrity_error(
                    exc,
                    anchor_label=(
                        f'concurrent update on entry {entry_id}; retry with the latest version'
                    ),
                ) from exc
            await session.refresh(entry)

        await self._enqueue_version_audit(
            _AUDIT_UPDATE,
            entry,
            {'edit_reason': payload.edit_reason, 'edited_by': payload.edited_by},
        )
        return self._to_dto(entry)

    async def deprecate(
        self,
        entry_id: UUID,
        *,
        superseded_by_id: UUID | None = None,
        vault_id: UUID | None = None,
    ) -> ProceduralEntryDTO:
        """Soft-deprecate an entry: status→deprecated + optional successor pointer."""
        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)
            entry.status = DBProceduralStatus.DEPRECATED
            if superseded_by_id is not None:
                entry.superseded_by_id = superseded_by_id
            entry.updated_at = datetime.now(timezone.utc)
            session.add(entry)
            await session.commit()
            await session.refresh(entry)

        await self._enqueue_version_audit(
            _AUDIT_DEPRECATE,
            entry,
            {'superseded_by_id': str(superseded_by_id) if superseded_by_id else None},
        )
        return self._to_dto(entry)

    async def record_outcome(
        self,
        entry_id: UUID,
        outcome: str,
        *,
        vault_id: UUID | None = None,
    ) -> ProceduralEntryDTO:
        """Increment the §18.5 outcome counters for an enacted entry.

        ``outcome`` ∈ {success, failure, mixed}; bumps the matching counter
        plus ``uses`` and ``last_used_at``. Does NOT append a version row —
        counters are governance signals, not content edits.
        """
        o = (outcome or '').strip().lower()
        if o not in ('success', 'failure', 'mixed'):
            raise ProceduralRepositoryError(
                f'outcome must be success|failure|mixed, got {outcome!r}'
            )
        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)
            if o == 'success':
                entry.success_count = int(entry.success_count or 0) + 1
            elif o == 'failure':
                entry.failure_count = int(entry.failure_count or 0) + 1
            else:
                entry.mixed_count = int(entry.mixed_count or 0) + 1
            entry.uses = int(entry.uses or 0) + 1
            entry.last_used_at = datetime.now(timezone.utc)
            entry.updated_at = datetime.now(timezone.utc)
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return self._to_dto(entry)

    async def upsert_by_identity(
        self,
        payload: ProceduralEntryCreate,
        *,
        trigger_embedding: list[float] | None = None,
    ) -> ProceduralEntryDTO:
        """Idempotent write for procedures and strategies.

        If a row already exists for the (kind, scope, verb, context) anchor,
        apply the equivalent of an :meth:`update` and return the merged row.
        Otherwise insert. Anchor shape is enforced by the
        :class:`ProceduralEntryCreate` DTO validator.
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralEntry)
                .where(col(DBProceduralEntry.kind) == DBProceduralKind(payload.kind))
                .where(col(DBProceduralEntry.scope) == payload.scope)
            )
            if payload.verb is None:
                stmt = stmt.where(col(DBProceduralEntry.verb).is_(None))
            else:
                stmt = stmt.where(col(DBProceduralEntry.verb) == payload.verb)
            if payload.context is None:
                stmt = stmt.where(col(DBProceduralEntry.context).is_(None))
            else:
                stmt = stmt.where(col(DBProceduralEntry.context) == payload.context)

            # FOR UPDATE on the lookup so two parallel upserts with the
            # same anchor can't both miss and both try to insert. The
            # unique constraint would catch one of them as
            # ``IntegrityError`` but only the second worker to commit
            # would see the conflict — the first one would happily
            # insert a duplicate and the second would get the 500
            # path. Locking the (likely-empty) row serialises the
            # upsert: the second worker waits, then re-reads the row
            # the first worker inserted, and falls into the
            # ``existing is not None`` branch below.
            existing = (await session.exec(stmt.with_for_update())).first()

            if existing is None:
                new_entry = DBProceduralEntry(
                    vault_id=payload.vault_id,
                    kind=DBProceduralKind(payload.kind),
                    scope=payload.scope,
                    verb=payload.verb,
                    context=payload.context,
                    title=payload.title,
                    summary=payload.summary,
                    body=payload.body,
                    trigger=payload.trigger,
                    trigger_embedding=trigger_embedding or None,
                    tags=payload.tags,
                    extra_metadata=payload.extra_metadata,
                    status=DBProceduralStatus(payload.status),
                    origin=DBProceduralOrigin(payload.origin),
                    supersedes_id=payload.supersedes_id,
                    published_at=(
                        datetime.now(timezone.utc) if payload.status == 'published' else None
                    ),
                )
                session.add(new_entry)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    # Belt-and-suspenders: if the row-lock above was
                    # bypassed (different isolation level, a lock
                    # that didn't extend to the commit boundary),
                    # the unique constraint rejects the duplicate
                    # insert. Translate to the same domain error the
                    # ``create`` path raises — 409 for the
                    # identity-anchor collision, 422 for any other
                    # constraint (CHECK, FK, sibling-table UNIQUE).
                    raise self._translate_integrity_error(
                        exc,
                        anchor_label=(
                            f'concurrent upsert on '
                            f'kind={payload.kind!r} scope={payload.scope!r} '
                            f'verb={payload.verb!r} context={payload.context!r}; '
                            're-read and retry'
                        ),
                    ) from exc
                await session.refresh(new_entry)
                merged = await self._get_entry(session, new_entry.id)
            else:
                # Rewrite path: preserve the same audit-and-version
                # guarantees as :meth:`update` so the agent's
                # "I learned something new" loop writes through
                # upsert without dropping the version ledger.
                #
                # Status is preserved unless the caller explicitly
                # promotes (draft → published) — deprecated stays
                # deprecated even when a stale upsert payload carries
                # ``status='draft'`` (a regression that would let a
                # noisy caller accidentally undelete a superseded
                # entry).
                pre_status = existing.status
                existing.title = payload.title
                existing.summary = payload.summary
                existing.body = payload.body
                if payload.trigger is not None:
                    existing.trigger = payload.trigger
                    # Caller-computed embedding follows the trigger;
                    # absent it, null the stale vector (§19.4 bug class).
                    existing.trigger_embedding = trigger_embedding or None
                existing.tags = payload.tags
                existing.extra_metadata = payload.extra_metadata
                existing.skill_hints = payload.skill_hints
                if pre_status != DBProceduralStatus.DEPRECATED:
                    existing.status = DBProceduralStatus(payload.status)
                if (
                    existing.status == DBProceduralStatus.PUBLISHED
                    and pre_status != DBProceduralStatus.PUBLISHED
                    and existing.published_at is None
                ):
                    existing.published_at = datetime.now(timezone.utc)
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
                await session.flush()
                # Append a version row to mirror :meth:`update`'s
                # contract. The version row carries the post-rewrite
                # body snapshot so the audit trail is reconstructable
                # from ``procedural_entry_versions`` alone.
                await self._append_version_row(
                    session, existing, edited_by=None, edit_reason='upsert_by_identity'
                )
                try:
                    await session.commit()
                except IntegrityError as exc:
                    # Belt-and-suspenders: the row-lock above plus
                    # the version row UNIQUE (entry_id, version)
                    # constraint. If a parallel upsert slipped
                    # through, the constraint catches it. Translate
                    # through the constraint-name router so a
                    # version-row collision surfaces as 409 (retry)
                    # but an unrelated constraint (FK, CHECK) surfaces
                    # as 422 (caller-correctable) — mirrors the
                    # convention in :meth:`create` and :meth:`update`.
                    raise self._translate_integrity_error(
                        exc,
                        anchor_label=(
                            f'concurrent upsert on '
                            f'kind={payload.kind!r} scope={payload.scope!r} '
                            f'verb={payload.verb!r} context={payload.context!r}; '
                            're-read and retry'
                        ),
                    ) from exc
                await session.refresh(existing)
                merged = await self._get_entry(session, existing.id)

        # Emit the audit event outside the session so a hung audit
        # service doesn't block the commit. The ``_AUDIT_UPDATE``
        # action lets the audit surface tell create-vs-upsert
        # apart from the version row alone.
        await self._enqueue_version_audit(
            _AUDIT_UPDATE if existing is not None else _AUDIT_CREATE,
            merged,
            {'edit_reason': 'upsert_by_identity', 'edited_by': None},
        )
        return self._to_dto(merged)

    # ------------------------------------------------------------------
    # Sources + pins
    # ------------------------------------------------------------------

    async def add_source(
        self,
        entry_id: UUID,
        payload: ProceduralSourceCreate,
        *,
        vault_id: UUID | None = None,
    ) -> ProceduralSourceDTO:
        """Attach a source edge to an entry."""
        if (
            payload.source_entry_id is None
            and payload.source_note_id is None
            and payload.source_memory_unit_id is None
        ):
            raise ValueError('at least one source pointer must be set')

        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)

            row = DBProceduralSource(
                entry_id=entry.id,
                source_entry_id=payload.source_entry_id,
                source_note_id=payload.source_note_id,
                source_memory_unit_id=payload.source_memory_unit_id,
                role=DBProceduralSourceRole(payload.role),
                weight=payload.weight,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_SOURCE_ADD,
            resource_type='procedural_source',
            resource_id=str(row.id),
            details={'entry_id': str(entry_id), 'role': payload.role},
        )
        return self._source_to_dto(row)

    async def add_pin(self, payload: ProceduralPinCreate) -> ProceduralPinDTO:
        """Pin an entry into a context-binding chain.

        ``payload.position=None`` appends after the context's current
        maximum. The per-context cap (:data:`PIN_CAP_PER_CONTEXT`, §19.8)
        is enforced here so an over-full chain fails the *pin*, not the
        briefing render.
        """
        from sqlmodel import func

        async with self._metastore.session() as session:
            # Cap check + append-position computation in one aggregate.
            count_row = (
                await session.exec(
                    select(
                        func.count(col(DBProceduralPin.id)),
                        func.max(DBProceduralPin.position),
                    ).where(col(DBProceduralPin.context_key) == payload.context_key)
                )
            ).one()
            pin_count = int(count_row[0] or 0)
            max_position = count_row[1]
            if pin_count >= PIN_CAP_PER_CONTEXT:
                raise ValueError(
                    f'context {payload.context_key!r} already holds '
                    f'{pin_count} pins (cap {PIN_CAP_PER_CONTEXT}, §19.8); '
                    'unpin something first'
                )
            position = (
                payload.position
                if payload.position is not None
                else (int(max_position) + 1 if max_position is not None else 0)
            )

            row = DBProceduralPin(
                context_key=payload.context_key,
                entry_id=payload.entry_id,
                position=position,
                pinned_by=payload.pinned_by,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise ProceduralRepositoryError(
                    f'pin already exists at context_key={payload.context_key!r} '
                    f'entry_id={payload.entry_id} position={position}'
                ) from exc
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_PIN_ADD,
            resource_type='procedural_pin',
            resource_id=str(row.id),
            details={
                'context_key': payload.context_key,
                'entry_id': str(payload.entry_id),
                # The computed position actually written (payload.position
                # is None on append) — not the requested value.
                'position': row.position,
            },
        )
        return self._pin_to_dto(row)

    async def remove_pin(
        self,
        *,
        entry_id: UUID,
        context_key: ShortLabel,
    ) -> int:
        """Unpin an entry from a context. Returns the number of pins removed.

        Positions of the remaining pins are left untouched — the chain
        is ordered, not dense, so a gap costs nothing.
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralPin)
                .where(col(DBProceduralPin.entry_id) == entry_id)
                .where(col(DBProceduralPin.context_key) == context_key)
            )
            rows = (await session.exec(stmt)).all()
            for row in rows:
                await session.delete(row)
            await session.commit()

        if rows:
            await self._enqueue_simple_audit(
                _AUDIT_PIN_REMOVE,
                resource_type='procedural_pin',
                resource_id=str(entry_id),
                details={'context_key': context_key, 'removed': len(rows)},
            )
        return len(rows)

    async def list_versions(
        self,
        entry_id: UUID,
    ) -> list[ProceduralEntryVersionDTO]:
        """Return the full (uncapped) version ledger, newest first."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralEntryVersion)
                .where(col(DBProceduralEntryVersion.entry_id) == entry_id)
                .order_by(col(DBProceduralEntryVersion.version).desc())
            )
            rows = (await session.exec(stmt)).all()
        return [self._version_to_dto(r) for r in rows]

    async def rollback(
        self,
        entry_id: UUID,
        version: int,
        *,
        vault_id: UUID | None = None,
        trigger_embedding: list[float] | None = None,
        rolled_back_by: str | None = None,
    ) -> ProceduralEntryDTO:
        """Non-destructive rollback: re-apply an old snapshot as a NEW version.

        Reads the requested version row and writes its
        title/summary/body/trigger/tags/metadata back onto the entry via
        the normal :meth:`update` path — the ledger gains a new row
        (edit_reason ``rollback to v<N>``); nothing is deleted (§18.8).
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralEntryVersion)
                .where(col(DBProceduralEntryVersion.entry_id) == entry_id)
                .where(col(DBProceduralEntryVersion.version) == version)
            )
            snapshot = (await session.exec(stmt)).first()
        if snapshot is None:
            raise ProceduralEntryNotFound(
                f'entry {entry_id} has no version {version} in the ledger'
            )

        payload = ProceduralEntryUpdate(
            title=snapshot.title,
            summary=snapshot.summary,
            body=snapshot.body,
            trigger=snapshot.trigger,
            tags=list(snapshot.tags or []),
            extra_metadata=dict(snapshot.extra_metadata or {}),
            edit_reason=f'rollback to v{version}',
            edited_by=rolled_back_by,
        )
        return await self.update(
            entry_id,
            payload,
            vault_id=vault_id,
            trigger_embedding=trigger_embedding,
        )

    async def list_pins(
        self,
        context_key: ShortLabel,
        *,
        limit: int | None = None,
    ) -> list[ProceduralPinDTO]:
        """Return pins for a context, ordered by position ascending."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralPin)
                .where(col(DBProceduralPin.context_key) == context_key)
                .order_by(col(DBProceduralPin.position).asc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await session.exec(stmt)).all()
        return [self._pin_to_dto(r) for r in rows]

    async def list_pins_for_entry(
        self,
        entry_id: UUID,
    ) -> list[ProceduralPinDTO]:
        """Return all pins pointing at the given entry."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralPin)
                .where(col(DBProceduralPin.entry_id) == entry_id)
                .order_by(
                    col(DBProceduralPin.context_key).asc(),
                    col(DBProceduralPin.position).asc(),
                )
            )
            rows = (await session.exec(stmt)).all()
        return [self._pin_to_dto(r) for r in rows]

    async def list_sources_for_entry(
        self,
        entry_id: UUID,
        *,
        role: str | None = None,
    ) -> list[ProceduralSourceDTO]:
        """Return source edges attached to the given entry."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralSource)
                .where(col(DBProceduralSource.entry_id) == entry_id)
                .order_by(col(DBProceduralSource.created_at).asc())
            )
            if role is not None:
                stmt = stmt.where(col(DBProceduralSource.role) == DBProceduralSourceRole(role))
            rows = (await session.exec(stmt)).all()
        return [self._source_to_dto(r) for r in rows]

    # ------------------------------------------------------------------
    # Derivation queue
    # ------------------------------------------------------------------

    async def enqueue_derivation(
        self,
        *,
        vault_id: UUID,
        source_entry_ids: list[UUID],
        target_kind: str,
        target_scope: ShortLabel,
        target_verb: str | None = None,
        target_context: str | None = None,
    ) -> ProceduralDerivationQueueDTO:
        """Add a row to the derivation queue.

        Workers claim via :meth:`claim_derivation_tasks`.
        """
        if target_kind not in ('procedure', 'strategy'):
            raise ValueError(f'target_kind must be procedure|strategy, got {target_kind!r}')
        if target_kind == 'procedure' and (not target_verb or not target_context):
            raise ValueError('procedure derivations require both target_verb and target_context')
        if target_kind == 'strategy' and (not target_verb or target_context):
            raise ValueError(
                'strategy derivations require target_verb and NO target_context '
                '(strategy anchor ≡ (scope, verb); §18.1)'
            )

        async with self._metastore.session() as session:
            row = DBProceduralDerivationQueue(
                vault_id=vault_id,
                source_entry_ids=source_entry_ids,
                target_kind=target_kind,
                target_scope=target_scope,
                target_verb=target_verb,
                target_context=target_context,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_QUEUE_ENQUEUE,
            resource_type='procedural_derivation_queue',
            resource_id=str(row.id),
            details={
                'vault_id': str(vault_id),
                'target_kind': target_kind,
                'target_scope': target_scope,
                'source_entry_count': len(source_entry_ids),
            },
        )
        return self._queue_to_dto(row)

    async def claim_derivation_tasks(
        self,
        *,
        limit: int = 1,
        vault_id: UUID | None = None,
    ) -> list[ProceduralDerivationQueueClaim]:
        """Claim pending derivation tasks via SELECT ... FOR UPDATE SKIP LOCKED.

        Mirrors the reflection queue's pattern. The returned DTOs are
        decoupled from the ORM rows so the worker can do its synthesis in
        a separate transaction. Mark claimed via :meth:`mark_derivation_completed`
        or :meth:`mark_derivation_failed`.
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralDerivationQueue)
                .where(col(DBProceduralDerivationQueue.status) == DBDerivationQueueStatus.PENDING)
                .order_by(col(DBProceduralDerivationQueue.created_at).asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralDerivationQueue.vault_id) == vault_id)

            rows = (await session.exec(stmt)).all()
            if not rows:
                return []

            now = datetime.now(timezone.utc)
            for r in rows:
                r.status = DBDerivationQueueStatus.IN_PROGRESS
                r.attempt_count += 1
                r.claimed_at = now
                session.add(r)
            await session.commit()
            for r in rows:
                await session.refresh(r)

            await self._enqueue_simple_audit(
                _AUDIT_QUEUE_CLAIM,
                resource_type='procedural_derivation_queue',
                resource_id=','.join(str(r.id) for r in rows),
                details={'count': len(rows)},
            )

            return [
                ProceduralDerivationQueueClaim(
                    queue_id=r.id,
                    vault_id=r.vault_id,
                    source_entry_ids=list(r.source_entry_ids),
                    target_kind=r.target_kind,  # type: ignore[arg-type]
                    target_scope=r.target_scope,
                    target_verb=r.target_verb,
                    target_context=r.target_context,
                )
                for r in rows
            ]

    async def mark_derivation_completed(
        self,
        queue_id: UUID,
        result_entry_id: UUID,
    ) -> ProceduralDerivationQueueDTO:
        """Mark a claimed derivation task as completed.

        ``result_entry_id`` is the entry that the worker produced.
        """
        async with self._metastore.session() as session:
            row = await self._get_queue_row(session, queue_id)
            row.status = DBDerivationQueueStatus.COMPLETED
            row.result_entry_id = result_entry_id
            row.completed_at = datetime.now(timezone.utc)
            session.add(row)
            await session.commit()
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_QUEUE_COMPLETE,
            resource_type='procedural_derivation_queue',
            resource_id=str(queue_id),
            details={'result_entry_id': str(result_entry_id)},
        )
        return self._queue_to_dto(row)

    async def mark_derivation_failed(
        self,
        queue_id: UUID,
        last_error: str,
        *,
        max_attempts: int = 3,
    ) -> ProceduralDerivationQueueDTO:
        """Mark a derivation task as failed (or re-queue if attempts remain)."""
        async with self._metastore.session() as session:
            row = await self._get_queue_row(session, queue_id)
            row.last_error = last_error
            if row.attempt_count >= max_attempts:
                row.status = DBDerivationQueueStatus.FAILED
            else:
                # Park the row back in pending so another worker can retry.
                row.status = DBDerivationQueueStatus.PENDING
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return self._queue_to_dto(row)

    async def list_derivation_queue(
        self,
        *,
        status: DerivationQueueStatus | None = None,
        limit: int = 50,
    ) -> list[ProceduralDerivationQueueDTO]:
        """Inspect the derivation queue (debug / dashboard use)."""
        async with self._metastore.session() as session:
            stmt = select(DBProceduralDerivationQueue).order_by(
                col(DBProceduralDerivationQueue.created_at).desc()
            )
            if status is not None:
                stmt = stmt.where(
                    col(DBProceduralDerivationQueue.status) == DBDerivationQueueStatus(status.value)
                )
            stmt = stmt.limit(limit)
            rows = (await session.exec(stmt)).all()
        return [self._queue_to_dto(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_entry(
        self,
        session: AsyncSession,
        entry_id: UUID,
        *,
        vault_id: UUID | None = None,
    ) -> DBProceduralEntry:
        stmt = select(DBProceduralEntry).where(col(DBProceduralEntry.id) == entry_id)
        if vault_id is not None:
            stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)
        entry = (await session.exec(stmt)).first()
        if entry is None:
            raise ProceduralEntryNotFound(str(entry_id))
        return entry

    async def _append_version_row(
        self,
        session: AsyncSession,
        entry: DBProceduralEntry,
        *,
        edited_by: str | None,
        edit_reason: str | None,
    ) -> DBProceduralEntryVersion:
        """Append a new ``procedural_entry_versions`` row carrying the
        post-mutation body snapshot.

        Used by both :meth:`update` and the upsert rewrite path so the
        version ledger is complete regardless of which write surface
        mutated the entry. Computes ``max(version) + 1`` within the
        caller's open session; the UNIQUE ``(entry_id, version)``
        constraint catches a parallel-worker's race that slipped
        through the row lock. Sequential single-writer workloads pay
        a single aggregate query per write.
        """
        from sqlmodel import func

        max_version = (
            await session.exec(
                select(func.max(DBProceduralEntryVersion.version)).where(
                    col(DBProceduralEntryVersion.entry_id) == entry.id
                )
            )
        ).one() or 0
        next_version = int(max_version) + _VERSION_BUMP

        version = DBProceduralEntryVersion(
            entry_id=entry.id,
            version=next_version,
            title=entry.title,
            summary=entry.summary,
            body=entry.body,
            trigger=entry.trigger,
            tags=entry.tags,
            extra_metadata=entry.extra_metadata,
            skill_hints=entry.skill_hints,
            edited_by=edited_by,
            edit_reason=edit_reason,
        )
        session.add(version)
        return version

    async def _get_queue_row(
        self,
        session: AsyncSession,
        queue_id: UUID,
    ) -> DBProceduralDerivationQueue:
        row = (
            await session.exec(
                select(DBProceduralDerivationQueue).where(
                    col(DBProceduralDerivationQueue.id) == queue_id
                )
            )
        ).first()
        if row is None:
            raise ProceduralRepositoryError(f'derivation queue row {queue_id!s} not found')
        return row

    async def _enqueue_version_audit(
        self,
        action: str,
        entry: DBProceduralEntry,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit an audit event for an entry mutation (fire-and-forget)."""
        # ``entry.kind`` is read back from a String column as a plain
        # str (the SQLModel ``ProceduralKind`` annotation is
        # declarative; the column type drives the runtime value). A
        # direct ``entry.kind.value`` blows up on the audit path. Use
        # ``str()`` to handle both the in-memory enum (mocked callers)
        # and the post-refresh str from the DB.
        kind_str = entry.kind.value if hasattr(entry.kind, 'value') else str(entry.kind)
        await self._enqueue_simple_audit(
            action,
            resource_type='procedural_entry',
            resource_id=str(entry.id),
            details={
                'vault_id': str(entry.vault_id),
                'kind': kind_str,
                **(details or {}),
            },
        )

    async def _enqueue_simple_audit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Mirror the V11 AuditService.log interface, in async form so the
        repository remains awaitable end-to-end (the underlying service is
        fire-and-forget)."""
        if self._audit_service is None:
            return
        try:
            self._audit_service.log(
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
            )
        except Exception:
            logger.exception('procedural audit log failed (action=%s)', action)

    # ------------------------------------------------------------------
    # ORM → DTO conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dto(entry: DBProceduralEntry) -> ProceduralEntryDTO:
        # ``kind``/``status``/``origin`` are typed as Python enums on
        # the SQLModel column annotation but stored as ``String`` in
        # the DB. After ``await session.refresh(entry)`` the values
        # come back as plain strings (the declarative annotation is
        # a type hint, not a runtime converter). Normalise to str
        # here so the DTO Literal types are satisfied without a
        # .value lookup that would crash on the str post-refresh.
        def _str(v: Any) -> str:
            return v.value if hasattr(v, 'value') else str(v)

        return ProceduralEntryDTO(
            id=entry.id,
            vault_id=entry.vault_id,
            kind=_str(entry.kind),  # type: ignore[arg-type]
            scope=entry.scope,
            verb=entry.verb,
            context=entry.context,
            title=entry.title,
            summary=entry.summary,
            body=entry.body or '',
            trigger=entry.trigger,
            tags=list(entry.tags or []),
            extra_metadata=dict(entry.extra_metadata or {}),
            skill_hints=list(entry.skill_hints or []),
            status=_str(entry.status),  # type: ignore[arg-type]
            origin=_str(entry.origin),  # type: ignore[arg-type]
            success_count=int(entry.success_count or 0),
            failure_count=int(entry.failure_count or 0),
            mixed_count=int(entry.mixed_count or 0),
            uses=int(entry.uses or 0),
            last_used_at=entry.last_used_at,
            supersedes_id=entry.supersedes_id,
            superseded_by_id=entry.superseded_by_id,
            published_at=entry.published_at,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    @staticmethod
    def _source_to_dto(row: DBProceduralSource) -> ProceduralSourceDTO:
        # ``role`` is typed as ``ProceduralSourceRole`` (a str-Enum) on
        # the SQLModel column annotation but stored as ``String`` in
        # the DB. After ``await session.refresh(row)`` the value comes
        # back as a plain ``str`` — the declarative annotation is a
        # type hint, not a runtime converter. The defensive
        # ``.value if hasattr(...) else str(...)`` pattern is what the
        # other DTO converters (``_to_dto`` at line 876,
        # ``_queue_to_dto`` at line 934) already use; this is the
        # same pattern. Calling ``row.role.value`` directly crashes
        # with ``AttributeError: 'str' object has no attribute 'value'``
        # on the first real-DB read.
        role_value = row.role.value if hasattr(row.role, 'value') else str(row.role)
        return ProceduralSourceDTO(
            id=row.id,
            entry_id=row.entry_id,
            source_entry_id=row.source_entry_id,
            source_note_id=row.source_note_id,
            source_memory_unit_id=row.source_memory_unit_id,
            role=role_value,  # type: ignore[arg-type]
            weight=float(row.weight),
            created_at=row.created_at,
        )

    @staticmethod
    def _version_to_dto(row: DBProceduralEntryVersion) -> ProceduralEntryVersionDTO:
        return ProceduralEntryVersionDTO(
            id=row.id,
            entry_id=row.entry_id,
            version=int(row.version),
            title=row.title,
            summary=row.summary,
            body=row.body or '',
            trigger=row.trigger,
            tags=list(row.tags or []),
            extra_metadata=dict(row.extra_metadata or {}),
            skill_hints=list(row.skill_hints or []),
            edited_by=row.edited_by,
            edit_reason=row.edit_reason,
            created_at=row.created_at,
        )

    @staticmethod
    def _pin_to_dto(row: DBProceduralPin) -> ProceduralPinDTO:
        return ProceduralPinDTO(
            id=row.id,
            context_key=row.context_key,
            entry_id=row.entry_id,
            position=int(row.position),
            pinned_by=row.pinned_by,
            created_at=row.created_at,
        )

    @staticmethod
    def _queue_to_dto(row: DBProceduralDerivationQueue) -> ProceduralDerivationQueueDTO:
        # ``status`` is a String-stored enum-typed column, so after
        # session.refresh() it's a plain str. Same defensive pattern
        # as _to_dto above.
        status_str = row.status.value if hasattr(row.status, 'value') else str(row.status)
        return ProceduralDerivationQueueDTO(
            id=row.id,
            vault_id=row.vault_id,
            source_entry_ids=list(row.source_entry_ids or []),
            target_kind=row.target_kind,  # type: ignore[arg-type]
            target_scope=row.target_scope,
            target_verb=row.target_verb,
            target_context=row.target_context,
            status=status_str,  # type: ignore[arg-type]
            attempt_count=int(row.attempt_count),
            last_error=row.last_error,
            result_entry_id=row.result_entry_id,
            claimed_at=row.claimed_at,
            completed_at=row.completed_at,
            created_at=row.created_at,
        )


__all__ = [
    'ProceduralEntryNotFound',
    'ProceduralIdentityConflict',
    'ProceduralRepository',
    'ProceduralRepositoryError',
]
