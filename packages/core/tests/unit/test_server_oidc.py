"""Unit tests for OIDC bearer-token verification (offline, no network)."""

import logging
import time

import httpx
import pytest
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


# ---------------------------------------------------------------------------
# Vault-style providers: the signature is on the id_token, not the access token
# ---------------------------------------------------------------------------

VAULT_ISSUER = 'https://vault.example.com/v1/identity/oidc/provider/memex'
VAULT_CLIENT_ID = 'memex-cli'
VAULT_DISCOVERY_URL = f'{VAULT_ISSUER}/.well-known/openid-configuration'
VAULT_JWKS_URL = f'{VAULT_ISSUER}/.well-known/keys'

# A Vault batch token: an opaque string, not a JWT. Only Vault's own userinfo
# endpoint can resolve it, so it can never verify against a JWKS.
VAULT_OPAQUE_ACCESS_TOKEN = 'hvb.AAAAAQKO3xkQ7Xr1YQpVZ2LmN4tGhRfDsWqEiUoPaS8dFgHjKlZxCvBnM'


def _vault_provider(**overrides) -> OidcProviderConfig:
    """A provider trusting Vault, with the CLIENT ID as the accepted audience.

    A Vault id_token's `aud` is the OIDC client id, not a separate API
    identifier, so that is what the operator configures.
    """
    defaults = dict(
        issuer=VAULT_ISSUER,
        audience=[VAULT_CLIENT_ID],
        grant_rules=[OidcGrantRule(claim='groups', value='memex-admins', policy='admin')],
    )
    defaults.update(overrides)
    return OidcProviderConfig(**defaults)


def _vault_id_token_claims(**overrides) -> dict:
    """Claims shaped like a Vault OIDC id_token, including id_token-only ones."""
    claims = {
        'iss': VAULT_ISSUER,
        'aud': VAULT_CLIENT_ID,
        'sub': 'vault-entity-id',
        'groups': ['memex-admins'],
        'exp': int(time.time()) + 3600,
        'iat': int(time.time()),
        'nonce': 'n-0S6_WzA2Mj',
        'at_hash': 'kQ7Xr1YQpVZ2LmN4tGhRfA',
        'azp': VAULT_CLIENT_ID,
    }
    claims.update(overrides)
    return claims


@respx.mock
async def test_vault_id_token_verifies_to_a_context():
    """The whole point of the id_token credential: no server change needed."""
    key = _make_key('test-key')
    respx.get(VAULT_DISCOVERY_URL).mock(
        return_value=httpx.Response(200, json={'jwks_uri': VAULT_JWKS_URL})
    )
    respx.get(VAULT_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(key)))
    verifier = OidcVerifier([_vault_provider()])

    ctx = await verifier.verify(_mint(key, _vault_id_token_claims()))

    assert ctx is not None
    assert ctx.policy is Policy.ADMIN
    assert ctx.key_prefix == 'oidc:vault-entity-id'


@respx.mock
async def test_vault_id_token_with_list_audience_verifies():
    key = _make_key('test-key')
    respx.get(VAULT_DISCOVERY_URL).mock(
        return_value=httpx.Response(200, json={'jwks_uri': VAULT_JWKS_URL})
    )
    respx.get(VAULT_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(key)))
    verifier = OidcVerifier([_vault_provider()])

    claims = _vault_id_token_claims(aud=[VAULT_CLIENT_ID, 'someone-else'])
    assert await verifier.verify(_mint(key, claims)) is not None


# ---------------------------------------------------------------------------
# Diagnostics: each of the three ways a bearer becomes a 403 says why
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_memex_logger_propagation():
    """Ensure ``memex.core.server`` records reach pytest's caplog.

    ``configure_logging()`` sets ``memex.propagate = False``
    (``packages/core/src/memex_core/logging_config.py:69``), and any earlier
    test in the suite that calls it leaves that in place. caplog attaches its
    handler to the ROOT logger, so without propagation ``caplog.records`` stays
    empty even though the ``logger.info(...)`` call fires. Same fixture as
    ``test_run_dspy_operation_timeout.py``. Production loggers are unaffected:
    this only restores the test's view.
    """
    memex_logger = logging.getLogger('memex')
    saved = memex_logger.propagate
    memex_logger.propagate = True
    try:
        yield
    finally:
        memex_logger.propagate = saved


async def test_opaque_token_logs_a_jwt_shape_diagnostic(caplog):
    """An opaque access token must be distinguishable from a bad signature."""
    verifier = OidcVerifier([_vault_provider()])

    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(VAULT_OPAQUE_ACCESS_TOKEN) is None

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno >= logging.WARNING
    message = caplog.records[0].getMessage()
    assert 'not a parseable JWT' in message
    assert 'id_token' in message
    # The token itself is a credential: it must never reach the log.
    assert VAULT_OPAQUE_ACCESS_TOKEN not in message


