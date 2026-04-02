"""API key authentication middleware for the Memex server."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from memex_common.config import (
    ApiKeyConfig,
    AuthConfig,
    Permission,
    Policy,
    POLICY_PERMISSIONS,
)
from memex_core.context import set_actor

if TYPE_CHECKING:
    from memex_core.api import MemexAPI
    from memex_core.services.audit import AuditService

logger = logging.getLogger('memex.core.server')


# ---------------------------------------------------------------------------
# Auth context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """Resolved auth state for the current request."""

    key_prefix: str  # first 8 chars for audit logging
    key_name: str | None  # human-readable label from ApiKeyConfig.description
    policy: Policy
    permissions: frozenset[Permission]
    vault_ids: list[str] | None  # None = all vaults (resolved lazily)
    read_vault_ids: list[str] | None  # additional read-only vault access


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def _validate_key(api_key: str, auth_config: AuthConfig) -> bool:
    """Check api_key against all configured keys using constant-time comparison.

    Uses ``secrets.compare_digest`` to prevent timing side-channel attacks.
    """
    for key_config in auth_config.keys:
        if secrets.compare_digest(api_key, key_config.key.get_secret_value()):
            return True
    return False


def _resolve_key(api_key: str, auth_config: AuthConfig) -> ApiKeyConfig | None:
    """Find the matching ApiKeyConfig for an API key.

    Uses ``secrets.compare_digest`` to prevent timing side-channel attacks.
    """
    for key_config in auth_config.keys:
        if secrets.compare_digest(api_key, key_config.key.get_secret_value()):
            return key_config
    return None


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _get_audit_service(request: Request) -> AuditService | None:
    """Safely retrieve the audit service from app state (may not be initialised yet)."""
    return getattr(request.app.state, 'audit_service', None)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def setup_auth(app: FastAPI, auth_config: AuthConfig) -> None:
    """Configure authentication on *app*.

    Stores the config on ``app.state.auth_config``.  The actual request
    checking is done by :func:`auth_middleware`, which must be registered
    on the app **before** startup (e.g. at module level via
    ``@app.middleware``).  This function is called from ``lifespan()`` so
    the config can be parsed from env vars set by test fixtures.

    When ``auth_config.enabled`` is ``False`` this stores nothing — the
    middleware will see no config and pass all requests through.
    """
    if not auth_config.enabled:
        logger.info('API key authentication is disabled.')
        if hasattr(app.state, 'auth_config'):
            del app.state.auth_config
        return

    if not auth_config.keys:
        logger.warning(
            'Authentication is enabled but no API keys are configured. '
            'All authenticated requests will be rejected.',
        )

    # Store config on app.state so the middleware and other components can inspect it.
    app.state.auth_config = auth_config

    logger.info(
        'API key authentication enabled (%d key(s) configured, %d exempt path(s)).',
        len(auth_config.keys),
        len(auth_config.exempt_paths),
    )


async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """HTTP middleware that enforces API key authentication.

    Reads ``app.state.auth_config`` (set by :func:`setup_auth`).  If no
    config is present, all requests pass through (auth disabled).  This
    function must be registered on the app at import time so it is part
    of the middleware stack before the app starts.
    """
    auth_config: AuthConfig | None = getattr(request.app.state, 'auth_config', None)
    if auth_config is None:
        return await call_next(request)

    # CORS preflight requests never carry credentials; let CORSMiddleware handle them.
    if request.method == 'OPTIONS':
        return await call_next(request)

    if request.url.path in auth_config.exempt_paths:
        return await call_next(request)

    audit = _get_audit_service(request)
    api_key = request.headers.get('X-API-Key')

    if not api_key:
        if audit:
            audit.log(
                action='auth.missing_key',
                details={'path': request.url.path, 'method': request.method},
            )
        return JSONResponse(
            status_code=401,
            content={'detail': 'Missing API key. Provide X-API-Key header.'},
        )

    key_config = _resolve_key(api_key, auth_config)
    if key_config is None:
        if audit:
            audit.log(
                action='auth.failure',
                details={'path': request.url.path, 'method': request.method},
            )
        return JSONResponse(
            status_code=403,
            content={'detail': 'Invalid API key.'},
        )

    # Build auth context and attach to request state.
    key_prefix = api_key[:8] + '...'
    key_name = key_config.description
    request.state.auth_context = AuthContext(
        key_prefix=key_prefix,
        key_name=key_name,
        policy=key_config.policy,
        permissions=POLICY_PERMISSIONS[key_config.policy],
        vault_ids=key_config.vault_ids,
        read_vault_ids=key_config.read_vault_ids,
    )

    # Set actor in context for downstream middleware and route handlers
    actor = f'{key_name} ({key_prefix})' if key_name else key_prefix
    set_actor(actor)

    return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_auth_context(request: Request) -> AuthContext | None:
    """FastAPI dependency: returns the AuthContext set by middleware, or None if auth disabled."""
    return getattr(request.state, 'auth_context', None)


def require_permission(permission: Permission) -> Callable[..., Any]:
    """Factory: returns a FastAPI dependency that checks for a specific permission."""

    async def _check(
        auth: AuthContext | None = Depends(get_auth_context),
    ) -> AuthContext | None:
        if auth is None:
            return None  # auth disabled, pass through
        if permission not in auth.permissions:
            raise HTTPException(
                status_code=403,
                detail=f'Insufficient permissions. Required: {permission.value}',
            )
        return auth

    # Give the dependency a readable name for debugging.
    _check.__name__ = f'require_{permission.value}'
    _check.__qualname__ = f'require_{permission.value}'
    return _check


require_read = require_permission(Permission.READ)
require_write = require_permission(Permission.WRITE)
require_delete = require_permission(Permission.DELETE)


async def check_vault_access(
    auth: AuthContext | None,
    vault_ids: list[UUID | str] | None,
    api: MemexAPI,
    *,
    permission: Permission = Permission.READ,
) -> None:
    """Raise 403 if the auth context restricts vault access and the request targets
    disallowed vaults.

    For READ operations the effective allowed set is ``vault_ids ∪ read_vault_ids``.
    For WRITE/DELETE operations only ``vault_ids`` is considered.

    Vault name → UUID resolution uses the existing LRU-cached VaultService.
    """
    if auth is None or auth.vault_ids is None:
        return  # no restriction
    if not vault_ids:
        return  # no vaults specified in request

    # Resolve the allowed vault identifiers to UUIDs.
    allowed: set[UUID] = set()
    for v in auth.vault_ids:
        try:
            allowed.add(await api.resolve_vault_identifier(v))
        except Exception:
            logger.warning('Could not resolve allowed vault %r for key %s', v, auth.key_prefix)

    # For read operations, also include read_vault_ids.
    if permission == Permission.READ and auth.read_vault_ids:
        for v in auth.read_vault_ids:
            try:
                allowed.add(await api.resolve_vault_identifier(v))
            except Exception:
                logger.warning(
                    'Could not resolve read-only vault %r for key %s', v, auth.key_prefix
                )

    # Check each requested vault against the allowed set.
    for vid in vault_ids:
        resolved = await api.resolve_vault_identifier(vid) if not isinstance(vid, UUID) else vid
        if resolved not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f'Access denied to vault {vid}.',
            )


async def require_admin_auth(request: Request) -> None:
    """FastAPI dependency that enforces admin-level API key auth.

    Unlike the global middleware, this always requires a valid API key
    regardless of whether global auth is enabled.  When no auth config
    is present (auth disabled globally), admin endpoints are blocked
    entirely — fail-closed.
    """
    auth_config: AuthConfig | None = getattr(request.app.state, 'auth_config', None)

    audit = _get_audit_service(request)
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        if audit:
            audit.log(
                action='auth.admin.missing_key',
                details={'path': request.url.path, 'method': request.method},
            )
        raise HTTPException(
            status_code=401,
            detail='Admin endpoints require authentication. Provide a valid X-API-Key header.',
        )

    if auth_config is None:
        raise HTTPException(status_code=403, detail='Invalid API key.')

    key_config = _resolve_key(api_key, auth_config)
    if key_config is None:
        if audit:
            audit.log(
                action='auth.admin.failure',
                details={'path': request.url.path, 'method': request.method},
            )
        raise HTTPException(status_code=403, detail='Invalid API key.')

    if key_config.policy != Policy.ADMIN:
        if audit:
            audit.log(
                action='auth.admin.insufficient',
                details={'path': request.url.path, 'method': request.method},
            )
        raise HTTPException(
            status_code=403,
            detail='Insufficient permissions. Admin policy required.',
        )
