"""Integration proof that the Hermes provider authenticates to memex via OAuth.

Drives the provider's real ``_build_api_client()`` (env → MemexConfig →
MemexClientAuth) against the REAL memex auth stack and a REAL signing OIDC
provider — keyless, via a Nomad-Workload-Identity-shaped token in a file. Nothing
in the auth path is mocked; only ``httpx.ASGITransport`` routes the request into
the memex app in-process.
"""

from __future__ import annotations

import json
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi import Depends, FastAPI

from memex_common.auth_client import MemexClientAuth
from memex_common.config import AuthConfig, OidcGrantRule, OidcProviderConfig
from memex_core.server.auth import AuthContext, auth_middleware, get_auth_context, setup_auth
from memex_core.server.oidc import setup_oidc
from memex_hermes_plugin.memex.provider import MemexMemoryProvider

AUDIENCE = 'memex'


class _JwksProvider:
    """A real OIDC issuer: discovery + JWKS only (keyless needs no token endpoint)."""

    def __init__(self) -> None:
        self.key = JsonWebKey.generate_key('RSA', 2048, {'kid': 'nomad-1'}, is_private=True)
        self.issuer = ''

    def discovery(self) -> dict:
        return {'issuer': self.issuer, 'jwks_uri': f'{self.issuer}/jwks'}

    def jwks(self) -> dict:
        return {'keys': [self.key.as_dict()]}

    def mint_workload_token(self, job_id: str) -> str:
        now = int(time.time())
        payload = {
            'iss': self.issuer,
            'sub': f'nomad:job:{job_id}',
            'aud': AUDIENCE,
            'nomad_job_id': job_id,
            'iat': now,
            'exp': now + 3600,
        }
        token = jwt.encode({'alg': 'RS256', 'kid': 'nomad-1'}, payload, self.key)
        return token.decode('ascii') if isinstance(token, bytes) else token


@pytest.fixture
def provider():
    prov = _JwksProvider()

    def _handler(*args, **kwargs):
        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = None
                if self.path == '/.well-known/openid-configuration':
                    body = prov.discovery()
                elif self.path == '/jwks':
                    body = prov.jwks()
                payload = json.dumps(body or {'error': 'not_found'}).encode()
                self.send_response(200 if body else 404)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a) -> None:
                pass

        return H(*args, **kwargs)

    server = ThreadingHTTPServer(('127.0.0.1', 0), _handler)
    prov.issuer = f'http://127.0.0.1:{server.server_address[1]}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield prov
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolate_memex_config(monkeypatch):
    """Keep MemexConfig() from picking up ambient env / local YAML."""
    import memex_common.config as cfg

    monkeypatch.setattr(cfg.GlobalYamlConfigSettingsSource, '__call__', lambda self: {})
    monkeypatch.setattr(cfg.LocalYamlConfigSettingsSource, '__call__', lambda self: {})
    for var in ('MEMEX_API_KEY', 'MEMEX_SERVER_URL'):
        monkeypatch.delenv(var, raising=False)


def _memex_app(provider: _JwksProvider) -> FastAPI:
    auth_config = AuthConfig(
        enabled=True,
        oidc=[
            OidcProviderConfig(
                issuer=provider.issuer,
                audience=[AUDIENCE],
                grant_rules=[OidcGrantRule(claim='nomad_job_id', value='hermes', policy='writer')],
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


def _fake_provider_self(server_url: str, api_key: str | None = None):
    # _build_api_client only touches self._config; a duck-typed stand-in avoids the
    # full Hermes bootstrap.
    return types.SimpleNamespace(
        _config=types.SimpleNamespace(server_url=server_url, api_key=api_key)
    )


@pytest.mark.asyncio
async def test_hermes_keyless_workload_end_to_end(provider, tmp_path, monkeypatch):
    """Hermes-on-Nomad: MEMEX_OIDC__GRANT=token_file → real bearer → real verifier → 200."""
    token_path = tmp_path / 'nomad_token.jwt'
    token_path.write_text(provider.mint_workload_token('hermes'))
    monkeypatch.setenv('MEMEX_OIDC__ISSUER', provider.issuer)
    monkeypatch.setenv('MEMEX_OIDC__GRANT', 'token_file')
    monkeypatch.setenv('MEMEX_OIDC__TOKEN_FILE', str(token_path))

    built = MemexMemoryProvider._build_api_client(_fake_provider_self('http://memex.local'))
    # The provider wired the shared OAuth auth layer, driven by env.
    assert isinstance(built.auth, MemexClientAuth)

    app = _memex_app(provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://memex.local',
        auth=built.auth,
    ) as client:
        resp = await client.get('/whoami')
    assert resp.status_code == 200, resp.text
    assert resp.json()['policy'] == 'writer'


@pytest.mark.asyncio
async def test_hermes_falls_back_to_api_key_without_oidc(monkeypatch):
    """No OIDC env: the client sends the Hermes-resolved X-API-Key."""
    monkeypatch.delenv('MEMEX_OIDC__ISSUER', raising=False)
    built = MemexMemoryProvider._build_api_client(
        _fake_provider_self('http://memex.local', api_key='hermes-key')
    )
    assert isinstance(built.auth, MemexClientAuth)

    captured: dict[str, str] = {}
    app = FastAPI()

    @app.get('/echo')
    async def echo(request_headers=Depends(lambda: None)) -> dict:  # noqa: ANN001
        return {}

    @app.middleware('http')
    async def _capture(request, call_next):  # noqa: ANN001
        captured['x-api-key'] = request.headers.get('X-API-Key', '')
        captured['authorization'] = request.headers.get('Authorization', '')
        return await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://memex.local',
        auth=built.auth,
    ) as client:
        await client.get('/echo')

    assert captured['x-api-key'] == 'hermes-key'
    assert captured['authorization'] == ''
