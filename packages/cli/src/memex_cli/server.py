import os
import sys
import asyncio
from uuid import UUID

import typer
import httpx
from rich.console import Console

from memex_cli.process import (
    check_port_available,
    graceful_stop,
    pid_file_path,
    read_pid,
)
from memex_cli.utils import async_command
from memex_common.config import (
    parse_memex_config,
    PostgresInstanceConfig,
    MemexConfig,
)

console = Console()
app = typer.Typer(name='server', help='Manage the Memex Core server.', no_args_is_help=True)

SERVICE = 'server'


def check_core_installed():
    """Verify memex-core is available."""
    try:
        import memex_core  # noqa: F401
        import asyncpg  # noqa: F401
    except ImportError as e:
        console.print(f"[bold red]Error:[/bold red] Missing dependency '{e.name}'.")
        console.print('To run the server, install memex-core:')
        console.print('  [cyan]uv add memex-core[/cyan]')
        raise typer.Exit(1)


async def _readiness_check(config: PostgresInstanceConfig):
    """Attempt to connect to the database."""
    import asyncpg

    try:
        conn = await asyncpg.connect(
            user=config.user,
            password=config.password.get_secret_value(),
            database=config.database,
            host=config.host,
            port=config.port,
        )
        await conn.execute('SELECT 1')
        await conn.close()
        return True
    except Exception as e:
        console.print(f'[yellow]Database check failed: {e}[/yellow]')
        return False


async def _initialize_database(config):
    """
    Initialize the database schema (tables, extensions) before starting workers.
    This prevents race conditions when multiple workers try to create schemas concurrently.
    """
    try:
        from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
        from memex_core.memory.sql_models import Vault
        from memex_core.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME

        # Ensure models are imported so SQLModel.metadata is populated
        import memex_core.memory.sql_models  # noqa: F401

        console.print('[dim]Initializing database schema and seed data...[/dim]')
        engine = AsyncPostgresMetaStoreEngine(config.server.meta_store)
        await engine.connect()

        # Seed Global Vault
        async with engine.session() as session:
            # 1. Ensure Global Vault
            vault = await session.get(Vault, GLOBAL_VAULT_ID)
            if not vault:
                console.print('[dim]Seeding Global Vault...[/dim]')
                vault = Vault(
                    id=GLOBAL_VAULT_ID,
                    name=GLOBAL_VAULT_NAME,
                    description='Default global vault for all memories.',
                )
                session.add(vault)

            # 2. Ensure Active Vault (if different)
            if config.server.active_vault != GLOBAL_VAULT_NAME:
                from sqlmodel import select

                # Check by name first
                stmt = select(Vault).where(Vault.name == config.server.active_vault)
                active_vault = (await session.exec(stmt)).first()

                if not active_vault:
                    # Check if it's a UUID
                    try:
                        v_id = UUID(config.server.active_vault)
                        active_vault = await session.get(Vault, v_id)
                    except ValueError:
                        pass

                if not active_vault:
                    console.print(
                        f"[dim]Seeding Active Vault: '{config.server.active_vault}'...[/dim]"
                    )
                    # If it's a UUID string, use it as ID
                    try:
                        v_id = UUID(config.server.active_vault)
                        new_vault = Vault(
                            id=v_id,
                            name=config.server.active_vault,
                            description=f'Auto-initialized vault (ID: {config.server.active_vault})',
                        )
                    except ValueError:
                        new_vault = Vault(
                            name=config.server.active_vault,
                            description=f'Auto-initialized vault: {config.server.active_vault}',
                        )
                    session.add(new_vault)

            await session.commit()

        await engine.close()
        console.print('[dim]Database initialized.[/dim]')
    except Exception as e:
        console.print(f'[bold red]Schema Initialization Error:[/bold red] {e}')
        raise typer.Exit(1)


