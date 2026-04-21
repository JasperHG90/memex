"""Per-project vault resolution.

Ports the pattern from Memex's Claude Code plugin (``scripts/on_session_start.sh``):
1. Derive a portable ``project_id`` from git remote origin or the CWD.
2. Look up ``project:<project_id>:vault`` in the Memex KV store.
3. Fall back in order: gateway ``user_id`` / ``agent_identity`` vault → config
   ``vault_id`` → Memex default.

Keeping the same KV namespace as the Claude Code plugin means users bind a
project once and both plugins resolve the same vault.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .async_bridge import run_sync


def derive_project_id(cwd: Path | None = None, home: Path | None = None) -> str:
    """Derive a project identifier portable across machines.

    Prefers the git remote origin URL (normalized: no .git, no scheme, no
    basic-auth prefix). Falls back to ``$HOME``-relative CWD or absolute CWD.
    """
    cwd = cwd or Path.cwd()
    home = home or Path(os.environ.get('HOME', str(Path.home())))

    remote = _git_remote_origin(cwd)
    if remote:
        return _normalize_remote(remote)

    try:
        relative = cwd.resolve().relative_to(home.resolve())
        return str(relative)
    except ValueError:
        return str(cwd.resolve())


def _git_remote_origin(cwd: Path) -> str | None:
    """Return the git remote origin URL for ``cwd`` or None if unavailable."""
    try:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _normalize_remote(url: str) -> str:
    """Normalize a git remote URL to match the claude-code-plugin shell script.

    Steps (in order):
    - Strip trailing ``.git``
    - Strip basic-auth prefix from ``https://user:pass@host/path``
    - Strip the URL scheme (``https://`` etc.)

    Note: SSH URLs (``git@host:path``) are returned as-is minus the ``.git``
    suffix — matching the shell script's behavior so KV keys are interchangeable
    between the Claude Code and Hermes plugins.
    """
    url = re.sub(r'\.git$', '', url)
    url = re.sub(r'^https://[^@]*@', 'https://', url)
    url = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', '', url)
    return url


def project_vault_kv_key(project_id: str) -> str:
    """Return the KV key under which the per-project vault is stored."""
    return f'project:{project_id}:vault'


async def _kv_lookup(api: Any, key: str) -> str | None:
    """Look up a KV value. Returns None on miss or error.

    ``RemoteMemexAPI.kv_get`` returns a ``KVEntryDTO`` (pydantic model with a
    ``value: str`` attribute) or None. Handles dict/str returns too for
    robustness against client-shape changes.
    """
    try:
        result = await api.kv_get(key)
    except Exception:
        return None
    if result is None:
        return None
    # KVEntryDTO / similar pydantic model.
    value = getattr(result, 'value', None)
    if isinstance(value, str):
        return value
    # Defensive fallbacks in case the client evolves.
    if isinstance(result, dict):
        dict_value = result.get('value')
        return dict_value if isinstance(dict_value, str) else None
    if isinstance(result, str):
        return result
    return None


async def _vault_exists(api: Any, identifier: str) -> bool:
    """Return True if a vault with ``identifier`` (name or UUID) exists."""
    try:
        await api.resolve_vault_identifier(identifier)
        return True
    except Exception:
        return False


def resolve_vault(
    api: Any,
    *,
    project_id: str,
    agent_identity: str | None,
    user_id: str | None,
    config_vault: str | None,
) -> str | None:
    """Resolve the active vault, returning its name or None if nothing found.

    Synchronous wrapper over async KV + vault checks; runs on the shared loop.
    Priority order:
    1. ``project:<project_id>:vault`` in KV
    2. ``hermes:user:<user_id>`` if a vault with that name exists
    3. ``hermes:agent:<agent_identity>`` if a vault with that name exists
    4. Plugin config ``vault_id``
    5. None (caller uses Memex default)
    """
    candidates: list[str] = []

    kv_key = project_vault_kv_key(project_id)
    kv_vault = run_sync(_kv_lookup(api, kv_key), timeout=5.0)
    if kv_vault:
        return kv_vault

    if user_id:
        candidates.append(f'hermes:user:{user_id}')
    if agent_identity:
        candidates.append(f'hermes:agent:{agent_identity}')

    for candidate in candidates:
        if run_sync(_vault_exists(api, candidate), timeout=3.0):
            return candidate

    if config_vault:
        return config_vault

    return None


__all__ = [
    'derive_project_id',
    'project_vault_kv_key',
    'resolve_vault',
]
