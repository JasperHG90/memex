"""CLI commands for managing the Memex Dashboard."""

import os
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path
import shutil

import httpx
import typer
from rich.console import Console

from memex_cli.process import (
    GRACEFUL_TIMEOUT,
    check_port_available,
    graceful_stop,
    log_file_path,
    read_pid,
    write_pid,
)
from platformdirs import user_data_dir
from memex_cli.utils import async_command
from memex_common.config import parse_memex_config

console = Console()
app = typer.Typer(name='dashboard', help='Manage the Memex Dashboard.', no_args_is_help=True)

SERVICE = 'dashboard'

GITHUB_REPO = 'JasperHG90/memex'
DASHBOARD_ASSET = 'dashboard-dist.tar.gz'


def _get_install_dir() -> Path:
    """Return the standard install location for the dashboard."""
    return Path(user_data_dir('memex')) / 'dashboard'


def _get_dashboard_dir() -> Path:
    """Locate the dashboard directory."""
    # Check relative to this package (monorepo layout)
    mono_path = Path(__file__).parent.parent.parent.parent / 'dashboard'
    if mono_path.exists():
        return mono_path
    # Check common install location
    return _get_install_dir()


def check_dashboard_installed() -> None:
    """Verify Node.js is available and dashboard exists."""
    if not shutil.which('node'):
        console.print('[bold red]Error:[/bold red] Node.js is not installed or not on PATH.')
        console.print('Install Node.js from: [cyan]https://nodejs.org/[/cyan]')
        raise typer.Exit(1)

    dashboard_dir = _get_dashboard_dir()
    if not (dashboard_dir / 'dist').exists():
        console.print('[bold red]Error:[/bold red] Dashboard is not installed.')
        console.print('Run [cyan]memex dashboard install[/cyan] to download and install it.')
        raise typer.Exit(1)


def _check_api_server(server_url: str) -> bool:
    """Check if the Memex API server is reachable. Returns True if healthy."""
    health_url = f'{server_url}/api/v1/health'
    try:
        resp = httpx.get(health_url, timeout=5.0)
        return resp.status_code == 200
    except httpx.RequestError:
        return False


def _download_dashboard_asset(version: str | None) -> Path:
    """Download dashboard-dist.tar.gz from GitHub releases. Returns path to temp file."""
    if version:
        url = f'https://github.com/{GITHUB_REPO}/releases/download/{version}/{DASHBOARD_ASSET}'
    else:
        url = f'https://github.com/{GITHUB_REPO}/releases/latest/download/{DASHBOARD_ASSET}'

    console.print(f'Downloading dashboard from [cyan]{url}[/cyan] ...')
    tmp = tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False)
    try:
        with httpx.stream('GET', url, follow_redirects=True, timeout=60.0) as resp:
            if resp.status_code == 404:
                console.print(
                    f'[bold red]Error:[/bold red] Release asset not found at [cyan]{url}[/cyan].'
                )
                if version:
                    console.print(
                        f'Check that version [cyan]{version}[/cyan] exists at: '
                        f'[cyan]https://github.com/{GITHUB_REPO}/releases[/cyan]'
                    )
                raise typer.Exit(1)
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                tmp.write(chunk)
        tmp.close()
        return Path(tmp.name)
    except httpx.RequestError as e:
        console.print(f'[bold red]Error:[/bold red] Download failed: {e}')
        raise typer.Exit(1) from e


@app.command()
def install(
    version: str = typer.Option(
        None, '--version', '-v', help='Release tag to install (e.g. v0.0.3a). Default: latest.'
    ),
    force: bool = typer.Option(False, '--force', '-f', help='Overwrite existing installation.'),
) -> None:
    """Download and install the Memex Dashboard from GitHub releases."""
    if not shutil.which('node'):
        console.print('[bold red]Error:[/bold red] Node.js is not installed or not on PATH.')
        console.print('Install Node.js from: [cyan]https://nodejs.org/[/cyan]')
        raise typer.Exit(1)

    install_dir = _get_install_dir()

    if (install_dir / 'dist').exists() and not force:
        console.print(f'[yellow]Dashboard is already installed at {install_dir}.[/yellow]')
        console.print('Use [cyan]--force[/cyan] to overwrite.')
        raise typer.Exit(0)

    tarball = _download_dashboard_asset(version)
    try:
        # Clean existing install
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(tarball, 'r:gz') as tf:
            tf.extractall(install_dir, filter='data')

        console.print(f'[green]Dashboard installed to {install_dir}.[/green]')
        console.print('Start it with: [cyan]memex dashboard start[/cyan]')
    finally:
        tarball.unlink(missing_ok=True)


