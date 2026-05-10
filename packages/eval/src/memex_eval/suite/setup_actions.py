"""Pluggable setup-action registry.

Setup actions are side-effects that run before each scenario's query —
e.g. record an outcome on a unit, deprioritize, write a KV entry, take a
snapshot. Suites declare them via ``Scenario.setup_actions``.

Built-in handlers register via ``@register_setup_action(name)``.
External callers register the same way — the framework dispatches by
name; nothing in core needs editing to add a new action.

Each handler's ``run()`` may return a dict; those returns merge into a
per-scenario ``context`` dict that's threaded into ``outcome.score()``.
This is the substrate for delta-style assertions
(e.g. ``memory_worth_delta``): a setup-action handler captures a
baseline; the outcome reads it back from ``context``.
"""

from __future__ import annotations

import abc
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.suite.setup_actions')


class SetupActionHandler(abc.ABC):
    """Pluggable side-effect runner.

    Subclasses register via ``@register_setup_action('myname')`` and
    override ``run()``. The optional dict return is merged into the
    per-scenario context that downstream outcomes can read.
    """

    name: ClassVar[str] = ''

    # Mark a handler as required: when ``required=True`` is set on the class
    # (or per-call via the ``SetupAction.required`` field — see base.py), a
    # raise inside ``run()`` flips the scenario to status='error' instead of
    # being soft-logged. Lets delta-style outcomes refuse to score against a
    # missing baseline.
    required: ClassVar[bool] = False

    # round-6 H4: declare whether this handler is safe to re-run on a
    # vault that's been preserved across runs (--reuse-vault). Default
    # True (idempotent). Set False on handlers with unbounded write-side
    # effects: ``record_outcome`` appends a new history entry every call
    # so re-running biases retrieval scoring; ``deprioritize`` is reset
    # by teardown but the next scenario inherits dirty state if reuse
    # skips teardown for prior runs. The runner skips entire scenarios
    # that declare any non-reusable setup action when --reuse-vault is
    # passed, with skip_reason='setup_action_not_reusable' (round-8 M3:
    # renamed from the original ``record_outcome_not_reusable`` once the
    # gate became registry-driven and could fire for ANY non-reusable
    # handler, not just ``record_outcome``).
    reusable_under_reuse_vault: ClassVar[bool] = True

    @abc.abstractmethod
    async def run(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Execute the side-effect. Return optional context to publish.

        Returned keys are auto-prefixed with the handler's registered ``name``
        (unless they already start with that prefix), so a custom handler
        publishing ``{'baseline': 0.7}`` from the ``snapshot`` action lands
        in context as ``snapshot.baseline``. This eliminates collision risk
        between multiple registered handlers running in one scenario.
        """

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Optional cleanup invoked after the scenario's score completes.

        Called by the runner regardless of pass/fail/error so the next
        scenario starts from a clean state. Default is a no-op.

        ``setup_context`` is the dict returned by ``run()`` (auto-prefixed
        with the handler name) so a teardown can reference IDs / keys the
        setup acted on without re-resolving them. The runner skips
        teardown for actions whose ``run()`` never executed (e.g. earlier
        required handler raised and broke the loop) — this default impl
        does not need to defend against that case.

        Failures inside teardown are caught by the runner with a warning;
        a failing teardown does NOT mark the scenario as errored. The
        next scenario may see dirty vault state — document the risk in
        each handler's teardown docstring.
        """
        return None


_SETUP_ACTION_REGISTRY: dict[str, type[SetupActionHandler]] = {}

# Names the runner seeds into ``scenario_context`` before any setup action
# runs (see ``runner.py:_run_setup_actions`` + the run_one_scenario
# context init). A handler whose ``kind`` matches one of these would
# either shadow seeded keys via the auto-prefix or be silently ignored.
# Reject at registration time so the failure surface is at import.
_RESERVED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        '_note_id_by_key',
        '_note_key_to_unit_ids',
        '_executed_action_kinds',
        '_executed_action_indices',
        '_required_setup_failed',
        '_inline_note_ids',
        '_note_assets_by_key',
        '_per_action_results',
        '_setup_failures',
    }
)


