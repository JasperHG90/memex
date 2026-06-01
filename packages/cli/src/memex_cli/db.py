"""Database migration and maintenance commands (wraps Alembic)."""

import asyncio
import logging
import os
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from memex_common.config import MemexConfig

console = Console()
logger = logging.getLogger('memex_cli.db')

app = typer.Typer(
    name='database',
    help='Database schema migration and maintenance commands.',
    no_args_is_help=True,
)


def _check_core_installed():
    """Verify memex-core is available (required for database commands)."""
    try:
        import memex_core  # noqa: F401
    except ImportError:
        console.print("[bold red]Error:[/bold red] Missing dependency 'memex-core'.")
        console.print('Database commands require memex-core:')
        console.print("  [cyan]uv pip install 'memex-cli[server]'[/cyan]")
        raise typer.Exit(1)


def _alembic_cfg(config: MemexConfig):
    """Build an Alembic Config and set the DB URL from the resolved MemexConfig."""
    _check_core_installed()

    from memex_core.migration import _alembic_cfg as core_alembic_cfg

    cfg = core_alembic_cfg()

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


_BACKFILL_BATCH_SIZE = 500


async def _backfill_section_assets(config: MemexConfig, vault: str | None) -> tuple[int, int]:
    """Parse embedded image refs into ``nodes.assets`` for nodes missing them.

    Only empty-asset nodes are fetched (server-side filter), and they are
    streamed in keyset-paginated batches with a commit per batch — so memory
    stays bounded regardless of vault size. Keyset cursor is the node ``id``
    (not the asset filter), so image-less nodes that stay empty don't get
    re-fetched into an infinite loop. The parser is deterministic for fixed
    text, so the command is idempotent. Returns ``(nodes_scanned,
    nodes_updated)`` where *scanned* counts only the empty-asset candidates.
    """
    from sqlalchemy import func
    from sqlmodel import col, select

    from memex_core.memory.extraction.pipeline.asset_parser import extract_image_refs
    from memex_core.memory.sql_models import Node, Vault
    from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine

    engine = AsyncPostgresMetaStoreEngine(config=config.server.meta_store)
    await engine.connect(create_schema=False)

    scanned = 0
    updated = 0
    try:
        async with engine.session() as session:
            vault_id: UUID | None = None
            if vault is not None:
                try:
                    vault_id = UUID(vault)
                except ValueError:
                    row = (await session.exec(select(Vault).where(Vault.name == vault))).first()
                    if row is None:
                        raise ValueError(f'No vault named {vault!r}.')
                    vault_id = row.id

            last_id: UUID | None = None
            while True:
                stmt = select(Node).where(func.jsonb_array_length(col(Node.assets)) == 0)
                if vault_id is not None:
                    stmt = stmt.where(col(Node.vault_id) == vault_id)
                if last_id is not None:
                    stmt = stmt.where(col(Node.id) > last_id)
                stmt = stmt.order_by(col(Node.id)).limit(_BACKFILL_BATCH_SIZE)

                batch = (await session.exec(stmt)).all()
                if not batch:
                    break

                for node in batch:
                    scanned += 1
                    refs = extract_image_refs(node.text or '')
                    if refs:
                        node.assets = refs
                        session.add(node)
                        updated += 1

                last_id = batch[-1].id  # read before commit expires the row
                await session.commit()
    finally:
        await engine.close()

    return scanned, updated


@app.command(name='backfill-section-assets')
def backfill_section_assets(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault UUID or name. Omit to scan all vaults.'),
    ] = None,
) -> None:
    """Populate ``nodes.assets`` for existing nodes by parsing their text.

    Walks node rows whose ``assets`` is empty, extracts embedded image
    references (markdown / wiki-link / HTML ``<img>``), and stores them.
    Safe to re-run — nodes that already carry assets are skipped.
    """
    _check_core_installed()
    scope = f'vault [bold]{vault}[/bold]' if vault else '[bold]all vaults[/bold]'
    console.print(f'Backfilling section assets for {scope} ...')
    try:
        scanned, updated = asyncio.run(_backfill_section_assets(ctx.obj, vault))
    except ValueError as e:
        console.print(f'[bold red]Error:[/bold red] {e}')
        raise typer.Exit(1)
    console.print(f'[green]Done.[/green] Scanned {scanned} nodes, updated {updated}.')
