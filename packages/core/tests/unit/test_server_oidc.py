"""Unit tests for OIDC bearer-token verification (offline, no network)."""

import time

import httpx
import respx
from authlib.jose import JsonWebKey, jwt

from memex_common.config import OidcGrantRule, OidcProviderConfig, Permission, Policy
from memex_core.server.oidc import OidcVerifier

ISSUER = 'https://issuer.example'
AUDIENCE = 'api://memex'
DISCOVERY_URL = f'{ISSUER}/.well-known/openid-configuration'
JWKS_URL = f'{ISSUER}/jwks'


def _make_key(kid: str) -> JsonWebKey:
    return JsonWebKey.generate_key('RSA', 2048, {'kid': kid}, is_private=True)


def _jwks(*keys: JsonWebKey) -> dict:
    return {'keys': [k.as_dict() for k in keys]}


def _mint(key: JsonWebKey, claims: dict, *, alg: str = 'RS256', kid: str = 'test-key') -> str:
    token = jwt.encode({'alg': alg, 'kid': kid}, claims, key)
    return token.decode('ascii') if isinstance(token, bytes) else token


def _valid_claims(**overrides) -> dict:
    claims = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'sub': 'user-123',
        'email': 'alice@example.com',
        'groups': ['memex-admins', 'other'],
        'exp': int(time.time()) + 3600,
        'iat': int(time.time()),
    }
    claims.update(overrides)
    return claims


def _provider(**overrides) -> OidcProviderConfig:
    defaults = dict(
        issuer=ISSUER,
        audience=[AUDIENCE],
        grant_rules=[
            OidcGrantRule(claim='groups', value='memex-admins', policy='admin'),
            OidcGrantRule(claim='groups', value='memex-readers', policy='reader'),
        ],
    )
    defaults.update(overrides)
    return OidcProviderConfig(**defaults)


def _mock_provider_endpoints(key: JsonWebKey) -> None:
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(key)))


@respx.mock
async def test_valid_token_maps_to_admin_context():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    ctx = await verifier.verify(_mint(key, _valid_claims()))

    assert ctx is not None
    assert ctx.policy is Policy.ADMIN
    assert Permission.DELETE in ctx.permissions
    assert ctx.key_prefix == 'oidc:user-123'
    assert ctx.key_name == 'alice@example.com'
    assert ctx.vault_ids is None


@respx.mock
async def test_first_matching_rule_wins():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    # In both admin and reader groups; admin rule is listed first.
    ctx = await verifier.verify(_mint(key, _valid_claims(groups=['memex-readers', 'memex-admins'])))
    assert ctx is not None
    assert ctx.policy is Policy.ADMIN


@respx.mock
async def test_scalar_claim_equality_and_vault_scope():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    provider = _provider(
        grant_rules=[
            OidcGrantRule(
                claim='email',
                value='alice@example.com',
                policy='writer',
                vault_ids=['team-vault'],
            )
        ]
    )
    verifier = OidcVerifier([provider])

    ctx = await verifier.verify(_mint(key, _valid_claims()))
    assert ctx is not None
    assert ctx.policy is Policy.WRITER
    assert ctx.vault_ids == ['team-vault']


@respx.mock
async def test_wrong_audience_rejected():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    assert await verifier.verify(_mint(key, _valid_claims(aud='api://other'))) is None


@respx.mock
async def test_unknown_issuer_rejected_without_network():
    key = _make_key('test-key')
    # No endpoints mocked: an unknown issuer must be rejected before any fetch.
    verifier = OidcVerifier([_provider()])

    token = _mint(key, _valid_claims(iss='https://evil.example'))
    assert await verifier.verify(token) is None


@respx.mock
async def test_expired_token_rejected():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    expired = _valid_claims(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)
    assert await verifier.verify(_mint(key, expired)) is None


