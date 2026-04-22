"""Tiny in-process TTL cache for vault-resolution lookups.

Keeps the plugin from re-querying Memex's KV store on every session start
when nothing has changed. Caches both hits and misses so a session that
has no binding doesn't repeatedly hit the network.

Single-process, thread-safe, no external dependencies. The cache is
module-level (singleton) so all provider instances within a process share
it; tests can call ``clear_vault_cache()`` to reset between cases.
"""

from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

V = TypeVar('V')

_DEFAULT_TTL_SECONDS = 300.0


class TtlCache(Generic[V]):
    """Thread-safe key/value cache with a per-entry expiry."""

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # key -> (value, expires_at_monotonic)
        self._data: dict[str, tuple[V, float]] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, key: str) -> tuple[bool, V | None]:
        """Return ``(hit, value)``. ``hit=False`` means the caller must fetch.

        ``value`` may be a sentinel-style ``None`` if the cached lookup itself
        was a miss (e.g. no KV binding). Callers should only fetch when
        ``hit`` is False.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                # Expired — clean up and signal a miss.
                del self._data[key]
                return False, None
            return True, value

    def set(self, key: str, value: V) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Module-level vault-resolution cache
# ---------------------------------------------------------------------------

_vault_cache: TtlCache[str | None] = TtlCache(ttl_seconds=_DEFAULT_TTL_SECONDS)


def vault_cache() -> TtlCache[str | None]:
    """Return the process-wide vault-resolution cache."""
    return _vault_cache


def clear_vault_cache() -> None:
    """Clear the process-wide vault-resolution cache (test helper)."""
    _vault_cache.clear()


def configure_vault_cache(ttl_seconds: float) -> None:
    """Replace the singleton with a fresh cache at ``ttl_seconds`` TTL.

    Tests use this to shrink the TTL for expiry assertions; production
    callers should leave the default.
    """
    global _vault_cache
    _vault_cache = TtlCache(ttl_seconds=ttl_seconds)


__all__ = [
    'TtlCache',
    'clear_vault_cache',
    'configure_vault_cache',
    'vault_cache',
]
