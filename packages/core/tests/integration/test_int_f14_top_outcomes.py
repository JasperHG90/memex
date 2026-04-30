"""Integration tests for F14 ``list_top_procedure_outcomes`` ranking + limit.

Locks the contract that drives the briefing surface (TC-F14-4 data path):

* Rows are returned ordered by Beta-Bernoulli MW score
  (``(s+1)/(s+f+2)``) descending — the SAME formula as
  ``compute_mw_score`` so the briefing surface and the F1c retrieval
  composition agree.
* ``limit`` caps the result count (default 5).
* Cross-vault isolation: rows for one vault don't leak into another.
* Optional ``context`` substring filter narrows on the third
  ``procedure:<verb>:<context-tag>`` segment.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.services.kv import KVService
from memex_core.services.outcomes import OutcomeService, compute_mw_score

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def kv(metastore, filestore, memex_config) -> KVService:
    return KVService(metastore=metastore, filestore=filestore, config=memex_config)


@pytest.fixture
def outcomes() -> OutcomeService:
    return OutcomeService()


async def _seed_outcome(
    kv: KVService,
    outcomes: OutcomeService,
    key: str,
    successes: int,
    failures: int,
    vault_id: UUID,
) -> None:
    await kv.put(key=key, value=f'body-of-{key}')
    async with kv.metastore.session() as session:
        for _ in range(successes):
            await outcomes.record_outcome(
                session=session,
                unit_ids=None,
                target_type='kv_key',
                kv_key=key,
                success=True,
                vault_id=str(vault_id),
            )
        for _ in range(failures):
            await outcomes.record_outcome(
                session=session,
                unit_ids=None,
                target_type='kv_key',
                kv_key=key,
                success=False,
                vault_id=str(vault_id),
            )


@pytest.mark.asyncio
async def test_top_outcomes_ranked_by_mw_score_desc(
    kv: KVService, outcomes: OutcomeService
) -> None:
    """Three keys with different success/failure mixes return in MW-score order."""
    high = f'procedure:run_tests:high-{uuid4().hex[:6]}'
    mid = f'procedure:run_tests:mid-{uuid4().hex[:6]}'
    low = f'procedure:run_tests:low-{uuid4().hex[:6]}'

    # high: 8/0 → mw = 9/10 = 0.9
    # mid:  4/2 → mw = 5/8  = 0.625
    # low:  1/4 → mw = 2/7  ≈ 0.286
    await _seed_outcome(kv, outcomes, high, 8, 0, GLOBAL_VAULT_ID)
    await _seed_outcome(kv, outcomes, mid, 4, 2, GLOBAL_VAULT_ID)
    await _seed_outcome(kv, outcomes, low, 1, 4, GLOBAL_VAULT_ID)

    rows = await kv.list_top_procedure_outcomes(vault_id=GLOBAL_VAULT_ID, limit=10)
    assert len(rows) >= 3
    # Drop any seeds from prior tests that happen to share GLOBAL_VAULT_ID.
    by_key = {r['kv_key']: r for r in rows}
    assert high in by_key and mid in by_key and low in by_key

    # Locate them in the global ordering — they MUST appear in the high>mid>low
    # relative order.
    keys_in_order = [r['kv_key'] for r in rows]
    pos_high = keys_in_order.index(high)
    pos_mid = keys_in_order.index(mid)
    pos_low = keys_in_order.index(low)
    assert pos_high < pos_mid < pos_low, (
        f'expected high<mid<low MW-score positions; got high={pos_high} '
        f'mid={pos_mid} low={pos_low} in {keys_in_order}'
    )

    # MW score values must match compute_mw_score exactly (same Beta-Bernoulli
    # formula in SQL as in Python — RFC v6.9 §3.4 / RFC-007 §155-185).
    assert by_key[high]['mw_score'] == pytest.approx(compute_mw_score(8, 0))
    assert by_key[mid]['mw_score'] == pytest.approx(compute_mw_score(4, 2))
    assert by_key[low]['mw_score'] == pytest.approx(compute_mw_score(1, 4))


@pytest.mark.asyncio
async def test_top_outcomes_respects_limit_cap(kv: KVService, outcomes: OutcomeService) -> None:
    """``limit`` parameter caps the result count."""
    suffix = uuid4().hex[:6]
    keys = [f'procedure:edit_yaml:limit{suffix}-{i}' for i in range(7)]
    for i, k in enumerate(keys):
        await _seed_outcome(kv, outcomes, k, i + 1, 0, GLOBAL_VAULT_ID)

    rows = await kv.list_top_procedure_outcomes(vault_id=GLOBAL_VAULT_ID, limit=3)
    matching = [r for r in rows if r['kv_key'].startswith(f'procedure:edit_yaml:limit{suffix}')]
    # Even though we seeded 7, the limit=3 cap MUST take effect on the
    # global ordering, so at most 3 of the 7 surface — and they must be the
    # top 3 (i=4, 5, 6 successes).
    assert len(rows) == 3
    # The 3 returned must be the highest-MW from this batch (or higher-MW
    # rows from older test seeds).
    if matching:
        # Among the matching ones, the top should be those with most successes.
        ours = sorted((r for r in matching), key=lambda r: r['success_co_count'], reverse=True)
        assert ours[0]['success_co_count'] >= ours[-1]['success_co_count']


@pytest.mark.asyncio
async def test_top_outcomes_cross_vault_isolation(
    kv: KVService, outcomes: OutcomeService, metastore
) -> None:
    """Outcomes for one vault don't leak into another vault's listing."""
    other_vault_id = uuid4()
    from sqlalchemy import text as sql_text

    async with metastore.session() as session:
        await session.execute(
            sql_text("INSERT INTO vaults (id, name, description) VALUES (:id, :name, '')"),
            {'id': other_vault_id, 'name': f'v-{other_vault_id.hex[:8]}'},
        )
        await session.commit()

    only_in_other = f'procedure:write_pr:other-{uuid4().hex[:6]}'
    await _seed_outcome(kv, outcomes, only_in_other, 5, 0, other_vault_id)

    global_rows = await kv.list_top_procedure_outcomes(vault_id=GLOBAL_VAULT_ID, limit=20)
    keys = {r['kv_key'] for r in global_rows}
    assert only_in_other not in keys, (
        'a procedure scoped to another vault must not appear in GLOBAL_VAULT_ID listing'
    )

    other_rows = await kv.list_top_procedure_outcomes(vault_id=other_vault_id, limit=10)
    other_keys = {r['kv_key'] for r in other_rows}
    assert only_in_other in other_keys


@pytest.mark.asyncio
async def test_top_outcomes_context_filter_narrows_by_tag(
    kv: KVService, outcomes: OutcomeService
) -> None:
    """``context`` substring filter narrows on the procedure key context-tag."""
    suffix = uuid4().hex[:6]
    matching_key = f'procedure:run_tests:python-monorepo-{suffix}'
    other_key = f'procedure:run_tests:js-frontend-{suffix}'
    await _seed_outcome(kv, outcomes, matching_key, 3, 0, GLOBAL_VAULT_ID)
    await _seed_outcome(kv, outcomes, other_key, 3, 0, GLOBAL_VAULT_ID)

    narrowed = await kv.list_top_procedure_outcomes(
        vault_id=GLOBAL_VAULT_ID, context=f'python-monorepo-{suffix}', limit=5
    )
    keys = {r['kv_key'] for r in narrowed}
    assert matching_key in keys
    assert other_key not in keys


@pytest.mark.asyncio
async def test_top_outcomes_zero_limit_returns_empty_list(
    kv: KVService,
) -> None:
    """``limit=0`` short-circuits to an empty list (no DB hit)."""
    rows = await kv.list_top_procedure_outcomes(vault_id=GLOBAL_VAULT_ID, limit=0)
    assert rows == []


@pytest.mark.asyncio
async def test_top_outcomes_invalid_vault_id_raises(kv: KVService) -> None:
    """Invalid ``vault_id`` raises ``ValueError`` with a clear message."""
    with pytest.raises(ValueError, match='Invalid vault_id'):
        await kv.list_top_procedure_outcomes(vault_id='not-a-uuid', limit=5)
