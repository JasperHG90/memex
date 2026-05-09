"""Snapshot cache for the eval suite framework.

When a suite run sets ``--from-snapshot auto``, the runner looks up a
cache entry keyed by ``(suite_name, sources_hash)`` and:

- on cache hit (and not ``--reingest``): imports the snapshot via V12,
  skipping LLM extraction;
- on cache miss (or ``--reingest``): runs the normal ingest+extract path,
  then exports the resulting vault into the cache so the next run hits.

The cache root resolution order is:

1. Explicit ``--snapshot-cache-dir`` flag (CLI)
2. ``MEMEX_EVAL_SNAPSHOT_ROOT`` environment variable
3. ``platformdirs.user_cache_dir('memex-eval', 'memex')`` default

A cache entry is considered valid only when both ``manifest.json`` and
``_complete.marker`` exist — the marker is written last so a partial /
crashed populate is observable as a miss.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import platformdirs


logger = logging.getLogger('memex_eval.suite.snapshot_cache')


CACHE_COMPLETE_MARKER = '_complete.marker'


def default_cache_root() -> Path:
    """Resolve the default cache root via platformdirs.

    Falls back to a sane location if the platform-specific cache dir is
    unwritable. Caller MUST be prepared for the dir to not exist yet.
    """
    return Path(platformdirs.user_cache_dir('memex-eval', 'memex'))


def resolve_cache_root(explicit: str | Path | None = None) -> Path:
    """Resolve cache root with priority: explicit > env > platformdirs.

    Always returns an absolute path; creates the dir if absent so callers
    can immediately write.
    """
    if explicit is not None:
        root = Path(explicit).expanduser()
    else:
        env = os.environ.get('MEMEX_EVAL_SNAPSHOT_ROOT')
        root = Path(env).expanduser() if env else default_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def cache_key(suite_name: str, sources_hash: str) -> str:
    """Cache directory name = ``<suite_name>-<sources_hash[:16]>``.

    Truncating the hash keeps directory names ergonomic; 16 hex chars =
    64 bits of collision space, ample for the eval workflow.
    """
    return f'{suite_name}-{sources_hash[:16]}'


@dataclass(frozen=True)
class CacheLookup:
    cache_root: Path
    cache_path: Path
    hit: bool


def lookup(cache_root: Path, suite_name: str, sources_hash: str) -> CacheLookup:
    """Probe the cache.

    A cache hit requires both ``manifest.json`` and ``_complete.marker``
    to be present. A directory missing either is a partially-populated
    leftover from a crashed run; treat as a miss and warn so a sweep can
    pick it up.
    """
    cache_path = cache_root / cache_key(suite_name, sources_hash)
    if not cache_path.is_dir():
        return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=False)

    has_manifest = (cache_path / 'manifest.json').is_file()
    has_marker = (cache_path / CACHE_COMPLETE_MARKER).is_file()
    if has_manifest and has_marker:
        return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=True)

    logger.warning(
        'Snapshot cache at %s is incomplete (manifest=%s, marker=%s); treating as miss',
        cache_path,
        has_manifest,
        has_marker,
    )
    return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=False)


def mark_complete(cache_path: Path) -> None:
    """Write the ``_complete.marker`` sentinel file.

    Called AFTER the V3 export has finished writing manifest.json. The
    marker order matters: lookup() requires both files to be present, so
    a crash between manifest write and marker write leaves the cache
    visible as a miss (correct fail-safe).
    """
    (cache_path / CACHE_COMPLETE_MARKER).write_text(
        'cache populated by memex-eval suite runner\n', encoding='utf-8'
    )


def clear_cache_entry(cache_path: Path) -> None:
    """Remove a (potentially partial) cache entry. Used by --reingest to
    force a re-export over an existing entry.

    Idempotent: missing path is treated as already-cleared.
    """
    if cache_path.exists():
        shutil.rmtree(cache_path, ignore_errors=False)
