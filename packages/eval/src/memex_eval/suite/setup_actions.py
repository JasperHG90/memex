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


_SETUP_ACTION_REGISTRY: dict[str, type[SetupActionHandler]] = {}


def register_setup_action(name: str):
    """Register a ``SetupActionHandler`` subclass under ``name``.

    Refuses to overwrite an existing registration. Use ``replace_setup_action``
    for tests / intentional overrides.
    """

    if not _NAME_RE.match(name):
        raise ValueError(f'Setup action name {name!r} must match {_NAME_RE.pattern!r}')

    def deco(cls: type[SetupActionHandler]) -> type[SetupActionHandler]:
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
    if params.get('unit_ids'):
        return [str(uid) for uid in params['unit_ids']]
    if params.get('search_query'):
        units = await api.search(query=params['search_query'], limit=5, vault_ids=[vault_id])
        return [str(u.id) for u in units]
    return []


@register_setup_action('record_outcome')
class _RecordOutcome(SetupActionHandler):
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


@register_setup_action('kv_write')
class _KvWrite(SetupActionHandler):
    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        await api.kv_put(
            value=params.get('kv_value') or '',
            key=params.get('kv_key') or '',
        )
        return None


@register_setup_action('consolidation_tick')
class _ConsolidationTick(SetupActionHandler):
    async def run(
        self, api: 'RemoteMemexAPI', vault_id: UUID, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        await api.consolidation_tick(vault_id=vault_id)
        return None


__all__ = [
    'SetupActionHandler',
    'register_setup_action',
    'replace_setup_action',
    'unregister_setup_action',
    'get_setup_action',
    'list_setup_actions',
]
