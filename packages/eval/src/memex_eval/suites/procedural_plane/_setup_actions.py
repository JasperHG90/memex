"""Suite-private setup actions for ``procedural_plane``.

The procedural plane has 8 HTTP routes. Scenarios that need a
known state pre-seed (search hits, briefing cards, identity-anchor
collisions) call ``procedural_upsert`` to write a deterministic
entry. Direct write via the API bypasses the LLM extraction path; it is a first-class write surface, so the seeding is faithful to
the production write shape.

The seeded entry IDs are deterministic UUIDv5 derived from the
(kind, scope, verb, context) anchor + the entry title, so re-running
the suite produces the same IDs across machines (the procedural contract
permits a different entry UUID for the same anchor — only the anchor
is UNIQUE — but for eval-test repeatability we want the IDs
stable).

The handler is ``required=True`` so a write failure flips the
scenario to status='error' (not soft-logged). A 409 collision with
pre-existing state would otherwise be silently absorbed.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from memex_eval.suite.setup_actions import (
    SetupActionHandler,
    register_setup_action,
)

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.suites.procedural_plane')


# Fixed namespace UUID for procedural-plane eval test entries.
# Distinct from any production namespace; this is purely a
# repeatability anchor.
_PROC_NS = uuid.UUID('f1a2b3c4-d5e6-4a7b-8c9d-0e1f2a3b4c5d')


def _deterministic_entry_id(
    kind: str, scope: str, verb: str | None, context: str | None, title: str
) -> UUID:
    """Stable UUIDv5 over the identity-anchor + title.

    The procedural contract permits the entry UUID to be different from
    the anchor — but for eval-test repeatability, a fixed UUID
    means the suite baselines survive cross-machine runs."""
    parts = [kind, scope, verb or '', context or '', title]
    return uuid.uuid5(_PROC_NS, '\x00'.join(parts))


@register_setup_action('procedural_upsert')
class _ProceduralUpsert(SetupActionHandler):
    """Seed a procedural-plane entry via the ``procedural_upsert``
    API call.

    Param names are prefixed with ``kind_`` to avoid colliding with
    the ``SetupAction.kind`` discriminator (the action kind itself,
    e.g. ``'procedural_upsert'``). When the runner builds the params
    dict from ``SetupAction.model_dump()`` the discriminator lands
    under ``params['kind']`` — reading that here would conflate
    "what action is this" with "what procedure kind should I write".
    The prefix convention (documented in ``.claude/rules/eval-suites.md``
    under ``eval-framework-first-pass``) keeps the two clean.

    Required params (with the ``kind_`` prefix):
    - ``kind_kind`` (procedure | strategy — cases are NOTES via case_submit)
    - ``kind_scope`` (global | project:<id> | app:<id> — no user scope)
    - ``kind_verb`` (required for both kinds)
    - ``kind_context`` (required for procedure; FORBIDDEN for strategy)
    - ``kind_title`` (str)
    - ``kind_trigger`` (the when_to_use phrase — required by the DTO)
    - ``kind_summary`` (str, optional)

    Optional params:
    - ``kind_status`` ('published' | 'draft', default 'published')
    - ``pin_to`` (context-key str, default None) — when set, pins the
      upserted entry into that context-binding chain after the write.
      The briefing-cards endpoint only surfaces PINNED entries, so any
      scenario that gates ``briefing_cards`` MUST pin its seed into the
      context key it queries (e.g. 'global', 'project:proc-eval').
    - ``deprecate_after`` (bool, default False) — when True, the
      action immediately deprecates the entry it just upserted.
      Used by the ``deprecate_drops_from_published_search`` scenario
      to land the seeded entry in ``status='deprecated'`` BEFORE
      the search call fires. Combining upsert+deprecate into one
      action avoids the framework's lack of context-reference
      substitution between sequential setup actions.

    Returns ``{'entry_id': <uuid>, 'identity_anchor': <str>}`` so
    scenarios can reference the seeded entry by ID.
    """

    required: ClassVar[bool] = True

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any]:
        # See class docstring for why every entry-shape param is
        # ``kind_<name>`` rather than ``<name>``. The prefix is
        # load-bearing: ``params['kind']`` is the SetupAction
        # discriminator itself, NOT the procedure kind.
        kind = (params.get('kind_kind') or '').strip()
        scope = (params.get('kind_scope') or '').strip()
        verb = params.get('kind_verb')
        context = params.get('kind_context')
        title = (params.get('kind_title') or '').strip()
        deprecate_after = bool(params.get('deprecate_after', False))
        pin_to = params.get('pin_to')
        if not kind or not scope or not title:
            raise ValueError(
                'procedural_upsert setup action requires kind_kind, kind_scope, '
                f'and kind_title. Got: kind_kind={kind!r}, kind_scope={scope!r}, '
                f'kind_title={title!r}.'
            )

        # Build the payload in the same shape the DTO accepts.
        # ``procedural_upsert`` is idempotent on the identity anchor,
        # so re-running the suite on a dirty vault produces the
        # same state (idempotent by design).
        #
        # ``vault_id`` (the runner-supplied scenario vault), ``summary``,
        # and ``trigger`` are REQUIRED by the DTO. We default summary/trigger
        # from the title when a scenario doesn't supply them so the seed
        # always validates.
        from memex_common.procedural_schemas import ProceduralEntryCreate

        payload: dict[str, Any] = {
            'vault_id': str(vault_id),
            'kind': kind,
            'scope': scope,
            'title': title,
            'summary': params.get('kind_summary') or f'Eval seed: {title}.',
            'trigger': params.get('kind_trigger') or f'when to {title.lower()}',
        }
        if verb is not None:
            payload['verb'] = verb
        if context is not None:
            payload['context'] = context
        if 'kind_status' in params:
            payload['status'] = params['kind_status']

        # Construct via Pydantic so any required-field validation
        # surfaces here (not deep in the API call).
        try:
            create = ProceduralEntryCreate.model_validate(payload)
        except Exception as exc:
            raise ValueError(
                f'procedural_upsert: payload validation failed: {exc}. Payload was: {payload!r}'
            ) from exc

        dto = await api.procedural_upsert(create)
        entry_id = UUID(str(dto.id))

        # Optional pin into a briefing context-binding chain. The
        # briefing-cards endpoint only surfaces PINNED entries, so a
        # scenario that gates briefing_cards must pin the seed into the
        # context key it queries (e.g. 'global', 'project:proc-eval').
        # Append (position=None); the server enforces the per-context cap.
        if pin_to:
            try:
                await api.procedural_pin(entry_id, context_key=str(pin_to))
            except Exception as exc:
                raise ValueError(
                    f'procedural_upsert: pin_to={pin_to!r} failed for {entry_id}: {exc}'
                ) from exc

        # Optional immediate deprecate — flips the entry to
        # ``status='deprecated'`` so the search call (which filters
        # by default ``status='published'``) sees the post-deprecation
        # state. Without this, the test couldn't gate the
        # "deprecate drops from search" contract because the entry
        # would still be published when the search runs.
        if deprecate_after:
            try:
                await api.procedural_deprecate(entry_id=entry_id)
            except Exception as exc:
                logger.warning(
                    'procedural_upsert: deprecate_after=%s failed for %s: %s',
                    deprecate_after,
                    entry_id,
                    exc,
                )

        # Build the identity-anchor string for cross-scenario
        # debuggability. Format: ``kind/scope[/verb[/context]]``
        # — verb/context omitted when NULL.
        anchor_parts = [kind, scope]
        if verb is not None:
            anchor_parts.append(verb)
        if context is not None:
            anchor_parts.append(context)
        identity_anchor = '/'.join(anchor_parts)

        result: dict[str, Any] = {
            'entry_id': str(entry_id),
            'identity_anchor': identity_anchor,
        }
        if pin_to:
            # Recorded so teardown can UNPIN (not just deprecate) — pins are
            # keyed by context_key, which is shared across scenarios. The
            # append-position is computed as max(position)+1 over ALL pins in
            # the context (deprecated entries included), so a leaked pin from a
            # prior scenario would push this scenario's pin off position 0 and
            # break the pin-position-order contract. Unpinning in teardown
            # keeps each scenario's briefing chain isolated.
            result['pinned_context'] = str(pin_to)
        return result

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Best-effort deprecate the seeded entry.

        Deprecation is preferred over deletion: the procedural contract
        treats deprecation as the lifecycle exit (status →
        'deprecated'), and a future test-reuse scenario (``--reuse-vault``)
        would otherwise see the seeded entry re-appear in any
        status='all' search.
        """
        ctx = setup_context or {}
        entry_id = ctx.get('entry_id') or ctx.get('procedural_upsert.entry_id')
        if not entry_id:
            return
        # Unpin FIRST so the pin state doesn't leak into later scenarios that
        # share the same context_key chain (see run() for why position leakage
        # breaks the pin-position-order contract). Deprecation alone leaves the
        # pin row in place, and append-position is computed over all pins.
        pinned_context = ctx.get('pinned_context') or ctx.get('procedural_upsert.pinned_context')
        if pinned_context:
            try:
                await api.procedural_unpin(UUID(str(entry_id)), context_key=str(pinned_context))
            except Exception as exc:
                logger.warning(
                    'procedural_upsert teardown: unpin(%s from %s) failed: %s',
                    entry_id,
                    pinned_context,
                    exc,
                )
        try:
            await api.procedural_deprecate(entry_id=UUID(str(entry_id)))
        except Exception as exc:
            logger.warning(
                'procedural_upsert teardown: deprecate(%s) failed: %s',
                entry_id,
                exc,
            )