@respx.mock
async def test_expiry_within_leeway_accepted():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider(leeway_seconds=120)])

    # Expired 30s ago, but within the 120s leeway.
    claims = _valid_claims(exp=int(time.time()) - 30)
    ctx = await verifier.verify(_mint(key, claims))
    assert ctx is not None


@respx.mock
async def test_bad_signature_rejected():
    signing_key = _make_key('test-key')
    other_key = _make_key('test-key')  # same kid, different key material
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(other_key)))
    verifier = OidcVerifier([_provider()])

    assert await verifier.verify(_mint(signing_key, _valid_claims())) is None


@respx.mock
async def test_disallowed_algorithm_rejected():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    # Provider only permits ES256, token is signed RS256.
    verifier = OidcVerifier([_provider(algorithms=['ES256'])])

    assert await verifier.verify(_mint(key, _valid_claims())) is None


@respx.mock
async def test_default_policy_when_no_rule_matches():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider(default_policy='reader')])

    ctx = await verifier.verify(_mint(key, _valid_claims(groups=['unrelated'])))
    assert ctx is not None
    assert ctx.policy is Policy.READER


@respx.mock
async def test_no_matching_rule_and_no_default_is_unauthorized():
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    assert await verifier.verify(_mint(key, _valid_claims(groups=['unrelated']))) is None


@respx.mock
async def test_key_rotation_triggers_single_refetch():
    old_key = _make_key('old-kid')
    new_key = _make_key('new-kid')
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    jwks_route = respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_jwks(old_key)),  # first fetch: stale set
            httpx.Response(200, json=_jwks(old_key, new_key)),  # refetch: rotated set
        ]
    )
    verifier = OidcVerifier([_provider()])

    token = _mint(new_key, _valid_claims(), kid='new-kid')
    ctx = await verifier.verify(token)
    assert ctx is not None
    assert ctx.policy is Policy.ADMIN
    assert jwks_route.call_count == 2


@respx.mock
async def test_unknown_kids_do_not_force_unbounded_refetch():
    """Attacker tokens with distinct bogus kids must not force a JWKS fetch each.

    With a long cooldown, many distinct unknown kids collapse to a single forced
    refetch on top of the initial fetch (2 total), not one per request.
    """
    key = _make_key('real-kid')
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    jwks_route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(key)))
    verifier = OidcVerifier([_provider()], force_refetch_cooldown_seconds=3600.0)

    for i in range(6):
        attacker_key = _make_key(f'bogus-{i}')
        token = _mint(attacker_key, _valid_claims(), kid=f'bogus-{i}')
        assert await verifier.verify(token) is None

    # 1 initial fetch + at most 1 forced refetch within the cooldown window.
    assert jwks_route.call_count <= 2


@respx.mock
async def test_malformed_token_rejected():
    verifier = OidcVerifier([_provider()])
    assert await verifier.verify('not-a-jwt') is None
    assert await verifier.verify('a.b') is None


@respx.mock
async def test_jwks_fetch_failure_returns_none_not_500():
    """A JWKS endpoint error must deny (return None), never raise a 500.

    verify()'s contract is None-on-failure and authenticate_request does not
    wrap the call, so an unhandled httpx error would surface as HTTP 500.
    """
    key = _make_key('test-key')
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    respx.get(JWKS_URL).mock(return_value=httpx.Response(503))
    verifier = OidcVerifier([_provider()])

    assert await verifier.verify(_mint(key, _valid_claims())) is None


@respx.mock
async def test_forced_refetch_failure_falls_back_to_stale_keyset():
    """If the rotation refetch fails, fall back to the cached keyset and return
    None for the unknown kid — the fetch error must not raise a 500."""
    old_key = _make_key('old-kid')
    new_key = _make_key('new-kid')
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_jwks(old_key)),  # initial fetch OK
            httpx.Response(500),  # forced refetch fails
        ]
    )
    verifier = OidcVerifier([_provider()])

    token = _mint(new_key, _valid_claims(), kid='new-kid')
    assert await verifier.verify(token) is None
