"""Tests for the `memex auth` command group (login/logout/status + helpers)."""

import base64
import json
import time

import httpx
import pytest
import respx

from memex_common import auth_client
from memex_common.auth_client import TokenCache, load_token_cache, save_token_cache
from memex_common.config import OidcClientConfig
from memex_cli import auth as auth_cmd

ISSUER = 'https://issuer.example'
DISCOVERY_URL = f'{ISSUER}/.well-known/openid-configuration'
TOKEN_URL = f'{ISSUER}/token'
DEVICE_URL = f'{ISSUER}/device'


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_client, 'user_config_dir', lambda *a, **k: str(tmp_path))
    return tmp_path


def _oidc():
    return OidcClientConfig(issuer=ISSUER, client_id='client-1')


def _id_token(**claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
    return f'header.{payload}.sig'


class TestHelpers:
    def test_b64url_no_padding(self):
        assert '=' not in auth_cmd._b64url(b'\x00\x01\x02\x03\x04')

    def test_subject_from_id_token_prefers_email(self):
        data = {'id_token': _id_token(sub='u1', email='alice@example.com')}
        assert auth_cmd._subject_from_id_token(data) == 'alice@example.com'

    def test_subject_from_id_token_missing(self):
        assert auth_cmd._subject_from_id_token({}) is None
        assert auth_cmd._subject_from_id_token({'id_token': 'garbage'}) is None


class TestStatusLogout:
    def test_status_not_logged_in(self, capsys):
        auth_cmd.status()
        assert 'Not logged in' in capsys.readouterr().out

    def test_status_valid_token(self, capsys):
        save_token_cache(
            TokenCache(
                issuer=ISSUER,
                access_token='a',
                expires_at=time.time() + 3600,
                subject='alice@example.com',
            )
        )
        auth_cmd.status()
        out = capsys.readouterr().out
        assert 'alice@example.com' in out
        assert 'valid' in out

    def test_logout_removes_cache(self, capsys):
        save_token_cache(TokenCache(issuer=ISSUER, access_token='a', expires_at=time.time() + 60))
        auth_cmd.logout()
        assert load_token_cache() is None
        assert 'Logged out' in capsys.readouterr().out

    def test_logout_when_absent(self, capsys):
        auth_cmd.logout()
        assert 'No cached token' in capsys.readouterr().out


class TestDeviceLogin:
    @respx.mock
    async def test_device_login_success(self):
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    'device_authorization_endpoint': DEVICE_URL,
                    'token_endpoint': TOKEN_URL,
                },
            )
        )
        respx.post(DEVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    'device_code': 'dev-1',
                    'user_code': 'WXYZ',
                    'verification_uri': 'https://issuer.example/device',
                    'interval': 0,
                    'expires_in': 60,
                },
            )
        )
        respx.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(400, json={'error': 'authorization_pending'}),
                httpx.Response(
                    200,
                    json={
                        'access_token': 'tok',
                        'expires_in': 3600,
                        'refresh_token': 'r1',
                        'id_token': _id_token(email='alice@example.com'),
                    },
                ),
            ]
        )

        cache = await auth_cmd._device_login(_oidc())
        assert cache.access_token == 'tok'
        assert cache.refresh_token == 'r1'
        assert cache.subject == 'alice@example.com'

    @respx.mock
    async def test_device_login_no_endpoint(self):
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={'token_endpoint': TOKEN_URL})
        )
        with pytest.raises(auth_cmd.LoginError, match='device_authorization_endpoint'):
            await auth_cmd._device_login(_oidc())

    @respx.mock
    async def test_device_login_hard_error(self):
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200,
                json={'device_authorization_endpoint': DEVICE_URL, 'token_endpoint': TOKEN_URL},
            )
        )
        respx.post(DEVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    'device_code': 'dev-1',
                    'user_code': 'WXYZ',
                    'verification_uri': 'https://issuer.example/device',
                    'interval': 0,
                    'expires_in': 60,
                },
            )
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={'error': 'access_denied'})
        )
        with pytest.raises(auth_cmd.LoginError, match='access_denied'):
            await auth_cmd._device_login(_oidc())
