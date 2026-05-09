"""Snapshot cache for the eval suite framework.

When a suite run sets ``--from-snapshot auto``, the runner looks up a
cache entry keyed by ``(suite_name, sources_hash)`` and:

- on cache hit (and not ``--reingest``): imports the snapshot via V12,
  skipping LLM extraction;
- on cache miss (or ``--reingest``): runs the normal ingest+extract path,
  then exports the resulting vault into the cache so the next run hits.

The cache root resolution order is:

1. Explicit ``--snapshot-cache-dir`` flag (CLI)
2. ``MEMEX_EVAL_SNAPSHOT_ROOT`` environment variable (shared with the
   server's allowlist root, see
   ``memex_core.services.snapshot.path_validation``)
3. ``platformdirs.user_cache_dir('memex-eval', 'memex')`` default

The default INTENTIONALLY matches the server's allowlist default so the
populate path (which posts the cache directory to the export route)
passes the server's path-allowlist check without any env config.

A cache entry is considered valid only when both ``manifest.json`` and
``_complete.marker`` exist — the marker is written last so a partial /
crashed populate is observable as a miss.

Atomic publish: ``stage_path()`` returns a unique tmp directory under the
same cache root; the runner exports into it, calls ``mark_complete()``,
then ``publish()`` atomically renames it to the final cache slot. The
old entry (if any) is preserved until the new one is fully written and
only deleted after the rename succeeds.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import platformdirs


logger = logging.getLogger('memex_eval.suite.snapshot_cache')


CACHE_COMPLETE_MARKER = '_complete.marker'
_TMP_PREFIX = '.tmp-'
_OLD_PREFIX = '.old-'


def default_cache_root() -> Path:
    """Resolve the default cache root via platformdirs.

    Matches the server's allowlist default
    (``memex_core.services.snapshot.path_validation``) so the populate
    path lands under the server's allowlist by default.
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
    leftover from a crashed run; this function deletes it so the next
    populate starts from a clean slot (otherwise a fresh export
    commingles with stale files and corrupts the cache).
    """
    cache_path = cache_root / cache_key(suite_name, sources_hash)
    if not cache_path.is_dir():
        return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=False)

    has_manifest = (cache_path / 'manifest.json').is_file()
    has_marker = (cache_path / CACHE_COMPLETE_MARKER).is_file()
    if has_manifest and has_marker:
        return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=True)

    logger.warning(
        'Snapshot cache at %s is partial (manifest=%s, marker=%s); cleaning up',
        cache_path,
        has_manifest,
        has_marker,
    )
    try:
        shutil.rmtree(cache_path)
    except OSError as e:
        logger.warning('Failed to clean partial cache at %s: %s', cache_path, e)
    return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=False)


def mark_complete(cache_path: Path) -> None:
    """Write the ``_complete.marker`` sentinel file.

    Called AFTER the export has finished writing manifest.json. The
    marker order matters: lookup() requires both files to be present, so
    a crash between manifest write and marker write leaves the cache
    visible as a miss (correct fail-safe). Caller fsyncs by default —
    we open with O_DSYNC so the marker is durable before this function
    returns.
    """
    marker = cache_path / CACHE_COMPLETE_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_DSYNC', 0)
    fd = os.open(marker, flags, 0o644)
    try:
        os.write(fd, b'cache populated by memex-eval suite runner\n')
    finally:
        os.close(fd)


def stage_path(cache_root: Path, cache_key_str: str) -> Path:
    """Allocate a unique tmp staging directory inside ``cache_root``.

    Two concurrent populates race-safe: each gets a distinct uuid suffix
    and writes independently. The race resolves at ``publish()`` time —
    one wins, the other replaces.
    """
    tmp = cache_root / f'{_TMP_PREFIX}{cache_key_str}-{uuid.uuid4().hex}'
    tmp.mkdir(parents=True, exist_ok=False)
    return tmp


def publish(staged: Path, final: Path) -> None:
    """Atomically replace ``final`` with ``staged``.

    ``staged`` MUST already contain a complete export (incl. the
    ``_complete.marker``). On success, ``staged`` no longer exists and
    ``final`` points to a complete cache entry.

    If ``final`` already exists (e.g. ``--reingest`` or a concurrent
    populate beat us), it is moved aside to a ``.old-`` prefix and
    rmtree'd AFTER the new entry is in place — so a crash between the
    two renames leaves a discoverable leftover (sweepable) rather than
    losing the cache slot.
    """
    if not (staged / CACHE_COMPLETE_MARKER).is_file():
        raise RuntimeError(f'Refusing to publish staged cache at {staged}: missing marker')

    if final.exists():
        old = final.parent / f'{_OLD_PREFIX}{final.name}-{uuid.uuid4().hex}'
        os.rename(final, old)
        try:
            os.rename(staged, final)
        except Exception:
            # Restore the original on rename failure so we don't lose
            # the previously-valid cache entry.
            with _suppress_rename_errors():
                os.rename(old, final)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.rename(staged, final)


def discard_staged(staged: Path) -> None:
    """Best-effort rmtree of a staged dir on populate failure."""
    shutil.rmtree(staged, ignore_errors=True)


def clear_cache_entry(cache_path: Path) -> None:
    """Remove a (potentially partial) cache entry. Used by the sweep CLI
    to force-clean a cache slot.

    Idempotent: missing path is treated as already-cleared.
    """
    if cache_path.exists():
        shutil.rmtree(cache_path, ignore_errors=False)


class _suppress_rename_errors:
    """Tiny context manager: swallow OSError from a recovery rename so
    the original exception (the one that triggered recovery) propagates
    cleanly. Standalone class because contextlib.suppress would also
    swallow the original raise."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)
