"""Integration tests for F14 procedure-key envelope semantics (TC-F14-2).

Covers RFC-007 §63-116 over a real Postgres backend:

* Envelope shape on first write (v=1, empty history)
* Version monotonic increment + capped history (5 entries, FIFO drop)
* Concurrent writers under optimistic concurrency (final v=N+2 with both
  intermediate values landing in history)
* ``include_history=True`` exposure of ``{value, version, history}``
* Default ``get`` shape unchanged for procedure keys (back-compat) — value
  is the unwrapped active string, not the JSON envelope
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio

from memex_core.memory.sql_models import KVEntry
from memex_core.services.kv import (
    PROCEDURE_HISTORY_CAP,
    KVService,
)

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def kv(metastore, filestore, memex_config):
    """KVService wired to the real test database."""
    return KVService(metastore=metastore, filestore=filestore, config=memex_config)


def _unique_proc_key() -> str:
    """A unique global procedure key per test run."""
    return f'global:procedure:write_pr:tag-{uuid4().hex[:8]}'


def _unique_project_proc_key(project_id: str = 'memex') -> str:
    """A unique project-scoped procedure key per test run."""
    return f'project:{project_id}:procedure:write_pr:tag-{uuid4().hex[:8]}'


@pytest.mark.asyncio
async def test_first_write_creates_envelope_at_v1_empty_history(kv: KVService) -> None:
    """First write wraps value in {v=1, value, tags={}, history=[]}."""
    key = _unique_proc_key()
    await kv.put(key=key, value='draft-then-self-review')

    async with kv.metastore.session() as session:
        from sqlmodel import select as sm_select

        result = await session.exec(sm_select(KVEntry).where(KVEntry.key == key))
        entry = result.first()
    assert entry is not None
    payload = json.loads(entry.value)
    assert payload['v'] == 1
    assert payload['value'] == 'draft-then-self-review'
    assert payload['history'] == []
    assert payload.get('tags') == {}


@pytest.mark.asyncio
async def test_seven_writes_v7_history_capped_at_5(kv: KVService) -> None:
    """Seven sequential writes: final version=7, history holds 5 most-recent superseded values."""
    key = _unique_proc_key()
    values = [f'step-{i}' for i in range(1, 8)]
    for v in values:
        await kv.put(key=key, value=v)

    async with kv.metastore.session() as session:
        from sqlmodel import select as sm_select

        result = await session.exec(sm_select(KVEntry).where(KVEntry.key == key))
        entry = result.first()
    assert entry is not None
    payload = json.loads(entry.value)

    assert payload['v'] == 7
    assert payload['value'] == 'step-7'
    history = payload['history']
    assert len(history) == PROCEDURE_HISTORY_CAP, (
        f'history must be capped at {PROCEDURE_HISTORY_CAP}; got {len(history)}'
    )
    # FIFO drop: history retains v2..v6 (after v7 wrote, v6 was the prior active).
    history_versions = [h['v'] for h in history]
    assert history_versions == [2, 3, 4, 5, 6], (
        f'expected oldest-dropped FIFO retention v2..v6; got {history_versions}'
    )
    history_values = [h['value'] for h in history]
    assert history_values == ['step-2', 'step-3', 'step-4', 'step-5', 'step-6']


@pytest.mark.asyncio
async def test_concurrent_writes_single_row_atomic(kv: KVService) -> None:
    """Two concurrent writers serialize via optimistic concurrency.

    After two ``gather``ed ``put`` calls on the same key (starting from v=1),
    the final version must be 3 and history must contain BOTH intermediate
    superseded entries (the original v=1 plus whichever writer landed first
    at v=2).
    """
    key = _unique_proc_key()
    await kv.put(key=key, value='baseline')

    await asyncio.gather(
        kv.put(key=key, value='writer-A'),
        kv.put(key=key, value='writer-B'),
    )

    async with kv.metastore.session() as session:
        from sqlmodel import select as sm_select

        result = await session.exec(sm_select(KVEntry).where(KVEntry.key == key))
        entry = result.first()
    assert entry is not None
    payload = json.loads(entry.value)

    assert payload['v'] == 3, (
        f'expected final v=3 (baseline → first writer → second writer); got {payload["v"]}'
    )
    assert payload['value'] in {'writer-A', 'writer-B'}

    history_versions = [h['v'] for h in payload['history']]
    assert history_versions == [1, 2], (
        f'history must hold both intermediates (v=1 baseline + v=2 first writer); '
        f'got {history_versions}'
    )
    superseded_values = {h['value'] for h in payload['history']}
    # baseline + the writer that landed at v=2.
    assert 'baseline' in superseded_values
    assert {'writer-A', 'writer-B'} & superseded_values, (
        'one of the two writers must appear at v=2 in history'
    )


@pytest.mark.asyncio
async def test_get_default_returns_unwrapped_active_value(kv: KVService) -> None:
    """Default ``get`` exposes the unwrapped string (back-compat) — not the envelope."""
    key = _unique_proc_key()
    await kv.put(key=key, value='active-procedure-text')
    await kv.put(key=key, value='updated-procedure-text')

    entry = await kv.get(key=key)
    assert entry is not None
    assert entry.value == 'updated-procedure-text', (
        'default get() must unwrap the envelope so existing procedure-naive callers '
        'see a string, not the JSON envelope'
    )


@pytest.mark.asyncio
async def test_get_include_history_exposes_value_version_history(kv: KVService) -> None:
    """``include_history=True`` swaps value→ {value, version, history}."""
    key = _unique_proc_key()
    await kv.put(key=key, value='v1')
    await kv.put(key=key, value='v2')
    await kv.put(key=key, value='v3')

    entry = await kv.get(key=key, include_history=True)
    assert entry is not None
    assert isinstance(entry.value, dict)
    assert entry.value['value'] == 'v3'
    assert entry.value['version'] == 3
    history = entry.value['history']
    assert [h['v'] for h in history] == [1, 2]
    assert [h['value'] for h in history] == ['v1', 'v2']


@pytest.mark.asyncio
async def test_non_procedure_key_get_shape_unchanged(kv: KVService) -> None:
    """Non-procedure namespaces never invoke envelope unwrapping."""
    key = f'global:test:plain:{uuid4().hex[:8]}'
    await kv.put(key=key, value='plain-text')

    entry = await kv.get(key=key)
    assert entry is not None
    assert entry.value == 'plain-text'


# ---------------------------------------------------------------------------
# Project-scoped procedure round-trip — pins that the new
# `project:<id>:procedure:<verb>:<context>` form uses the same envelope
# wrap-on-write + unwrap-on-read as the global form. Per Hermes review on
# PR #182: `_procedure_put` is opaque to the key shape, but no test
# previously exercised the project form end-to-end through put → get.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_scoped_first_write_creates_envelope_at_v1(kv: KVService) -> None:
    """First write of a project-scoped procedure key wraps in the same v=1
    envelope as the global form."""
    key = _unique_project_proc_key()
    await kv.put(key=key, value='initial-project-procedure')

    entry = await kv.get(key=key, include_history=True)
    assert entry is not None
    assert isinstance(entry.value, dict)
    assert entry.value['value'] == 'initial-project-procedure'
    assert entry.value['version'] == 1
    assert entry.value['history'] == []


@pytest.mark.asyncio
async def test_project_scoped_versioning_and_history_round_trip(kv: KVService) -> None:
    """Subsequent writes to a project-scoped procedure key increment the
    version + append to history identically to the global form."""
    key = _unique_project_proc_key()
    await kv.put(key=key, value='v1-text')
    await kv.put(key=key, value='v2-text')
    await kv.put(key=key, value='v3-text')

    entry = await kv.get(key=key, include_history=True)
    assert entry is not None
    assert isinstance(entry.value, dict)
    assert entry.value['value'] == 'v3-text'
    assert entry.value['version'] == 3
    history = entry.value['history']
    assert [h['v'] for h in history] == [1, 2]
    assert [h['value'] for h in history] == ['v1-text', 'v2-text']


@pytest.mark.asyncio
async def test_project_scoped_default_get_unwraps_active_value(kv: KVService) -> None:
    """Default `get` on a project-scoped procedure unwraps the envelope —
    callers see the active string, not the JSON envelope."""
    key = _unique_project_proc_key()
    await kv.put(key=key, value='active-text')
    await kv.put(key=key, value='updated-text')

    entry = await kv.get(key=key)
    assert entry is not None
    assert entry.value == 'updated-text'


@pytest.mark.asyncio
async def test_project_scoped_with_ssh_form_project_id(kv: KVService) -> None:
    """Project IDs from SSH-form git remotes (containing `@` and embedded `:`)
    must round-trip through put → get without the rsplit-based parser
    mis-segmenting the key."""
    # `git@github.com:acme/foo-{uuid}` — SSH form with embedded colon
    project_id = f'git@github.com:acme/foo-{uuid4().hex[:8]}'
    key = f'project:{project_id}:procedure:write_pr:tag-{uuid4().hex[:8]}'

    await kv.put(key=key, value='ssh-form-procedure')

    entry = await kv.get(key=key, include_history=True)
    assert entry is not None
    assert isinstance(entry.value, dict)
    assert entry.value['value'] == 'ssh-form-procedure'
    assert entry.value['version'] == 1
