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


def register_setup_action(name: str):
    """Register a ``SetupActionHandler`` subclass under ``name``.

    Refuses to overwrite an existing registration. Use ``replace_setup_action``
    for tests / intentional overrides.
    """

    if not _NAME_RE.match(name):
        raise ValueError(f'Setup action name {name!r} must match {_NAME_RE.pattern!r}')

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
        for _ in range(params.get('count', 1) or 1):
            await api.record_outcome(
                unit_ids=ids,
                success=params.get('success', True),
                vault_id=str(vault_id),
                reason=params.get('reason'),
            )
        return {'unit_ids': ids}

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """No-op by design.

        Memory Worth counters are append-only at the storage layer and
        the retrieval pre-filter at engine.py:154 fires when
        (success_co + failure_co) >= 5. A flip-and-cancel teardown would
        push counters into that gated regime after one reuse cycle,
        silently changing scoring of unrelated downstream scenarios in
        the same vault. The runner therefore skips outcome scenarios on
        --reuse-vault with skip_reason='setup_action_not_reusable';
        outcome scenarios must be exercised on a fresh vault.

        Follow-up ticket will add an admin record_outcome_reset(unit_ids)
        endpoint that resets counters to zero — at which point this
        teardown becomes a real reset.
        """
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
        unit_ids = ctx.get('deprioritize.unit_ids') or []
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
        key = ctx.get('kv_write.kv_key') or (params.get('kv_key') or '').strip()
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
    at least one mental_model search hit (matches legacy
    ``internal/runner.py:_trigger_reflections``).

    Params:
    - ``count``: how many top entities to reflect on (default 5)
    - ``timeout_s``: seconds to wait for mental_model results (default 120)
    - ``target_entity_names``: list of entity names that MUST have a
      mental_model materialized before the action returns. Without this,
      the action polls only the most-mentioned entity, which may not be
      the one a downstream scenario queries. With it, the action keeps
      polling until each named entity has at least one mental_model
      result (or the timeout fires; partial returns set
      ``unmaterialized_targets`` in the context).

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
        early_targets: list[str] = list(params.get('target_entity_names') or [])
        top_names = {getattr(e, 'name', None) or '' for e in top_entities}
        logger.info(
            'trigger_reflections: top-%d=%s, target_entity_names=%s',
            limit,
            sorted(top_names),
            early_targets,
        )
        target_entities: list[Any] = []
        for tname in early_targets:
            # If a target is already in top-N, find it there to preserve identity.
            top_match = next(
                (e for e in top_entities if (getattr(e, 'name', '') or '') == tname),
                None,
            )
            if top_match is not None:
                target_entities.append(top_match)
                continue
            try:
                hits = await api.search_entities(query=tname, limit=3, vault_id=vault_id)
            except Exception as exc:
                logger.warning('search_entities(%r) failed: %s', tname, exc)
                continue
            match = next((h for h in hits if (getattr(h, 'name', '') or '') == tname), None)
            if match is None:
                logger.warning(
                    'target entity %r not found in vault %s (search_entities returned %d candidates: %s)',
                    tname,
                    vault_id,
                    len(hits),
                    [getattr(h, 'name', '?') for h in hits],
                )
                continue
            logger.info(
                'trigger_reflections: prioritising target entity %r (id=%s)', tname, match.id
            )
            target_entities.append(match)

        # Targets first, then top-N (deduped by id).
        seen_ids: set[Any] = {e.id for e in target_entities}
        entities = list(target_entities) + [e for e in top_entities if e.id not in seen_ids]

        if not entities:
            return {'reflected_count': 0, 'requested_count': 0, 'failed_count': 0}

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
        }

        # Build the list of entities that need to have a mental_model
        # materialized before the action returns. Default: the
        # most-mentioned entity (legacy behavior). With
        # ``target_entity_names``: every named entity.
        target_names: list[str] = list(params.get('target_entity_names') or [])
        if not target_names:
            top_name = getattr(entities[0], 'name', None) or ''
            if top_name:
                target_names = [top_name]
        if not target_names:
            return base_ctx

        # ``min_mental_model_hits`` lets the consumer scenario require ≥k
        # materialized observations per target before proceeding. Default
        # 1 preserves legacy behavior; raise to match a downstream
        # ``UsefulAtK(k=N)`` so ``mental_model_strategy`` doesn't race the
        # reflection writer.
        min_hits = max(1, int(params.get('min_mental_model_hits', 1) or 1))
        probe_query = str(params.get('probe_query') or '')

        async def _hit_count(name: str) -> int:
            try:
                # When a probe_query is set, use it (matches the consumer
                # scenario's actual query so race detection mirrors what the
                # scenario will see). Otherwise probe by entity name.
                q = probe_query or name
                results = await api.search(
                    query=q, limit=min_hits, strategies=['mental_model'], vault_ids=[vault_id]
                )
            except Exception as exc:
                logger.warning('mental_model probe %r failed: %s', name, exc)
                return 0
            return len(results)

        pending = set(target_names)
        deadline = time.monotonic() + timeout_s
        last_log = 0.0
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(3)
            counts = {n: await _hit_count(n) for n in pending}
            ready = {n for n, c in counts.items() if c >= min_hits}
            now = time.monotonic()
            if now - last_log > 15:
                logger.info(
                    'trigger_reflections: polling counts=%s pending=%s elapsed=%.1fs',
                    counts,
                    sorted(pending - ready),
                    timeout_s - (deadline - now),
                )
                last_log = now
            pending -= ready
        ctx = {**base_ctx, 'probe_entities': target_names, 'min_mental_model_hits': min_hits}
        if pending:
            logger.warning(
                'trigger_reflections: timed out waiting for targets %s (min_hits=%d, timeout=%.0fs)',
                sorted(pending),
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
