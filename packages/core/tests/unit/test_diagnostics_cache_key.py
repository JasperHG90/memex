from datetime import datetime, timedelta, timezone
from uuid import uuid4

from memex_core.diagnostics import cache_key


def test_cache_key_invalidates_on_ingestion():
    vault_id = uuid4()
    t0 = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)

    k_before = cache_key(vault_id, 10, t0)
    k_after = cache_key(vault_id, 11, t1)

    assert k_before != k_after
    assert len(k_before) == 64
    assert len(k_after) == 64


def test_cache_key_per_vault():
    vault_a = uuid4()
    vault_b = uuid4()
    t = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    assert cache_key(vault_a, 10, t) != cache_key(vault_b, 10, t)


def test_cache_key_count_zero():
    vault_id = uuid4()
    t = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    k_empty = cache_key(vault_id, 0, None)
    k_one = cache_key(vault_id, 1, t)

    assert k_empty != k_one
    assert len(k_empty) == 64


def test_cache_key_deterministic():
    vault_id = uuid4()
    t = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    k1 = cache_key(vault_id, 5, t)
    k2 = cache_key(vault_id, 5, t)

    assert k1 == k2


def test_cache_key_param_change_invalidates():
    vault_id = uuid4()
    t = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    k_default = cache_key(vault_id, 10, t)
    k_alt = cache_key(vault_id, 10, t, params={'n_neighbors': 30})

    assert k_default != k_alt
