"""Apply / reverse a contradiction winner-proposal lint finding.

A ``propose_contradiction_winner`` finding nominates a winner / loser
between two memory units that FSFM lint has flagged as in tension. Each
finding carries an ``action`` literal under ``evidence.action``:

- ``mark_loser_stale`` — flip the loser MemoryUnit.status to ``'stale'``.
- ``supersede_loser_note`` — set the loser's parent Note.superseded_by
  to the winner's parent note id. Falls back to ``mark_loser_stale``
  (and records the fallback) when both units share the same note.
- ``refine_not_contradict`` — rewrite the inbound MemoryLink.link_type
  from ``'contradicts'`` to ``'refines'`` (graph-pressure weight 0.0).
- ``inconclusive`` — no-op mutation; just flips the finding to resolved.

Each apply captures ``prior_state`` under ``evidence.resolution`` so the
reverse path can restore exactly what was changed. Reverse writes a
``reversal`` row alongside the resolved one (status stays resolved so
the unique partial index on pending findings is not violated).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from memex_core import metrics as _metrics

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.services.contradiction_resolution')


_LOAD_PROPOSAL_SQL = text("""
    SELECT id::text AS id,
           vault_id::text AS vault_id,
           rule_name,
           target_type,
           target_id,
           evidence,
           status
    FROM maintenance_proposals
    WHERE id = :finding_id
""")


_UPDATE_RESOLVED_SQL = text("""
    UPDATE maintenance_proposals
    SET status = 'resolved',
        resolved_at = now(),
        resolved_by = :actor,
        evidence = :evidence
    WHERE id = :finding_id
      AND status = 'pending'
""")


_UPDATE_EVIDENCE_SQL = text("""
    UPDATE maintenance_proposals
    SET evidence = :evidence
    WHERE id = :finding_id
""")


_INSERT_REVERSAL_SQL = text("""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source,
        resolved_at, resolved_by
    )
    VALUES (
        :vault_id, :lint_type, :target_type, :target_id,
        :rule_name, CAST(:evidence AS jsonb), :suggested_action,
        'resolved', 'llm', now(), :actor
    )
""")


_LOAD_UNIT_SQL = text("""
    SELECT id::text AS id, status, note_id::text AS note_id
    FROM memory_units WHERE id = :unit_id
""")


_UPDATE_UNIT_STATUS_SQL = text("""
    UPDATE memory_units SET status = :status WHERE id = :unit_id
""")


_LOAD_NOTE_SQL = text("""
    SELECT id::text AS id, superseded_by::text AS superseded_by
    FROM notes WHERE id = :note_id
""")


_UPDATE_NOTE_SUPERSEDED_BY_SQL = text("""
    UPDATE notes SET superseded_by = :superseded_by WHERE id = :note_id
""")


_UPDATE_NOTE_SUPERSEDED_BY_NULL_SQL = text("""
    UPDATE notes SET superseded_by = NULL WHERE id = :note_id
""")


_LOAD_LINK_SQL = text("""
    SELECT id::text AS id, link_type, from_unit_id::text AS from_unit_id,
           to_unit_id::text AS to_unit_id
    FROM memory_links WHERE id = :link_id
""")


_UPDATE_LINK_TYPE_SQL = text("""
    UPDATE memory_links SET link_type = :link_type WHERE id = :link_id