@app.command()
def start(
    ctx: typer.Context,
    host: str = typer.Option('0.0.0.0', envvar='MEMEX_HOST', help='Host to bind the server to'),
    port: int = typer.Option(8000, envvar='MEMEX_PORT', help='Port to bind the server to'),
    workers: int = typer.Option(
        None, '--workers', '-w', envvar='MEMEX_WORKERS', help='Number of worker processes'
    ),
    config: str = typer.Option(
        None, '--config', '-c', envvar='MEMEX_CONFIG_PATH', help='Path to configuration file'
    ),
    reload: bool = typer.Option(False, help='Enable auto-reload for development'),
    daemon: bool = typer.Option(
        False, '--daemon', '-d', help='Run the server in the background (gunicorn only)'
    ),
):
    """Start the Memex Core API server."""
    check_core_installed()

    # Check if already running via PID file
    existing_pid = read_pid(SERVICE)
    if existing_pid:
        console.print(
            f'[yellow]Memex Core server is already running (PID {existing_pid}).[/yellow]'
        )
        console.print('Use [cyan]memex server stop[/cyan] to stop it first.')
        raise typer.Exit(0)

    # Check port availability
    if not check_port_available(host, port):
        console.print(f'[bold red]Error:[/bold red] Port {port} is already in use.')
        raise typer.Exit(1)

    import uvicorn

    if config:
        os.environ['MEMEX_CONFIG_PATH'] = config

    # Load config and check DB
    conf = parse_memex_config()
    db_ready = asyncio.run(_readiness_check(conf.server.meta_store.instance))
    if not db_ready:
        console.print(
            f'[bold red]Error:[/bold red] Unable to connect to database on {conf.server.meta_store.instance.host}:{conf.server.meta_store.instance.port}.'
        )
        raise typer.Exit(1)

    # Initialize schema once to avoid race conditions in workers
    asyncio.run(_initialize_database(conf))

    if reload:
        # Dev mode: use uvicorn directly
        if daemon:
            console.print(
                '[yellow]Warning: --daemon is not supported with --reload. Ignoring daemon flag.[/yellow]'
            )

        console.print(f'Starting Uvicorn development server on {host}:{port}...')
        uvicorn.run(
            'memex_core.server:app',
            host=host,
            port=port,
            reload=True,
        )
    else:
        # Prod mode: use gunicorn
        if workers is None:
            workers = 2

        # Signal to workers (via env) that schema is already created
        os.environ['MEMEX_SKIP_SCHEMA_CREATION'] = 'true'

        preload = False  # Preloading can cause issues on mac, but helps on other OS
        import platform

        if platform.system() == 'Darwin':
            # macOS networking/SSL libraries are not fork-safe.
            # This flag tells the OS to relax those checks.
            os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

            # 2. Prevent the "NSCharacterSet" crash (The Proxy Lookup Fix)
            # This stops Python from calling into macOS SystemConfiguration to check
            # for proxies, which is what triggers the NSCharacterSet crash.
            os.environ['no_proxy'] = '*'

            # 3. Prevent OpenMP Deadlocks
            os.environ['OMP_NUM_THREADS'] = '1'

            console.print(
                '[yellow]macOS detected: Fork safety, No-Proxy, & Threading fixes applied.[/yellow]'
            )

            console.print('[dim]macOS detected: Enabling fork safety workaround.[/dim]')
        else:
            preload = True

        from platformdirs import user_log_dir
        import pathlib as plb

        log_dir = plb.Path(user_log_dir('memex', appauthor=False))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'memex.log'

        # Determine log level by traversing up context
        debug_mode = False
        current_ctx = ctx
        while current_ctx:
            if current_ctx.params.get('debug'):
                debug_mode = True
                break
            current_ctx = current_ctx.parent

        log_level = 'debug' if debug_mode else 'info'

        # Pass log level to worker processes via env var so the FastAPI app
        # can configure application-level loggers (memex.*).
        os.environ['MEMEX_LOG_LEVEL'] = log_level.upper()

        if daemon:
            console.print(f'Starting Gunicorn with {workers} workers on {host}:{port}')
            console.print(f'Logs will be written to: {log_file}')
        else:
            console.print(f'Starting Gunicorn with {workers} workers on {host}:{port}')

        # In daemon mode logs go to the persistent log file.
        # In foreground mode logs go to stdout/stderr so the operator can see
        # them directly in the terminal (gunicorn uses '-' to mean stdout).
        if daemon:
            access_log = str(log_file)
            error_log = str(log_file)
        else:
            access_log = '-'  # gunicorn: stdout
            error_log = '-'  # gunicorn: stderr

        cmd = [
            'gunicorn',
            '-k',
            'uvicorn.workers.UvicornWorker',
            '-w',
            str(workers),
            '-b',
            f'{host}:{port}',
            '--access-logfile',
            access_log,
            '--error-logfile',
            error_log,
            '--log-level',
            log_level,
            '--capture-output',  # Capture stdout/stderr from workers
        ]
        if preload:
            cmd.append('--preload')  # Load app in master process to save memory via copy-on-write

        if daemon:
            cmd.append('--daemon')
            # Gunicorn writes the master PID and cleans it on shutdown
            pf = pid_file_path(SERVICE)
            pf.parent.mkdir(parents=True, exist_ok=True)
            cmd.extend(['--pid', str(pf)])
            console.print('Server starting in daemon mode...')

        cmd.append('memex_core.server:app')

        # Replace current process with gunicorn
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvp('gunicorn', cmd)
        except FileNotFoundError:
            console.print(
                "[bold red]Error:[/bold red] 'gunicorn' not found. Ensure it is installed."
            )
            sys.exit(1)


@app.command()
def stop():
    """Stop the running Memex Core API server."""
    console.print('Stopping Memex Core API server...')
    stopped = graceful_stop(SERVICE)
    if stopped:
        console.print('[green]Server stopped.[/green]')
    else:
        console.print('No running server found.')


@app.command('status')
@async_command
async def status(ctx: typer.Context):
    """
    Check the status of the Memex Core API server.
    """
    config: MemexConfig = ctx.obj

    # Check PID file
    pid = read_pid(SERVICE)
    if not pid:
        console.print('[red]Server is NOT running (no process found).[/red]')
        raise typer.Exit(code=1)

    console.print(f'[green]Server is running.[/green] PID: {pid}')

    # 2. Check HTTP Health/Metrics
    server_url = config.server_url
    metrics_url = f'{server_url.rstrip("/")}/api/v1/metrics'

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(metrics_url)

            if resp.status_code == 200:
                console.print(f'[green]Metrics endpoint reachable:[/green] {metrics_url}')
                # Basic parsing of prometheus text format to find uptime or request count if available
                # For now, just confirming 200 OK is good enough for health check.
                console.print('[dim]Health check passed.[/dim]')
            else:
                console.print(
                    f'[yellow]Warning: Server returned {resp.status_code} on metrics endpoint.[/yellow]'
                )

    except httpx.RequestError as e:
        console.print(f'[red]Error connecting to server:[/red] {e}')
        console.print('[yellow]The process exists but is not responding to HTTP requests.[/yellow]')
