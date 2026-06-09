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
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

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


class ExperientialRepositoryError(Exception):
    """Base error for the experiential repository."""


class ExperientialEntryNotFound(ExperientialRepositoryError):
    """Raised when an entry lookup misses."""


class ExperientialIdentityConflict(ExperientialRepositoryError):
    """Raised when an upsert collides with an existing row that does NOT
    match the (kind, scope, verb, context) anchor of the input — the only
    case where the unique constraint should re-raise to the caller."""


class ExperientialRepository:
    """Async CRUD for the experiential plane.

    Construction is cheap: the repository is a thin wrapper around the
    metastore's session factory. All methods take an explicit ``vault_id``
    wherever multi-tenancy is at risk.
    """

    def __init__(self, metastore: AsyncBaseMetaStoreEngine) -> None:
        self._metastore = metastore
        # Optional — set by MemexAPI after construction (see V11 pattern).
        self._audit_service: Any | None = None

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
        )

        try:
            async with self._metastore.session() as session:
                session.add(entry)
                await session.commit()
                await session.refresh(entry)
        except IntegrityError as exc:
            logger.info(
                'experiential.create: identity anchor conflict for '
                'kind=%s scope=%s verb=%r context=%r',
                payload.kind,
                payload.scope,
                payload.verb,
                payload.context,
            )
            raise ExperientialIdentityConflict(
                f'an entry with kind={payload.kind!r} scope={payload.scope!r} '
                f'verb={payload.verb!r} context={payload.context!r} already exists'
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
            entry = await self._get_entry(session, entry_id, vault_id=vault_id)

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

            # Compute the next version within the same transaction. The
            # UNIQUE (entry_id, version) constraint catches a parallel
            # worker's race if one slips through.
            from sqlmodel import func

            max_version = (
                await session.exec(
                    select(func.max(DBExperientialEntryVersion.version)).where(
                        col(DBExperientialEntryVersion.entry_id) == entry.id
                    )
                )
            ).scalar_one() or 0
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
                edited_by=payload.edited_by,
                edit_reason=payload.edit_reason,
            )
            session.add(version)
            await session.commit()
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

            existing = (await session.exec(stmt)).first()

            if existing is None:
                session.add(
                    DBExperientialEntry(
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
                    )
                )
                await session.commit()
            else:
                existing.title = payload.title
                existing.summary = payload.summary
                existing.body = payload.body
                if payload.trigger is not None:
                    existing.trigger = payload.trigger
                    existing.trigger_embedding = None
                existing.tags = payload.tags
                existing.extra_metadata = payload.extra_metadata
                existing.status = DBExperientialStatus(payload.status)
                existing.updated_at = datetime.now(timezone.utc)
                if (
                    existing.status == DBExperientialStatus.PUBLISHED
                    and existing.published_at is None
                ):
                    existing.published_at = existing.updated_at
                session.add(existing)
                await session.commit()
                await session.refresh(existing)

            merged = await self._get_entry(session, existing.id if existing else uuid.uuid4())

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
        await self._enqueue_simple_audit(
            action,
            resource_type='experiential_entry',
            resource_id=str(entry.id),
            details={
                'vault_id': str(entry.vault_id),
                'kind': entry.kind.value,
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
        return ExperientialEntryDTO(
            id=entry.id,
            vault_id=entry.vault_id,
            kind=entry.kind.value,  # type: ignore[arg-type]
            scope=entry.scope,
            verb=entry.verb,
            context=entry.context,
            title=entry.title,
            summary=entry.summary,
            body=entry.body or '',
            trigger=entry.trigger,
            tags=list(entry.tags or []),
            extra_metadata=dict(entry.extra_metadata or {}),
            status=entry.status.value,  # type: ignore[arg-type]
            origin=entry.origin.value,  # type: ignore[arg-type]
            supersedes_id=entry.supersedes_id,
            superseded_by_id=entry.superseded_by_id,
            published_at=entry.published_at,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    @staticmethod
    def _source_to_dto(row: DBExperientialSource) -> ExperientialSourceDTO:
        return ExperientialSourceDTO(
            id=row.id,
            entry_id=row.entry_id,
            source_entry_id=row.source_entry_id,
            source_note_id=row.source_note_id,
            source_memory_unit_id=row.source_memory_unit_id,
            role=row.role.value,  # type: ignore[arg-type]
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
        return ExperientialDerivationQueueDTO(
            id=row.id,
            vault_id=row.vault_id,
            source_entry_ids=list(row.source_entry_ids or []),
            target_kind=row.target_kind,  # type: ignore[arg-type]
            target_scope=row.target_scope,
            target_verb=row.target_verb,
            target_context=row.target_context,
            status=row.status.value,  # type: ignore[arg-type]
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
