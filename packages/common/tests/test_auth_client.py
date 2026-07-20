"""Tests for the shared client-side OIDC token cache and header resolution."""

import stat
import time
import types

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex_common import auth_client
from memex_common.auth_client import (
    TokenCache,
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
