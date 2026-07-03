"""Utility functions for the Memex CLI."""

import asyncio
import importlib
import json
import logging
from enum import Enum
from functools import wraps
from typing import Annotated, Any, Callable, Coroutine, NoReturn, TypeVar, AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

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

VaultOption = Annotated[
    str | None,
    typer.Option('--vault', '-v', help='Vault name or UUID. Defaults to the active vault.'),
]

VaultFilterOption = Annotated[
    str | None,
    typer.Option('--vault', '-v', help='Filter to one vault by name or UUID. Omit for all scopes.'),
]

VaultScopeOption = Annotated[
    str | None,
    typer.Option('--vault', '-v', help='Vault scope (name or UUID). Omit for the global scope.'),
]


class ListFormat(str, Enum):
    """How a list/search command renders its results — one axis, four views.

    This replaces the old pile of overlapping boolean flags (``--minimal``,
    ``--compact``, ``--json``) which modelled a single choice as several
    independent switches. ``--slim`` is a SEPARATE axis (it controls how much
    is fetched, not how it is printed) and stays its own option.
    """

    table = 'table'  # rich table — human default
    ids = 'ids'  # one id per line — pipe-friendly
    line = 'line'  # one compact line per item
    json = 'json'  # structured JSON for downstream tooling


ListFormatOption = Annotated[
    ListFormat,
    typer.Option(
        '--format',
        '-f',
        help=(
            'Output view: table (default, human), ids (one id per line), '
            'line (one line per item), json (structured).'
        ),
    ),
]


def resolve_list_format(output_format: ListFormat, json_flag: bool) -> ListFormat:
    """Resolve the effective list format.

    ``--json`` is kept as a documented shorthand for ``--format json`` (it is
    the universal flag across every other command in the CLI); when set it
    wins over ``--format``.
    """
    return ListFormat.json if json_flag else output_format


# Lazy loaded subcommands map: command_name -> import_path:object_name
LAZY_SUBCOMMANDS: dict[str, str] = {
    'vault': 'memex_cli.vaults:app',
    'memory': 'memex_cli.memory:app',
    'entity': 'memex_cli.entities:app',
    'note': 'memex_cli.notes:app',
    'kv': 'memex_cli.kv:app',
    'lint': 'memex_cli.lint:app',
    'stats': 'memex_cli.stats:app',
    'config': 'memex_cli.config:app',
    'server': 'memex_cli.server:app',
    'database': 'memex_cli.db:app',
    'mcp': 'memex_cli.mcp:app',
    'briefing': 'memex_cli.session:app',
    'report-bug': 'memex_cli.report_bug:app',
    'diagnostics': 'memex_cli.diagnose:app',
    'agent-surface': 'memex_cli.agent_surface:app',
    'procedure': 'memex_cli.procedural:app',
    'case': 'memex_cli.procedural:case_app',
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

    headers = {}
    if config.api_key:
        headers['X-API-Key'] = config.api_key.get_secret_value()

    async with httpx.AsyncClient(base_url=base_url, timeout=240.0, headers=headers) as client:
        yield RemoteMemexAPI(client)


class LazyTyperGroup(TyperGroup):
    """
    A TyperGroup that lazy loads subcommands to improve CLI startup time.
    Adapted from memex_core.
    """

    def list_commands(self, ctx: Any) -> list[str]:
        """List available commands, including lazy-loaded ones."""
        base = super().list_commands(ctx)
        return list(sorted(base + list(LAZY_SUBCOMMANDS.keys())))

    def get_command(self, ctx: Any, cmd_name: str) -> Any | None:
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
                console.print("Install with: [cyan]uv pip install 'memex-cli\\[server]'[/cyan]")
                raise typer.Exit(code=1)
            elif cmd_name == 'mcp':
                console.print('[bold red]Error:[/bold red] Missing dependency for MCP.')
                console.print("Install with: [cyan]uv pip install 'memex-cli\\[mcp]'[/cyan]")
                raise typer.Exit(code=1)
            console.print(f"[bold red]Error:[/bold red] Failed to load command '{cmd_name}': {e}")
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


async def resolve_active_vault(api: RemoteMemexAPI, config: MemexConfig, vault: str | None) -> UUID:
    """Resolve a --vault value (or the configured active vault) to a vault UUID."""
    effective_vault = vault if vault is not None else config.write_vault
    return await api.resolve_vault_identifier(effective_vault)


def normalize_project_id(value: str | None) -> str | None:
    """Normalize a project-id argument for scoping surfaces.

    The canonical project id is the raw id (e.g. `github.com/owner/repo`).
    Callers sometimes pass the full scope form (`project:github.com/owner/repo`)
    because that is how it appears in pin contexts and KV namespaces. Strip
    a leading `project:` prefix so both forms resolve to the same value and
    avoid a doubled prefix like `project:project:...`.
    """
    if value is None:
        return None
    return value.removeprefix('project:') or None


def emit_json(data: Any) -> None:
    """Print data as formatted JSON, serializing non-JSON types via str()."""
    console.print_json(json.dumps(data, default=str))


def handle_api_error(e: Exception) -> NoReturn:
    """
    Handle exceptions from RemoteMemexAPI and provide helpful feedback.
    """
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        console.print('[bold red]Error:[/bold red] Could not reach the Memex server.')
        console.print(
            '  - Is it running? Start it with: [bold cyan]memex server start --daemon[/bold cyan]'
        )
        console.print('  - Check the configured URL: [bold cyan]memex config show[/bold cyan]')
        raise typer.Exit(1)
    if isinstance(e, (httpx.ReadTimeout, httpx.PoolTimeout)):
        console.print('[bold red]Error:[/bold red] The Memex server did not respond in time.')
        console.print('  - The operation may still be running on the server; retry shortly.')
        console.print('  - Check server health: [bold cyan]memex server status[/bold cyan]')
        raise typer.Exit(1)
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
        raise typer.Exit(2)


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
