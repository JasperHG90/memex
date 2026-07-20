"""End-to-end proof that service-account OIDC auth works, with NO mocks.

A real OIDC provider (a threaded HTTP server) signs real JWTs, serves real
discovery + JWKS, and runs a real token endpoint. The real memex auth
middleware + OidcVerifier verify those tokens, and the real client-side
`MemexClientAuth` acquires a service-account token and attaches it. The only
in-process shortcut is httpx.ASGITransport, which routes the client's request
into the memex ASGI app without a socket — the auth path itself (signing,
token exchange, JWKS fetch, verification, claim mapping) is entirely real.
"""

import json
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from authlib.jose import JsonWebKey, JsonWebToken, jwt
from fastapi import Depends, FastAPI

from memex_common import auth_client
from memex_common.auth_client import MemexClientAuth
from memex_common.config import (
    AuthConfig,
    OidcClientConfig,
    OidcGrantRule,
    OidcProviderConfig,
)
from memex_core.server.auth import AuthContext, auth_middleware, get_auth_context, setup_auth
from memex_core.server.oidc import setup_oidc

AUDIENCE = 'memex-api'
CLIENT_ID = 'memex-svc'
CLIENT_SECRET = 'super-secret'
SERVICE_USER_ID = 'user-42'


# ---------------------------------------------------------------------------
# A real, signing OIDC provider
# ---------------------------------------------------------------------------


class _Provider:
    """A minimal but real OIDC provider: discovery, JWKS, and a token endpoint."""

    def __init__(self) -> None:
        # Key the provider signs ACCESS tokens with (published via JWKS).
        self.signing_key = JsonWebKey.generate_key('RSA', 2048, {'kid': 'prov-1'}, is_private=True)
        # The service account's key: provider verifies jwt_profile assertions with
        # its public half; the client signs with the private half (via key_file).
        self.client_key = JsonWebKey.generate_key('RSA', 2048, {'kid': 'cli-1'}, is_private=True)
        self.issuer = ''  # set once the server is bound to a port

    def discovery(self) -> dict:
        return {
            'issuer': self.issuer,
            'jwks_uri': f'{self.issuer}/jwks',
            'token_endpoint': f'{self.issuer}/token',
            'authorization_endpoint': f'{self.issuer}/authorize',
            'device_authorization_endpoint': f'{self.issuer}/device',
        }

    def jwks(self) -> dict:
        return {'keys': [self.signing_key.as_dict()]}

    def mint_access_token(self, subject: str, roles: list[str]) -> str:
        now = int(time.time())
        payload = {
            'iss': self.issuer,
            'sub': subject,
            'aud': AUDIENCE,
            'roles': roles,
            'iat': now,
            'exp': now + 3600,
        }
        token = jwt.encode({'alg': 'RS256', 'kid': 'prov-1'}, payload, self.signing_key)
        return token.decode('ascii') if isinstance(token, bytes) else token

    def handle_token(self, form: dict) -> tuple[int, dict]:
        grant = form.get('grant_type', [None])[0]
        if grant == 'client_credentials':
            if form.get('client_id', [None])[0] != CLIENT_ID:
                return 401, {'error': 'invalid_client'}
            if form.get('client_secret', [None])[0] != CLIENT_SECRET:
                return 401, {'error': 'invalid_client'}
            token = self.mint_access_token(CLIENT_ID, ['memex.writer'])
            return 200, {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600}

        if grant == 'urn:ietf:params:oauth:grant-type:jwt-bearer':
            assertion = form.get('assertion', [None])[0]
            if not assertion:
                return 400, {'error': 'invalid_request'}
            # REAL verification of the client's self-signed assertion.
            try:
                claims = JsonWebToken(['RS256']).decode(
                    assertion,
                    self.client_key,
                    claims_options={
                        'iss': {'essential': True, 'value': SERVICE_USER_ID},
                        'aud': {'essential': True, 'value': self.issuer},
                    },
                )
                claims.validate()
            except Exception as exc:  # noqa: BLE001 - provider returns an OAuth error
                return 400, {'error': 'invalid_grant', 'error_description': str(exc)}
            token = self.mint_access_token(SERVICE_USER_ID, ['memex.writer'])
            return 200, {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600}

        return 400, {'error': 'unsupported_grant_type'}


def _make_provider_server(provider: _Provider) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == '/.well-known/openid-configuration':
                self._send(200, provider.discovery())
            elif self.path == '/jwks':
                self._send(200, provider.jwks())
            else:
                self._send(404, {'error': 'not_found'})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length).decode()
            form = urllib.parse.parse_qs(raw)
            if self.path == '/token':
                status, body = provider.handle_token(form)
                self._send(status, body)
            else:
                self._send(404, {'error': 'not_found'})

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)