def register_setup_action(name: str):
    """Register a ``SetupActionHandler`` subclass under ``name``.

    Refuses to overwrite an existing registration. Use ``replace_setup_action``
    for tests / intentional overrides.
    """

    if not _NAME_RE.match(name):
        raise ValueError(f'Setup action name {name!r} must match {_NAME_RE.pattern!r}')
    if name in _RESERVED_CONTEXT_KEYS:
        raise ValueError(
            f'Setup action name {name!r} collides with a runner-reserved '
            f'scenario_context key. Reserved: {sorted(_RESERVED_CONTEXT_KEYS)}'
        )

    def deco(cls: type[SetupActionHandler]) -> type[SetupActionHandler]:
        # Round-7 M5: refuse classes that don't inherit from
        # SetupActionHandler. The runner's reuse-refusal check reads
        # ``reusable_under_reuse_vault`` via getattr with a default of
        # True; an unrelated class without that ClassVar would silently
        # be treated as reusable, defeating the H4 protection.
        if not isinstance(cls, type) or not issubclass(cls, SetupActionHandler):
            raise TypeError(
                f'Setup action {name!r}: {cls!r} must subclass SetupActionHandler '
                f'(needed for the reusable_under_reuse_vault ClassVar contract).'
            )
        existing = _SETUP_ACTION_REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f'Setup action {name!r} already registered to {existing.__qualname__}. '
                f'Use replace_setup_action() to override.'
            )
        # Set ``name`` only on first registration. Re-registering the same
        # class under a new name (via replace_*) leaves the original name
        # intact on the class so the auto-prefix in the runner is sourced
        # from the registry key (action.kind), not cls.name.
        if not getattr(cls, 'name', ''):
            cls.name = name
        _SETUP_ACTION_REGISTRY[name] = cls
        return cls

    return deco


def replace_setup_action(name: str):
    """Like ``register_setup_action`` but allows overriding an existing entry."""

    if not _NAME_RE.match(name):
        raise ValueError(f'Setup action name {name!r} must match {_NAME_RE.pattern!r}')
    if name in _RESERVED_CONTEXT_KEYS:
        raise ValueError(
            f'Setup action name {name!r} collides with a runner-reserved '
            f'scenario_context key. Reserved: {sorted(_RESERVED_CONTEXT_KEYS)}'
        )

    def deco(cls: type[SetupActionHandler]) -> type[SetupActionHandler]:
        # Round-7 M5: same subclass guard as register_setup_action.
        if not isinstance(cls, type) or not issubclass(cls, SetupActionHandler):
            raise TypeError(
                f'Setup action {name!r}: {cls!r} must subclass SetupActionHandler '
                f'(needed for the reusable_under_reuse_vault ClassVar contract).'
            )
        if name in _SETUP_ACTION_REGISTRY:
            logger.warning(
                'Replacing setup action %r (was %s, now %s)',
                name,
                _SETUP_ACTION_REGISTRY[name].__qualname__,
                cls.__qualname__,
            )
        # Same rule as register_setup_action: don't clobber the class's
        # original ``name``. Auto-prefix sources from the registry key.
        if not getattr(cls, 'name', ''):
            cls.name = name
        _SETUP_ACTION_REGISTRY[name] = cls
        return cls

    return deco


def unregister_setup_action(name: str) -> None:
    _SETUP_ACTION_REGISTRY.pop(name, None)


def get_setup_action(name: str) -> SetupActionHandler:
    if name not in _SETUP_ACTION_REGISTRY:
        raise KeyError(
            f'Unknown setup action {name!r}. Registered: {sorted(_SETUP_ACTION_REGISTRY)}'
        )
    return _SETUP_ACTION_REGISTRY[name]()


def list_setup_actions() -> list[str]:
    return sorted(_SETUP_ACTION_REGISTRY)


async def _resolve_unit_ids(
    api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
) -> list[str]:
    """Resolve unit IDs for a setup action, in priority order.

    1. ``unit_ids``: explicit UUIDs.
    2. ``note_key``: deterministic — looked up in ``_note_key_to_unit_ids``,
       a private params key the runner injects. Resolves to every unit
       extracted from that source note. Use this for OUTCOMES_MW /
       DEPRIORITIZATION scenarios where you know which note's units
       should be affected.
    3. ``search_query``: brittle — top-5 results of a memory search.
       Kept for backwards compatibility but emits a one-time WARNING
       per session so callers know they're using an unstable resolver.
    """
    if params.get('unit_ids'):
        return [str(uid) for uid in params['unit_ids']]
    note_key = params.get('note_key')
    if note_key:
        nk_map: dict[str, list[str]] = params.get('_note_key_to_unit_ids') or {}
        ids = nk_map.get(note_key)
        if not ids:
            logger.warning(
                '  Setup: note_key=%r resolved to 0 units. Was the note ingested? '
                'Check Suite.sources.notes and the per-note extraction wait.',
                note_key,
            )
            return []
        return list(ids)
    if params.get('search_query'):
        if not getattr(_resolve_unit_ids, '_warned_search_query', False):
            logger.warning(
                "Setup actions using search_query='%s' for unit resolution are brittle "
                '(top-5 search hit; semantic drift can deprioritize unrelated units). '
                'Prefer note_key= for deterministic scoping.',
                params['search_query'],
            )
            _resolve_unit_ids._warned_search_query = True  # type: ignore[attr-defined]
        units = await api.search(query=params['search_query'], limit=5, vault_ids=[vault_id])
        return [str(u.id) for u in units]
    return []