@app.command()
def start(
    host: str = typer.Option(None, help='Host to bind to (default: from config)'),
    port: int = typer.Option(None, help='Port to bind to (default: from config)'),
    dev: bool = typer.Option(False, '--dev', help='Run in development mode (hot reload)'),
    daemon: bool = typer.Option(
        False, '--daemon', '-d', help='Run in background (production only)'
    ),
) -> None:
    """Start the Memex Dashboard."""
    check_dashboard_installed()

    config = parse_memex_config()
    host = host or config.dashboard.host
    port = port or config.dashboard.port
    server_url = config.server_url

    # Verify the API server is live before starting
    if not _check_api_server(server_url):
        console.print(
            f'[bold red]Error:[/bold red] Memex API server is not reachable at '
            f'[cyan]{server_url}[/cyan].'
        )
        console.print('Start the server first with: [cyan]memex server start[/cyan]')
        raise typer.Exit(1)

    # Check for existing process
    existing_pid = read_pid(SERVICE)
    if existing_pid:
        console.print(f'[yellow]Dashboard is already running (PID {existing_pid}).[/yellow]')
        console.print('Use [cyan]memex dashboard stop[/cyan] to stop it first.')
        raise typer.Exit(0)

    # Check port availability
    if not check_port_available(host, port):
        console.print(f'[bold red]Error:[/bold red] Port {port} is already in use.')
        console.print(f'Try: [cyan]memex dashboard start --port {port + 1}[/cyan]')
        raise typer.Exit(1)

    if dev and daemon:
        console.print(
            '[yellow]Warning: --daemon is not supported with --dev. Ignoring daemon flag.[/yellow]'
        )
        daemon = False

    dashboard_dir = _get_dashboard_dir()

    if dev:
        cmd = ['npm', 'run', 'dev', '--', '--host', host, '--port', str(port)]
    else:
        dist_dir = dashboard_dir / 'dist'
        if not dist_dir.exists():
            console.print('[bold red]Error:[/bold red] Dashboard is not installed.')
            console.print('Run [cyan]memex dashboard install[/cyan] to download and install it.')
            raise typer.Exit(1)
        serve_script = dashboard_dir / 'serve.cjs'
        if not serve_script.exists():
            console.print(
                '[bold red]Error:[/bold red] serve.cjs not found. '
                'Run [cyan]memex dashboard install[/cyan] to update.'
            )
            raise typer.Exit(1)
        cmd = [
            'node',
            str(serve_script),
            '--host',
            host,
            '--port',
            str(port),
            '--api',
            server_url,
        ]

    mode = 'development' if dev else 'production'
    log = log_file_path(SERVICE)

    if daemon:
        console.print(f'Starting dashboard in daemon mode on {host}:{port}')
        console.print(f'API proxy target: {server_url}')
        console.print(f'Logs will be written to: {log}')

        with open(log, 'a') as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
                cwd=dashboard_dir,
            )
        write_pid(SERVICE, proc.pid)
        console.print(f'[green]Dashboard started (PID {proc.pid}).[/green]')
    else:
        console.print(f'Starting dashboard in {mode} mode on {host}:{port}')
        if not dev:
            console.print(f'API proxy target: {server_url}')
        proc = subprocess.Popen(
            cmd,
            cwd=dashboard_dir,
            start_new_session=True,
        )
        try:
            proc.wait()
        except KeyboardInterrupt:
            # Terminate the entire process group (npm/npx + children)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=GRACEFUL_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
            console.print('\nDashboard stopped.')
        else:
            if proc.returncode and proc.returncode != 0:
                console.print(
                    f'[bold red]Error:[/bold red] Dashboard exited with code {proc.returncode}.'
                )
                raise typer.Exit(proc.returncode)


@app.command()
def stop() -> None:
    """Stop the running Memex Dashboard."""
    stopped = graceful_stop(SERVICE)
    if stopped:
        console.print('[green]Dashboard stopped.[/green]')
    else:
        console.print('No running dashboard found.')


@app.command('status')
@async_command
async def status() -> None:
    """Check the status of the Memex Dashboard."""
    pid = read_pid(SERVICE)
    if not pid:
        console.print('[red]Dashboard is NOT running.[/red]')
        raise typer.Exit(code=1)

    console.print(f'[green]Dashboard is running.[/green] PID: {pid}')

    config = parse_memex_config()
    ping_url = f'http://localhost:{config.dashboard.port}/'

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(ping_url)
            if resp.status_code == 200:
                console.print(f'[green]Health check passed:[/green] {ping_url}')
            else:
                console.print(f'[yellow]Health check returned {resp.status_code}.[/yellow]')
    except httpx.RequestError as e:
        console.print(f'[red]Error connecting to dashboard:[/red] {e}')
        console.print('[yellow]Process is running but not responding to HTTP.[/yellow]')
