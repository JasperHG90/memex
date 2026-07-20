"""Client-side OIDC token cache and header resolution.

Shared by the CLI and MCP so both attach the same credential to requests. When
OIDC is configured and a cached token exists, clients send
``Authorization: Bearer <token>`` (refreshing it near expiry); otherwise they
fall back to the ``X-API-Key`` header. The cache lives at
``<user_config_dir>/memex/token.json`` with ``0600`` permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib as plb
import time
from typing import TYPE_CHECKING

import httpx
from authlib.jose import jwt
from platformdirs import user_config_dir
from pydantic import BaseModel

if TYPE_CHECKING:
    from typing import Literal

    from memex_common.config import MemexConfig, OidcClientConfig

logger = logging.getLogger('memex.common.auth_client')

# Refresh a token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 60.0
_HTTP_TIMEOUT_SECONDS = 30.0

# Serialize refreshes within a process so overlapping requests don't each refresh.
_refresh_lock = asyncio.Lock()


class TokenCache(BaseModel):
    """Persisted OIDC token state for a single logged-in provider."""

    issuer: str
    access_token: str
    token_type: str = 'Bearer'
    expires_at: float  # epoch seconds
    refresh_token: str | None = None
    scope: str | None = None
    subject: str | None = None  # for `memex auth status` display only
    # Which auth mode produced this token. Read paths require a matching mode so
    # an interactive login token and a service-account token never get reused
    # across modes on a shared config dir.
    mode: str = 'interactive'

    def is_fresh(self, *, skew: float = _EXPIRY_SKEW_SECONDS) -> bool:
        return time.time() < (self.expires_at - skew)


def token_cache_path() -> plb.Path:
    """Location of the token cache file."""
    return plb.Path(user_config_dir('memex', appauthor=False)) / 'token.json'


def load_token_cache() -> TokenCache | None:
    """Load the cached token, or None if absent or unreadable."""
    path = token_cache_path()
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    try:
        return TokenCache.model_validate_json(raw)
    except ValueError:
        logger.warning('Token cache at %s is malformed; ignoring it.', path)
        return None


def save_token_cache(cache: TokenCache) -> None:
    """Persist the token cache with owner-only (0600) permissions."""
    path = token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600, then fchmod the open fd before writing so the secret is
    # never briefly world-readable even when the file already existed with looser
    # permissions (O_CREAT's mode is ignored for a pre-existing file).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(cache.model_dump_json())


def clear_token_cache() -> bool:
    """Delete the token cache. Returns True if a file was removed."""
    path = token_cache_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


async def discover_oidc(issuer: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a provider's OIDC discovery document."""
    url = issuer.rstrip('/') + '/.well-known/openid-configuration'
    if client is not None:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as owned:
        response = await owned.get(url)
        response.raise_for_status()
        return response.json()


def token_cache_from_response(
    data: dict,
    *,
    issuer: str,
    previous: TokenCache | None = None,
    mode: Literal['interactive', 'service_account'] = 'interactive',
) -> TokenCache:
    """Build a TokenCache from an OAuth token-endpoint response."""
    expires_in = float(data.get('expires_in', 3600))
    # Refresh tokens may rotate; keep the previous one if the response omits it.
    refresh_token = data.get('refresh_token') or (previous.refresh_token if previous else None)
    return TokenCache(
        issuer=issuer,
        access_token=data['access_token'],
        token_type=data.get('token_type', 'Bearer'),
        expires_at=time.time() + expires_in,
        refresh_token=refresh_token,
        scope=data.get('scope', previous.scope if previous else None),
        subject=previous.subject if previous else None,
        mode=mode,
    )


async def _refresh_token(
    oidc: OidcClientConfig, cache: TokenCache, *, client: httpx.AsyncClient | None = None
) -> TokenCache:
    """Exchange the refresh token for a fresh access token and persist it."""
    discovery = await discover_oidc(oidc.issuer, client=client)
    token_endpoint = discovery.get('token_endpoint')
    if not token_endpoint:
        raise ValueError(f'OIDC discovery for {oidc.issuer!r} has no token_endpoint.')

    form = {
        'grant_type': 'refresh_token',
        'refresh_token': cache.refresh_token,
        'client_id': oidc.client_id,
    }
    if oidc.client_secret is not None:
        form['client_secret'] = oidc.client_secret.get_secret_value()

    async def _post(c: httpx.AsyncClient) -> dict:
        resp = await c.post(token_endpoint, data=form)
        resp.raise_for_status()
        return resp.json()

    if client is not None:
        data = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as owned:
            data = await _post(owned)

    refreshed = token_cache_from_response(data, issuer=oidc.issuer, previous=cache)
    save_token_cache(refreshed)
    return refreshed


class MemexClientAuth(httpx.Auth):
    """httpx auth flow that re-resolves the credential on every request.

    Attaching this (via ``auth=``) instead of baking headers in at construction
    means a refreshed OIDC token reaches long-lived clients (MCP, the sync
    watcher) without rebuilding the client.
    """

    def __init__(self, config: MemexConfig) -> None:
        self._config = config

    async def async_auth_flow(self, request):  # type: ignore[no-untyped-def]
        headers = await resolve_client_headers(self._config)
        request.headers.update(headers)
        yield request


async def resolve_client_headers(
    config: MemexConfig, *, client: httpx.AsyncClient | None = None
) -> dict[str, str]:
    """Resolve the auth header for a client request.

    Precedence: a valid (or refreshable) OIDC bearer token, else the configured
    API key, else no auth header. Must be called at each client build so a
    refreshed token reaches long-lived processes.
    """
    bearer = await _resolve_bearer(config, client=client)
    if bearer is not None:
        return {'Authorization': bearer}
    if config.api_key is not None:
        return {'X-API-Key': config.api_key.get_secret_value()}
    return {}