@pytest.fixture
def provider():
    prov = _Provider()
    server = _make_provider_server(prov)
    port = server.server_address[1]
    prov.issuer = f'http://127.0.0.1:{port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield prov
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_client, 'user_config_dir', lambda *a, **k: str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# A minimal memex app that uses the REAL auth stack
# ---------------------------------------------------------------------------


def _build_memex_app(provider: _Provider) -> FastAPI:
    auth_config = AuthConfig(
        enabled=True,
        oidc=[
            OidcProviderConfig(
                issuer=provider.issuer,
                audience=[AUDIENCE],
                grant_rules=[
                    OidcGrantRule(claim='roles', value='memex.writer', policy='writer'),
                ],
            )
        ],
    )
    app = FastAPI()
    app.middleware('http')(auth_middleware)
    setup_auth(app, auth_config)
    setup_oidc(app, auth_config)

    @app.get('/whoami')
    async def whoami(auth: AuthContext | None = Depends(get_auth_context)) -> dict:
        assert auth is not None
        return {'policy': auth.policy.value, 'actor': auth.key_prefix}

    return app


def _client_config(provider: _Provider, oidc: OidcClientConfig):
    return types.SimpleNamespace(oidc=oidc, api_key=None)


async def _call_whoami(app: FastAPI, client_config) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url='http://memex.local',
        auth=MemexClientAuth(client_config),
    ) as client:
        return await client.get('/whoami')


# ---------------------------------------------------------------------------
# The proofs
# ---------------------------------------------------------------------------


async def test_client_credentials_end_to_end(provider):
    """SA client-credentials: acquire a real token, verify it, reach a route."""
    app = _build_memex_app(provider)
    oidc = OidcClientConfig(
        issuer=provider.issuer,
        client_id=CLIENT_ID,
        grant='client_credentials',
        client_secret=CLIENT_SECRET,
        scopes=['openid'],
    )
    resp = await _call_whoami(app, _client_config(provider, oidc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['policy'] == 'writer'
    assert body['actor'] == f'oidc:{CLIENT_ID}'


async def test_jwt_profile_end_to_end(provider, tmp_path):
    """SA jwt_profile: sign a real assertion, exchange it, verify, reach a route."""
    key_file = tmp_path / 'sa-key.json'
    key_file.write_text(
        json.dumps(
            {
                'type': 'serviceaccount',
                'keyId': 'cli-1',
                'key': provider.client_key.as_pem(is_private=True).decode('ascii'),
                'userId': SERVICE_USER_ID,
            }
        )
    )
    app = _build_memex_app(provider)
    oidc = OidcClientConfig(
        issuer=provider.issuer,
        client_id=CLIENT_ID,
        grant='jwt_profile',
        key_file=str(key_file),
        scopes=['openid'],
    )
    resp = await _call_whoami(app, _client_config(provider, oidc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['policy'] == 'writer'
    assert body['actor'] == f'oidc:{SERVICE_USER_ID}'


async def test_jwt_profile_wrong_client_key_is_rejected(provider, tmp_path):
    """A key the provider does not trust must fail the token exchange -> no access."""
    wrong_key = JsonWebKey.generate_key('RSA', 2048, {'kid': 'cli-1'}, is_private=True)
    key_file = tmp_path / 'bad-key.json'
    key_file.write_text(
        json.dumps(
            {
                'keyId': 'cli-1',
                'key': wrong_key.as_pem(is_private=True).decode('ascii'),
                'userId': SERVICE_USER_ID,
            }
        )
    )
    app = _build_memex_app(provider)
    oidc = OidcClientConfig(
        issuer=provider.issuer,
        client_id=CLIENT_ID,
        grant='jwt_profile',
        key_file=str(key_file),
        scopes=['openid'],
    )
    # Token acquisition fails -> no bearer -> no api key -> 401 at the server.
    resp = await _call_whoami(app, _client_config(provider, oidc))
    assert resp.status_code == 401


async def test_garbage_bearer_rejected(provider):
    """The server really enforces: an unsigned/garbage bearer is 403, none is 401."""
    app = _build_memex_app(provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://memex.local') as client:
        assert (await client.get('/whoami')).status_code == 401
        bad = await client.get('/whoami', headers={'Authorization': 'Bearer not.a.jwt'})
        assert bad.status_code == 403