""")


class ContradictionResolutionError(RuntimeError):
    """Raised when apply/reverse cannot be performed safely."""


def _coerce_evidence(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


async def apply_winner_proposal(
    api: 'MemexAPI',
    finding_id: UUID,
    *,
    vault_id: UUID | None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply the action recorded on a winner-proposal finding.

    Captures pre-mutation state under ``evidence.resolution.prior_state``
    so :func:`reverse_winner_proposal` can undo the change atomically.
    Flips the finding status to ``resolved`` in the same transaction.
    """
    if not actor:
        raise ContradictionResolutionError('actor required to record the resolution audit trail')
    if vault_id is None:
        raise ContradictionResolutionError('vault_id required to apply a winner proposal')
    async with api.metastore.session() as session:
        proposal = (
            await session.execute(_LOAD_PROPOSAL_SQL, {'finding_id': str(finding_id)})
        ).first()
        if proposal is None:
            raise ContradictionResolutionError(f'finding {finding_id} not found')
        if proposal.rule_name != 'propose_contradiction_winner':
            raise ContradictionResolutionError(
                f'finding {finding_id} is not a propose_contradiction_winner '
                f'finding (rule_name={proposal.rule_name!r})'
            )
        if proposal.status != 'pending':
            raise ContradictionResolutionError(
                f'finding {finding_id} is not pending (status={proposal.status!r})'
            )
        if proposal.vault_id is not None and str(vault_id) != proposal.vault_id:
            raise ContradictionResolutionError('vault_id mismatch between caller and finding')

        evidence = _coerce_evidence(proposal.evidence)
        action = str(evidence.get('action') or 'inconclusive')
        winner_unit_id = evidence.get('winner_unit_id')
        loser_unit_id = evidence.get('loser_unit_id') or proposal.target_id
        link_id = evidence.get('link_id')

        prior_state: dict[str, Any] = {}
        applied: dict[str, Any] = {'action': action}
        fallback_reason: str | None = None

        loser_row = (await session.execute(_LOAD_UNIT_SQL, {'unit_id': str(loser_unit_id)})).first()
        if loser_row is None:
            raise ContradictionResolutionError(f'loser unit {loser_unit_id} no longer exists')

        winner_row = None
        if winner_unit_id:
            winner_row = (
                await session.execute(_LOAD_UNIT_SQL, {'unit_id': str(winner_unit_id)})
            ).first()

        effective_action = action
        if action == 'supersede_loser_note':
            if winner_row is None:
                raise ContradictionResolutionError('supersede_loser_note requires a winner unit')
            if loser_row.note_id == winner_row.note_id:
                effective_action = 'mark_loser_stale'
                fallback_reason = 'shared_parent_note'

        if effective_action == 'mark_loser_stale':
            prior_state['loser_unit_status'] = loser_row.status
            await session.execute(
                _UPDATE_UNIT_STATUS_SQL,
                {'unit_id': str(loser_unit_id), 'status': 'stale'},
            )
            applied['loser_unit_status'] = 'stale'

        elif effective_action == 'supersede_loser_note':
            if loser_row.note_id is None or winner_row is None or winner_row.note_id is None:
                raise ContradictionResolutionError(
                    'both units must have note_id set to supersede a note'
                )
            note_row = (
                await session.execute(_LOAD_NOTE_SQL, {'note_id': loser_row.note_id})
            ).first()
            prior_state['loser_note_id'] = loser_row.note_id
            prior_state['loser_note_superseded_by'] = (
                note_row.superseded_by if note_row is not None else None
            )
            await session.execute(
                _UPDATE_NOTE_SUPERSEDED_BY_SQL,
                {
                    'note_id': loser_row.note_id,
                    'superseded_by': winner_row.note_id,
                },
            )
            applied['loser_note_superseded_by'] = winner_row.note_id

        elif effective_action == 'refine_not_contradict':
            if not link_id:
                raise ContradictionResolutionError(
                    'refine_not_contradict requires link_id in evidence'
                )
            link_row = (await session.execute(_LOAD_LINK_SQL, {'link_id': str(link_id)})).first()
            if link_row is None:
                raise ContradictionResolutionError(f'link {link_id} no longer exists')
            prior_state['link_id'] = link_row.id
            prior_state['link_type'] = link_row.link_type
            await session.execute(
                _UPDATE_LINK_TYPE_SQL,
                {'link_id': str(link_id), 'link_type': 'refines'},
            )
            applied['link_type'] = 'refines'

        elif effective_action == 'inconclusive':
            applied['noop'] = True

        else:
            raise ContradictionResolutionError(f'unknown action: {action!r}')

        resolution = {
            'action': action,
            'effective_action': effective_action,
            'actor': actor,
            'prior_state': prior_state,
            'applied': applied,
        }
        if fallback_reason is not None:
            resolution['fallback_reason'] = fallback_reason
        evidence['resolution'] = resolution

        result = await session.execute(
            _UPDATE_RESOLVED_SQL,
            {
                'finding_id': str(finding_id),
                'actor': actor,
                'evidence': json.dumps(evidence),
            },
        )
        if not result.rowcount:
            await session.rollback()
            raise ContradictionResolutionError(
                f'finding {finding_id} could not be flipped to resolved'
            )

        await session.commit()

    try:
        _metrics.CONTRADICTION_RESOLUTION_APPLIED_TOTAL.labels(
            action=effective_action,
            vault_id=proposal.vault_id if proposal.vault_id is not None else 'global',
        ).inc()
    except Exception:
        logger.debug('metrics increment failed', exc_info=True)

    logger.info(
        'contradiction_resolution.applied',
        extra={
            'finding_id': str(finding_id),
            'loser_unit_id': str(loser_unit_id),
            'effective_action': effective_action,
            'fallback_reason': fallback_reason,
        },
    )

    return {
        'finding_id': str(finding_id),
        'status': 'resolved',
        'effective_action': effective_action,
        'fallback_reason': fallback_reason,
        'applied': applied,
    }