async def _resolve_bearer(
    config: MemexConfig, *, client: httpx.AsyncClient | None = None
) -> str | None:
    oidc = config.oidc
    if oidc is None:
        return None
    if oidc.grant in ('client_credentials', 'jwt_profile'):
        return await _resolve_service_bearer(oidc, client=client)
    cache = load_token_cache()
    if cache is None or cache.issuer != oidc.issuer or cache.mode != 'interactive':
        # Not logged in, logged into a different provider, or the cache holds a
        # service-account token (wrong mode): fall back.
        return None
    if cache.is_fresh():
        return f'{cache.token_type} {cache.access_token}'
    if not cache.refresh_token:
        logger.warning('Cached OIDC token expired and no refresh token is available.')
        return None
    async with _refresh_lock:
        # Re-load inside the lock: another coroutine may have refreshed already.
        current = load_token_cache() or cache
        if current.issuer == oidc.issuer and current.mode == 'interactive' and current.is_fresh():
            return f'{current.token_type} {current.access_token}'
        try:
            refreshed = await _refresh_token(oidc, current, client=client)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning('OIDC token refresh failed: %s', exc)
            return None
        return f'{refreshed.token_type} {refreshed.access_token}'


# ---------------------------------------------------------------------------
# Service-account (non-interactive) authentication
# ---------------------------------------------------------------------------


async def _resolve_service_bearer(
    oidc: OidcClientConfig, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """Bearer header for a service account: use the cached token, or acquire one.

    Service tokens are not refreshable (no refresh token); on expiry a fresh
    token is acquired via the configured grant.
    """
    if (cache := load_token_cache()) is not None and _sa_cache_matches(cache, oidc):
        return f'{cache.token_type} {cache.access_token}'
    async with _refresh_lock:
        if (current := load_token_cache()) is not None and _sa_cache_matches(current, oidc):
            return f'{current.token_type} {current.access_token}'
        try:
            acquired = await acquire_service_token(oidc, client=client)
        except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
            logger.warning('Service-account token acquisition failed: %s', exc)
            return None
        return f'{acquired.token_type} {acquired.access_token}'


async def acquire_service_token(
    oidc: OidcClientConfig, *, client: httpx.AsyncClient | None = None
) -> TokenCache:
    """Acquire and cache an access token for a service account.

    Supports the ``client_credentials`` grant (client_id + client_secret) and
    the RFC 7523 ``jwt_profile`` grant (a self-signed assertion from a provider
    key file, e.g. a Zitadel service-account key JSON).
    """
    # Build the request body first (jwt_profile signs a local assertion) so a
    # bad key file fails fast without a network round-trip.
    if oidc.grant == 'client_credentials':
        form = {
            'grant_type': 'client_credentials',
            'client_id': oidc.client_id,
            'scope': ' '.join(oidc.scopes),
        }
        if oidc.client_secret is not None:
            form['client_secret'] = oidc.client_secret.get_secret_value()
    elif oidc.grant == 'jwt_profile':
        form = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': _build_client_assertion(oidc),
            'scope': ' '.join(oidc.scopes),
        }
    else:  # pragma: no cover - guarded by the caller and config validator
        raise ValueError(f'grant {oidc.grant!r} is not a service-account grant.')

    discovery = await discover_oidc(oidc.issuer, client=client)
    token_endpoint = discovery.get('token_endpoint')
    if not token_endpoint:
        raise ValueError(f'OIDC discovery for {oidc.issuer!r} has no token_endpoint.')

    async def _post(c: httpx.AsyncClient) -> dict:
        resp = await c.post(token_endpoint, data=form)
        resp.raise_for_status()
        return resp.json()

    if client is not None:
        data = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as owned:
            data = await _post(owned)

    cache = token_cache_from_response(data, issuer=oidc.issuer, mode='service_account')
    cache.subject = oidc.client_id
    save_token_cache(cache)
    return cache


def _sa_cache_matches(cache: TokenCache, oidc: OidcClientConfig) -> bool:
    """Whether a cached token belongs to this service account and is still fresh.

    Requires the service-account mode, issuer, and client identity to match so a
    human's interactive token (or another SA's token) is never reused here.
    """
    return (
        cache.mode == 'service_account'
        and cache.issuer == oidc.issuer
        and cache.subject == oidc.client_id
        and cache.is_fresh()
    )


def _build_client_assertion(oidc: OidcClientConfig) -> str:
    """Build a signed JWT assertion from a service-account key file (RFC 7523).

    Parses a Zitadel-style key JSON (``keyId``, ``key`` PEM, ``userId``), signs
    a short-lived JWT, and returns it for the jwt-bearer token exchange. The
    assertion's issuer/subject is the key's ``userId`` and its audience is the
    OIDC issuer, per Zitadel's JWT-profile flow.
    """
    if not oidc.key_file:  # pragma: no cover - guarded by config validator
        raise ValueError('jwt_profile grant requires key_file.')
    raw = plb.Path(oidc.key_file).read_text(encoding='utf-8')
    key_data = json.loads(raw)
    key_id = key_data.get('keyId')
    private_key = key_data.get('key')
    user_id = key_data.get('userId')
    if not (key_id and private_key and user_id):
        raise ValueError(
            f'Service-account key file {oidc.key_file!r} is missing keyId, key, or userId.'
        )

    now = int(time.time())
    header = {'alg': 'RS256', 'kid': key_id}
    payload = {
        'iss': user_id,
        'sub': user_id,
        'aud': oidc.issuer,
        'iat': now,
        'exp': now + 3600,
    }
    token = jwt.encode(header, payload, private_key)
    return token.decode('ascii') if isinstance(token, bytes) else token
