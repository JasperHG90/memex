"""Standalone installer for the Memex Hermes plugin.

Exposed as the ``memex-hermes`` console script (see ``[project.scripts]`` in
``pyproject.toml``). Intended to be used WITHOUT the Memex CLI:

    uv tool install 'memex-hermes-plugin @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/hermes-plugin'
    memex-hermes install
    memex-hermes status
    memex-hermes uninstall

The command walks the bundled ``memex_hermes_plugin.memex`` package to find
the plugin directory, then symlinks (dev) or copies (distribution) it into
``$HERMES_HOME/plugins/memex/`` so Hermes' real loader can discover it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal

import httpx
import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from memex_common.config import MemexConfig

logger = logging.getLogger('memex_hermes_plugin.install')
console = Console()

app = typer.Typer(
    name='memex-hermes',
    help='Manage the Memex plugin for Hermes Agent.',
    no_args_is_help=True,
)


def _default_hermes_home() -> Path:
    raw = os.environ.get('HERMES_HOME')
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.hermes'


def _plugin_source() -> Path:
    from memex_hermes_plugin import PLUGIN_DIR

    return PLUGIN_DIR


def _plugin_destination(hermes_home: Path) -> Path:
    return hermes_home / 'plugins' / 'memex'


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _set_provider_in_hermes_config(hermes_home: Path) -> bool:
    """Set ``memory.provider: memex`` in ``$HERMES_HOME/config.yaml``."""
    cfg_path = hermes_home / 'config.yaml'
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError:
            console.print(f'[yellow]Could not parse {cfg_path}; leaving it untouched.[/]')
            return False
    else:
        data = {}

    memory = data.setdefault('memory', {}) if isinstance(data, dict) else None
    if memory is None or not isinstance(memory, dict):
        return False
    if memory.get('provider') == 'memex':
        return False
    memory['provider'] = 'memex'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    return True


@app.command('install')
def install(
    hermes_home: Annotated[
        Path | None,
        typer.Option(
            '--hermes-home',
            help='Hermes home directory. Defaults to $HERMES_HOME or ~/.hermes.',
        ),
    ] = None,
    mode: Annotated[
        str,
        typer.Option(
            '--mode',
            help='symlink (default, for dev) or copy (for distribution).',
        ),
    ] = 'symlink',
    force: Annotated[
        bool,
        typer.Option('--force', '-f', help='Replace an existing installation.'),
    ] = False,
    set_provider: Annotated[
        bool,
        typer.Option(
            '--set-provider/--no-set-provider',
            help='Set memory.provider: memex in $HERMES_HOME/config.yaml.',
        ),
    ] = True,
) -> None:
    """Install the Memex plugin into ``$HERMES_HOME/plugins/memex/``."""
    mode_literal: Literal['symlink', 'copy']
    if mode == 'symlink':
        mode_literal = 'symlink'
    elif mode == 'copy':
        mode_literal = 'copy'
    else:
        raise typer.BadParameter("--mode must be 'symlink' or 'copy'")

    source = _plugin_source()
    if not source.exists():
        raise typer.Exit(f'Plugin source directory not found: {source}')

    home = (hermes_home or _default_hermes_home()).expanduser()
    dest = _plugin_destination(home)

    if dest.exists() or dest.is_symlink():
        if not force:
            console.print(f'[yellow]Plugin already installed at {dest}. Use --force to replace.[/]')
            raise typer.Exit(1)
        _remove_path(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)

    if mode_literal == 'symlink':
        dest.symlink_to(source.resolve(), target_is_directory=True)
        console.print(f'[green]✓[/] Symlinked {source} → {dest}')
    else:
        shutil.copytree(source, dest)
        console.print(f'[green]✓[/] Copied {source} → {dest}')

    if set_provider:
        if _set_provider_in_hermes_config(home):
            console.print(f'[green]✓[/] Set memory.provider: memex in {home / "config.yaml"}')
        else:
            console.print(f'memory.provider already set or unchanged in {home / "config.yaml"}')

    console.print(
        Panel.fit(
            'Next steps:\n'
            '  1. Start the Memex server:   [cyan]memex server start -d[/]\n'
            '  2. Configure the plugin:     [cyan]hermes memory setup[/]\n'
            '  3. Start a session:          [cyan]hermes chat[/]',
            title='Memex Hermes plugin installed',
        )
    )


@app.command('uninstall')
def uninstall(
    hermes_home: Annotated[
        Path | None,
        typer.Option('--hermes-home', help='Hermes home directory.'),
    ] = None,
    purge_config: Annotated[
        bool,
        typer.Option('--purge-config', help='Also remove $HERMES_HOME/memex/config.json.'),
    ] = False,
) -> None:
    """Remove the plugin directory from ``$HERMES_HOME/plugins/memex/``."""
    home = (hermes_home or _default_hermes_home()).expanduser()
    dest = _plugin_destination(home)

    if not (dest.exists() or dest.is_symlink()):
        console.print(f'[yellow]Plugin is not installed at {dest}.[/]')
        raise typer.Exit(0)

    _remove_path(dest)
    console.print(f'[green]✓[/] Removed {dest}')

    if purge_config:
        cfg_path = home / 'memex' / 'config.json'
        if cfg_path.exists():
            cfg_path.unlink()
            console.print(f'[green]✓[/] Removed {cfg_path}')


@app.command('status')
def status(
    hermes_home: Annotated[
        Path | None,
        typer.Option('--hermes-home', help='Hermes home directory.'),
    ] = None,
) -> None:
    """Show install state, config, active provider, and server reachability."""
    home = (hermes_home or _default_hermes_home()).expanduser()
    dest = _plugin_destination(home)
    cfg_path = home / 'memex' / 'config.json'
    hermes_cfg_path = home / 'config.yaml'

    table = Table(title='Memex plugin for Hermes', show_header=False, expand=False)
    table.add_column('Check')
    table.add_column('Result')

    table.add_row('Hermes home', str(home))

    if dest.is_symlink():
        table.add_row('Plugin dir', f'[green]symlink[/] → {dest.resolve()}')
    elif dest.is_dir():
        table.add_row('Plugin dir', f'[green]directory[/] {dest}')
    else:
        table.add_row('Plugin dir', '[red]not installed[/]')

    active = _active_provider(hermes_cfg_path)
    if active == 'memex':
        table.add_row('memory.provider', '[green]memex[/]')
    elif active:
        table.add_row('memory.provider', f'[yellow]{active}[/] (not memex)')
    else:
        table.add_row('memory.provider', '[yellow]unset[/]')

    if cfg_path.exists():
        table.add_row('Plugin config', f'[green]{cfg_path}[/]')
    else:
        table.add_row(
            'Plugin config',
            f'[yellow]missing[/] — will fall back to env + {_memex_config_hint()}',
        )

    server_url = _resolve_server_url(cfg_path)
    reachable, detail = _check_server(server_url)
    if reachable:
        table.add_row('Server', f'[green]reachable[/] {server_url}')
    else:
        table.add_row('Server', f'[red]unreachable[/] {server_url} ({detail})')

    console.print(table)


def _active_provider(hermes_cfg_path: Path) -> str | None:
    if not hermes_cfg_path.exists():
        return None
    try:
        data = yaml.safe_load(hermes_cfg_path.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    memory = data.get('memory') or {}
    if not isinstance(memory, dict):
        return None
    provider = memory.get('provider')
    return provider if isinstance(provider, str) else None


def _memex_config_hint() -> str:
    try:
        return str(Path(MemexConfig().server_url))
    except Exception:
        return 'MemexConfig()'


def _resolve_server_url(cfg_path: Path) -> str:
    if 'MEMEX_SERVER_URL' in os.environ:
        return os.environ['MEMEX_SERVER_URL']
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('server_url'), str):
                return data['server_url']
        except json.JSONDecodeError:
            pass
    try:
        return MemexConfig().server_url or 'http://127.0.0.1:8000'
    except Exception:
        return 'http://127.0.0.1:8000'


def _check_server(url: str) -> tuple[bool, str]:
    base = url.rstrip('/')
    try:
        response = httpx.get(f'{base}/health', timeout=3.0)
        if response.status_code == 200:
            return True, 'ok'
        return False, f'HTTP {response.status_code}'
    except httpx.HTTPError as e:
        return False, str(e)


if __name__ == '__main__':
    app()
