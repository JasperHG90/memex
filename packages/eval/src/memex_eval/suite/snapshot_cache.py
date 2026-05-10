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

platformdirs gives a stable per-user cache location across platforms
(``~/.cache/memex-eval/`` on Linux, ``~/Library/Caches/memex-eval/`` on
macOS). The cache is entirely client-side — there is no server-side
allowlist because import/export runs in-process in the eval runner.

A cache entry is considered valid only when both ``manifest.json`` and
``_complete.marker`` exist — the marker is written last so a partial /
crashed populate is observable as a miss.

Atomic publish: ``stage_path()`` returns a unique tmp directory under the
same cache root; the runner exports into it, calls ``mark_complete()``,
then ``publish()`` atomically renames it to the final cache slot under a
per-slot ``fcntl.flock`` advisory lock. Concurrent populates serialize on
the lock — losers see the winner's marker after acquiring and discard
their staged work, so the cache slot resolves to exactly one entry with
no leaked ``.old-`` / ``.tmp-`` dirs.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
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
_LOCK_PREFIX = '.lock-'


def default_cache_root() -> Path:
    """Resolve the default cache root via platformdirs.

    Gives a stable per-user cache location across platforms — the cache
    is purely client-side; no server agreement is needed.
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

    Valid layouts:

    - **Sharded** (current): ``vaults/_default/manifest.json`` +
      ``_complete.marker``. Per-vault subdirs under ``vaults/``.
    - **Flat legacy** (V12.0 single-vault): ``manifest.json`` +
      ``_complete.marker`` at the cache slot root.

    A directory missing the marker or any required manifest is a
    partially-populated leftover from a crashed run; this function
    deletes it so the next populate starts from a clean slot.
    """
    cache_path = cache_root / cache_key(suite_name, sources_hash)
    if not cache_path.is_dir():
        return CacheLookup(cache_root=cache_root, cache_path=cache_path, hit=False)

    has_marker = (cache_path / CACHE_COMPLETE_MARKER).is_file()
    sharded_manifest = (cache_path / 'vaults' / '_default' / 'manifest.json').is_file()
    flat_manifest = (cache_path / 'manifest.json').is_file()
    has_manifest = sharded_manifest or flat_manifest
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
    visible as a miss (correct fail-safe).

    Durability: ``O_DSYNC`` is requested when the platform supports it;
    we ALSO call ``os.fsync(fd)`` after write so platforms missing
    ``O_DSYNC`` (Windows, some BSDs) still persist before return. The
    parent directory is fsync'd separately so the marker's directory
    entry survives crashes.
    """
    marker = cache_path / CACHE_COMPLETE_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_DSYNC', 0)
    fd = os.open(marker, flags, 0o644)
    try:
        os.write(fd, b'cache populated by memex-eval suite runner\n')
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
    # fsync parent dir so the marker's dirent is durable.
    with contextlib.suppress(OSError):
        dfd = os.open(cache_path, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def stage_path(cache_root: Path, cache_key_str: str) -> Path:
    """Allocate a unique tmp staging directory inside ``cache_root``.

    Two concurrent populates each get a distinct uuid suffix and write
    independently. ``publish()`` then serializes the final rename via a
    per-slot ``fcntl.flock`` so the cache slot ends up holding exactly
    one entry.
    """
    tmp = cache_root / f'{_TMP_PREFIX}{cache_key_str}-{uuid.uuid4().hex}'
    tmp.mkdir(parents=True, exist_ok=False)
    return tmp


@contextlib.contextmanager
def _slot_lock(cache_root: Path, slot_name: str):
    """Acquire an advisory ``fcntl.flock`` on a per-slot lock file.

    The lock file lives at ``<cache_root>/.lock-<slot_name>`` and is
    NEVER deleted — flock semantics on Linux require the file to remain
    on disk for fairness. Held only for the duration of the publish
    rename, never around the (slow) export, so the lock window is tiny.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / f'{_LOCK_PREFIX}{slot_name}'
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def publish(staged: Path, final: Path) -> None:
    """Atomically replace ``final`` with ``staged``.

    ``staged`` MUST already contain a complete export (incl. the
    ``_complete.marker``). On success ``staged`` no longer exists and
    ``final`` points to a complete cache entry.

    Concurrency: serialized via a per-slot ``fcntl.flock`` on
    ``<final.parent>/.lock-<final.name>`` so two concurrent populates
    can't interleave their renames. The lock is held only across the
    rename pair (not the export), so its window is microseconds.
    Last-writer-wins by design — every populate produces a valid
    independently-exported snapshot, so overwriting is correctness-
    neutral; the lock just keeps the on-disk shape consistent.

    Cross-filesystem ``EXDEV`` raises ``RuntimeError`` with an
    actionable hint pointing the user at ``MEMEX_EVAL_SNAPSHOT_ROOT``.
    """
    if not (staged / CACHE_COMPLETE_MARKER).is_file():
        raise RuntimeError(f'Refusing to publish staged cache at {staged}: missing marker')

    with _slot_lock(final.parent, final.name):
        if final.exists():
            old = final.parent / f'{_OLD_PREFIX}{final.name}-{uuid.uuid4().hex}'
            try:
                os.rename(final, old)
            except OSError as e:
                _raise_with_xdev_hint(e, staged, final)
            try:
                os.rename(staged, final)
            except OSError as e:
                # Restore the original so we don't lose the
                # previously-valid cache entry.
                try:
                    os.rename(old, final)
                except OSError as restore_err:
                    logger.error(
                        'Cache publish recovery failed at %s; leaked .old- dir at %s '
                        '(original error: %s; recovery error: %s)',
                        final,
                        old,
                        e,
                        restore_err,
                    )
                _raise_with_xdev_hint(e, staged, final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            try:
                os.rename(staged, final)
            except OSError as e:
                _raise_with_xdev_hint(e, staged, final)


def _raise_with_xdev_hint(err: OSError, staged: Path, final: Path) -> None:
    """Re-raise OSError, wrapping cross-filesystem (EXDEV) errors with a
    config hint. Used by publish() so users see actionable text rather
    than a bare 'Invalid cross-device link'."""
    if err.errno == errno.EXDEV:
        raise RuntimeError(
            f'Cannot publish snapshot cache: {staged} and {final} live on '
            f'different filesystems. The cache root must be on the same fs '
            f'as its tmp staging dir. Set MEMEX_EVAL_SNAPSHOT_ROOT to a path '
            f'on the same filesystem as the export target.'
        ) from err
    raise err


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
