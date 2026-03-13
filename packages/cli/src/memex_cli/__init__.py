"""
Memex CLI Entry Point.
"""

import warnings
import logging
import pathlib as plb
from typing import Annotated
import os

import typer
from rich.logging import RichHandler
from rich.console import Console
from platformdirs import user_config_dir, user_log_dir

from memex_cli.utils import LazyTyperGroup, merge_overrides
from memex_common.config import (
    parse_memex_config,
    MemexConfig,
    GlobalYamlConfigSettingsSource,
    LocalYamlConfigSettingsSource,
    deep_merge,
)

from .__about__ import __version__ as __version__

# Suppress Pydantic serializer warnings
warnings.filterwarnings('ignore', message='Pydantic serializer warnings')

logger = logging.getLogger('memex')

# Default Global Config Path
DEFAULT_CONFIG_DIR = plb.Path(user_config_dir('memex', appauthor=False))
DEFAULT_GLOBAL_CONFIG = DEFAULT_CONFIG_DIR / 'config.yaml'

app = typer.Typer(
    cls=LazyTyperGroup,
    help='Memex: Long-term memory and knowledge management for LLMs.',
    invoke_without_command=True,
    context_settings={'help_option_names': ['-h', '--help']},
)


def setup_logging(ctx: typer.Context, debug: bool, log_file: plb.Path):
    """
    Configure logging.
    - Always log to file.
    - Log to console (stderr) UNLESS the subcommand is 'mcp'.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if debug else logging.WARNING

    handlers: list[logging.Handler] = [logging.FileHandler(log_file)]

    # Only add console handler if NOT running MCP
    # MCP requires pure stdout for JSON-RPC
    if ctx.invoked_subcommand != 'mcp':
        handlers.append(
            RichHandler(
                console=Console(stderr=True),  # Use stderr to keep stdout clean
                rich_tracebacks=True,
                markup=True,
            )
        )

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='[%X]',
        handlers=handlers,
        force=True,  # Ensure we override any existing config
    )

    # If debug is on, ensure our logger is set
    if debug:
        logger.setLevel(logging.DEBUG)


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Annotated[
        plb.Path | None,
        typer.Option(
            '--config',
            '-c',
            help=f'Path to the configuration file. Defaults to {DEFAULT_GLOBAL_CONFIG}, then looks for local config.',
            exists=False,  # We handle existence check manually
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            envvar='MEMEX_CONFIG_PATH',
        ),
    ] = None,
    set_vars: Annotated[
        list[str] | None,
        typer.Option(
            '--set',
            '-s',
            help='Override config values (e.g., --set meta_store.type=postgres).',
        ),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option(
            '--vault',
            '-v',
            help='Override the active vault for this command.',
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            '--debug',
            '-d',
            help='Enable debug logging.',
        ),
    ] = False,
):
    """
    Memex CLI - Your personal knowledge vault.
    """
    # 1. Set Config Path Env Var if provided
    if config_path:
        os.environ['MEMEX_CONFIG_PATH'] = str(config_path.resolve())

    # 2. Load Raw Configs (using common sources to reuse logic)
    global_source = GlobalYamlConfigSettingsSource(MemexConfig)
    local_source = LocalYamlConfigSettingsSource(MemexConfig)

    global_data = global_source()
    local_data = local_source()

    # 3. Parse Overrides
    overrides_data = {}
    if set_vars:
        overrides_data = merge_overrides({}, set_vars)

    # 4. Merge: Global -> Local -> Overrides
    config_data = deep_merge(global_data, local_data)
    config_data = deep_merge(config_data, overrides_data)

    # Store raw config (with overrides) in meta for init to use as defaults
    ctx.meta['raw_config'] = config_data
    ctx.meta['config_path'] = config_path or DEFAULT_GLOBAL_CONFIG

    # 5. Parse and Validate Config
    try:
        # Pydantic Settings will also load from Environment Variables (MEMEX_*)
        config = parse_memex_config(config_data)

        if vault:
            config.vault.active = vault
            logger.info('Active vault overridden via --vault: "%s"', vault)

        # Track which config source set active vault
        _local_vault = (local_data.get('vault', {}) or {}).get('active') or (
            local_data.get('server', {}) or {}
        ).get('default_active_vault')
        _global_vault = (global_data.get('vault', {}) or {}).get('active') or (
            global_data.get('server', {}) or {}
        ).get('default_active_vault')
        if vault:
            vault_source = 'cli'
        elif _local_vault:
            vault_source = 'local'
            logger.info(
                'Active vault "%s" set by local config (overrides global).',
                config.write_vault,
            )
        elif _global_vault:
            vault_source = 'global'
        else:
            vault_source = 'default'
        ctx.meta['vault_source'] = vault_source

        ctx.obj = config

        # 6. Setup Logging (now that we have the config)
        setup_logging(ctx, debug, plb.Path(config.server.logging.log_file))

        if debug:
            logger.debug(f'Logging configured. Log file: {config.server.logging.log_file}')
            if global_data:
                logger.debug(f'Loaded global config from {DEFAULT_GLOBAL_CONFIG}')
            if local_data:
                logger.debug('Loaded local config')
            logger.debug(f'Configuration valid. Root: {config.server.file_store.root}')

    except Exception as e:
        # DEBUG
        print(f'DEBUG EXCEPTION: {e}')

        # If the subcommand is 'init' or 'help', we can proceed without a valid config
        # But we still need basic logging to report errors if we crash later
        if ctx.invoked_subcommand in ['init', 'report-bug']:
            # Fallback logging
            setup_logging(
                ctx, debug, plb.Path(user_log_dir('memex', appauthor=False)) / 'memex.log'
            )
            ctx.obj = None
            return

        # Setup fallback logging to report the critical error
        setup_logging(ctx, debug, plb.Path(user_log_dir('memex', appauthor=False)) / 'memex.log')
        logger.critical(f'Configuration Error: {e}')

        # Heuristic check for missing config
        if not global_data and not local_data and not overrides_data:
            logger.info("Run 'memex init' to generate a valid configuration.")

        raise typer.Exit(code=1)

    # Show banner + help for bare `memex` invocation (no subcommand)
    if ctx.invoked_subcommand is None:
        from memex_cli.banner import print_banner

        print_banner(Console(stderr=True))
        import click

        click.echo(ctx.get_help())
        raise typer.Exit(0)


if __name__ == '__main__':
    app()
