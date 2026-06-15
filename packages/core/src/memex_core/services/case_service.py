"""Case submission — the §5.1 episode path onto the declarative plane.

A case is a NOTE (``notes.role='case'``) filed into the hidden
``procedural`` system vault (§18.3 / §18.9.0), never a row on the
procedural plane. This service owns the whole submission flow:

1. **Compose** the §5.1 episode template (Trigger / Situation / Actions /
   Outcome & Lesson) from the :class:`CaseSubmit` payload. Template
   structure is the distillation precondition — free-form notes distill
   into mush (§5.1).
2. **File** the note into the system vault via the normal ingestion path
   (extraction still runs — it is per-note, not per-vault, so the entity
   bridge keeps working; §18.9.0).
3. **Stamp** ``role='case'`` + provenance (``outcome``, ``project``,
   ``submitted_by``, ``case_of``) onto the note row. ``role`` is the
   indexed distillation gate (migration 062); provenance lives in
   ``doc_metadata`` — read per-case *after* the indexed filter (§18.3).
4. **Assign** (file-then-lint, decision #5):

   * ``case_of`` supplied → direct assignment (the PRIMARY API, §18.1 —
     the agent that just enacted a procedure knows which one it was).
   * else → the §19.3 judge over trigger-search candidates. ``clean``
     separation auto-assigns (or creates a draft anchor for
     ``new_procedure``); anything else — including a judge failure —
     lands in the lint queue with the candidates + judgment as evidence
     and ``assign_case`` pre-selected. Never an in-session
     clarification round-trip.

Assignment side effects: an ``procedural_sources`` provenance edge
(case note → entry) + a derivation-queue row for the affected entry
(§18.4 dirty event; the queue consumer is Phase 3).
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import dspy
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select

from memex_common.procedural_schemas import (
    ANCHOR_LABEL_PATTERN,
    CaseAssignment,
    CaseSubmit,
    CaseSubmitResult,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralSearchRequest,
    ProceduralSourceCreate,
)
from memex_core.memory.procedural_assignment import (
    AssignmentCandidate,
    AssignmentJudgment,
    judge_assignment,
)

if TYPE_CHECKING:
    from memex_core.api import MemexAPI, NoteInput

logger = logging.getLogger('memex.core.services.case_service')

# The hidden system vault seeded by migration 063 (renamed by 064).
CASE_VAULT_NAME = 'procedural'

# Policy for the case system vault: reflection OFF, summary OFF (both the
# system-vault default). §18.9.0 originally specced reflection ON, but the
# cases → procedures → strategies derivation pipeline reads the cases
# DIRECTLY (procedural_distillation), never the per-entity mental models
# reflection produces — so reflection over the hidden case vault would
# spend LLM calls building models nothing consumes. Set explicitly to
# document the deliberate OFF. MUST validate against the typed VaultPolicy
# (extra='forbid') — see migration 063.
CASE_VAULT_POLICY = {'reflect': False}

# External-proposal rule for escalated assignments. NOT llm_-prefixed —
# that namespace is reserved for internal emitters (lint.py validator).
ASSIGNMENT_RULE_NAME = 'case_assignment'

_CANDIDATE_LIMIT = 3


def compose_case_markdown(payload: CaseSubmit) -> str:
    """Render the §5.1 episode template."""
    actions_md = (
        '\n'.join(f'{i}. {step}' for i, step in enumerate(payload.actions, start=1))
        or '_none recorded_'
    )
    lesson = payload.lesson.strip()
    outcome_line = payload.outcome if not lesson else f'{payload.outcome}. **Lesson:** {lesson}'
    return (
        f'## Trigger\n{payload.trigger.strip()}\n\n'
        f'## Situation\n{payload.situation.strip() or "_not recorded_"}\n\n'
        f'## Actions\n{actions_md}\n\n'
        f'## Outcome / Lesson\n{outcome_line}\n'
    )


class CaseSubmissionError(Exception):
    """Raised when the submission flow cannot complete."""


class CaseService:
    """Owns case submission + assignment. Constructed per-call by the
    :class:`MemexAPI` facade (cheap — holds only the api reference)."""

    def __init__(self, api: 'MemexAPI') -> None:
        self._api = api

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def submit(self, payload: CaseSubmit) -> CaseSubmitResult:
        """File a case note + run assignment. See module docstring."""
        vault_id = await self._resolve_case_vault()

        # Validate an explicit case_of BEFORE filing the note, so a bad
        # pointer fails fast (clean 404) instead of orphaning a stamped
        # case note when apply_assignment's get() raises post-ingest. This is
        # EXISTENCE validation only (it serves direct, non-HTTP callers too) —
        # tenancy/authz on case_of is the server's job (cases.py
        # check_vault_access), since the service has no auth context.
        if payload.case_of is not None:
            try:
                await self._api._procedural_repo.get(payload.case_of)
            except Exception as exc:
                raise CaseSubmissionError(
                    f'case_of {payload.case_of} does not resolve to a procedural '
                    'entry — fix the id or omit it to let the judge assign.'
                ) from exc

        # 1+2. Compose + ingest via the normal path (extraction runs).
        note_input = self._build_note_input(payload)
        ingest_result = await self._api.ingest(note_input, vault_id=vault_id)

        # Ingest is content-idempotent: re-submitting byte-identical case
        # content skips and returns {'status': 'skipped'} with NO 'note_id'.
        # Treat that as "already filed" rather than KeyError-ing into a 500 —
        # return the existing case note (its id is the deterministic
        # idempotency_key) WITHOUT re-stamping/re-assigning, which would
        # duplicate the provenance edge.
        if ingest_result.get('status') == 'skipped':
            return CaseSubmitResult(
                note_id=UUID(note_input.idempotency_key),
                vault_id=vault_id,
                assignment=CaseAssignment(
                    mode='skipped',
                    reasoning='identical case content already filed (idempotent skip)',
                ),
            )
        note_id = UUID(str(ingest_result['note_id']))

        # 3+4. Stamp role + provenance, then assign. A failure here would
        # otherwise orphan the freshly-ingested note in the system vault —
        # filed but with no role='case' and no provenance edge, indistinguishable
        # from junk. Archive it (non-destructive) before re-raising so it cannot
        # masquerade as a real case. (`_assign` is itself fail-safe — a judge
        # failure escalates to the lint queue rather than raising — so this
        # mainly guards a stamp/DB error or a bad explicit case_of.)
        try:
            await self._stamp_case_note(note_id, payload)
            assignment = await self._assign(note_id, vault_id, payload)
        except Exception:
            with contextlib.suppress(Exception):
                await self._api.set_note_status(note_id, 'archived')
            raise

        return CaseSubmitResult(note_id=note_id, vault_id=vault_id, assignment=assignment)

    async def submit_job(
        self,
        *,
        request: CaseSubmit,
        vault_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Adapter for the durable job runner (``JobManager.create_single_job``).

        Runs :meth:`submit` and returns a job-result dict so the tracked
        ``BatchJob`` records the materialised case note id — queryable at
        ``GET /api/v1/ingestions/{job_id}``. ``vault_id`` is accepted for the
        runner's forwarding contract but ignored (the case vault is implicit).
        If :meth:`submit` raises, the runner records the failure on the job
        (status ``failed`` + ``error_info``) rather than swallowing it.
        """
        result = await self.submit(request)
        return {
            'note_id': str(result.note_id),
            'vault_id': str(result.vault_id),
            'assignment_mode': result.assignment.mode,
        }

    async def list_cases(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        outcome: str | None = None,
        tags: list[str] | None = None,
        project_id: str | None = None,
        case_of: UUID | str | None = None,
        submitted_by: str | None = None,
        slim: bool = False,
    ) -> list[Any]:
        """List case notes (``role='case'``) in the hidden procedural system vault.

        Filters match the provenance stamped onto the case note's
        ``doc_metadata`` at submission time. Returns ORM ``Note`` objects with
        ``vault_name`` and (unless ``slim``) ``summaries`` attached so the
        HTTP layer can reuse ``build_note_list_item_dto``.
        """
        import json

        from sqlalchemy import func, literal
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlmodel import select

        from memex_common.schemas import BlockSummaryDTO
        from memex_core.memory.sql_models import Chunk, Note, Vault

        vault_id = await self._resolve_case_vault()

        async with self._api.metastore.session() as session:
            stmt = (
                select(Note).where(col(Note.vault_id) == vault_id).where(col(Note.role) == 'case')
            )
            if outcome is not None:
                stmt = stmt.where(col(Note.doc_metadata)['outcome'].astext == outcome)
            if project_id is not None:
                stmt = stmt.where(col(Note.doc_metadata)['project'].astext == project_id)
            if case_of is not None:
                stmt = stmt.where(col(Note.doc_metadata)['case_of'].astext == str(case_of))
            if submitted_by is not None:
                stmt = stmt.where(col(Note.doc_metadata)['submitted_by'].astext == submitted_by)
            if tags:
                stmt = stmt.where(
                    col(Note.doc_metadata)['tags']
                    .astext.cast(JSONB)
                    .contains(literal(json.dumps(tags)).cast(JSONB))
                )
            stmt = (
                stmt.order_by(func.coalesce(Note.publish_date, Note.created_at).desc())
                .offset(offset)
                .limit(limit)
            )
            notes = list((await session.exec(stmt)).all())

            # Attach vault name (single vault, but keep the same shape as notes).
            vault = (await session.exec(select(Vault).where(col(Vault.id) == vault_id))).first()
            vault_name = vault.name if vault else None
            for note in notes:
                object.__setattr__(note, 'vault_name', vault_name)

            if slim:
                for note in notes:
                    object.__setattr__(note, 'summaries', [])
                return notes

            note_ids = [n.id for n in notes]
            summaries_map: dict[UUID, list[BlockSummaryDTO]] = {}
            if note_ids:
                chunk_stmt = (
                    select(Chunk.note_id, Chunk.summary)
                    .where(col(Chunk.note_id).in_(note_ids), col(Chunk.status) == 'active')
                    .order_by(col(Chunk.note_id), col(Chunk.chunk_index))
                )
                for note_id, summary_blob in (await session.exec(chunk_stmt)).all():
                    if summary_blob and isinstance(summary_blob, dict):
                        summaries_map.setdefault(note_id, []).append(
                            BlockSummaryDTO(**summary_blob)
                        )
            for note in notes:
                object.__setattr__(note, 'summaries', summaries_map.get(note.id, []))
            return notes

    async def get_case(self, note_id: UUID) -> Any:
        """Get a single case note by ID.

        Raises :class:`ResourceNotFoundError` if the note does not exist,
        is not in the case vault, or does not have ``role='case'``.
        """
        from memex_common.exceptions import ResourceNotFoundError
        from sqlmodel import select

        from memex_core.memory.sql_models import Note, Vault

        vault_id = await self._resolve_case_vault()

        async with self._api.metastore.session() as session:
            note = await session.get(Note, note_id)
            if note is None or note.vault_id != vault_id or note.role != 'case':
                raise ResourceNotFoundError(f'Case note {note_id} not found.')

            vault = (await session.exec(select(Vault).where(col(Vault.id) == vault_id))).first()
            object.__setattr__(note, 'vault_name', vault.name if vault else None)
            return note

    def _build_note_input(self, payload: CaseSubmit) -> NoteInput:
        """Compose the §5.1 case markdown into the NoteInput that gets
        ingested. Pure function of the payload — no I/O — so the note id is
        content-addressed and predictable (a byte-identical re-submit maps to
        the same idempotency key)."""
        from memex_core.api import NoteInput

        markdown = compose_case_markdown(payload)
        return NoteInput(
            name=payload.title,
            description=f'Case ({payload.outcome}): {payload.trigger[:200]}',
            content=markdown.encode('utf-8'),
            tags=['case', *payload.tags],
            author=payload.submitted_by,
        )

    async def apply_assignment(
        self,
        *,
        note_id: UUID,
        entry_id: UUID,
    ) -> None:
        """Attach a case note to a procedural entry (provenance edge +
        outcome counter + derivation enqueue). Shared by the auto path and
        the ``assign_case`` catalogue action."""
        entry = await self._api._procedural_repo.get(entry_id)
        await self._api._procedural_repo.add_source(
            entry_id,
            ProceduralSourceCreate(source_note_id=note_id, role='provenance'),
        )

        # §18.5: the case IS the outcome report — assigning a case with an
        # outcome bumps the target's success/failure/mixed counters. The
        # outcome rides the case note's metadata (stamped at submission), so
        # every assignment path (explicit case_of, clean auto-assign, the
        # assign_case resolution) records it without threading the payload.
        outcome = await self._note_outcome(note_id)
        if outcome in ('success', 'failure', 'mixed'):
            try:
                await self._api._procedural_repo.record_outcome(entry_id, outcome)
            except Exception:
                logger.exception('outcome record failed for entry %s', entry_id)

        # §18.4 dirty event (a): case assigned → enqueue the procedure.
        # The consumer is Phase 3; pending-row dedup gives debounce.
        try:
            await self._api._procedural_repo.enqueue_derivation(
                vault_id=entry.vault_id,
                source_entry_ids=[entry_id],
                target_kind=entry.kind,
                target_scope=entry.scope,
                target_verb=entry.verb,
                target_context=entry.context,
            )
        except Exception:
            # The edge is the load-bearing artefact; a queue hiccup is
            # log-worthy, not submission-fatal.
            logger.exception('derivation enqueue failed for entry %s', entry_id)

    async def _note_outcome(self, note_id: UUID) -> str | None:
        """The ``outcome`` stamped onto the case note's metadata at
        submission (``success|failure|mixed``), or None."""
        from memex_core.memory.sql_models import Note

        async with self._api.metastore.session() as session:
            note = await session.get(Note, note_id)
            if note is None:
                return None
            val = (note.doc_metadata or {}).get('outcome')
        return str(val).strip().lower() if val else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_case_vault(self) -> UUID:
        """Resolve (or create) the hidden ``procedural`` system vault.

        Normally migration 063 seeds it. But a DB stood up via
        ``SQLModel.metadata.create_all`` (eval-runner resets, some
        tests) skips migrations, so the vault is absent. We ensure it
        here with the correct kind + §18.9.0 policy rather than hard-
        failing — case submission must work regardless of how the
        schema was created. A concurrent create losing the
        name-unique race falls back to the now-present row.
        """
        from memex_core.memory.sql_models import Vault

        async with self._api.metastore.session() as session:
            stmt = (
                select(Vault)
                .where(col(Vault.name) == CASE_VAULT_NAME)
                .where(col(Vault.kind) == 'system')
            )
            vault = (await session.exec(stmt)).first()
        if vault is not None:
            return vault.id

        try:
            created = await self._api._vaults.create_vault(
                name=CASE_VAULT_NAME,
                description='procedural memory plane (system vault).',
                kind='system',
                policy=CASE_VAULT_POLICY,
            )
            return created.id
        except ValueError:
            # Lost the unique-name race (or a non-system vault squats the
            # name). Re-read; require it to be the system vault.
            async with self._api.metastore.session() as session:
                stmt = (
                    select(Vault)
                    .where(col(Vault.name) == CASE_VAULT_NAME)
                    .where(col(Vault.kind) == 'system')
                )
                vault = (await session.exec(stmt)).first()
            if vault is None:
                raise CaseSubmissionError(
                    f'could not resolve or create the {CASE_VAULT_NAME!r} system '
                    'vault (a non-system vault may be squatting the name).'
                )
            return vault.id

    async def _stamp_case_note(self, note_id: UUID, payload: CaseSubmit) -> None:
        from memex_core.memory.sql_models import Note

        async with self._api.metastore.session() as session:
            note = await session.get(Note, note_id)
            if note is None:
                raise CaseSubmissionError(f'ingested note {note_id} not found for stamping')
            note.role = 'case'
            meta = dict(note.doc_metadata or {})
            meta['outcome'] = payload.outcome
            if payload.project_id:
                meta['project'] = payload.project_id
            if payload.submitted_by:
                meta['submitted_by'] = payload.submitted_by
            if payload.case_of:
                meta['case_of'] = str(payload.case_of)
            note.doc_metadata = meta
            flag_modified(note, 'doc_metadata')
            session.add(note)
            await session.commit()

    async def _assign(
        self,
        note_id: UUID,
        vault_id: UUID,
        payload: CaseSubmit,
    ) -> CaseAssignment:
        # Explicit assignment — the primary API (§18.1).
        if payload.case_of is not None:
            await self.apply_assignment(note_id=note_id, entry_id=payload.case_of)
            return CaseAssignment(mode='explicit', entry_id=payload.case_of)

        # Stage 1: candidates by trigger search.
        candidates = await self._candidates(payload)

        # Stage 2: judge — any failure is the escalation path (fail-safe).
        judgment: AssignmentJudgment | None = None
        try:
            judgment = await judge_assignment(
                self._judge_lm(),
                case_trigger=payload.trigger,
                case_summary=self._case_summary(payload),
                candidates=candidates,
                existing_verbs=await self._existing_verbs(payload),
            )
        except Exception as exc:
            logger.warning('assignment judge unavailable (%s); escalating to lint', exc)

        if judgment is not None and judgment.is_clean:
            if judgment.decision == 'instance_of' and judgment.target_entry_id:
                entry_id = UUID(judgment.target_entry_id)
                await self.apply_assignment(note_id=note_id, entry_id=entry_id)
                return CaseAssignment(
                    mode='auto_assigned',
                    entry_id=entry_id,
                    decision=judgment.decision,
                    separation=judgment.separation,
                    reasoning=judgment.reasoning,
                )
            draft = await self._create_draft_anchor(payload, judgment)
            if draft is not None:
                await self.apply_assignment(note_id=note_id, entry_id=draft.id)
                # Surface the fresh anchor for activation NOW (draft → published
                # via the lint queue) rather than waiting on the Phase-3
                # derivation worker to distill + file it. Without this, a
                # case-spawned procedure sits invisible with no lint item.
                finding_id = await self._file_activation_proposal(draft.id, vault_id, payload)
                return CaseAssignment(
                    mode='new_procedure_draft',
                    entry_id=draft.id,
                    finding_id=finding_id,
                    decision=judgment.decision,
                    separation=judgment.separation,
                    reasoning=judgment.reasoning,
                )
            # Anchor proposal was malformed — contested outcome.

        finding_id = await self._escalate(note_id, vault_id, payload, candidates, judgment)
        return CaseAssignment(
            mode='escalated',
            finding_id=finding_id,
            decision=judgment.decision if judgment else None,
            separation=judgment.separation if judgment else None,
            reasoning=judgment.reasoning if judgment else None,
        )

    async def _candidates(self, payload: CaseSubmit) -> list[AssignmentCandidate]:
        response = await self._api._procedural_search.search(
            ProceduralSearchRequest(
                query=payload.trigger,
                kind='procedure',
                status='published',
                limit=_CANDIDATE_LIMIT,
            )
        )
        out: list[AssignmentCandidate] = []
        for hit in response.hits:
            entry = hit.entry
            out.append(
                AssignmentCandidate(
                    entry_id=str(entry.id),
                    verb=entry.verb or '',
                    context=entry.context or '',
                    scope=entry.scope,
                    trigger=entry.trigger or '',
                    title=entry.title,
                )
            )
        return out

    async def _existing_verbs(self, payload: CaseSubmit) -> list[str]:
        from memex_core.memory.sql_models import ProceduralEntry

        scopes = ['global']
        if payload.project_id:
            scopes.append(f'project:{payload.project_id}')
        async with self._api.metastore.session() as session:
            stmt = (
                select(ProceduralEntry.verb)
                .where(col(ProceduralEntry.scope).in_(scopes))
                .where(col(ProceduralEntry.verb).is_not(None))
                .distinct()
            )
            rows = (await session.exec(stmt)).all()
        return [str(v) for v in rows if v]

    def _case_summary(self, payload: CaseSubmit) -> str:
        steps = '; '.join(payload.actions[:6])
        return f'{payload.title} — outcome: {payload.outcome}. Actions: {steps or "(none)"}'

    def _judge_lm(self) -> dspy.LM:
        model_config = self._api.config.server.memory.extraction.model
        if model_config is None:
            raise CaseSubmissionError('no LLM configured for the assignment judge')
        return dspy.LM(
            model=model_config.model,
            api_base=str(model_config.base_url) if model_config.base_url else None,
            api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
            timeout=model_config.timeout,
            num_retries=model_config.num_retries,
        )

    async def _create_draft_anchor(
        self,
        payload: CaseSubmit,
        judgment: AssignmentJudgment,
    ) -> ProceduralEntryDTO | None:
        """Create the draft procedure anchor a clean ``new_procedure``
        verdict proposes. Body stays empty — distillation fills it; the
        draft is invisible to search/briefing until promoted.

        Uses ``create`` (NOT ``upsert``): if the proposed anchor already
        exists — e.g. the judge's "new" verdict was a candidate-recall
        miss against a real published procedure — the create 409s and we
        escalate (return None) rather than silently demoting + overwriting
        that live procedure with a stub draft."""
        from memex_core.services.procedural_repository import ProceduralIdentityConflict

        verb = judgment.proposed_verb or ''
        context = judgment.proposed_context or ''
        if not ANCHOR_LABEL_PATTERN.match(verb) or not ANCHOR_LABEL_PATTERN.match(context):
            logger.warning(
                'new_procedure verdict with malformed anchor (%r, %r); escalating',
                verb,
                context,
            )
            return None
        scope = f'project:{payload.project_id}' if payload.project_id else 'global'
        try:
            return await self._api.procedural.create(
                ProceduralEntryCreate(
                    vault_id=await self._resolve_case_vault(),
                    kind='procedure',
                    scope=scope,
                    verb=verb,
                    context=context,
                    title=payload.title,
                    summary=f'Draft anchor seeded by case assignment ({payload.outcome}).',
                    trigger=payload.trigger,
                    status='draft',
                    origin='derived',
                )
            )
        except ProceduralIdentityConflict:
            logger.info(
                'new_procedure anchor (%s, %s, %s) already exists — escalating '
                'instead of overwriting the live entry',
                scope,
                verb,
                context,
            )
            return None
        except Exception:
            logger.exception('draft anchor creation failed; escalating')
            return None

    async def _file_activation_proposal(
        self,
        entry_id: UUID,
        vault_id: UUID,
        payload: CaseSubmit,
    ) -> UUID | None:
        """File the governance lint item that activates a case-spawned draft
        procedure (draft → published) via ``activate_procedural_entry``.

        Filed at submission so the new anchor is reviewable in ``memex lint
        review`` immediately, not only after the Phase-3 derivation worker
        distills it. Reuses the derivation rule name + (target, vault) so the
        partial-unique pending index collapses a later distillation re-file
        onto this row (no double-nag); the post-resolution cooldown stops a
        re-nag once a reviewer has activated it. Best-effort: a filing failure
        is logged, never fatal — the draft is a valid entry, activatable by
        any other path."""
        from memex_core.services.lint_external import (
            ExternalProposalRequest,
            insert_external_proposal,
        )
        from memex_core.services.procedural_derivation_service import (
            DISTILLATION_RULE_NAME,
        )

        try:
            req = ExternalProposalRequest(
                rule_name=DISTILLATION_RULE_NAME,
                lint_type='governance',
                target_type='procedural_entry',
                target_id=str(entry_id),
                description=(
                    f'New procedure anchor from case, ready to activate: {payload.title[:140]}'
                ),
                suggested_action=(
                    'Review the new procedure anchor and activate it '
                    '(draft → published) via activate_procedural_entry.'
                ),
                vault_id=str(vault_id),
                # Keep evidence to a compact one-line preview; the reviewer reads
                # the full procedure + source case in the cockpit detail view (it
                # fetches the entry by target_id), not from a stuffed evidence blob.
                evidence={
                    'procedure_title': payload.title,
                    'case_outcome': payload.outcome,
                },
                proposed_action={'action_name': 'activate_procedural_entry', 'params': {}},
            )
            status, finding_id = await insert_external_proposal(
                self._api,
                req,
                vault_id=vault_id,
                actor=payload.submitted_by or 'case_submit',
            )
            logger.info('case filed activation proposal: %s (finding=%s)', status, finding_id)
            return finding_id
        except Exception:
            # The draft anchor is already created + attached — a missing lint
            # breadcrumb only delays activation, never loses the procedure.
            logger.exception('failed to file activation proposal for draft %s', entry_id)
            return None

    async def _escalate(
        self,
        note_id: UUID,
        vault_id: UUID,
        payload: CaseSubmit,
        candidates: list[AssignmentCandidate],
        judgment: AssignmentJudgment | None,
    ) -> UUID | None:
        """File the contested assignment on the lint surface (§18.6 /
        §19.7). Targets the case NOTE; ``assign_case`` is pre-selected
        with the judge's lean as default params."""
        from memex_core.services.lint_external import (
            ExternalProposalRequest,
            insert_external_proposal,
        )

        lean: dict[str, Any] = {}
        if judgment is not None:
            if judgment.decision == 'instance_of' and judgment.target_entry_id:
                lean = {'mode': 'assign', 'entry_id': judgment.target_entry_id}
            elif judgment.decision == 'new_procedure':
                lean = {
                    'mode': 'new_procedure',
                    'verb': judgment.proposed_verb or '',
                    'context': judgment.proposed_context or '',
                    'scope': f'project:{payload.project_id}' if payload.project_id else 'global',
                    'title': payload.title,
                }

        evidence: dict[str, Any] = {
            'case_trigger': payload.trigger,
            'case_outcome': payload.outcome,
            'candidates': [c.model_dump() for c in candidates],
            'judgment': judgment.as_dict() if judgment else None,
        }
        try:
            req = ExternalProposalRequest(
                rule_name=ASSIGNMENT_RULE_NAME,
                lint_type='governance',
                target_type='note',
                target_id=str(note_id),
                description=(
                    f'Case assignment contested (separation='
                    f'{judgment.separation if judgment else "judge_unavailable"}): '
                    f'{payload.title[:140]}'
                ),
                suggested_action=(
                    'Review the candidates and assign the case to a procedure '
                    '(or accept the proposed new anchor) via assign_case.'
                ),
                vault_id=str(vault_id),
                evidence=evidence,
                proposed_action=({'action_name': 'assign_case', 'params': lean} if lean else None),
            )
            status, finding_id = await insert_external_proposal(
                self._api,
                req,
                vault_id=vault_id,
                actor=payload.submitted_by or 'case_submit',
            )
            logger.info('case assignment escalated: %s (finding=%s)', status, finding_id)
            return finding_id
        except Exception:
            # The case note is already filed — escalation failure loses
            # only the review breadcrumb, never the case.
            logger.exception('failed to file assignment escalation for note %s', note_id)
            return None


__all__ = [
    'ASSIGNMENT_RULE_NAME',
    'CASE_VAULT_NAME',
    'CaseService',
    'CaseSubmissionError',
    'compose_case_markdown',
]
