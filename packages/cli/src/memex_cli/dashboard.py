"""CLI commands for managing the Memex Dashboard."""

import subprocess
from pathlib import Path
import shutil

import httpx
import typer
from rich.console import Console

from memex_cli.process import (
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


def _get_dashboard_dir() -> Path:
    """Locate the dashboard directory."""
    # Check relative to this package (monorepo layout)
    mono_path = Path(__file__).parent.parent.parent.parent / 'dashboard'
    if mono_path.exists():
        return mono_path
    # Check common install location
    return Path(user_data_dir('memex')) / 'dashboard'


def check_dashboard_installed() -> None:
    """Verify Node.js is available and dashboard exists."""
    if not shutil.which('node'):
        console.print('[bold red]Error:[/bold red] Node.js is not installed or not on PATH.')
        console.print('Install Node.js from: [cyan]https://nodejs.org/[/cyan]')
        raise typer.Exit(1)

    dashboard_dir = _get_dashboard_dir()
    if not dashboard_dir.exists():
        console.print(f'[bold red]Error:[/bold red] Dashboard UI not found at {dashboard_dir}')
        console.print('Run: [cyan]cd packages/dashboard && npm install[/cyan]')
        raise typer.Exit(1)


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
            console.print(
                '[bold red]Error:[/bold red] Production build not found. '
                'Run [cyan]npm run build[/cyan] in the dashboard directory first.'
            )
            raise typer.Exit(1)
        cmd = ['npx', 'serve', 'dist', '-l', f'tcp://{host}:{port}']

    mode = 'development' if dev else 'production'
    log = log_file_path(SERVICE)

    if daemon:
        console.print(f'Starting dashboard in daemon mode on {host}:{port}')
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
        try:
            subprocess.run(cmd, cwd=dashboard_dir, check=True, stdout=None, stderr=None)
        except KeyboardInterrupt:
            console.print('\nDashboard stopped.')
        except subprocess.CalledProcessError as e:
            console.print(f'[bold red]Error:[/bold red] Dashboard exited with code {e.returncode}.')
            raise typer.Exit(e.returncode)


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
