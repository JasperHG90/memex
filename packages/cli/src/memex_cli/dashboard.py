"""CLI commands for managing the Memex Dashboard."""

import subprocess
import sys
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


def check_dashboard_installed() -> None:
    """Verify memex_dashboard and reflex are available."""
    try:
        import memex_dashboard  # noqa: F401
        import reflex  # noqa: F401
    except ImportError as e:
        console.print(f"[bold red]Error:[/bold red] Missing dependency '{e.name}'.")
        console.print('Install the dashboard package:')
        console.print('  [cyan]uv add memex-dashboard[/cyan]')
        raise typer.Exit(1)


def _get_dashboard_pkg_root() -> Path:
    """Return the location where the package is installed."""
    import memex_dashboard

    return Path(memex_dashboard.__file__).parent.parent


def _setup_runtime_cwd() -> Path:
    """Sets up a writable directory for Reflex to run in."""
    import memex_dashboard

    # 1. Locate the installed package directory
    # This points to .../site-packages/memex_dashboard/
    pkg_dir = Path(memex_dashboard.__file__).parent

    # 2. Target: Writable runtime dir
    runtime_dir = Path(user_data_dir('memex')) / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # 3. Find rxconfig.py
    # We look INSIDE the package directory first (where force-include puts it)
    config_src = (pkg_dir.parent.parent) / 'rxconfig.py'

    # Fallback: specific to some editable install structures
    if not config_src.exists():
        # Check root of the install (parent of memex_dashboard)
        config_src = pkg_dir.parent / 'rxconfig.py'

    if config_src.exists():
        config_dst = runtime_dir / 'rxconfig.py'
        # Copy if newer or missing
        if not config_dst.exists() or config_src.stat().st_mtime > config_dst.stat().st_mtime:
            shutil.copy2(config_src, config_dst)
            console.print(f'[dim]Copied config from {config_src}[/dim]')
    else:
        # CRITICAL ERROR: We can't start without config
        console.print(f'[bold red]Error:[/bold red] Could not find rxconfig.py in {pkg_dir}')
        console.print(
            '[yellow]Hint: Ensure rxconfig.py is included in your package build (e.g. via force-include).[/yellow]'
        )
        raise typer.Exit(1)

    return runtime_dir


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

    env_flag = [] if dev else ['--env', 'prod']
    port_flags = (
        ['--frontend-port', str(port), '--backend-port', str(port + 1)]
        if dev
        else ['--single-port', '--backend-host', host, '--backend-port', str(port)]
    )
    cmd = [
        sys.executable,
        '-m',
        'reflex',
        'run',
        *env_flag,
        *port_flags,
    ]

    try:
        cwd = _setup_runtime_cwd()
        console.print(f'[dim]Dashboard runtime directory: {cwd}[/dim]')
    except Exception as e:
        console.print(f'[bold red]Error setup runtime env:[/bold red] {e}')
        raise typer.Exit(1)

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
                cwd=cwd,
            )
        write_pid(SERVICE, proc.pid)
        console.print(f'[green]Dashboard started (PID {proc.pid}).[/green]')
    else:
        console.print(f'Starting dashboard in {mode} mode on {host}:{port}')
        try:
            # Foreground: inherit parent stdout/stderr so logs stream to terminal
            subprocess.run(cmd, cwd=cwd, check=True, stdout=None, stderr=None)
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
    ping_url = f'http://localhost:{config.dashboard.port}/ping/'

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