async def reverse_winner_proposal(
    api: 'MemexAPI',
    finding_id: UUID,
    *,
    vault_id: UUID | None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Undo a previously applied winner-proposal.

    Reads ``evidence.resolution.prior_state``, restores the affected
    rows, and writes a paired ``reversal`` MaintenanceProposal row.
    The original resolved finding is NOT flipped back to pending —
    that would violate the partial unique index on pending findings.
    Instead the original carries ``evidence.resolution.reversed_at``.
    """
    if not actor:
        raise ContradictionResolutionError('actor required to record the resolution audit trail')
    if vault_id is None:
        raise ContradictionResolutionError('vault_id required to reverse a winner proposal')
    async with api.metastore.session() as session:
        proposal = (
            await session.execute(_LOAD_PROPOSAL_SQL, {'finding_id': str(finding_id)})
        ).first()
        if proposal is None:
            raise ContradictionResolutionError(f'finding {finding_id} not found')
        if proposal.rule_name != 'propose_contradiction_winner':
            raise ContradictionResolutionError(
                f'finding {finding_id} is not a propose_contradiction_winner finding'
            )
        if proposal.status != 'resolved':
            raise ContradictionResolutionError(
                f'finding {finding_id} is not resolved (status={proposal.status!r})'
            )
        if proposal.vault_id is not None and str(vault_id) != proposal.vault_id:
            raise ContradictionResolutionError('vault_id mismatch between caller and finding')

        evidence = _coerce_evidence(proposal.evidence)
        resolution = evidence.get('resolution') or {}
        if resolution.get('reversed_at') is not None:
            raise ContradictionResolutionError(f'finding {finding_id} has already been reversed')
        effective_action = str(resolution.get('effective_action') or '')
        prior_state = resolution.get('prior_state') or {}

        if effective_action == 'mark_loser_stale':
            loser_unit_id = evidence.get('loser_unit_id') or proposal.target_id
            prev_status = prior_state.get('loser_unit_status', 'active')
            await session.execute(
                _UPDATE_UNIT_STATUS_SQL,
                {'unit_id': str(loser_unit_id), 'status': prev_status},
            )

        elif effective_action == 'supersede_loser_note':
            note_id = prior_state.get('loser_note_id')
            prev_superseded_by = prior_state.get('loser_note_superseded_by')
            if note_id is None:
                raise ContradictionResolutionError(
                    'cannot reverse supersede_loser_note without prior_state.loser_note_id'
                )
            if prev_superseded_by is None:
                await session.execute(
                    _UPDATE_NOTE_SUPERSEDED_BY_NULL_SQL,
                    {'note_id': str(note_id)},
                )
            else:
                await session.execute(
                    _UPDATE_NOTE_SUPERSEDED_BY_SQL,
                    {
                        'note_id': str(note_id),
                        'superseded_by': str(prev_superseded_by),
                    },
                )

        elif effective_action == 'refine_not_contradict':
            link_id = prior_state.get('link_id')
            prev_link_type = prior_state.get('link_type', 'contradicts')
            if link_id is None:
                raise ContradictionResolutionError(
                    'cannot reverse refine_not_contradict without prior_state.link_id'
                )
            await session.execute(
                _UPDATE_LINK_TYPE_SQL,
                {'link_id': str(link_id), 'link_type': prev_link_type},
            )

        elif effective_action == 'inconclusive':
            pass

        else:
            raise ContradictionResolutionError(
                f'unknown effective_action on resolved finding: {effective_action!r}'
            )

        from datetime import datetime, timezone

        resolution['reversed_at'] = datetime.now(timezone.utc).isoformat()
        resolution['reversal_actor'] = actor
        evidence['resolution'] = resolution

        await session.execute(
            _UPDATE_EVIDENCE_SQL,
            {'finding_id': str(finding_id), 'evidence': json.dumps(evidence)},
        )

        reversal_evidence = {
            'reverses_finding_id': str(finding_id),
            'effective_action': effective_action,
            'prior_state': prior_state,
        }
        await session.execute(
            _INSERT_REVERSAL_SQL,
            {
                'vault_id': proposal.vault_id,
                'lint_type': 'quality',
                'target_type': proposal.target_type,
                'target_id': proposal.target_id,
                'rule_name': 'propose_contradiction_winner_reversal',
                'evidence': json.dumps(reversal_evidence),
                'suggested_action': ('Audit row: reverses a previously applied winner-proposal.'),
                'actor': actor,
            },
        )

        await session.commit()

    try:
        _metrics.CONTRADICTION_RESOLUTION_REVERSED_TOTAL.labels(
            vault_id=proposal.vault_id if proposal.vault_id is not None else 'global',
        ).inc()
    except Exception:
        logger.debug('metrics increment failed', exc_info=True)

    logger.warning(
        'contradiction_resolution.reversed',
        extra={
            'finding_id': str(finding_id),
            'effective_action': effective_action,
        },
    )

    return {
        'finding_id': str(finding_id),
        'status': 'reversed',
        'effective_action': effective_action,
    }
