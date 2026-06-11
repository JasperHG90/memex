"""Synchronous wrappers for the procedural-plane client.

Hermes's memory provider runs on a synchronous thread (the Hermes CLI
is sync). The 4 functions in :mod:`memex_hermes_plugin.memex.procedural`
marshal the async :class:`RemoteMemexAPI` calls onto the shared event
loop in :mod:`memex_hermes_plugin.memex.async_bridge`.

Only the READ wrappers (get / get_by_identity / search) plus
``case_submit`` are exposed: procedures/strategies are DERIVED from
cases, so the agent's only procedural write is ``case_submit``. The
write wrappers (create / upsert / update / deprecate) are intentionally
gone.

These tests cover the marshalling surface — the sync wrapper must
forward arguments to the right async method, return the coroutine's
result unchanged, and pass through a custom timeout. We do NOT re-test
the client method semantics here; that lives in
``packages/common/tests/test_procedural_client_methods.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_common.procedural_schemas import (
    CaseSubmit,
    ProceduralSearchRequest,
)
from memex_hermes_plugin.memex import procedural


def _api() -> MagicMock:
    """Build a stub API where every ``procedural_*`` method returns an
    awaitable that resolves to a sentinel. Each call site is asserted
    in its own test."""
    api = MagicMock()
    sentinel = object()
    for name in (
        'procedural_get',
        'procedural_get_by_identity',
        'procedural_search',
        'case_submit',
    ):
        method = AsyncMock(return_value=sentinel)
        setattr(api, name, method)
    return api


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


def test_search_forwards_request():
    api = _api()
    request = ProceduralSearchRequest.model_construct()  # type: ignore[call-arg]
    procedural.search(api, request)
    api.procedural_search.assert_awaited_once_with(request)


def test_case_submit_forwards_payload_and_returns_result():
    api = _api()
    payload = CaseSubmit.model_construct()  # type: ignore[call-arg]
    result = procedural.case_submit(api, payload)
    assert result is api.case_submit.return_value
    api.case_submit.assert_awaited_once_with(payload)


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
    proc_mod.search(
        api,
        ProceduralSearchRequest.model_construct(),
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
        'get',
        'get_by_identity',
        'search',
        'case_submit',
    ],
)
def test_all_procedural_read_methods_exposed(fn_name):
    """The 4 sync wrappers mirror the read + case_submit routes 1:1. Any
    drift (extra or missing method) trips this test."""
    assert hasattr(procedural, fn_name)
    assert callable(getattr(procedural, fn_name))


@pytest.mark.parametrize('fn_name', ['create', 'upsert', 'update', 'deprecate'])
def test_procedural_write_wrappers_absent(fn_name):
    """The procedural WRITE wrappers must NOT exist — agents write
    procedural knowledge only via case_submit; derivation/governance own
    create / upsert / update / deprecate."""
    assert not hasattr(procedural, fn_name)


def test_four_methods_in_dunder_all():
    """`__all__` is the public surface — it must list exactly the 3 read
    wrappers plus case_submit, no more no less."""
    assert sorted(procedural.__all__) == sorted(
        [
            'get',
            'get_by_identity',
            'search',
            'case_submit',
        ]
    )
