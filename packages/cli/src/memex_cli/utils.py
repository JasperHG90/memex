"""Utility functions for the Memex CLI."""

import asyncio
import importlib
import json
import logging
from functools import wraps
from typing import Any, Callable, Coroutine, NoReturn, TypeVar, AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

import click
import httpx
import typer
from box import Box
from rich.console import Console
from typer.core import TyperGroup
from typer.main import get_command as typer_get_command

from memex_common.client import RemoteMemexAPI
from memex_common.config import MemexConfig

console = Console()
logger = logging.getLogger('memex_cli')

T = TypeVar('T')

# Lazy loaded subcommands map: command_name -> import_path:object_name
LAZY_SUBCOMMANDS: dict[str, str] = {
    'vault': 'memex_cli.vaults:app',
    'memory': 'memex_cli.memory:app',
    'entity': 'memex_cli.entities:app',
    'note': 'memex_cli.notes:app',
    'stats': 'memex_cli.stats:app',
    'config': 'memex_cli.config:app',
    'server': 'memex_cli.server:app',
    'database': 'memex_cli.db:app',
    'mcp': 'memex_cli.mcp:app',
    'dashboard': 'memex_cli.dashboard:app',
    'setup': 'memex_cli.setup_claude_code:app',
    'report-bug': 'memex_cli.report_bug:app',
}


@asynccontextmanager
async def get_api_context(
    config: MemexConfig,
) -> AsyncGenerator[RemoteMemexAPI, None]:
    """
    Context manager to initialize RemoteMemexAPI.
    """
    # Strict API Mode: Always use RemoteMemexAPI
    server_url = config.server_url
    base_url = f'{server_url.rstrip("/")}/api/v1/'

    async with httpx.AsyncClient(base_url=base_url, timeout=240.0) as client:
        yield RemoteMemexAPI(client)


class LazyTyperGroup(TyperGroup):
    """
    A TyperGroup that lazy loads subcommands to improve CLI startup time.
    Adapted from memex_core.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        """List available commands, including lazy-loaded ones."""
        base = super().list_commands(ctx)
        return list(sorted(base + list(LAZY_SUBCOMMANDS.keys())))

    def get_command(self, ctx: click.Context, cmd_name: str) -> Any | None:
        """Get a command, loading it if it's in the lazy map."""
        if cmd_name in LAZY_SUBCOMMANDS:
            return self._lazy_load(cmd_name)
        return super().get_command(ctx, cmd_name)

    def _lazy_load(self, cmd_name: str) -> Any:
        """Import and load the command object."""
        import_path = LAZY_SUBCOMMANDS[cmd_name]
        modname, app_obj_name = import_path.split(':')
        try:
            mod = importlib.import_module(modname)
            typer_app = getattr(mod, app_obj_name)
            return typer_get_command(typer_app)
        except (ImportError, AttributeError) as e:
            # Check if this is due to missing optional dependencies
            if cmd_name == 'server':
                console.print('[bold red]Error:[/bold red] Missing dependency for server.')
                console.print('Install with: [cyan]uv add memex-cli[server][/cyan]')
                raise typer.Exit(code=1)
            elif cmd_name == 'mcp':
                console.print('[bold red]Error:[/bold red] Missing dependency for MCP.')
                console.print('Install with: [cyan]uv add memex-cli[mcp][/cyan]')
                raise typer.Exit(code=1)
            elif cmd_name == 'dashboard':
                console.print('[bold red]Error:[/bold red] Dashboard requires Node.js.')
                console.print(
                    'Install Node.js and run: [cyan]cd packages/dashboard && npm install[/cyan]'
                )
                raise typer.Exit(code=1)

            logger.error(f"Failed to load command '{cmd_name}': {e}")
            raise typer.Exit(code=1) from e


def async_command(f: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """
    Decorator to run an async command function in the asyncio event loop.
    Use this for any CLI command that needs to await coroutines.
    """

    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def handle_api_error(e: Exception) -> NoReturn:
    """
    Handle exceptions from RemoteMemexAPI and provide helpful feedback.
    """
    if isinstance(e, httpx.HTTPStatusError):
        try:
            detail = e.response.json().get('detail', str(e))
        except Exception as exc:
            logger.debug('Failed to parse error response JSON: %s', exc)
            detail = str(e)

        if e.response.status_code == 404 and 'Vault' in detail:
            console.print(f'[bold red]Error: {detail}[/bold red]')
            console.print('[yellow]Suggestions:[/yellow]')
            console.print('  - List vaults: [bold cyan]memex vault list[/bold cyan]')
            console.print('  - Create vault: [bold cyan]memex vault create <name>[/bold cyan]')
        elif e.response.status_code == 404:
            console.print(f'[bold red]Resource not found: {detail}[/bold red]')
        elif e.response.status_code == 400:
            console.print(f'[bold red]Invalid request: {detail}[/bold red]')
        elif e.response.status_code == 409:
            console.print(f'[bold red]Conflict: {detail}[/bold red]')
        else:
            console.print(f'[bold red]Server Error ({e.response.status_code}): {detail}[/bold red]')
    else:
        console.print(f'[bold red]Error: {e}[/bold red]')

    raise typer.Exit(1)


def parse_uuid(value: str, label: str = 'ID') -> UUID:
    """
    Parse a string as a UUID, exiting with a user-friendly error on failure.

    This is CLI-specific validation: it provides fast local feedback without
    a network round-trip to the server.
    """
    try:
        return UUID(value)
    except ValueError:
        console.print(f'[red]Invalid UUID for {label}: {value}[/red]')
        raise typer.Exit(1)


def merge_overrides(config_data: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """
    Merge CLI overrides (e.g., ["meta_store.type=postgres"]) into the config dictionary.
    Supports dot notation for nested keys.
    """
    if not overrides:
        return config_data

    # Use Box for easy dot-notation access
    box = Box(config_data, box_dots=True, default_box=True)

    for override in overrides:
        if '=' not in override:
            logger.warning(f'Ignoring invalid override format: {override}. Expected key=value.')
            continue

        key, value = override.split('=', 1)
        key = key.strip()
        value = value.strip()

        # Attempt to parse value as JSON (for lists, dicts, numbers, booleans)
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            # Fallback to string if not valid JSON
            parsed_value = value

        # Set the value in the Box
        try:
            box[key] = parsed_value
        except Exception as e:
            logger.error(f"Failed to set override '{key}={value}': {e}")

    return box.to_dict()
