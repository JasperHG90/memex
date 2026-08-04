"""Tests for the shared client-side OIDC token cache and header resolution."""

import base64
import json
import logging
import stat
import time
import types
import urllib.parse

import httpx
import pytest
import respx
from authlib.jose import JsonWebKey
from pydantic import SecretStr

from memex_common import auth_client
from memex_common.auth_client import (
    TokenCache,
    acquire_service_token,
    clear_token_cache,
    load_token_cache,
    resolve_client_headers,
    save_token_cache,
    token_cache_path,
)
from memex_common.config import OidcClientConfig

ISSUER = 'https://issuer.example'
DISCOVERY_URL = f'{ISSUER}/.well-known/openid-configuration'
TOKEN_URL = f'{ISSUER}/token'


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the token cache into a temp dir for every test."""
    monkeypatch.setattr(auth_client, 'user_config_dir', lambda *a, **k: str(tmp_path))
    return tmp_path


def _config(*, oidc=True, api_key=None):
    oidc_cfg = OidcClientConfig(issuer=ISSUER, client_id='client-1') if oidc else None
    return types.SimpleNamespace(
        oidc=oidc_cfg,
        api_key=SecretStr(api_key) if api_key else None,
    )


def _cache(**overrides):
    data = dict(
        issuer=ISSUER,
        access_token='access-1',
        expires_at=time.time() + 3600,
        refresh_token='refresh-1',
    )
    data.update(overrides)
    return TokenCache(**data)


class TestTokenCachePersistence:
    def test_roundtrip(self):
        save_token_cache(_cache())
        loaded = load_token_cache()
        assert loaded is not None
        assert loaded.access_token == 'access-1'
        assert loaded.refresh_token == 'refresh-1'

    def test_file_is_owner_only(self):
        save_token_cache(_cache())
        mode = stat.S_IMODE(token_cache_path().stat().st_mode)
        assert mode == 0o600

    def test_missing_cache_returns_none(self):
        assert load_token_cache() is None

    def test_malformed_cache_returns_none(self):
        path = token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not json', encoding='utf-8')
        assert load_token_cache() is None

    def test_clear(self):
        save_token_cache(_cache())
        assert clear_token_cache() is True
        assert clear_token_cache() is False
        assert load_token_cache() is None


class TestResolveClientHeaders:
    async def test_no_oidc_uses_api_key(self):
        headers = await resolve_client_headers(_config(oidc=False, api_key='secret-key'))
        assert headers == {'X-API-Key': 'secret-key'}

    async def test_no_credentials_is_empty(self):
        headers = await resolve_client_headers(_config(oidc=False, api_key=None))
        assert headers == {}

    async def test_fresh_bearer_token(self):
        save_token_cache(_cache())
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'Authorization': 'Bearer access-1'}

    async def test_no_cache_falls_back_to_api_key(self):
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    async def test_issuer_mismatch_falls_back(self):
        save_token_cache(_cache(issuer='https://other.example'))
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    @respx.mock
    async def test_expired_token_is_refreshed(self):
        save_token_cache(_cache(access_token='old', expires_at=time.time() - 10))
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={'access_token': 'new', 'expires_in': 3600, 'refresh_token': 'refresh-2'},
            )
        )

        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'Authorization': 'Bearer new'}
        # Rotated refresh token is persisted.
        persisted = load_token_cache()
        assert persisted.access_token == 'new'
        assert persisted.refresh_token == 'refresh-2'

    @respx.mock
    async def test_refresh_keeps_previous_refresh_token_when_omitted(self):
        save_token_cache(_cache(access_token='old', expires_at=time.time() - 10))
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'new', 'expires_in': 3600})
        )

        await resolve_client_headers(_config())
        persisted = load_token_cache()
        assert persisted.refresh_token == 'refresh-1'

    @respx.mock
    async def test_refresh_failure_falls_back_to_api_key(self):
        save_token_cache(_cache(access_token='old', expires_at=time.time() - 10))
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={'error': 'invalid_grant'})
        )

        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    async def test_expired_without_refresh_token_falls_back(self):
        save_token_cache(
            _cache(access_token='old', expires_at=time.time() - 10, refresh_token=None)
        )
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}


def _unsigned_jwt(claims: dict) -> str:
    """A structurally valid JWT with the given claims and a dummy signature.

    Only the payload matters here: the client reads `exp` without verifying,
    and the server (not under test in this module) does the real verification.
    """

    def _seg(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    return f'{_seg({"alg": "RS256"})}.{_seg(claims)}.sig'


class TestIdTokenCredential:
    """`credential='id_token'` sends the id_token that a Vault-style provider signs."""

    def _id_config(self, *, api_key=None):
        oidc = OidcClientConfig(issuer=ISSUER, client_id='client-1', credential='id_token')
        return types.SimpleNamespace(oidc=oidc, api_key=SecretStr(api_key) if api_key else None)

    def test_from_response_stores_id_token_and_clamps_expiry(self):
        # id_token expires FIRST: expires_in would send an expired token.
        id_token = _unsigned_jwt({'exp': int(time.time()) + 300})
        cache = auth_client.token_cache_from_response(
            {'access_token': 'opaque-hvb', 'id_token': id_token, 'expires_in': 3600},
            issuer=ISSUER,
            credential='id_token',
        )
        assert cache.id_token == id_token
        assert cache.credential == 'id_token'
        assert cache.bearer_token == id_token
        assert cache.expires_at == pytest.approx(time.time() + 300, abs=5)

    def test_from_response_expiry_uses_the_earlier_bound(self):
        # id_token expires LAST: exp alone would stretch the refresh interval.
        id_token = _unsigned_jwt({'exp': int(time.time()) + 3600})
        cache = auth_client.token_cache_from_response(
            {'access_token': 'opaque-hvb', 'id_token': id_token, 'expires_in': 300},
            issuer=ISSUER,
            credential='id_token',
        )
        assert cache.expires_at == pytest.approx(time.time() + 300, abs=5)

    def test_from_response_without_id_token_raises(self):
        with pytest.raises(ValueError, match='carried no id_token'):
            auth_client.token_cache_from_response(
                {'access_token': 'opaque-hvb', 'expires_in': 3600},
                issuer=ISSUER,
                credential='id_token',
            )

    @pytest.mark.parametrize(
        'id_token',
        [
            _unsigned_jwt({'sub': 'alice'}),  # no exp claim
            'not-a-jwt',  # unparseable payload
            _unsigned_jwt({'exp': 'tomorrow'}),  # non-numeric exp
        ],
    )
    def test_from_response_unreadable_exp_raises(self, id_token):
        # Falling back to expires_in here is the bug the clamp exists to prevent.
        with pytest.raises(ValueError, match='no readable exp claim'):
            auth_client.token_cache_from_response(
                {'access_token': 'opaque-hvb', 'id_token': id_token, 'expires_in': 3600},
                issuer=ISSUER,
                credential='id_token',
            )

    def test_access_token_credential_does_not_persist_the_id_token(self):
        # Persisting it would put a second live credential on disk that nothing
        # reads: `_subject_from_id_token` parses the token RESPONSE, not the cache.
        id_token = _unsigned_jwt({'exp': int(time.time()) + 300})
        cache = auth_client.token_cache_from_response(
            {'access_token': 'access-1', 'id_token': id_token, 'expires_in': 3600},
            issuer=ISSUER,
        )
        assert cache.credential == 'access_token'
        assert cache.id_token is None
        assert cache.bearer_token == 'access-1'
        assert cache.expires_at == pytest.approx(time.time() + 3600, abs=5)
        save_token_cache(cache)
        assert id_token not in token_cache_path().read_text(encoding='utf-8')

    def test_cache_naming_id_token_without_one_is_rejected(self):
        with pytest.raises(ValueError, match='requires a non-empty id_token'):
            TokenCache(
                issuer=ISSUER,
                access_token='access-1',
                expires_at=time.time() + 3600,
                credential='id_token',
            )

    async def test_sends_the_id_token(self):
        save_token_cache(_cache(id_token='signed-id-token', credential='id_token'))
        headers = await resolve_client_headers(self._id_config(api_key='fallback'))
        assert headers == {'Authorization': 'Bearer signed-id-token'}

    async def test_cache_from_other_credential_is_not_reused(self, caplog):
        # Cache minted under access_token, config now asks for id_token.
        save_token_cache(_cache())
        with caplog.at_level(logging.WARNING, logger='memex.common.auth_client'):
            headers = await resolve_client_headers(self._id_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}
        # Silently dropping the cache would leave the user with an unexplained
        # 401/403 and no hint to log in again.
        assert len(caplog.records) == 1
        assert 'memex auth login' in caplog.records[0].getMessage()

    async def test_id_token_cache_not_reused_by_access_token_config(self):
        # The mirror case: sending an id_token to a provider expecting the
        # access token would be just as wrong.
        save_token_cache(_cache(id_token='signed-id-token', credential='id_token'))
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    def test_old_cache_file_without_the_new_fields_still_loads(self):
        # A token.json written before this feature existed.
        legacy = {
            'issuer': ISSUER,
            'access_token': 'access-1',
            'token_type': 'Bearer',
            'expires_at': time.time() + 3600,
            'mode': 'interactive',
        }
        token_cache_path().write_text(json.dumps(legacy), encoding='utf-8')
        loaded = load_token_cache()
        assert loaded.credential == 'access_token'
        assert loaded.id_token is None
        assert loaded.bearer_token == 'access-1'

    @respx.mock
    async def test_refresh_preserves_the_credential(self):
        save_token_cache(
            _cache(
                id_token=_unsigned_jwt({'exp': int(time.time()) - 10}),
                credential='id_token',
                expires_at=time.time() - 10,
            )
        )
        fresh_id = _unsigned_jwt({'exp': int(time.time()) + 3600})
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={'access_token': 'opaque-new', 'id_token': fresh_id, 'expires_in': 3600},
            )
        )

        headers = await resolve_client_headers(self._id_config(api_key='fallback'))
        assert headers == {'Authorization': f'Bearer {fresh_id}'}
        assert load_token_cache().credential == 'id_token'

    @respx.mock
    async def test_refresh_dropping_the_id_token_falls_back(self):
        # Never quietly resend the opaque access token: that is the 403 this
        # credential exists to avoid.
        save_token_cache(
            _cache(
                id_token=_unsigned_jwt({'exp': int(time.time()) - 10}),
                credential='id_token',
                expires_at=time.time() - 10,
            )
        )
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={'access_token': 'opaque-new', 'expires_in': 3600}
            )
        )

        headers = await resolve_client_headers(self._id_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}


def _decode_jwt_part(segment: str) -> dict:
    segment += '=' * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


class TestServiceAccount:
    def _cc_config(self, api_key=None):
        oidc = OidcClientConfig(
            issuer=ISSUER,
            client_id='svc-client',
            grant='client_credentials',
            client_secret='svc-secret',
        )
        return types.SimpleNamespace(oidc=oidc, api_key=SecretStr(api_key) if api_key else None)

    def _write_key_file(self, tmp_path):
        key = JsonWebKey.generate_key('RSA', 2048, {'kid': 'kid-1'}, is_private=True)
        pem = key.as_pem(is_private=True).decode('ascii')
        key_file = tmp_path / 'sa-key.json'
        key_file.write_text(
            json.dumps({'type': 'serviceaccount', 'keyId': 'kid-1', 'key': pem, 'userId': 'user-1'})
        )
        return key_file

    @respx.mock
    async def test_client_credentials_acquisition(self):
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'svc-tok', 'expires_in': 3600})
        )

        headers = await resolve_client_headers(self._cc_config())
        assert headers == {'Authorization': 'Bearer svc-tok'}

        body = dict(urllib.parse.parse_qsl(route.calls.last.request.content.decode()))
        assert body['grant_type'] == 'client_credentials'
        assert body['client_id'] == 'svc-client'
        assert body['client_secret'] == 'svc-secret'
        # Token cached for reuse.
        assert load_token_cache().access_token == 'svc-tok'

    @respx.mock
    async def test_client_credentials_cached_token_reused(self):
        save_token_cache(
            TokenCache(
                issuer=ISSUER,
                access_token='cached',
                expires_at=time.time() + 3600,
                subject='svc-client',
                mode='service_account',
            )
        )
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'new', 'expires_in': 3600})
        )
        headers = await resolve_client_headers(self._cc_config())
        assert headers == {'Authorization': 'Bearer cached'}
        assert route.call_count == 0  # fresh cache => no acquisition

    @respx.mock
    async def test_service_account_ignores_interactive_token(self):
        """An interactive-mode cached token must NOT be reused by the SA path."""
        save_token_cache(
            TokenCache(
                issuer=ISSUER,
                access_token='human-token',
                expires_at=time.time() + 3600,
                subject='alice@example.com',
                mode='interactive',
            )
        )
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'svc-fresh', 'expires_in': 3600})
        )
        headers = await resolve_client_headers(self._cc_config())
        assert headers == {'Authorization': 'Bearer svc-fresh'}  # acquired, not the human token

    async def test_interactive_ignores_service_account_token(self):
        """A service-account cached token must NOT be reused by the interactive path."""
        save_token_cache(
            TokenCache(
                issuer=ISSUER,
                access_token='svc-token',
                expires_at=time.time() + 3600,
                subject='svc-client',
                mode='service_account',
            )
        )
        headers = await resolve_client_headers(_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}  # SA token not reused -> fall back

    async def test_acquire_missing_key_file_raises(self, tmp_path):
        oidc = OidcClientConfig(
            issuer=ISSUER,
            client_id='c',
            grant='jwt_profile',
            key_file=str(tmp_path / 'does-not-exist.json'),
        )
        with pytest.raises(OSError):
            await acquire_service_token(oidc)

    @respx.mock
    async def test_client_credentials_reacquires_on_expiry(self):
        save_token_cache(TokenCache(issuer=ISSUER, access_token='old', expires_at=time.time() - 10))
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'fresh', 'expires_in': 3600})
        )
        headers = await resolve_client_headers(self._cc_config())
        assert headers == {'Authorization': 'Bearer fresh'}

    @respx.mock
    async def test_client_credentials_failure_falls_back_to_api_key(self):
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(401, json={'error': 'invalid_client'})
        )
        headers = await resolve_client_headers(self._cc_config(api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    @respx.mock
    async def test_jwt_profile_signs_and_exchanges_assertion(self, tmp_path):
        key_file = self._write_key_file(tmp_path)
        oidc = OidcClientConfig(
            issuer=ISSUER,
            client_id='svc-client',
            grant='jwt_profile',
            key_file=str(key_file),
        )
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={'access_token': 'jwt-tok', 'expires_in': 3600})
        )

        cache = await acquire_service_token(oidc)
        assert cache.access_token == 'jwt-tok'

        body = dict(urllib.parse.parse_qsl(route.calls.last.request.content.decode()))
        assert body['grant_type'] == 'urn:ietf:params:oauth:grant-type:jwt-bearer'
        assertion = body['assertion']
        header_b64, payload_b64, _ = assertion.split('.')
        assert _decode_jwt_part(header_b64)['kid'] == 'kid-1'
        payload = _decode_jwt_part(payload_b64)
        assert payload['iss'] == 'user-1'
        assert payload['sub'] == 'user-1'
        assert payload['aud'] == ISSUER

    async def test_jwt_profile_missing_key_file_fields(self, tmp_path):
        bad = tmp_path / 'bad.json'
        bad.write_text(json.dumps({'keyId': 'k'}))  # missing key + userId
        oidc = OidcClientConfig(
            issuer=ISSUER, client_id='c', grant='jwt_profile', key_file=str(bad)
        )
        with pytest.raises(ValueError, match='missing keyId, key, or userId'):
            await acquire_service_token(oidc)


class TestKeylessWorkloadGrants:
    def _cfg(self, oidc, api_key=None):
        return types.SimpleNamespace(oidc=oidc, api_key=SecretStr(api_key) if api_key else None)

    async def test_token_file_presented_as_bearer(self, tmp_path):
        token_path = tmp_path / 'nomad.jwt'
        token_path.write_text('  header.payload.sig\n')  # whitespace stripped
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_file', token_file=str(token_path))
        headers = await resolve_client_headers(self._cfg(oidc, api_key='fallback'))
        assert headers == {'Authorization': 'Bearer header.payload.sig'}

    async def test_token_file_read_fresh_no_cache_written(self, tmp_path):
        token_path = tmp_path / 'nomad.jwt'
        token_path.write_text('tok-1')
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_file', token_file=str(token_path))
        cfg = self._cfg(oidc)

        assert (await resolve_client_headers(cfg))['Authorization'] == 'Bearer tok-1'
        # Rotate the file (as Nomad would) — next read reflects it, no cache in the way.
        token_path.write_text('tok-2')
        assert (await resolve_client_headers(cfg))['Authorization'] == 'Bearer tok-2'
        # Keyless grants never write the interactive/service token cache.
        assert load_token_cache() is None

    async def test_token_env_presented_as_bearer(self, monkeypatch):
        monkeypatch.setenv('NOMAD_WI_TOKEN', 'env.jwt.sig')
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_env', token_env='NOMAD_WI_TOKEN')
        headers = await resolve_client_headers(self._cfg(oidc))
        assert headers == {'Authorization': 'Bearer env.jwt.sig'}

    async def test_missing_token_file_falls_back_to_api_key(self, tmp_path):
        oidc = OidcClientConfig(
            issuer=ISSUER, grant='token_file', token_file=str(tmp_path / 'nope.jwt')
        )
        headers = await resolve_client_headers(self._cfg(oidc, api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    async def test_empty_token_file_falls_back(self, tmp_path):
        token_path = tmp_path / 'empty.jwt'
        token_path.write_text('   \n')
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_file', token_file=str(token_path))
        headers = await resolve_client_headers(self._cfg(oidc, api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    async def test_missing_token_env_falls_back(self, monkeypatch):
        monkeypatch.delenv('NOMAD_WI_TOKEN', raising=False)
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_env', token_env='NOMAD_WI_TOKEN')
        headers = await resolve_client_headers(self._cfg(oidc, api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}

    async def test_permission_error_falls_back_and_does_not_leak_token(
        self, tmp_path, monkeypatch, caplog
    ):
        token_path = tmp_path / 'secret.jwt'
        token_path.write_text('super-secret-token-bytes')

        real_read_text = auth_client.plb.Path.read_text

        def boom(self, *a, **k):
            if str(self) == str(token_path):
                raise PermissionError('denied')
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(auth_client.plb.Path, 'read_text', boom)
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_file', token_file=str(token_path))
        with caplog.at_level('WARNING'):
            headers = await resolve_client_headers(self._cfg(oidc, api_key='fallback'))
        assert headers == {'X-API-Key': 'fallback'}
        assert 'super-secret-token-bytes' not in caplog.text

    async def test_success_path_never_logs_token(self, tmp_path, caplog):
        """A successful read must not log the token at any level (regression guard)."""
        token_path = tmp_path / 'ok.jwt'
        token_path.write_text('super-secret-success-token')
        oidc = OidcClientConfig(issuer=ISSUER, grant='token_file', token_file=str(token_path))
        with caplog.at_level('DEBUG'):
            headers = await resolve_client_headers(self._cfg(oidc))
        assert headers == {'Authorization': 'Bearer super-secret-success-token'}
        assert 'super-secret-success-token' not in caplog.text