@register_setup_action('record_outcome')
class _RecordOutcome(SetupActionHandler):
    # round-6 H4: not safe to re-run on a preserved vault. Each call
    # appends a new history entry to the unit's outcome log, biasing
    # subsequent retrieval scoring. The runner refuses to run scenarios
    # with this setup action under --reuse-vault.
    reusable_under_reuse_vault: ClassVar[bool] = False

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        ids = await _resolve_unit_ids(api, vault_id, params)
        if not ids:
            logger.warning('  Setup record_outcome: no units')
            return None
        success = bool(params.get('success', True))
        count = int(params.get('count', 1) or 1)

        # Capture pre-state via direct DB access so teardown can restore
        # exact counter values + ``last_outcome_at``. Without these the
        # teardown can only zero the differential, leaving total evidence
        # inflated (which trips the >=5 gate in engine.py:154 and changes
        # the retrieval regime for future scenarios touching the same units).
        #
        # ``audit_ts_low`` is captured from Postgres (``SELECT now()``), NOT
        # the eval-process wall-clock (review round-1 HIGH #4). The audit
        # rows ``api.record_outcome`` writes use the SERVER's
        # ``transaction_timestamp()``; comparing them against a Python
        # ``datetime.now()`` would silently drop rows on any clock skew
        # between the eval host and the Postgres host (containerized CI,
        # NTP step, virt clock drift). Using PG's own clock makes the
        # comparison monotonic by construction.
        #
        # ``dsn_validated``: we also verify our DSN actually points at a
        # DB that contains the unit_ids we're about to revert. If
        # ``MEMEX_EVAL_DATABASE_URL`` is misconfigured, the SELECT returns
        # zero rows → we abort the DB-direct path and force the API-level
        # flip-cancel fallback so the teardown can't issue UPDATE/DELETE
        # against unrelated data (review round-1 HIGH #6).
        from memex_eval.suite.db_teardown import eval_db_session

        audit_ts_low_iso: str | None = None
        prev_state: dict[str, dict[str, Any]] = {}
        dsn_validated = False
        try:
            async with eval_db_session() as conn:
                # PG-side timestamp BEFORE we initiate the writes. Any audit
                # row inserted by record_outcome will have timestamp >= this.
                ts = await conn.fetchval('SELECT now()')
                audit_ts_low_iso = ts.isoformat() if ts is not None else None
                rows = await conn.fetch(
                    'SELECT id::text AS id, success_co_count, failure_co_count, '
                    'last_outcome_at FROM memory_units WHERE id = ANY($1::uuid[])',
                    ids,
                )
                for r in rows:
                    prev_state[r['id']] = {
                        'success_co_count': r['success_co_count'],
                        'failure_co_count': r['failure_co_count'],
                        'last_outcome_at': r['last_outcome_at'],
                    }
                # DSN sanity check: every unit_id we plan to stamp MUST
                # exist in the connected DB. A zero-rows result means the
                # configured DSN points at the wrong Postgres instance,
                # OR the units don't exist (which itself is a setup bug).
                if len(rows) == len(ids):
                    dsn_validated = True
                else:
                    logger.warning(
                        'record_outcome run: DSN sanity check failed — '
                        'expected %d memory_units, found %d. '
                        'Either MEMEX_EVAL_DATABASE_URL points at the wrong '
                        'database, or the units were never ingested. '
                        'Teardown will use API-level flip-cancel; '
                        'no DB-direct UPDATE/DELETE will run.',
                        len(ids),
                        len(rows),
                    )
        except Exception as exc:
            logger.warning(
                'record_outcome run: pre-state capture failed (%s: %s); '
                'teardown will fall back to flip_cancel semantics for these units.',
                type(exc).__name__,
                exc,
            )

        # Per-call try/except so a partial failure doesn't leak counter
        # increments without a teardown contract (review round-1 MEDIUM #8).
        # ``actual_success`` / ``actual_failure`` track how many calls
        # actually landed; teardown reverts exactly that many — never the
        # requested ``count`` (which would over-revert on partial failure).
        actual_success = 0
        actual_failure = 0
        for _ in range(count):
            try:
                await api.record_outcome(
                    unit_ids=ids,
                    success=success,
                    vault_id=str(vault_id),
                    reason=params.get('reason'),
                )
            except Exception as exc:
                logger.warning(
                    'record_outcome run: api.record_outcome call failed (%s: %s) '
                    'after %d/%d calls; teardown will revert what landed.',
                    type(exc).__name__,
                    exc,
                    actual_success + actual_failure,
                    count,
                )
                break
            if success:
                actual_success += 1
            else:
                actual_failure += 1
        return {
            'unit_ids': ids,
            'stamped_success': actual_success,
            'stamped_failure': actual_failure,
            'audit_ts_low': audit_ts_low_iso,
            'prev_state': prev_state,
            'dsn_validated': dsn_validated,
        }

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Restore pristine MW state via direct DB access.

        Steps (atomic in one transaction, run as the local Postgres user):
          1. UPDATE memory_units rows back to their captured pre-state
             (success_co_count, failure_co_count, last_outcome_at).
          2. Decrement the same counters on linked unit_entities by the
             stamped amount — propagation mirrors what record_outcome
             did at run time (services/outcomes.py:230-260).
          3. Decrement the same counters on linked mental_models.
          4. DELETE the audit_logs rows my run() created (filtered by
             action='outcome.record', resource_id ∈ unit_ids, timestamp
             ≥ run-time entry).

        Why DB-direct rather than a public reset endpoint: the public
        ``record_outcome`` API only does atomic ``+1`` increments by
        design. A reset endpoint would be test-only surface in core.
        Eval has access to the same Postgres instance the server uses;
        running the inverse SQL keeps memex-core clean.

        ``teardown_strategy='noop'`` opts out (e.g. for tests that want
        to inspect the outcome log post-run). The default is full reset.

        Failure mode: if pre-state capture failed at run-time
        (``prev_state`` empty for some unit_ids), the teardown falls
        back to plain decrement-by-stamped-amount on those rows —
        ``last_outcome_at`` may end up stale. Logged at WARNING.
        """
        strategy = (params.get('teardown_strategy') or 'reset').lower()
        if strategy == 'noop':
            return None
        if strategy not in ('reset',):
            logger.warning(
                'record_outcome teardown: unknown teardown_strategy=%r '
                '(expected "reset" | "noop"); falling back to reset.',
                strategy,
            )

        ctx = setup_context or {}
        # Prefer per-action keys (no prefix). The legacy prefixed keys are
        # accepted only as a back-compat fallback for external callers that
        # pass the merged context.
        unit_ids = ctx.get('unit_ids') or ctx.get('record_outcome.unit_ids') or []
        stamped_success = int(
            ctx.get('stamped_success', ctx.get('record_outcome.stamped_success')) or 0
        )
        stamped_failure = int(
            ctx.get('stamped_failure', ctx.get('record_outcome.stamped_failure')) or 0
        )
        prev_state = ctx.get('prev_state') or ctx.get('record_outcome.prev_state') or {}
        audit_ts_low_iso = ctx.get('audit_ts_low') or ctx.get('record_outcome.audit_ts_low')
        dsn_validated = bool(
            ctx.get('dsn_validated', ctx.get('record_outcome.dsn_validated', False))
        )

        if not unit_ids:
            return None

        # Refuse the DB-direct path if any safety precondition is missing
        # (review round-1 HIGH #6, MEDIUM #10, MEDIUM #12).
        # - ``dsn_validated``: the run() proved every unit_id exists in the
        #   connected DB. False → DSN may point at a different Postgres,
        #   or pre-state capture failed. Fall back to flip-cancel.
        # - ``prev_state`` covers EVERY unit_id: required so we can both
        #   restore the per-unit counters AND propagate to unit_entities /
        #   mental_models without double-decrement risk on retry.
        # - ``audit_ts_low_iso``: required so the audit DELETE has a
        #   guaranteed-correct timestamp lower bound.
        full_prev_state = all(uid in prev_state for uid in unit_ids)
        db_path_safe = dsn_validated and full_prev_state and bool(audit_ts_low_iso)

        from datetime import datetime, timezone
        from memex_eval.suite.db_teardown import eval_db_session

        if db_path_safe:
            try:
                async with eval_db_session() as conn:
                    async with conn.transaction():
                        # 1. Restore each memory_unit row to its captured
                        # pre-state (counters + last_outcome_at). Scoped by
                        # ``vault_id`` for defense-in-depth — consistent with
                        # the propagation queries below and the audit DELETE
                        # below (review round-2 MEDIUM #4). UUID PK uniqueness
                        # already guarantees one match, but the explicit
                        # vault filter prevents accidental cross-vault writes
                        # if a future change weakens the invariant.
                        for uid in unit_ids:
                            snap = prev_state[uid]
                            await conn.execute(
                                'UPDATE memory_units SET success_co_count = $1, '
                                'failure_co_count = $2, last_outcome_at = $3 '
                                'WHERE id = $4::uuid AND vault_id = $5::uuid',
                                snap['success_co_count'],
                                snap['failure_co_count'],
                                snap['last_outcome_at'],
                                uid,
                                str(vault_id),
                            )

                        # 2 + 3. Propagate the decrement to unit_entities and
                        # mental_models — mirroring services/outcomes.py:230-260.
                        if stamped_success or stamped_failure:
                            await conn.execute(
                                'UPDATE unit_entities SET '
                                'success_co_count = GREATEST(success_co_count - $1, 0), '
                                'failure_co_count = GREATEST(failure_co_count - $2, 0) '
                                'WHERE unit_id = ANY($3::uuid[]) AND vault_id = $4::uuid',
                                stamped_success,
                                stamped_failure,
                                unit_ids,
                                str(vault_id),
                            )
                            await conn.execute(
                                'UPDATE mental_models SET '
                                'success_co_count = GREATEST(success_co_count - $1, 0), '
                                'failure_co_count = GREATEST(failure_co_count - $2, 0) '
                                'WHERE entity_id IN ('
                                '  SELECT entity_id FROM unit_entities '
                                '  WHERE unit_id = ANY($3::uuid[]) AND vault_id = $4::uuid'
                                ') AND vault_id = $4::uuid',
                                stamped_success,
                                stamped_failure,
                                unit_ids,
                                str(vault_id),
                            )

                        # 4. DELETE only the audit_log rows our run() created.
                        # ``audit_ts_low_iso`` is captured from PG's clock
                        # (review round-1 HIGH #4) so the >= comparison is
                        # monotonic regardless of host clock skew.
                        # ``vault_id`` predicate scopes the delete to this
                        # action's vault only — multi-vault eval setups
                        # cannot accidentally clobber audit history that
                        # happens to share a unit_id (review round-1
                        # MEDIUM #9). vault_id lives inside ``details``
                        # JSONB at insert site (services/outcomes.py:267-275).
                        audit_ts_low = datetime.fromisoformat(audit_ts_low_iso)  # type: ignore[arg-type]
                        # Defensive: external callers might pass a naive
                        # datetime ISO string. asyncpg's TIMESTAMPTZ codec
                        # requires tz-aware (review round-2 MEDIUM #3).
                        if audit_ts_low.tzinfo is None:
                            audit_ts_low = audit_ts_low.replace(tzinfo=timezone.utc)
                        await conn.execute(
                            "DELETE FROM audit_logs WHERE action = 'outcome.record' "
                            "AND resource_type = 'memory_unit' "
                            'AND resource_id = ANY($1::text[]) '
                            'AND timestamp >= $2 '
                            "AND details->>'vault_id' = $3",
                            unit_ids,
                            audit_ts_low,
                            str(vault_id),
                        )
                return None
            except Exception as exc:
                logger.warning(
                    'record_outcome teardown SQL failed (%s: %s); '
                    'MW state may be polluted. Falling back to API-level flip-cancel.',
                    type(exc).__name__,
                    exc,
                )
        else:
            # Skip the DB-direct path entirely — preconditions failed.
            # Log loudly so the operator knows MW state is being recovered
            # via the lossy flip-cancel path rather than precise reset.
            reasons = []
            if not dsn_validated:
                reasons.append('dsn_validated=False (DSN may be misconfigured)')
            if not full_prev_state:
                reasons.append('prev_state missing for some unit_ids')
            if not audit_ts_low_iso:
                reasons.append('audit_ts_low not captured')
            logger.warning(
                'record_outcome teardown: skipping DB-direct path — %s. '
                'Falling back to API-level flip-cancel; MW counters will be '
                'balanced (success+failure pair) but NOT zeroed.',
                '; '.join(reasons),
            )

        # Fallback: flip-and-cancel via the public API. Records inverse
        # outcomes so the differential nets to zero. Note this leaves
        # absolute counters at (stamped_success, stamped_failure) — the
        # >=5 evidence gate (services/outcomes.py engine.py:154) may still
        # fire. Better than leaving the unbalanced increment in place.
        for _ in range(stamped_success):
            try:
                await api.record_outcome(
                    unit_ids=unit_ids,
                    success=False,
                    vault_id=str(vault_id),
                    reason='eval-teardown fallback: cancel stamped success',
                )
            except Exception as inner:
                logger.warning('flip-cancel fallback (success): %s', inner)
        for _ in range(stamped_failure):
            try:
                await api.record_outcome(
                    unit_ids=unit_ids,
                    success=True,
                    vault_id=str(vault_id),
                    reason='eval-teardown fallback: cancel stamped failure',
                )
            except Exception as inner:
                logger.warning('flip-cancel fallback (failure): %s', inner)
        return None


@register_setup_action('deprioritize')
class _Deprioritize(SetupActionHandler):
    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        ids = await _resolve_unit_ids(api, vault_id, params)
        if not ids:
            logger.warning('  Setup deprioritize: no units')
            return None
        for uid in ids:
            await api.deprioritize_memory_unit(
                unit_id=UUID(uid),
                reason=params.get('reason') or 'eval-suite deprioritize',
                vault_id=vault_id,
            )
        return {'unit_ids': ids}

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Restore every unit deprioritized by run().

        Reads ``deprioritize.unit_ids`` (auto-prefixed by the runner) from
        the setup context — the unit IDs the run() side-effect operated
        on. Idempotent on the API side (RemoteMemexAPI.restore_memory_unit
        is no-op if not deprioritized)."""
        ctx = setup_context or {}
        unit_ids = ctx.get('unit_ids') or ctx.get('deprioritize.unit_ids') or []
        for uid in unit_ids:
            try:
                await api.restore_memory_unit(unit_id=UUID(str(uid)), vault_id=vault_id)
            except Exception as exc:
                logger.warning(
                    'deprioritize teardown: restore_memory_unit(%s) failed: %s', uid, exc
                )


