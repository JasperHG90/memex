"""Synchronous wrappers for the procedural-plane client.

Hermes's memory provider runs on a synchronous thread (the Hermes CLI
is sync). The 8 functions in :mod:`memex_hermes_plugin.memex.procedural`
marshal the async :class:`RemoteMemexAPI` calls onto the shared event
loop in :mod:`memex_hermes_plugin.memex.async_bridge`.

These tests cover the marshalling surface — the sync wrapper must
forward arguments to the right async method, return the coroutine's
result unchanged, and pass through a custom timeout. We do NOT re-test
the client method semantics here; that lives in
``packages/common/tests/test_procedural_client_methods.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_common.experiential_schemas import (
    ExperientialEntryCreate,
    ExperientialEntryUpdate,
    ExperientialSearchRequest,
)
from memex_hermes_plugin.memex import procedural


def _api() -> MagicMock:
    """Build a stub API where every ``procedural_*`` method returns an
    awaitable that resolves to a sentinel. Each call site is asserted
    in its own test."""
    api = MagicMock()
    sentinel = object()
    for name in (
        'procedural_create',
        'procedural_upsert',
        'procedural_get',
        'procedural_get_by_identity',
        'procedural_update',
        'procedural_deprecate',
        'procedural_search',
        'procedural_briefing_cards',
    ):
        method = AsyncMock(return_value=sentinel)
        setattr(api, name, method)
    return api


def test_create_forwards_payload_and_returns_dto():
    api = _api()
    payload = ExperientialEntryCreate.model_construct()  # type: ignore[call-arg]
    result = procedural.create(api, payload)
    assert result is api.procedural_create.return_value
    api.procedural_create.assert_awaited_once_with(payload)


def test_upsert_forwards_payload_and_returns_dto():
    api = _api()
    payload = ExperientialEntryCreate.model_construct()  # type: ignore[call-arg]
    result = procedural.upsert(api, payload)
    assert result is api.procedural_upsert.return_value
    api.procedural_upsert.assert_awaited_once_with(payload)


def test_get_forwards_id_and_optional_vault():
    api = _api()
    eid = api.procedural_get.call_args  # placeholder
    from uuid import uuid4

    eid = uuid4()
    result = procedural.get(api, eid, vault_id='v1')
    assert result is api.procedural_get.return_value
    api.procedural_get.assert_awaited_once_with(eid, vault_id='v1')


def test_get_with_no_vault_passes_none():
    api = _api()
    from uuid import uuid4

    eid = uuid4()
    procedural.get(api, eid)
    api.procedural_get.assert_awaited_once_with(eid, vault_id=None)


def test_get_by_identity_forwards_all_kwargs():
    api = _api()
    procedural.get_by_identity(
        api,
        kind='procedure',
        scope='user',
        verb='rotate',
        context='api_key',
        vault_id='v1',
    )
    api.procedural_get_by_identity.assert_awaited_once_with(
        'procedure', 'user', verb='rotate', context='api_key', vault_id='v1'
    )


def test_get_by_identity_omits_none_optionals():
    """The wrapper must NOT pass ``None`` explicitly for the verb /
    context / vault_id keys when the caller didn't supply them. The
    underlying client signature has defaults of None, so omitting the
    key entirely is equivalent and avoids masking caller intent.
    """
    api = _api()
    procedural.get_by_identity(api, kind='procedure', scope='user')
    api.procedural_get_by_identity.assert_awaited_once_with(
        'procedure', 'user', verb=None, context=None, vault_id=None
    )


def test_update_forwards_id_payload_and_optional_vault():
    api = _api()
    from uuid import uuid4

    eid = uuid4()
    payload = ExperientialEntryUpdate.model_construct()  # type: ignore[call-arg]
    procedural.update(api, eid, payload, vault_id='v2')
    api.procedural_update.assert_awaited_once_with(eid, payload, vault_id='v2')


def test_deprecate_forwards_optional_successor():
    api = _api()
    from uuid import uuid4

    eid = uuid4()
    succ = uuid4()
    procedural.deprecate(api, eid, superseded_by_id=succ, vault_id='v1')
    api.procedural_deprecate.assert_awaited_once_with(eid, superseded_by_id=succ, vault_id='v1')


def test_search_forwards_request():
    api = _api()
    request = ExperientialSearchRequest.model_construct()  # type: ignore[call-arg]
    procedural.search(api, request)
    api.procedural_search.assert_awaited_once_with(request)


def test_briefing_cards_forwards_context_keys():
    api = _api()
    procedural.briefing_cards(
        api,
        context_keys=['project:42', 'app:claude-code'],
        scope='user',
        limit_per_context=3,
    )
    api.procedural_briefing_cards.assert_awaited_once_with(
        ['project:42', 'app:claude-code'],
        scope='user',
        limit_per_context=3,
    )


def test_briefing_cards_default_limit_is_5():
    """The wrapper's default must match the operator-config default so
    the briefing surface never silently overshoots the per-context cap."""
    api = _api()
    procedural.briefing_cards(api, context_keys=['k'])
    api.procedural_briefing_cards.assert_awaited_once_with(['k'], scope=None, limit_per_context=5)


def test_custom_timeout_is_honored(monkeypatch):
    """A caller can pass a custom timeout to wrap a slow search; the
    wrapper must forward it to ``run_sync`` rather than always use the
    module default. We assert this by patching ``run_sync`` and
    inspecting its call kwargs.
    """
    from memex_hermes_plugin.memex import procedural as proc_mod

    seen: dict[str, float] = {}
    real_run_sync = proc_mod.run_sync

    def spy_run_sync(coro, timeout):  # type: ignore[no-untyped-def]
        seen['timeout'] = timeout
        return real_run_sync(coro, timeout=timeout)

    monkeypatch.setattr(proc_mod, 'run_sync', spy_run_sync)

    api = _api()
    proc_mod.create(
        api,
        ExperientialEntryCreate.model_construct(),
        timeout=0.5,  # type: ignore[call-arg]
    )
    assert seen['timeout'] == 0.5


def test_default_timeout_is_30s():
    """The module default must be high enough to cover the search path
    (BM25 + vector + RRF), but low enough to surface a hung call
    within an LLM turn."""
    from memex_hermes_plugin.memex import procedural

    assert procedural._DEFAULT_TIMEOUT == 30.0


@pytest.mark.parametrize(
    'fn_name',
    [
        'create',
        'upsert',
        'get',
        'get_by_identity',
        'update',
        'deprecate',
        'search',
        'briefing_cards',
    ],
)
def test_all_eight_procedural_methods_exposed(fn_name):
    """The 8 sync wrappers mirror the 8 HTTP routes 1:1. Any drift
    (extra or missing method) trips this test."""
    assert hasattr(procedural, fn_name)
    assert callable(getattr(procedural, fn_name))


def test_eight_methods_in_dunder_all():
    """`__all__` is the public surface — it must list exactly the 8
    sync wrappers, no more no less."""
    assert sorted(procedural.__all__) == sorted(
        [
            'create',
            'upsert',
            'get',
            'get_by_identity',
            'update',
            'deprecate',
            'search',
            'briefing_cards',
        ]
    )
