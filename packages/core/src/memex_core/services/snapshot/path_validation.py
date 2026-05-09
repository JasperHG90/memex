"""Path validation for the eval-mode snapshot-import route.

The route accepts a server-local snapshot directory path. Without strict
validation, an attacker who reaches the eval route could trigger reads of
arbitrary server files (the route reads ``manifest.json``, ``vault.json``,
note bodies, etc.).

Defense layers:

1. ``Path.resolve(strict=True)`` — must exist; resolves symlinks.
2. ``is_relative_to(allowlist_root)`` — must live under the configured root.
3. ``os.open(O_NOFOLLOW)`` + ``/proc/self/fd/N`` realpath re-check on every
   file actually opened — defends against post-validation symlink swaps
   (TOCTOU).

The allowlist root defaults to platformdirs' user cache directory
(``~/.cache/memex-eval/`` on Linux) and is overridable via the
``MEMEX_EVAL_SNAPSHOT_ROOT`` env var. The eval client's snapshot cache
shares this root by default — see
``memex_eval.suite.snapshot_cache.default_cache_root``. They MUST agree:
the cache populate path posts the cache directory to the export route,
which validates it against this allowlist.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs


ALLOWLIST_ROOT_ENV = 'MEMEX_EVAL_SNAPSHOT_ROOT'


def _default_allowlist_root() -> str:
    """Resolve the platform-default allowlist root.

    Wrapped in a function (vs. a module-level constant) so tests can
    monkeypatch ``platformdirs.user_cache_dir`` if needed.
    """
    return platformdirs.user_cache_dir('memex-eval', 'memex')


class SnapshotPathError(ValueError):
    """Raised when a snapshot path fails validation."""


def get_allowlist_root() -> Path:
    """Resolve the configured allowlist root.

    Always returns a fully resolved (symlink-followed, absolute) path. The
    root is created if it doesn't exist — refusing all imports just because
    nobody has run ``memex-eval snapshot create`` yet would be a worse UX
    than creating an empty directory.
    """
    raw = os.environ.get(ALLOWLIST_ROOT_ENV) or _default_allowlist_root()
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def validate_snapshot_dir(path: str | Path, *, allowlist_root: Path | None = None) -> Path:
    """Validate a candidate snapshot directory.

    Returns the resolved absolute path on success. Raises ``SnapshotPathError``
    on any policy violation.
    """
    root = allowlist_root if allowlist_root is not None else get_allowlist_root()
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise SnapshotPathError(f'Snapshot path does not exist: {path}') from e
    if not resolved.is_dir():
        raise SnapshotPathError(f'Snapshot path is not a directory: {resolved}')
    if not resolved.is_relative_to(root):
        raise SnapshotPathError(
            f'Snapshot path {resolved} is outside the allowlist root {root}. '
            f'Set {ALLOWLIST_ROOT_ENV} or move the snapshot under {root}.'
        )
    return resolved


def validate_path_under_root(
    path: str | Path,
    *,
    allowlist_root: Path | None = None,
    must_exist: bool = False,
) -> Path:
    """Validate a candidate path lives under the allowlist root.

    Unlike ``validate_snapshot_dir`` this does NOT require the path to
    already exist (``must_exist=False`` by default) — used by the export
    route, which is going to *create* the destination dir. Resolution uses
    ``Path.resolve(strict=False)`` so a non-existent leaf still resolves
    its parents. The check then walks up to the closest existing ancestor
    and verifies THAT is under the root, defending against symlink/escape
    attacks via parent components.
    """
    root = allowlist_root if allowlist_root is not None else get_allowlist_root()
    candidate = Path(path).expanduser()
    if must_exist:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as e:
            raise SnapshotPathError(f'Path does not exist: {path}') from e
    else:
        resolved = candidate.resolve(strict=False)

    # Resolve the deepest existing ancestor strictly so symlinks in the
    # path's existing prefix are followed before the containment check.
    probe = resolved
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.exists():
        probe_resolved = probe.resolve(strict=True)
        # Reattach the non-existent suffix and re-normalize. The reattached
        # path may contain `..` components from the candidate's lexical
        # form; resolve(strict=False) collapses them so the containment
        # check below sees the canonical path.
        suffix = resolved.relative_to(probe) if resolved != probe else Path()
        if str(suffix) != '.':
            resolved = (probe_resolved / suffix).resolve(strict=False)
        else:
            resolved = probe_resolved

    if not resolved.is_relative_to(root):
        raise SnapshotPathError(
            f'Path {resolved} is outside the allowlist root {root}. '
            f'Set {ALLOWLIST_ROOT_ENV} or use a path under {root}.'
        )
    return resolved


def open_validated(path: Path, *, expected_root: Path) -> int:
    """Open a file with O_NOFOLLOW and verify the resolved path is still
    under ``expected_root`` after open (TOCTOU defense).

    Returns a file descriptor. Caller is responsible for closing it.
    """
    if not path.is_absolute():
        raise SnapshotPathError(f'Refusing to open non-absolute path: {path}')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        # On Linux, /proc/self/fd/N realpaths to the actual opened file.
        # Mismatch = symlink swap between resolve() and open().
        proc_link = f'/proc/self/fd/{fd}'
        if os.path.exists(proc_link):
            actual = Path(os.path.realpath(proc_link)).resolve()
            if not actual.is_relative_to(expected_root):
                raise SnapshotPathError(
                    f'Post-open path {actual} escaped allowlist root {expected_root}.'
                )
        return fd
    except Exception:
        os.close(fd)
        raise


def read_validated_text(path: Path, *, expected_root: Path) -> str:
    fd = open_validated(path, expected_root=expected_root)
    try:
        with os.fdopen(fd, 'rb', closefd=True) as f:
            data = f.read()
    finally:
        # fdopen closes the fd on context exit; nothing to do here.
        pass
    return data.decode('utf-8')


def read_validated_bytes(path: Path, *, expected_root: Path) -> bytes:
    fd = open_validated(path, expected_root=expected_root)
    with os.fdopen(fd, 'rb', closefd=True) as f:
        return f.read()