@register_setup_action('kv_write')
class _KvWrite(SetupActionHandler):
    required: ClassVar[bool] = True

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        key = (params.get('kv_key') or '').strip()
        value = params.get('kv_value')
        if not key:
            raise ValueError(
                'kv_write setup action requires a non-empty kv_key. '
                'Empty keys silently no-op when the API path-encodes them.'
            )
        if value is None:
            raise ValueError(
                'kv_write setup action requires kv_value to be set explicitly '
                '(use empty string to write a sentinel; None signals a misconfigured suite).'
            )
        await api.kv_put(value=value, key=key)
        return {'kv_key': key}

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Delete the KV entry the setup wrote."""
        ctx = setup_context or {}
        key = (
            ctx.get('kv_key') or ctx.get('kv_write.kv_key') or (params.get('kv_key') or '').strip()
        )
        if not key:
            return
        try:
            await api.kv_delete(key=key)
        except Exception as exc:
            logger.warning('kv_write teardown: kv_delete(%r) failed: %s', key, exc)


@register_setup_action('consolidation_tick')
class _ConsolidationTick(SetupActionHandler):
    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        await api.consolidation_tick(vault_id=vault_id)
        return None

    # teardown intentionally inherits the no-op default — consolidation
    # produces idempotent state mutations (contradiction edges, reflections)
    # which are cheap to keep and unsafe to revert.


@register_setup_action('trigger_reflections')
class _TriggerReflections(SetupActionHandler):
    """Trigger reflection on the top-N entities in the vault and wait for
    a mental_model search to be visible (matches legacy
    ``internal/runner.py:_trigger_reflections``).

    Params:
    - ``count``: how many top entities to reflect on (default 5)
    - ``timeout_s``: seconds to wait for mental_model results
      (default ``max(60, min_mental_model_hits * 30)``)
    - ``target_entity_names``: entity names that the handler resolves and
      **prepends to the reflection queue**, so they get reflected even
      when they don't crack the top-N by mention_count. Whether a target
      is independently verified before the handler returns depends on
      the polling mode — see ``probe_query`` below.
    - ``min_mental_model_hits``: how many hits the gate requires before
      declaring ready (default 1). Raise to match a downstream
      ``UsefulAtK(k=N)`` so the consumer scenario doesn't race the
      reflection writer.
    - ``probe_query``: when set, switches polling to **shared-probe**
      mode — one search per loop iteration using this query, gating ALL
      targets together. Use this when the consumer scenario asserts on
      the same query (the gate then mirrors what the consumer will see).
      When unset, polling runs in **per-target** mode: one search per
      target name, each gated independently. Per-target is the only
      mode that genuinely verifies each target's mental_model exists;
      shared-probe is faster but cannot distinguish "all targets
      materialized" from "the probe query happens to match enough hits
      from one target." Returned context's ``probe_mode`` reflects which
      one ran. Partial misses populate ``unmaterialized_targets``.

    The action ranks entities by mention_count via ``api.get_top_entities``
    so reflection focuses on the most-mentioned subjects in the vault.
    """

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        import asyncio
        import logging
        import time

        from memex_common.schemas import ReflectionRequest

        logger = logging.getLogger('memex_eval.suite.setup_actions.trigger_reflections')

        limit = int(params.get('count', 5) or 5)
        timeout_s = float(params.get('timeout_s', 120) or 120)

        top_entities = await api.get_top_entities(limit=limit, vault_id=vault_id)

        # Resolve ``target_entity_names`` FIRST so they head the reflection
        # queue. With multiple reflects queued, the server processes them
        # serially; the consumer scenario polls until the targets'
        # mental_models are visible. If we put targets at the back of the
        # queue, polling races the queue and frequently times out before
        # Sarah Chen / Project Alpha materialize.
        target_entity_names: list[str] = list(params.get('target_entity_names') or [])
        top_names = [getattr(e, 'name', '') or '' for e in top_entities]
        logger.info(
            'trigger_reflections: top-%d=%s, target_entity_names=%s',
            limit,
            top_names,
            target_entity_names,
        )

        # Use the shared canonicalisation helper so this matches the
        # ingest + reuse-vault paths (round-3 MEDIUM 1).
        from memex_eval.suite.sources import canonicalize_name

        def _name_match(a: str, b: str) -> bool:
            return canonicalize_name(a) == canonicalize_name(b)

        target_entities: list[Any] = []
        dropped_targets: list[str] = []
        for tname in target_entity_names:
            top_match = next(
                (e for e in top_entities if _name_match(getattr(e, 'name', '') or '', tname)),
                None,
            )
            if top_match is not None:
                target_entities.append(top_match)
                continue
            try:
                hits = await api.search_entities(query=tname, limit=5, vault_id=vault_id)
            except Exception as exc:
                logger.warning('search_entities(%r) failed: %s', tname, exc)
                dropped_targets.append(tname)
                continue
            match = next(
                (h for h in hits if _name_match(getattr(h, 'name', '') or '', tname)), None
            )
            if match is None:
                logger.warning(
                    'target entity %r not found in vault %s (candidates: %s)',
                    tname,
                    vault_id,
                    [getattr(h, 'name', '?') for h in hits],
                )
                dropped_targets.append(tname)
                continue
            actual_name = getattr(match, 'name', '?') or '?'
            if actual_name != tname:
                logger.info(
                    'trigger_reflections: target %r resolved via case-insensitive match to %r',
                    tname,
                    actual_name,
                )
            logger.info(
                'trigger_reflections: prioritising target entity %r (id=%s)', tname, match.id
            )
            target_entities.append(match)

        # Targets first, then top-N (deduped by id).
        seen_ids: set[UUID] = {e.id for e in target_entities}
        entities = list(target_entities) + [e for e in top_entities if e.id not in seen_ids]

        if not entities:
            return {
                'reflected_count': 0,
                'requested_count': 0,
                'failed_count': 0,
                'dropped_targets': dropped_targets,
            }

        failed: list[str] = []
        succeeded: list[str] = []
        for ent in entities:
            ent_name = getattr(ent, 'name', '?') or '?'
            try:
                resp = await api.reflect(
                    ReflectionRequest(entity_id=ent.id, vault_id=str(vault_id))
                )
                succeeded.append(ent_name)
                logger.info(
                    'reflect(%s) -> status=%s new_observations=%d',
                    ent_name,
                    getattr(resp, 'status', '?'),
                    len(getattr(resp, 'new_observations', []) or []),
                )
            except Exception as exc:
                failed.append(ent_name)
                logger.warning('reflect(%s) failed: %s', ent_name, exc)

        if not succeeded:
            raise RuntimeError(
                f'trigger_reflections: all {len(entities)} reflect() calls failed '
                f'(entities tried: {failed}). Suite cannot exercise reflection paths.'
            )

        base_ctx: dict[str, Any] = {
            'reflected_count': len(succeeded),
            'requested_count': len(entities),
            'failed_count': len(failed),
            'failed_entities': failed,
            'dropped_targets': dropped_targets,
        }

        # Polling defaults. ``min_mental_model_hits`` raises the bar from
        # "≥1 hit per target" (legacy) to "≥k hits" so a downstream
        # ``UsefulAtK(k=N)`` doesn't race the reflection writer.
        min_hits = max(1, int(params.get('min_mental_model_hits', 1) or 1))
        # Default budget: 30s per required hit, floor at 60s. Overrideable
        # by the suite when an unusual server posture demands it.
        timeout_default = max(60.0, min_hits * 30.0)
        timeout_s = float(params.get('timeout_s', timeout_default) or timeout_default)
        probe_query = str(params.get('probe_query') or '')

        # Probe semantics:
        #
        # - **per-target probe** (``probe_query`` empty): each target name
        #   is searched independently; ``ready`` requires the per-target
        #   count to reach ``min_hits``. This is the legacy contract:
        #   "every named entity has a mental_model materialised."
        #
        # - **shared probe** (``probe_query`` set): the suite is asserting
        #   "this exact query produces ≥k hits before the consumer
        #   scenario runs." We run the probe ONCE per loop iteration and
        #   gate every target on the same count. This matches what the
        #   consumer will see, but it does NOT guarantee per-target
        #   materialisation — e.g. if Sarah Chen has 10 mental models and
        #   Project Alpha has 0, the shared probe may declare both ready.
        #   Kept distinct from per-target so we can document the trade.
        target_names: list[str] = (
            list(target_entity_names)
            if target_entity_names
            else (
                [getattr(entities[0], 'name', None) or '']
                if entities and getattr(entities[0], 'name', None)
                else []
            )
        )
        if not target_names:
            return base_ctx

        # Search ``limit`` is min_hits + 1 so the polling predicate
        # ``count >= min_hits`` distinguishes "exactly k" from "more
        # than k" — keeps the comparison meaningful if a future change
        # tightens to ``> min_hits``.
        probe_limit = min_hits + 1

        async def _per_target_count(name: str) -> int:
            try:
                results = await api.search(
                    query=name,
                    limit=probe_limit,
                    strategies=['mental_model'],
                    vault_ids=[vault_id],
                )
            except Exception as exc:
                logger.warning('mental_model probe %r failed: %s', name, exc)
                return 0
            return len(results)

        async def _shared_probe_count() -> int:
            try:
                results = await api.search(
                    query=probe_query,
                    limit=probe_limit,
                    strategies=['mental_model'],
                    vault_ids=[vault_id],
                )
            except Exception as exc:
                logger.warning('shared mental_model probe failed: %s', exc)
                return 0
            return len(results)

        pending = set(target_names)
        deadline = time.monotonic() + timeout_s
        last_log = 0.0
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(3)
            if probe_query:
                shared_count = await _shared_probe_count()
                ready = set(pending) if shared_count >= min_hits else set()
                counts: dict[str, int] = {n: shared_count for n in pending}
            else:
                # Per-target probe runs N concurrent searches per loop
                # iteration — sequential awaits would scale wall-clock by
                # O(targets × latency) for no algorithmic gain.
                pending_list = list(pending)
                target_counts = await asyncio.gather(*(_per_target_count(n) for n in pending_list))
                counts = dict(zip(pending_list, target_counts, strict=True))
                ready = {n for n, c in counts.items() if c >= min_hits}
            now = time.monotonic()
            if now - last_log > 15:
                logger.info(
                    'trigger_reflections: polling mode=%s counts=%s pending=%s elapsed=%.1fs',
                    'shared' if probe_query else 'per-target',
                    counts,
                    sorted(pending - ready),
                    timeout_s - (deadline - now),
                )
                last_log = now
            pending -= ready
        ctx = {
            **base_ctx,
            'probe_entities': target_names,
            'min_mental_model_hits': min_hits,
            'probe_mode': 'shared' if probe_query else 'per-target',
        }
        if pending:
            logger.warning(
                'trigger_reflections: timed out waiting for targets %s (mode=%s, min_hits=%d, timeout=%.0fs)',
                sorted(pending),
                ctx['probe_mode'],
                min_hits,
                timeout_s,
            )
            ctx['unmaterialized_targets'] = sorted(pending)
            ctx['timed_out'] = True
        else:
            logger.info('trigger_reflections: all targets materialized within %.0fs', timeout_s)
        return ctx


@register_setup_action('lint_run')
class _LintRun(SetupActionHandler):
    """Trigger the V1 lint rule registry on the scenario's vault.

    Wires the eval-suite to the new ``POST /api/v1/lint/run/{vault_id}``
    endpoint added in P6. ``required=True`` because a scenario asserting
    on lint findings needs lint to actually have run; silent failure
    would produce a false-fail.
    """

    required: ClassVar[bool] = True

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        result = await api.run_lint_rules(vault_id)
        return {
            'total_findings': int(result.get('total_findings', 0) or 0),
            'rules_run': len(result.get('rules', []) or []),
        }

    # teardown intentionally inherits the no-op default — lint findings
    # are idempotent on (rule_name, target, vault_id) at the SQL layer
    # so back-to-back runs don't accumulate. See server/lint.py:lint_run.


@register_setup_action('lint_llm_run')
class _LintLLMRun(SetupActionHandler):
    """Trigger the LLM-gated lint pass on the scenario's vault.

    Wires the eval-suite to the new ``POST /api/v1/lint/llm/run/{vault_id}``
    endpoint added in P6. Returns 503 from the server when lint_llm is
    config-disabled — the suite framework's ``requires_nli_classifier``
    metadata (P7) is the right gate to skip these scenarios in advance.
    """

    required: ClassVar[bool] = True

    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        result = await api.run_lint_llm(vault_id)
        summaries = result.get('summaries', []) or []
        emitted = 0
        for s in summaries:
            try:
                emitted += int(s.get('emitted', 0) or 0)
            except (TypeError, ValueError):
                continue
        return {
            'findings_emitted': emitted,
            'summaries': summaries,
        }


__all__ = [
    'SetupActionHandler',
    'register_setup_action',
    'replace_setup_action',
    'unregister_setup_action',
    'get_setup_action',
    'list_setup_actions',
]
