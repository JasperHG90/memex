"""OIDC bearer-token verification for the Memex server.

Verifies ``Authorization: Bearer <jwt>`` access tokens locally against a
configured provider's JWKS, then maps the token's claims onto the existing
:class:`~memex_core.server.auth.AuthContext` (policy + vault scope) so every
downstream permission dependency works unchanged.

The bearer must be a *signed JWT*, because verification is local against the
JWKS; an opaque token cannot be verified here. Which OAuth token carries the
signature is the provider's choice: a signed access token verifies as is, and a
provider that signs only the id_token works once its client sends that instead
(``oidc.credential: id_token``). The provider is selected by matching the
token's ``iss`` claim.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time

from typing import TYPE_CHECKING

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

from memex_common.config import (
    POLICY_PERMISSIONS,
    OidcGrantRule,
    OidcProviderConfig,
)
from memex_core.server.auth import AuthContext

if TYPE_CHECKING:
    from fastapi import FastAPI

    from memex_common.config import AuthConfig

logger = logging.getLogger('memex.core.server')

# JWKS documents are cached this long before a background-free refetch.
_JWKS_CACHE_TTL_SECONDS = 3600.0
# Minimum gap between unknown-kid forced refetches per issuer. Bounds the
# pre-auth DoS where an attacker signs tokens with distinct bogus kids to force
# an outbound JWKS fetch on every request; genuine key rotation still refetches
# once per window.
_FORCE_REFETCH_COOLDOWN_SECONDS = 30.0
# Outbound timeout for discovery / JWKS fetches.
_HTTP_TIMEOUT_SECONDS = 10.0
# Cap on the unverified `iss` echoed into the rejection log. The claim is
# attacker-controlled and unbounded, so it is repr()'d and then truncated.
_MAX_LOGGED_ISSUER_CHARS = 200

# Every reason a bearer is REFUSED logs at WARNING, not INFO, because the
# server's default level is WARNING (`LoggingConfig.level`) and an operator
# debugging a 403 must see the cause without first discovering a log-level knob.
# That includes a JWKS/discovery fetch failure, which refuses every bearer.
# This does not add an amplification channel: the audit middleware already
# writes one record per request, 403s included, so a refused bearer was already
# one write. Lines that do NOT by themselves refuse anything stay at INFO: the
# forced-refetch failure falls back to the cached keyset (a real refusal after
# it logs through the verification path), and the startup line.


def _b64url_json(segment: str) -> dict:
    """Base64url-decode a JWT segment into a JSON object (no verification)."""
    padding = '=' * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + padding)
    return json.loads(raw)


def _unverified_parts(token: str) -> tuple[dict, dict]:
    """Return (header, payload) of a JWT without verifying the signature.

    Used only to read ``kid`` (for JWKS selection) and ``iss`` (for provider
    selection). The signature and all claims are verified afterwards.
    """
    header_b64, payload_b64, _ = token.split('.')
    return _b64url_json(header_b64), _b64url_json(payload_b64)


def _rule_matches(claims: dict, rule: OidcGrantRule) -> bool:
    """Whether a verified token's claims activate *rule*.

    Membership when the claim is a list/tuple/set (e.g. ``groups``); equality
    when it is a scalar (e.g. ``email``).
    """
    value = claims.get(rule.claim)
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return rule.value in [str(v) for v in value]
    return str(value) == rule.value


def _claims_to_context(claims: dict, provider: OidcProviderConfig) -> AuthContext | None:
    """Map verified claims onto an AuthContext via the provider's grant rules.

    Returns ``None`` when no grant rule matches and the provider has no
    ``default_policy`` (the token authenticated but is not authorized).
    """
    for rule in provider.grant_rules:
        if _rule_matches(claims, rule):
            policy = rule.policy
            vault_ids = rule.vault_ids
            read_vault_ids = rule.read_vault_ids
            break
    else:
        if provider.default_policy is None:
            return None
        policy = provider.default_policy
        vault_ids = None
        read_vault_ids = None

    subject = str(claims.get('sub') or claims.get('email') or 'unknown')
    key_name = (
        claims.get('email')
        or claims.get('preferred_username')
        or claims.get('name')
        or provider.issuer
    )
    return AuthContext(
        key_prefix=f'oidc:{subject}',
        key_name=key_name,
        policy=policy,
        permissions=POLICY_PERMISSIONS[policy],
        vault_ids=vault_ids,
        read_vault_ids=read_vault_ids,
    )


class OidcVerifier:
    """Verifies bearer tokens against a fixed set of trusted OIDC providers."""

    def __init__(
        self,
        providers: list[OidcProviderConfig],
        *,
        http_client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = _JWKS_CACHE_TTL_SECONDS,
        force_refetch_cooldown_seconds: float = _FORCE_REFETCH_COOLDOWN_SECONDS,
    ) -> None:
        self._providers: dict[str, OidcProviderConfig] = {p.issuer: p for p in providers}
        # A per-provider decoder pinned to that provider's allowed algorithms.
        # JsonWebToken enforces the allowlist against the token header and
        # rejects ``alg: none``, closing algorithm-confusion attacks.
        self._decoders: dict[str, JsonWebToken] = {
            p.issuer: JsonWebToken(p.algorithms) for p in providers
        }
        self._http_client = http_client
        self._cache_ttl = cache_ttl_seconds
        self._force_refetch_cooldown = force_refetch_cooldown_seconds
        self._jwks_cache: dict[str, tuple[object, float]] = {}
        self._jwks_uri_cache: dict[str, str] = {}
        self._last_force_refetch: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def has_providers(self) -> bool:
        return bool(self._providers)

    async def verify(self, token: str) -> AuthContext | None:
        """Verify *token* and return an AuthContext, or None if it is not valid.

        None covers every failure a caller should treat as ``403 invalid``:
        malformed token, unknown issuer, bad signature, failed claim checks, or
        a valid token that maps to no policy.
        """
        try:
            header, payload = _unverified_parts(token)
        except (ValueError, binascii.Error, json.JSONDecodeError):
            # Almost always an opaque token. Some providers (HashiCorp Vault,
            # Google) issue an opaque access token but DO sign the id_token, so
            # their clients work by sending that instead; others (GitHub OAuth)
            # issue no JWT at all and cannot use this path. Logging the segment
            # count distinguishes both from a signature or claim failure, which
            # logs below. The token itself is never logged.
            logger.warning(
                'OIDC bearer rejected: not a parseable JWT (%d dot-separated segments). '
                'Memex verifies signed JWT bearer tokens locally against the '
                "provider's JWKS; an opaque token cannot be verified. If the provider "
                'signs the id_token instead, set oidc.credential="id_token" on the client.',
                token.count('.') + 1,
            )
            return None

        issuer = payload.get('iss')
        provider = self._providers.get(issuer) if isinstance(issuer, str) else None
        if provider is None:
            # `issuer` here is UNVERIFIED, attacker-controlled input. repr() it
            # FIRST and truncate the result, so a newline or terminal escape
            # cannot forge a log line and a huge claim cannot flood the log.
            # repr-then-slice is also total: a non-string or absent `iss` (which
            # reaches here via the isinstance guard above) would raise under
            # slice-then-repr.
            logger.warning(
                'OIDC bearer rejected: no configured provider matches issuer %s. '
                'Configured issuers: %s.',
                repr(issuer)[:_MAX_LOGGED_ISSUER_CHARS],
                sorted(self._providers),
            )
            return None

        # A JWKS/discovery fetch failure must deny the request (return None),
        # not raise out of verify() into an unhandled 500 (verify's contract is
        # None-on-failure, and authenticate_request does not wrap this call).
        try:
            keyset = await self._get_keyset(provider)
        except httpx.HTTPError as exc:
            logger.warning('OIDC JWKS fetch failed for issuer %s: %s', provider.issuer, exc)
            return None
        kid = header.get('kid')
        if kid and not _keyset_has_kid(keyset, kid):
            # Unknown kid: the provider may have rotated keys, so refetch — but
            # rate-limit forced refetches per issuer so an attacker signing
            # tokens with distinct bogus kids cannot force an outbound fetch on
            # every request (pre-auth DoS). Within the cooldown the stale keyset
            # is reused; a genuinely unknown kid then fails verification (-> None).
            last = self._last_force_refetch.get(provider.issuer)
            now = time.monotonic()
            if last is None or (now - last) >= self._force_refetch_cooldown:
                # Set the cooldown before fetching so a failed refetch still
                # counts against the rate limit (preserves the DoS bound). On
                # failure, keep the keyset already in hand: the unknown kid then
                # fails verification (-> None) instead of raising a 500.
                self._last_force_refetch[provider.issuer] = now
                try:
                    keyset = await self._get_keyset(provider, force=True)
                except httpx.HTTPError as exc:
                    logger.info(
                        'OIDC forced JWKS refetch failed for issuer %s: %s',
                        provider.issuer,
                        exc,
                    )

        claims_options = {
            'iss': {'essential': True, 'value': provider.issuer},
            'aud': {'essential': True, 'values': list(provider.audience)},
            'exp': {'essential': True},
        }
        try:
            claims = self._decoders[provider.issuer].decode(
                token, keyset, claims_options=claims_options
            )
            claims.validate(now=int(time.time()), leeway=provider.leeway_seconds)
        except JoseError as exc:
            logger.warning('OIDC token rejected for issuer %s: %s', provider.issuer, exc)
            return None
        except ValueError as exc:
            # find_by_kid / malformed key material.
            logger.warning('OIDC token verification error for issuer %s: %s', provider.issuer, exc)
            return None

        verified = dict(claims)
        context = _claims_to_context(verified, provider)
        if context is None:
            # The token is genuine but authorizes nothing: no grant_rule matched
            # and the provider sets no default_policy. Without this line the
            # result is a 403 indistinguishable from a bad signature. Claim
            # NAMES only: the values are the caller's identity data.
            logger.warning(
                'OIDC token verified for issuer %s but matched no grant_rule and the '
                'provider has no default_policy, so it authorizes nothing. Claims '
                'present on the token: %s.',
                provider.issuer,
                sorted(verified),
            )
        return context

    async def _get_keyset(self, provider: OidcProviderConfig, *, force: bool = False) -> object:
        issuer = provider.issuer
        now = time.monotonic()
        if not force:
            cached = self._jwks_cache.get(issuer)
            if cached is not None and (now - cached[1]) < self._cache_ttl:
                return cached[0]

        async with self._lock:
            # Re-check after acquiring the lock in case a concurrent call filled it.
            cached = self._jwks_cache.get(issuer)
            if (
                not force
                and cached is not None
                and (time.monotonic() - cached[1]) < self._cache_ttl
            ):
                return cached[0]
            jwks = await self._fetch_jwks(provider)
            keyset = JsonWebKey.import_key_set(jwks)
            self._jwks_cache[issuer] = (keyset, time.monotonic())
            return keyset

    async def _fetch_jwks(self, provider: OidcProviderConfig) -> dict:
        jwks_uri = provider.jwks_uri or await self._discover_jwks_uri(provider)
        return await self._get_json(jwks_uri)

    async def _discover_jwks_uri(self, provider: OidcProviderConfig) -> str:
        cached = self._jwks_uri_cache.get(provider.issuer)
        if cached:
            return cached
        discovery_url = provider.issuer.rstrip('/') + '/.well-known/openid-configuration'
        doc = await self._get_json(discovery_url)
        jwks_uri = doc.get('jwks_uri')
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise ValueError(f'OIDC discovery for {provider.issuer!r} has no jwks_uri.')
        self._jwks_uri_cache[provider.issuer] = jwks_uri
        return jwks_uri

    async def _get_json(self, url: str) -> dict:
        if self._http_client is not None:
            response = await self._http_client.get(url)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()


def _keyset_has_kid(keyset: object, kid: str) -> bool:
    try:
        keyset.find_by_kid(kid)  # type: ignore[attr-defined]
    except ValueError:
        return False
    return True


def setup_oidc(app: FastAPI, auth_config: AuthConfig) -> None:
    """Configure OIDC bearer-token verification on *app*.

    Stores an :class:`OidcVerifier` on ``app.state.oidc_verifier`` when auth is
    enabled and providers are configured; otherwise removes it so the bearer
    path resolves to ``invalid``. Called from ``lifespan()`` right after
    :func:`~memex_core.server.auth.setup_auth`.
    """
    if not auth_config.enabled or not auth_config.oidc:
        if hasattr(app.state, 'oidc_verifier'):
            del app.state.oidc_verifier
        return

    app.state.oidc_verifier = OidcVerifier(auth_config.oidc)
    logger.info(
        'OIDC bearer-token authentication enabled (%d provider(s)).',
        len(auth_config.oidc),
    )
