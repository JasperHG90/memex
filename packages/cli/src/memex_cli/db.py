"""Database migration commands (wraps Alembic)."""

import logging
import os
import pathlib as plb
from typing import Annotated

import typer
from rich.console import Console

from memex_common.config import MemexConfig

console = Console()
logger = logging.getLogger('memex_cli.db')

app = typer.Typer(name='database', help='Database schema migration commands.', no_args_is_help=True)


def _alembic_cfg(config: MemexConfig):
    """Build an Alembic Config and set the DB URL from the resolved MemexConfig."""
    from alembic.config import Config

    # Locate alembic.ini shipped with memex-core.
    import memex_core

    core_root = plb.Path(memex_core.__file__).resolve().parent.parent.parent
    ini_path = core_root / 'alembic.ini'

    if not ini_path.exists():
        console.print(f'[red]alembic.ini not found at {ini_path}[/red]')
        raise typer.Exit(1)

    cfg = Config(str(ini_path))
    # Ensure script_location is absolute so Alembic finds the versions dir.
    cfg.set_main_option('script_location', str(core_root / 'alembic'))

    # Pass the DB URL from the resolved config so env.py can find it via
    # get_database_url() (which reads MEMEX_DATABASE_URL first).
    db_url = config.server.meta_store.instance.connection_string
    os.environ['MEMEX_DATABASE_URL'] = db_url

    return cfg


@app.command()
def upgrade(
    ctx: typer.Context,
    revision: Annotated[
        str,
        typer.Argument(help='Target revision (default: head).'),
    ] = 'head',
) -> None:
    """Run pending migrations (up to *revision*)."""
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    console.print(f'Upgrading database to [bold]{revision}[/bold] ...')
    command.upgrade(cfg, revision)
    console.print('[green]Done.[/green]')


@app.command()
def downgrade(
    ctx: typer.Context,
    revision: Annotated[
        str,
        typer.Argument(help='Target revision (default: -1 = rollback one step).'),
    ] = '-1',
) -> None:
    """Roll back migrations (default: one step)."""
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    console.print(f'Downgrading database to [bold]{revision}[/bold] ...')
    command.downgrade(cfg, revision)
    console.print('[green]Done.[/green]')


@app.command()
def current(ctx: typer.Context) -> None:
    """Show the current migration revision."""
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    command.current(cfg, verbose=True)


@app.command()
def history(ctx: typer.Context) -> None:
    """Show full migration history."""
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    command.history(cfg, verbose=True)


@app.command()
def stamp(
    ctx: typer.Context,
    revision: Annotated[
        str,
        typer.Argument(help='Revision to stamp (e.g. head).'),
    ] = 'head',
) -> None:
    """Stamp the database with a revision without running migrations.

    Use this for existing databases that were created via create_all
    and already have the correct schema.
    """
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    console.print(f'Stamping database at [bold]{revision}[/bold] ...')
    command.stamp(cfg, revision)
    console.print('[green]Done.[/green]')


@app.command()
def revision(
    ctx: typer.Context,
    message: Annotated[
        str,
        typer.Option('--message', '-m', help='Migration message.'),
    ] = 'auto',
    autogenerate: Annotated[
        bool,
        typer.Option('--autogenerate/--no-autogenerate', help='Auto-detect schema changes.'),
    ] = True,
) -> None:
    """Generate a new migration script."""
    from alembic import command

    cfg = _alembic_cfg(ctx.obj)
    console.print(f'Generating migration: [bold]{message}[/bold] ...')
    command.revision(cfg, message=message, autogenerate=autogenerate)
    console.print('[green]Done.[/green]')
