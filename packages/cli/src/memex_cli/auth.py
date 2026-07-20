"""OIDC login commands: ``memex auth login | logout | status``.

Obtains a bearer token from the configured OIDC provider and caches it (via
``memex_common.auth_client``) so the CLI and MCP send ``Authorization: Bearer``
instead of ``X-API-Key``. Supports Authorization Code + PKCE with a loopback
redirect (default) and the Device Authorization Grant (``--device``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser

import httpx
import typer
from rich.console import Console

from memex_common.auth_client import (
    TokenCache,
    clear_token_cache,
    discover_oidc,
    load_token_cache,
    save_token_cache,
    token_cache_from_response,
    token_cache_path,
)
from memex_common.config import MemexConfig, OidcClientConfig

console = Console()

app = typer.Typer(
    name='auth',
    help='Authenticate with an OIDC provider (bearer tokens alongside API keys).',
    no_args_is_help=True,
)

_HTTP_TIMEOUT = 30.0
_LOGIN_TIMEOUT_SECONDS = 300


@app.callback()
def auth_callback() -> None:
    """Authenticate with an OIDC provider."""


def _require_oidc(ctx: typer.Context) -> OidcClientConfig:
    config: MemexConfig = ctx.obj
    if config.oidc is None:
        console.print(
            '[red]No OIDC provider configured.[/red] Set '
            '[cyan]oidc.issuer[/cyan] and [cyan]oidc.client_id[/cyan] in your config.'
        )
        raise typer.Exit(1)
    return config.oidc


@app.command('login')
def login(
    ctx: typer.Context,
    device: bool = typer.Option(
        False, '--device', help='Use the device authorization grant (headless / no browser).'
    ),
) -> None:
    """Log in and cache a bearer token."""
    oidc = _require_oidc(ctx)
    try:
        if device:
            cache = asyncio.run(_device_login(oidc))
        else:
            cache = asyncio.run(_loopback_login(oidc))
    except httpx.HTTPError as exc:
        console.print(f'[red]Login failed: {exc}[/red]')
        raise typer.Exit(1)
    except (LoginError, KeyError) as exc:
        console.print(f'[red]Login failed: {exc}[/red]')
        raise typer.Exit(1)
    save_token_cache(cache)
    who = f' as [cyan]{cache.subject}[/cyan]' if cache.subject else ''
    console.print(f'[green]Logged in{who}.[/green] Token cached at {token_cache_path()}.')


@app.command('logout')
def logout() -> None:
    """Delete the cached token."""
    if clear_token_cache():
        console.print('[green]Logged out.[/green]')
    else:
        console.print('[yellow]No cached token to remove.[/yellow]')


@app.command('status')
def status() -> None:
    """Show the cached token's identity and expiry."""
    cache = load_token_cache()
    if cache is None:
        console.print('[yellow]Not logged in.[/yellow]')
        return
    remaining = int(cache.expires_at - time.time())
    state = '[green]valid[/green]' if cache.is_fresh() else '[red]expired[/red]'
    console.print(f'Issuer:  {cache.issuer}')
    if cache.subject:
        console.print(f'Subject: {cache.subject}')
    console.print(f'Token:   {state} (expires in {remaining}s)')
    console.print(f'Refresh: {"available" if cache.refresh_token else "none"}')


class LoginError(RuntimeError):
    """Raised on a login-flow protocol error (state mismatch, timeout, etc.)."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _subject_from_id_token(token_response: dict) -> str | None:
    """Best-effort display name from the id_token (unverified — display only)."""
    id_token = token_response.get('id_token')
    if not isinstance(id_token, str) or id_token.count('.') != 2:
        return None
    try:
        payload_b64 = id_token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, KeyError):
        return None
    value = claims.get('email') or claims.get('preferred_username') or claims.get('sub')
    return str(value) if value is not None else None


def _make_callback_server(result: dict, event: threading.Event) -> http.server.HTTPServer:
    """A one-shot loopback server that captures the OAuth redirect params."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != '/callback':
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            result['code'] = params.get('code', [None])[0]
            result['state'] = params.get('state', [None])[0]
            result['error'] = params.get('error', [None])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body><h3>Login complete. You can close this tab.</h3></body></html>'
            )
            event.set()

        def log_message(self, *args) -> None:  # silence the default stderr logging
            pass

    return http.server.HTTPServer(('127.0.0.1', 0), _Handler)