@respx.mock
async def test_unknown_issuer_logs_the_unmatched_issuer(caplog):
    key = _make_key('test-key')
    verifier = OidcVerifier([_provider()])

    token = _mint(key, _valid_claims(iss='https://elsewhere.example'))
    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(token) is None

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno >= logging.WARNING
    message = caplog.records[0].getMessage()
    assert 'https://elsewhere.example' in message
    assert ISSUER in message  # the configured issuers, so the mismatch is visible


@respx.mock
async def test_unknown_issuer_log_escapes_a_newline_in_the_claim(caplog):
    """`iss` is unverified, attacker-controlled input: it must not forge a line."""
    key = _make_key('test-key')
    verifier = OidcVerifier([_provider()])

    hostile = 'https://a.example/\nINFO forged log line\x1b[31m'
    token = _mint(key, _valid_claims(iss=hostile))
    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(token) is None

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    # repr() escapes both, so the injected text cannot start its own log line
    # nor recolor the operator's terminal.
    assert '\nINFO forged log line' not in message
    assert '\\n' in message
    assert '\x1b' not in message


@respx.mock
async def test_unknown_issuer_log_truncates_an_oversized_claim(caplog):
    """An unbounded claim must not flood the log."""
    key = _make_key('test-key')
    verifier = OidcVerifier([_provider()])

    token = _mint(key, _valid_claims(iss='https://a.example/' + 'A' * 8000))
    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(token) is None

    assert len(caplog.records) == 1
    assert len(caplog.records[0].getMessage()) < 600


@respx.mock
async def test_verified_but_unauthorized_logs_the_reason(caplog):
    """The third silent 403: verified, but no grant_rule matched."""
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    token = _mint(key, _valid_claims(groups=['unmapped-group']))
    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(token) is None

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert caplog.records[0].levelno >= logging.WARNING
    assert 'matched no grant_rule' in messages[0]
    assert ISSUER in messages[0]
    assert 'groups' in messages[0]  # claim NAMES help the operator write the rule
    # ...but never the claim VALUES, which are the caller's identity data.
    assert 'unmapped-group' not in messages[0]
    assert 'alice@example.com' not in messages[0]


@respx.mock
async def test_authorized_token_logs_nothing(caplog):
    """The diagnostics are for failures only; a good request stays quiet."""
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    with caplog.at_level('INFO', logger='memex.core.server'):
        assert await verifier.verify(_mint(key, _valid_claims())) is not None

    assert caplog.records == []


@respx.mock
async def test_all_rejection_reasons_survive_the_default_log_level(caplog):
    """The diagnostics are useless if the operator must first find a log knob.

    `LoggingConfig.level` defaults to WARNING
    (`packages/common/src/memex_common/config.py`), so a diagnostic emitted at
    INFO is invisible on a stock server and the troubleshooting doc's "read the
    log" instruction dead-ends. Capture at WARNING, not INFO, and assert every
    refusal reason still shows up.
    """
    key = _make_key('test-key')
    _mock_provider_endpoints(key)
    verifier = OidcVerifier([_provider()])

    with caplog.at_level(logging.WARNING, logger='memex.core.server'):
        # 1. opaque bearer
        assert await verifier.verify(VAULT_OPAQUE_ACCESS_TOKEN) is None
        # 2. unknown issuer
        assert await verifier.verify(_mint(key, _valid_claims(iss='https://nope.example'))) is None
        # 3. bad audience (the pre-existing reason, raised to WARNING for parity)
        assert await verifier.verify(_mint(key, _valid_claims(aud='wrong-audience'))) is None
        # 4. verified but authorizes nothing
        assert await verifier.verify(_mint(key, _valid_claims(groups=['unmapped']))) is None

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 4, messages
    assert 'not a parseable JWT' in messages[0]
    assert 'no configured provider matches issuer' in messages[1]
    assert 'OIDC token rejected for issuer' in messages[2]
    assert 'matched no grant_rule' in messages[3]


@respx.mock
async def test_jwks_fetch_failure_is_visible_at_the_default_log_level(caplog):
    """An unreachable JWKS refuses EVERY bearer, so it cannot be silent either.

    DNS, egress rules, or an expired cert on the provider makes `verify` return
    None for every token. At INFO that whole failure mode is invisible on a
    stock server, which is the same silent-403 class this ticket closes.
    """
    key = _make_key('test-key')
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json={'jwks_uri': JWKS_URL}))
    respx.get(JWKS_URL).mock(return_value=httpx.Response(503))
    verifier = OidcVerifier([_provider()])

    with caplog.at_level(logging.WARNING, logger='memex.core.server'):
        assert await verifier.verify(_mint(key, _valid_claims())) is None

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert 'OIDC JWKS fetch failed for issuer' in messages[0]
