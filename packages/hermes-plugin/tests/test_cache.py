"""Tests for the TTL cache used by vault resolution.

Verifies hit/miss behaviour, TTL expiry, and that the singleton cache is
exercised by ``resolve_vault`` so repeat lookups don't hit Memex.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_common.schemas import KVEntryDTO

from memex_hermes_plugin.memex.cache import (
    TtlCache,
    clear_vault_cache,
    configure_vault_cache,
    vault_cache,
)
from memex_hermes_plugin.memex.project import resolve_vault


def _kv_entry(value: str) -> KVEntryDTO:
    now = datetime.now(timezone.utc)
    return KVEntryDTO(id=uuid4(), key='k', value=value, created_at=now, updated_at=now)


class TestTtlCache:
    def test_miss_returns_false(self):
        c: TtlCache[str | None] = TtlCache(ttl_seconds=60)
        hit, value = c.get('absent')
        assert hit is False
        assert value is None

    def test_hit_returns_value(self):
        c: TtlCache[str | None] = TtlCache(ttl_seconds=60)
        c.set('k', 'v')
        hit, value = c.get('k')
        assert hit is True
        assert value == 'v'

    def test_caches_none_explicitly(self):
        """A cached miss (None) should still register as a hit so the caller
        doesn't repeatedly fetch when the upstream value is genuinely absent."""
        c: TtlCache[str | None] = TtlCache(ttl_seconds=60)
        c.set('k', None)
        hit, value = c.get('k')
        assert hit is True
        assert value is None

    def test_expiry(self):
        c: TtlCache[str | None] = TtlCache(ttl_seconds=0.05)
        c.set('k', 'v')
        time.sleep(0.1)
        hit, value = c.get('k')
        assert hit is False
        assert value is None

    def test_clear(self):
        c: TtlCache[str | None] = TtlCache(ttl_seconds=60)
        c.set('a', '1')
        c.set('b', '2')
        c.clear()
        assert c.get('a') == (False, None)
        assert c.get('b') == (False, None)
        assert len(c) == 0


class TestVaultCacheIntegration:
    """resolve_vault() must consult the cache before hitting the network."""

    def setup_method(self) -> None:
        configure_vault_cache(ttl_seconds=60)

    def teardown_method(self) -> None:
        clear_vault_cache()

    def test_repeat_resolve_does_not_call_kv_again(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=_kv_entry('proj-vault'))

        first = resolve_vault(
            api, project_id='p', agent_identity=None, user_id=None, config_vault=None
        )
        second = resolve_vault(
            api, project_id='p', agent_identity=None, user_id=None, config_vault=None
        )

        assert first == 'proj-vault' == second
        # Only one KV call across two resolves.
        assert api.kv_get.await_count == 1

    def test_cached_miss_short_circuits_too(self):
        api = Mock()
        api.kv_get = AsyncMock(return_value=None)

        resolve_vault(api, project_id='p', agent_identity=None, user_id=None, config_vault='cfg')
        first_count = api.kv_get.await_count

        resolve_vault(api, project_id='p', agent_identity=None, user_id=None, config_vault='cfg')

        # Second call must not have triggered another KV lookup.
        assert api.kv_get.await_count == first_count

    def test_short_ttl_re_fetches_after_expiry(self):
        configure_vault_cache(ttl_seconds=0.05)
        api = Mock()
        api.kv_get = AsyncMock(return_value=_kv_entry('v'))

        resolve_vault(api, project_id='p', agent_identity=None, user_id=None, config_vault=None)
        time.sleep(0.1)
        resolve_vault(api, project_id='p', agent_identity=None, user_id=None, config_vault=None)

        assert api.kv_get.await_count == 2

    def test_singleton_is_shared(self):
        """All callers see the same cache instance."""
        a = vault_cache()
        b = vault_cache()
        assert a is b
