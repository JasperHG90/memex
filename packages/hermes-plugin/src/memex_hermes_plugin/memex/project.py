"""Per-project vault resolution via KV.

Vault binding lives in the Memex KV store under the ``app:hermes:*``
namespace — same convention as any other app on top of Memex (e.g. a
trading bot would use ``app:trading:*``). Aligns with
``VALID_NAMESPACES = ('global', 'user', 'project', 'app')`` server-side.

Lookup chain at session start (each is a single ``KV GET``):

1. ``app:hermes:project:<project_id>:vault`` — if a project_id is derivable
2. ``app:hermes:user:<user_id>:vault`` — if Hermes passed a user_id
3. ``app:hermes:agent:<agent_identity>:vault`` — if Hermes passed an agent identity
4. Plugin config ``vault_id`` (``MEMEX_VAULT``)
5. None — the caller falls back to Memex's server-side default

Bind a vault the obvious way:

    memex kv put "app:hermes:user:10650075:vault" my-personal-vault
    memex kv put "app:hermes:project:github.com/acme/foo:vault" foo-vault

Lookups are cached in-process via :mod:`.cache` (5-minute TTL by default)
so repeated session starts within the same long-running plugin process
don't re-query Memex.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .async_bridge import run_sync
from .cache import vault_cache


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
    """Normalize a git remote URL to a stable identifier.

    Steps (in order):
    - Strip trailing ``.git``
    - Strip basic-auth prefix from ``https://user:pass@host/path``
    - Strip the URL scheme (``https://`` etc.)

    SSH URLs (``git@host:path``) are returned as-is minus the ``.git``
    suffix so the same project resolves to a stable id regardless of
    transport.
    """
    url = re.sub(r'\.git$', '', url)
    url = re.sub(r'^https://[^@]*@', 'https://', url)
    url = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', '', url)
    return url


# ---------------------------------------------------------------------------
# KV key helpers
# ---------------------------------------------------------------------------

# All Hermes-plugin state lives under app:hermes:* so it can coexist cleanly
# with any other app's KV usage (e.g. app:trading:*).
KV_NAMESPACE = 'app:hermes'


def project_vault_kv_key(project_id: str) -> str:
    return f'{KV_NAMESPACE}:project:{project_id}:vault'


def user_vault_kv_key(user_id: str) -> str:
    return f'{KV_NAMESPACE}:user:{user_id}:vault'


def agent_vault_kv_key(agent_identity: str) -> str:
    return f'{KV_NAMESPACE}:agent:{agent_identity}:vault'


# ---------------------------------------------------------------------------
# Vault resolution
# ---------------------------------------------------------------------------


async def _kv_lookup(api: Any, key: str) -> str | None:
    """Look up a KV value. Returns the string value or None on miss/error.

    ``RemoteMemexAPI.kv_get`` returns a ``KVEntryDTO`` (pydantic model with a
    ``.value: str``) or None. Handles dict/str returns too for robustness
    against client-shape changes.
    """
    try:
        result = await api.kv_get(key)
    except Exception:
        return None
    if result is None:
        return None
    value = getattr(result, 'value', None)
    if isinstance(value, str):
        return value
    if isinstance(result, dict):
        dict_value = result.get('value')
        return dict_value if isinstance(dict_value, str) else None
    if isinstance(result, str):
        return result
    return None


def _resolve_kv_key(api: Any, kv_key: str) -> str | None:
    """Look up ``kv_key`` against Memex KV with TTL caching."""
    cache = vault_cache()
    hit, cached = cache.get(kv_key)
    if hit:
        return cached
    value = run_sync(_kv_lookup(api, kv_key), timeout=5.0)
    cache.set(kv_key, value)
    return value


def resolve_vault(
    api: Any,
    *,
    project_id: str | None,
    agent_identity: str | None,
    user_id: str | None,
    config_vault: str | None,
) -> str | None:
    """Resolve the active vault by walking the KV chain, then config.

    See module docstring for the lookup chain and KV key conventions.
    """
    candidates: list[str] = []
    if project_id:
        candidates.append(project_vault_kv_key(project_id))
    if user_id:
        candidates.append(user_vault_kv_key(user_id))
    if agent_identity:
        candidates.append(agent_vault_kv_key(agent_identity))

    for kv_key in candidates:
        value = _resolve_kv_key(api, kv_key)
        if value:
            return value

    if config_vault:
        return config_vault

    return None


__all__ = [
    'KV_NAMESPACE',
    'agent_vault_kv_key',
    'derive_project_id',
    'project_vault_kv_key',
    'resolve_vault',
    'user_vault_kv_key',
]