@register_setup_action('derive_from_cases')
class _DeriveFromCases(SetupActionHandler):
    """Exercise the full cases → procedure derivation pipeline (design §9).

    1. Create a draft procedure anchor (the empty stub assignment would
       create).
    2. Submit ``kind_n_cases`` worked episodes via ``case_submit`` with an
       explicit ``case_of`` (direct assignment — deterministic, no judge),
       each carrying the same load-bearing quantitative anchor in its
       actions (e.g. "roll the canary at 10%").
    3. Trigger derivation (``POST /procedural/derive``) — the §9 distillation
       pass fills the draft's body/trigger/summary.
    4. GATE: assert the distilled body is non-empty AND preserves every
       expected quantitative anchor verbatim (§9 rule 6 / §19.5 — the one
       failure mode the spike found). ``required=True``, so a regression in
       distillation fidelity errors the scenario.
    5. Promote the entry draft → published (the §9 confirm step) so the
       paired ``procedural_entry_roundtrip`` outcome can retrieve it.

    Params (``kind_``-prefixed, per the suite convention):
    - ``kind_scope`` / ``kind_verb`` / ``kind_context`` — the procedure anchor.
    - ``kind_trigger`` — the stub's when_to_use.
    - ``kind_n_cases`` (int, default 1 — one case is enough to derive a
      procedure; §9 amended, JG 2026-06-11).
    - ``kind_anchors`` (list[str]) — quantitative tokens that MUST survive
      distillation; embedded in the case actions and asserted in the body.

    Returns ``{'entry_id', 'identity_anchor', 'derived_body'}``.
    """

    required: ClassVar[bool] = True

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any]:
        from memex_common.procedural_schemas import (
            CaseSubmit,
            ProceduralEntryCreate,
            ProceduralEntryUpdate,
        )

        scope = (params.get('kind_scope') or 'global').strip()
        verb = (params.get('kind_verb') or '').strip()
        context = (params.get('kind_context') or '').strip()
        trigger = params.get('kind_trigger') or f'about to {verb} on {context}'
        n_cases = int(params.get('kind_n_cases') or 1)
        anchors: list[str] = list(params.get('kind_anchors') or ['10%', '15 minutes'])
        if not verb or not context:
            raise ValueError('derive_from_cases requires kind_verb and kind_context')

        # 1. Draft anchor (the empty stub assignment creates).
        draft = await api.procedural_create(
            ProceduralEntryCreate(
                vault_id=vault_id,
                kind='procedure',
                scope=scope,
                verb=verb,
                context=context,
                title=f'draft {verb}:{context}',
                summary='draft stub awaiting distillation',
                body='',
                trigger=trigger,
                status='draft',
                origin='derived',
            )
        )
        entry_id = UUID(str(draft.id))

        # 2. N worked episodes, each citing the load-bearing anchors so the
        #    distiller has shared, quantitative signal to preserve.
        anchor_phrase = ', '.join(anchors)
        for i in range(n_cases):
            await api.case_submit(
                CaseSubmit(
                    title=f'{verb} {context} run {i}',
                    trigger=trigger,
                    situation=f'Routine {verb} on {context}; case {i}.',
                    actions=[
                        f'Roll out the change and watch the canary at {anchors[0]}.',
                        f'Hold the watch window for {anchors[1] if len(anchors) > 1 else "the window"}.',
                        f'Promote once healthy (anchors: {anchor_phrase}).',
                    ],
                    outcome='success',
                    lesson=f'Keep the canary at {anchors[0]} before promoting.',
                    case_of=entry_id,
                )
            )

        # 3. Drain the derivation queue — distils the procedure.
        await api.procedural_derive(limit=20)

        # 4. GATE: the distilled body must exist and preserve the anchors.
        derived = await api.procedural_get(entry_id)
        body = derived.body or ''
        if not body.strip():
            raise ValueError(
                f'derive_from_cases: distillation produced an empty body for '
                f'{scope}/{verb}/{context} after {n_cases} case(s) (§9 single-case path).'
            )
        missing = [a for a in anchors if a not in body]
        if missing:
            raise ValueError(
                f'derive_from_cases: distilled body dropped quantitative anchors '
                f'{missing} (§9 rule 6 / §19.5). Body was: {body[:400]!r}'
            )

        # 5. Confirm (draft → published) so the entry is retrievable.
        await api.procedural_update(entry_id, ProceduralEntryUpdate(status='published'))

        return {
            'entry_id': str(entry_id),
            'identity_anchor': f'{scope}/{verb}/{context}',
            'derived_body': body,
        }

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        ctx = setup_context or {}
        entry_id = ctx.get('entry_id') or ctx.get('derive_from_cases.entry_id')
        if not entry_id:
            return
        try:
            await api.procedural_deprecate(entry_id=UUID(str(entry_id)))
        except Exception as exc:
            logger.warning('derive_from_cases teardown: deprecate(%s) failed: %s', entry_id, exc)
