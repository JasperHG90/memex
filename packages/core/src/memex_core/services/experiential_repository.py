"""V7 Experiential Plane — repository.

The repository owns CRUD + identity-anchor upsert for the ``experiential_*``
tables, plus the source-edge, pin, and derivation-queue mutations. The
search layer (see ``experiential_search_service.py``) is a separate
service that composes the repository with the embedding model and
asynchronous RRF.

Identity anchor
---------------

Procedures and strategies have a stable identity: ``(kind, scope, verb,
context)`` is unique (see the ``uq_experiential_identity`` partial unique
index — NULLS NOT DISTINCT — in migration 061). Cases do not participate
in the identity anchor; they are free-form experience records keyed by
``trigger`` text + ``trigger_embedding``. ``upsert_by_identity`` exploits
the anchor: a re-write of the same procedure (same scope, same verb,
same context) is one UPDATE, not a new row.

Embeddings
----------

The repository does **not** compute embeddings. ``body_embedding`` and
``trigger_embedding`` are stored as ``None`` on create/update; the search
service is responsible for back-filling them. This keeps the write path
fast and avoids one shared model mutex per request. (See V7 design
§3.4 — "lazy embedding".)

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
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.exceptions import MemexError
from memex_common.experiential_schemas import (
    DerivationQueueStatus,
    ExperientialDerivationQueueClaim,
    ExperientialDerivationQueueDTO,
    ExperientialEntryCreate,
    ExperientialEntryDTO,
    ExperientialEntryUpdate,
    ExperientialPinCreate,
    ExperientialPinDTO,
    ExperientialSourceCreate,
    ExperientialSourceDTO,
    ShortLabel,
)
from memex_core.memory.sql_models import (
    DerivationQueueStatus as DBDerivationQueueStatus,
)
from memex_core.memory.sql_models import (
    ExperientialDerivationQueue as DBExperientialDerivationQueue,
)
from memex_core.memory.sql_models import (
    ExperientialEntry as DBExperientialEntry,
)
from memex_core.memory.sql_models import (
    ExperientialEntryVersion as DBExperientialEntryVersion,
)
from memex_core.memory.sql_models import (
    ExperientialKind as DBExperientialKind,
)
from memex_core.memory.sql_models import (
    ExperientialOrigin as DBExperientialOrigin,
)
from memex_core.memory.sql_models import (
    ExperientialPin as DBExperientialPin,
)
from memex_core.memory.sql_models import (
    ExperientialSource as DBExperientialSource,
)
from memex_core.memory.sql_models import (
    ExperientialSourceRole as DBExperientialSourceRole,
)
from memex_core.memory.sql_models import (
    ExperientialStatus as DBExperientialStatus,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.experiential_repository')

# Bumped per claim — used by the audit surface to tell create vs update.
_VERSION_BUMP = 1

# Actions logged to the audit service.
_AUDIT_CREATE = 'experiential_entry_create'
_AUDIT_UPDATE = 'experiential_entry_update'
_AUDIT_DEPRECATE = 'experiential_entry_deprecate'
_AUDIT_SOURCE_ADD = 'experiential_source_add'
_AUDIT_PIN_ADD = 'experiential_pin_add'
_AUDIT_QUEUE_ENQUEUE = 'experiential_queue_enqueue'
_AUDIT_QUEUE_CLAIM = 'experiential_queue_claim'
_AUDIT_QUEUE_COMPLETE = 'experiential_queue_complete'


class ExperientialRepositoryError(MemexError):
    """Base error for the experiential repository.

    Inherits from :class:`MemexError` so the procedural HTTP route's
    ``except (MemexError, ...)`` clause catches it and the
    ``_handle_error`` translator in ``server/common.py`` can map the
    specific subclass to its status code (409 for
    :class:`ExperientialIdentityConflict`, 404 for
    :class:`ExperientialEntryNotFound`). A regression to plain
    ``Exception`` would let the exception bubble up to FastAPI as a 500
    and look indistinguishable from a real internal failure.
    """


class ExperientialEntryNotFound(ExperientialRepositoryError):
    """Raised when an entry lookup misses."""


class ExperientialIdentityConflict(ExperientialRepositoryError):
    """Raised when an upsert collides with an existing row that does NOT
    match the (kind, scope, verb, context) anchor of the input — the only
    case where the unique constraint should re-raise to the caller."""


class ExperientialConstraintViolation(ExperientialRepositoryError):
    """Raised when a non-anchor DB constraint rejects a write.

    A check (e.g. ``ck_strategy_context``) or foreign-key violation
    surfaces as ``IntegrityError`` from asyncpg. The repository's
    identity-anchor contract is the *one* unique constraint that
    should re-raise as 409 (``ExperientialIdentityConflict``) — every
    other constraint failure is a *caller-correctable* input error
    and maps to 422 so the agent's loop surfaces the actual cause
    instead of a misleading "concurrent upsert, retry" message.

    The ``constraint`` attribute is the asyncpg
    ``diag.constraint_name`` (e.g. ``ck_strategy_context``,
    ``experiential_entries_vault_id_fkey``) so the agent's log
    surface can tell which rule fired.
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class ExperientialRepository:
    """Async CRUD for the experiential plane.

    Construction is cheap: the repository is a thin wrapper around the
    metastore's session factory. All methods take an explicit ``vault_id``
    wherever multi-tenancy is at risk.
    """

    # The partial unique index that backs the identity anchor — the
    # only UNIQUE constraint that should re-raise as
    # :class:`ExperientialIdentityConflict` (409). Every other UNIQUE
    # constraint is a real conflict and uses the same 409 mapping;
    # the distinction is informative for the log line.
    _ANCHOR_CONSTRAINTS = frozenset({'uq_experiential_identity'})

    def __init__(self, metastore: AsyncBaseMetaStoreEngine) -> None:
        self._metastore = metastore
        # Optional — set by MemexAPI after construction (see V11 pattern).
        self._audit_service: Any | None = None

    @staticmethod
    def _translate_integrity_error(
        exc: IntegrityError,
        *,
        anchor_label: str,
    ) -> ExperientialRepositoryError:
        """Translate a raw asyncpg ``IntegrityError`` into the right
        domain error.

        The identity-anchor UNIQUE is the only case that maps to
        :class:`ExperientialIdentityConflict` (409, "retry"). Every
        other constraint — a CHECK, an FK, a sibling-table UNIQUE
        tripped by a parallel writer — maps to
        :class:`ExperientialConstraintViolation` (422) so the agent's
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

        if constraint_name in ExperientialRepository._ANCHOR_CONSTRAINTS:
            return ExperientialIdentityConflict(anchor_label)
        message = anchor_label
        if constraint_name:
            message = f'{anchor_label} (constraint={constraint_name!r})'
        elif sqlstate:
            # CHECK / FK without a named constraint (rare — most of
            # ours are named) still leaves a SQLSTATE the agent's
            # log surface can pivot on.
            message = f'{anchor_label} (sqlstate={sqlstate!r})'
        return ExperientialConstraintViolation(message, constraint=constraint_name)

    # ------------------------------------------------------------------
    # Entry CRUD
    # ------------------------------------------------------------------

    async def create(self, payload: ExperientialEntryCreate) -> ExperientialEntryDTO:
        """Insert a new experiential entry.

        For procedures and strategies, the (kind, scope, verb, context)
        anchor must be unique; a collision raises
        :class:`ExperientialIdentityConflict`. For cases, the call is
        idempotent against repeated identical triggers — a new UUID is
        minted on every call (cases do not have an identity anchor).
        """
        if payload.kind == 'strategy' and (not payload.verb or not payload.context):
            raise ValueError('strategy entries require both verb and context')

        # Auto-stamp ``published_at`` when the create request lands
        # directly in the published lifecycle state. ``update()`` does
        # the same on a draft→published transition; a regression
        # that drops this branch would let search/briefing return
        # rows with no published_at, which the briefing's
        # "freshness" sort relies on.
        published_at = datetime.now(timezone.utc) if payload.status == 'published' else None

        entry = DBExperientialEntry(
            vault_id=payload.vault_id,
            kind=DBExperientialKind(payload.kind),
            scope=payload.scope,
            verb=payload.verb,
            context=payload.context,
            title=payload.title,
            summary=payload.summary,
            body=payload.body,
            trigger=payload.trigger,
            tags=payload.tags,
            extra_metadata=payload.extra_metadata,
            status=DBExperientialStatus(payload.status),
            origin=DBExperientialOrigin(payload.origin),
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
                'experiential.create: integrity error for kind=%s scope=%s verb=%r context=%r: %s',
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
    ) -> ExperientialEntryDTO:
        """Look up a single entry by id. Raises if missing or vault-mismatched."""
        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)
        return self._to_dto(entry)

    async def get_many(
        self,
        entry_ids: list[UUID],
        *,
        vault_id: UUID | None = None,
    ) -> list[ExperientialEntryDTO]:
        """Bulk lookup. Missing ids are skipped (not raised)."""
        if not entry_ids:
            return []
        async with self._metastore.session() as session:
            stmt = select(DBExperientialEntry).where(col(DBExperientialEntry.id).in_(entry_ids))
            if vault_id is not None:
                stmt = stmt.where(col(DBExperientialEntry.vault_id) == vault_id)
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
    ) -> ExperientialEntryDTO | None:
        """Look up a single entry by its identity anchor.

        Returns ``None`` on a miss (does NOT raise :class:`ExperientialEntryNotFound`).
        The route uses this as the "have we learned this?" probe; a 404
        would be a contract violation because an unbound anchor is a
        valid, expected answer.

        The query uses ``IS NOT DISTINCT FROM`` so NULL verb/context
        match the ``UNIQUE NULLS NOT DISTINCT`` partial index exactly.
        A case with ``(kind='case', scope, NULL, NULL)`` and a procedure
        with ``(kind='procedure', scope, 'verb', NULL)`` do NOT collide,
        and a miss against ``NULL, NULL`` still distinguishes "no row"
        from "row with NULL verb/context".

        ``status`` defaults to ``'published'`` so the agent's read-
        before-write loop matches the lifecycle state it would actually
        get back from search. Pass ``status=None`` to ignore lifecycle
        state and surface drafts (operator-only path).
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialEntry)
                .where(col(DBExperientialEntry.kind) == DBExperientialKind(kind))
                .where(col(DBExperientialEntry.scope) == scope)
                .where(col(DBExperientialEntry.verb).is_not_distinct_from(verb))
                .where(col(DBExperientialEntry.context).is_not_distinct_from(context))
            )
            if vault_id is not None:
                stmt = stmt.where(col(DBExperientialEntry.vault_id) == vault_id)
            if status is not None:
                stmt = stmt.where(col(DBExperientialEntry.status) == DBExperientialStatus(status))
            entry = (await session.exec(stmt.limit(1))).first()
        if entry is None:
            return None
        return self._to_dto(entry)

    async def update(
        self,
        entry_id: UUID,
        payload: ExperientialEntryUpdate,
        *,
        vault_id: UUID | None = None,
    ) -> ExperientialEntryDTO:
        """Mutate an existing entry in place.

        Always appends a new ``experiential_entry_versions`` row carrying
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
            stmt = select(DBExperientialEntry).where(col(DBExperientialEntry.id) == entry_id)
            if vault_id is not None:
                stmt = stmt.where(col(DBExperientialEntry.vault_id) == vault_id)
            stmt = stmt.with_for_update()
            entry = (await session.exec(stmt)).first()
            if entry is None:
                raise ExperientialEntryNotFound(str(entry_id))

            pre_status = entry.status
            if payload.title is not None:
                entry.title = payload.title
            if payload.summary is not None:
                entry.summary = payload.summary
            if payload.body is not None:
                entry.body = payload.body
            if payload.trigger is not None:
                entry.trigger = payload.trigger
                # Drop the stale trigger embedding; the search service
                # re-computes it on next index.
                entry.trigger_embedding = None
            if payload.tags is not None:
                entry.tags = payload.tags
            if payload.extra_metadata is not None:
                entry.extra_metadata = payload.extra_metadata
            if payload.status is not None:
                entry.status = DBExperientialStatus(payload.status)
            if payload.supersedes_id is not None:
                entry.supersedes_id = payload.supersedes_id

            # published_at transitions
            if (
                entry.status == DBExperientialStatus.PUBLISHED
                and pre_status != DBExperientialStatus.PUBLISHED
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
    ) -> ExperientialEntryDTO:
        """Soft-deprecate an entry: status→deprecated + optional successor pointer."""
        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)
            entry.status = DBExperientialStatus.DEPRECATED
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

    async def upsert_by_identity(
        self,
        payload: ExperientialEntryCreate,
    ) -> ExperientialEntryDTO:
        """Idempotent write for procedures and strategies.

        If a row already exists for the (kind, scope, verb, context) anchor,
        apply the equivalent of an :meth:`update` and return the merged row.
        Otherwise insert. Cases are *not* supported — call :meth:`create`
        directly.
        """
        if payload.kind == 'case':
            raise ValueError('upsert_by_identity does not apply to cases')
        # Mirror the strategy-anchor check from :meth:`create` so a
        # caller that forgets a required field sees a ValueError
        # (cheap, validation-style) rather than a CHECK violation
        # translated to ``ExperientialIdentityConflict`` (a misleading
        # 409 that would tell the agent to retry).
        if payload.kind == 'strategy' and (not payload.verb or not payload.context):
            raise ValueError('strategy entries require both verb and context')

        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialEntry)
                .where(col(DBExperientialEntry.kind) == DBExperientialKind(payload.kind))
                .where(col(DBExperientialEntry.scope) == payload.scope)
            )
            if payload.verb is None:
                stmt = stmt.where(col(DBExperientialEntry.verb).is_(None))
            else:
                stmt = stmt.where(col(DBExperientialEntry.verb) == payload.verb)
            if payload.context is None:
                stmt = stmt.where(col(DBExperientialEntry.context).is_(None))
            else:
                stmt = stmt.where(col(DBExperientialEntry.context) == payload.context)

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
                new_entry = DBExperientialEntry(
                    vault_id=payload.vault_id,
                    kind=DBExperientialKind(payload.kind),
                    scope=payload.scope,
                    verb=payload.verb,
                    context=payload.context,
                    title=payload.title,
                    summary=payload.summary,
                    body=payload.body,
                    trigger=payload.trigger,
                    tags=payload.tags,
                    extra_metadata=payload.extra_metadata,
                    status=DBExperientialStatus(payload.status),
                    origin=DBExperientialOrigin(payload.origin),
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
                    # Drop the stale trigger embedding; the search
                    # service re-computes it on next index.
                    existing.trigger_embedding = None
                existing.tags = payload.tags
                existing.extra_metadata = payload.extra_metadata
                if pre_status != DBExperientialStatus.DEPRECATED:
                    existing.status = DBExperientialStatus(payload.status)
                if (
                    existing.status == DBExperientialStatus.PUBLISHED
                    and pre_status != DBExperientialStatus.PUBLISHED
                    and existing.published_at is None
                ):
                    existing.published_at = datetime.now(timezone.utc)
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
                await session.flush()
                # Append a version row to mirror :meth:`update`'s
                # contract. The version row carries the post-rewrite
                # body snapshot so the audit trail is reconstructable
                # from ``experiential_entry_versions`` alone.
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
                    # to the same domain error so the route returns
                    # 409 (not 500) and the agent's upsert loop can
                    # re-read + re-merge.
                    raise ExperientialIdentityConflict(
                        f'concurrent upsert on '
                        f'kind={payload.kind!r} scope={payload.scope!r} '
                        f'verb={payload.verb!r} context={payload.context!r}; '
                        're-read and retry'
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
        payload: ExperientialSourceCreate,
        *,
        vault_id: UUID | None = None,
    ) -> ExperientialSourceDTO:
        """Attach a source edge to an entry."""
        if (
            payload.source_entry_id is None
            and payload.source_note_id is None
            and payload.source_memory_unit_id is None
        ):
            raise ValueError('at least one source pointer must be set')

        async with self._metastore.session() as session:
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)

            row = DBExperientialSource(
                entry_id=entry.id,
                source_entry_id=payload.source_entry_id,
                source_note_id=payload.source_note_id,
                source_memory_unit_id=payload.source_memory_unit_id,
                role=DBExperientialSourceRole(payload.role),
                weight=payload.weight,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_SOURCE_ADD,
            resource_type='experiential_source',
            resource_id=str(row.id),
            details={'entry_id': str(entry_id), 'role': payload.role},
        )
        return self._source_to_dto(row)

    async def add_pin(self, payload: ExperientialPinCreate) -> ExperientialPinDTO:
        """Pin an entry into a context-binding chain."""
        async with self._metastore.session() as session:
            row = DBExperientialPin(
                context_key=payload.context_key,
                entry_id=payload.entry_id,
                position=payload.position,
                pinned_by=payload.pinned_by,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise ExperientialRepositoryError(
                    f'pin already exists at context_key={payload.context_key!r} '
                    f'entry_id={payload.entry_id} position={payload.position}'
                ) from exc
            await session.refresh(row)

        await self._enqueue_simple_audit(
            _AUDIT_PIN_ADD,
            resource_type='experiential_pin',
            resource_id=str(row.id),
            details={
                'context_key': payload.context_key,
                'entry_id': str(payload.entry_id),
                'position': payload.position,
            },
        )
        return self._pin_to_dto(row)

    async def list_pins(
        self,
        context_key: ShortLabel,
        *,
        limit: int | None = None,
    ) -> list[ExperientialPinDTO]:
        """Return pins for a context, ordered by position ascending."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialPin)
                .where(col(DBExperientialPin.context_key) == context_key)
                .order_by(col(DBExperientialPin.position).asc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await session.exec(stmt)).all()
        return [self._pin_to_dto(r) for r in rows]

    async def list_pins_for_entry(
        self,
        entry_id: UUID,
    ) -> list[ExperientialPinDTO]:
        """Return all pins pointing at the given entry."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialPin)
                .where(col(DBExperientialPin.entry_id) == entry_id)
                .order_by(
                    col(DBExperientialPin.context_key).asc(),
                    col(DBExperientialPin.position).asc(),
                )
            )
            rows = (await session.exec(stmt)).all()
        return [self._pin_to_dto(r) for r in rows]

    async def list_sources_for_entry(
        self,
        entry_id: UUID,
        *,
        role: str | None = None,
    ) -> list[ExperientialSourceDTO]:
        """Return source edges attached to the given entry."""
        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialSource)
                .where(col(DBExperientialSource.entry_id) == entry_id)
                .order_by(col(DBExperientialSource.created_at).asc())
            )
            if role is not None:
                stmt = stmt.where(col(DBExperientialSource.role) == DBExperientialSourceRole(role))
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
    ) -> ExperientialDerivationQueueDTO:
        """Add a row to the derivation queue.

        Workers claim via :meth:`claim_derivation_tasks`.
        """
        if target_kind not in ('procedure', 'strategy'):
            raise ValueError(f'target_kind must be procedure|strategy, got {target_kind!r}')
        if target_kind == 'strategy' and (not target_verb or not target_context):
            raise ValueError('strategy derivations require both target_verb and target_context')

        async with self._metastore.session() as session:
            row = DBExperientialDerivationQueue(
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
            resource_type='experiential_derivation_queue',
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
    ) -> list[ExperientialDerivationQueueClaim]:
        """Claim pending derivation tasks via SELECT ... FOR UPDATE SKIP LOCKED.

        Mirrors the reflection queue's pattern. The returned DTOs are
        decoupled from the ORM rows so the worker can do its synthesis in
        a separate transaction. Mark claimed via :meth:`mark_derivation_completed`
        or :meth:`mark_derivation_failed`.
        """
        async with self._metastore.session() as session:
            stmt = (
                select(DBExperientialDerivationQueue)
                .where(col(DBExperientialDerivationQueue.status) == DBDerivationQueueStatus.PENDING)
                .order_by(col(DBExperientialDerivationQueue.created_at).asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if vault_id is not None:
                stmt = stmt.where(col(DBExperientialDerivationQueue.vault_id) == vault_id)

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
                resource_type='experiential_derivation_queue',
                resource_id=','.join(str(r.id) for r in rows),
                details={'count': len(rows)},
            )

            return [
                ExperientialDerivationQueueClaim(
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
    ) -> ExperientialDerivationQueueDTO:
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
            resource_type='experiential_derivation_queue',
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
    ) -> ExperientialDerivationQueueDTO:
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
    ) -> list[ExperientialDerivationQueueDTO]:
        """Inspect the derivation queue (debug / dashboard use)."""
        async with self._metastore.session() as session:
            stmt = select(DBExperientialDerivationQueue).order_by(
                col(DBExperientialDerivationQueue.created_at).desc()
            )
            if status is not None:
                stmt = stmt.where(
                    col(DBExperientialDerivationQueue.status)
                    == DBDerivationQueueStatus(status.value)
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
    ) -> DBExperientialEntry:
        stmt = select(DBExperientialEntry).where(col(DBExperientialEntry.id) == entry_id)
        if vault_id is not None:
            stmt = stmt.where(col(DBExperientialEntry.vault_id) == vault_id)
        entry = (await session.exec(stmt)).first()
        if entry is None:
            raise ExperientialEntryNotFound(str(entry_id))
        return entry

    async def _append_version_row(
        self,
        session: AsyncSession,
        entry: DBExperientialEntry,
        *,
        edited_by: str | None,
        edit_reason: str | None,
    ) -> DBExperientialEntryVersion:
        """Append a new ``experiential_entry_versions`` row carrying the
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
                select(func.max(DBExperientialEntryVersion.version)).where(
                    col(DBExperientialEntryVersion.entry_id) == entry.id
                )
            )
        ).one() or 0
        next_version = int(max_version) + _VERSION_BUMP

        version = DBExperientialEntryVersion(
            entry_id=entry.id,
            version=next_version,
            title=entry.title,
            summary=entry.summary,
            body=entry.body,
            trigger=entry.trigger,
            tags=entry.tags,
            extra_metadata=entry.extra_metadata,
            edited_by=edited_by,
            edit_reason=edit_reason,
        )
        session.add(version)
        return version

    async def _get_queue_row(
        self,
        session: AsyncSession,
        queue_id: UUID,
    ) -> DBExperientialDerivationQueue:
        row = (
            await session.exec(
                select(DBExperientialDerivationQueue).where(
                    col(DBExperientialDerivationQueue.id) == queue_id
                )
            )
        ).first()
        if row is None:
            raise ExperientialRepositoryError(f'derivation queue row {queue_id!s} not found')
        return row

    async def _enqueue_version_audit(
        self,
        action: str,
        entry: DBExperientialEntry,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit an audit event for an entry mutation (fire-and-forget)."""
        # ``entry.kind`` is read back from a String column as a plain
        # str (the SQLModel ``ExperientialKind`` annotation is
        # declarative; the column type drives the runtime value). A
        # direct ``entry.kind.value`` blows up on the audit path. Use
        # ``str()`` to handle both the in-memory enum (mocked callers)
        # and the post-refresh str from the DB.
        kind_str = entry.kind.value if hasattr(entry.kind, 'value') else str(entry.kind)
        await self._enqueue_simple_audit(
            action,
            resource_type='experiential_entry',
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
            logger.exception('experiential audit log failed (action=%s)', action)

    # ------------------------------------------------------------------
    # ORM → DTO conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dto(entry: DBExperientialEntry) -> ExperientialEntryDTO:
        # ``kind``/``status``/``origin`` are typed as Python enums on
        # the SQLModel column annotation but stored as ``String`` in
        # the DB. After ``await session.refresh(entry)`` the values
        # come back as plain strings (the declarative annotation is
        # a type hint, not a runtime converter). Normalise to str
        # here so the DTO Literal types are satisfied without a
        # .value lookup that would crash on the str post-refresh.
        def _str(v: Any) -> str:
            return v.value if hasattr(v, 'value') else str(v)

        return ExperientialEntryDTO(
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
            status=_str(entry.status),  # type: ignore[arg-type]
            origin=_str(entry.origin),  # type: ignore[arg-type]
            supersedes_id=entry.supersedes_id,
            superseded_by_id=entry.superseded_by_id,
            published_at=entry.published_at,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    @staticmethod
    def _source_to_dto(row: DBExperientialSource) -> ExperientialSourceDTO:
        # ``role`` is typed as ``ExperientialSourceRole`` (a str-Enum) on
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
        return ExperientialSourceDTO(
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
    def _pin_to_dto(row: DBExperientialPin) -> ExperientialPinDTO:
        return ExperientialPinDTO(
            id=row.id,
            context_key=row.context_key,
            entry_id=row.entry_id,
            position=int(row.position),
            pinned_by=row.pinned_by,
            created_at=row.created_at,
        )

    @staticmethod
    def _queue_to_dto(row: DBExperientialDerivationQueue) -> ExperientialDerivationQueueDTO:
        # ``status`` is a String-stored enum-typed column, so after
        # session.refresh() it's a plain str. Same defensive pattern
        # as _to_dto above.
        status_str = row.status.value if hasattr(row.status, 'value') else str(row.status)
        return ExperientialDerivationQueueDTO(
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
    'ExperientialEntryNotFound',
    'ExperientialIdentityConflict',
    'ExperientialRepository',
    'ExperientialRepositoryError',
]