async def _loopback_login(oidc: OidcClientConfig) -> TokenCache:
    discovery = await discover_oidc(oidc.issuer)
    auth_endpoint = discovery['authorization_endpoint']
    token_endpoint = discovery['token_endpoint']

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode('ascii')).digest())
    state = secrets.token_urlsafe(16)

    result: dict = {}
    event = threading.Event()
    server = _make_callback_server(result, event)
    port = server.server_address[1]
    redirect_uri = f'http://127.0.0.1:{port}/callback'

    params = {
        'response_type': 'code',
        'client_id': oidc.client_id,
        'redirect_uri': redirect_uri,
        'scope': ' '.join(oidc.scopes),
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    url = auth_endpoint + '?' + urllib.parse.urlencode(params)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        console.print('Opening your browser to log in. If it does not open, visit:')
        console.print(f'[cyan]{url}[/cyan]')
        webbrowser.open(url)
        got = await asyncio.get_running_loop().run_in_executor(
            None, event.wait, _LOGIN_TIMEOUT_SECONDS
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    if not got:
        raise LoginError('timed out waiting for the browser redirect.')
    if result.get('error'):
        raise LoginError(f'provider returned error: {result["error"]}')
    if result.get('state') != state:
        raise LoginError('state mismatch (possible CSRF); aborting.')
    code = result.get('code')
    if not code:
        raise LoginError('no authorization code returned.')

    form = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': oidc.client_id,
        'code_verifier': verifier,
    }
    if oidc.client_secret is not None:
        form['client_secret'] = oidc.client_secret.get_secret_value()

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(token_endpoint, data=form)
        response.raise_for_status()
        data = response.json()

    cache = token_cache_from_response(data, issuer=oidc.issuer)
    cache.subject = _subject_from_id_token(data)
    return cache


async def _device_login(oidc: OidcClientConfig) -> TokenCache:
    discovery = await discover_oidc(oidc.issuer)
    device_endpoint = discovery.get('device_authorization_endpoint')
    token_endpoint = discovery['token_endpoint']
    if not device_endpoint:
        raise LoginError('provider does not advertise a device_authorization_endpoint.')

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        start = await client.post(
            device_endpoint,
            data={'client_id': oidc.client_id, 'scope': ' '.join(oidc.scopes)},
        )
        start.raise_for_status()
        device = start.json()

        verification_uri = device.get('verification_uri_complete') or device['verification_uri']
        console.print('To log in, visit:')
        console.print(f'[cyan]{verification_uri}[/cyan]')
        console.print(f'and enter code: [bold]{device["user_code"]}[/bold]')

        interval = int(device.get('interval', 5))
        deadline = time.time() + int(device.get('expires_in', 300))
        form = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': device['device_code'],
            'client_id': oidc.client_id,
        }
        if oidc.client_secret is not None:
            form['client_secret'] = oidc.client_secret.get_secret_value()

        while time.time() < deadline:
            await asyncio.sleep(interval)
            poll = await client.post(token_endpoint, data=form)
            if poll.status_code == 200:
                data = poll.json()
                cache = token_cache_from_response(data, issuer=oidc.issuer)
                cache.subject = _subject_from_id_token(data)
                return cache
            error = poll.json().get('error')
            if error == 'authorization_pending':
                continue
            if error == 'slow_down':
                interval += 5
                continue
            raise LoginError(f'device login failed: {error or poll.status_code}')

    raise LoginError('timed out waiting for device authorization.')
