"""
Configuration Management Commands.
"""

import json
import pathlib as plb
import logging
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from pydantic import SecretStr
from platformdirs import user_config_dir
from box import Box

from memex_common.config import MemexConfig

logger = logging.getLogger('memex_cli.config')
console = Console()

app = typer.Typer(
    name='config',
    help='Manage Memex Configuration.',
    no_args_is_help=True,
)


@app.callback()
def config_callback():
    """
    Manage Memex Configuration.
    """
    pass


def mask_secrets(obj: Any) -> Any:
    """Recursively mask secrets in a dictionary or list."""
    if isinstance(obj, dict):
        return {k: mask_secrets(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [mask_secrets(i) for i in obj]
    elif isinstance(obj, SecretStr):
        return str(obj)  # Returns '**********'
    return obj


@app.command('show')
def show_config(
    ctx: typer.Context,
    format: str = typer.Option('yaml', '--format', '-f', help='Output format (yaml or json).'),
    compact: bool = typer.Option(
        False, '--compact', help='Hide default values (show only user overrides).'
    ),
):
    """
    Show the current configuration.
    Secrets are masked.
    """
    config: MemexConfig = ctx.obj
    if not config:
        console.print('[red]No configuration loaded.[/red]')
        raise typer.Exit(1)

    # Show vault summary with config source
    vault_source = ctx.meta.get('vault_source', 'unknown')
    source_labels = {
        'cli': '--vault flag',
        'local': f'local config ({ctx.meta.get("config_path", "?")})',
        'global': f'global config ({ctx.meta.get("config_path", "?")})',
        'default': 'default',
    }
    source_label = source_labels.get(vault_source, vault_source)
    vault_section = (
        '\n[bold]Vault Configuration:[/bold]\n'
        f'  Active (Writer): {config.server.active_vault}\n'
        f'  Attached (Read):  {config.server.attached_vaults or "[none]"}\n'
        f'  Source:           {source_label}\n'
    )
    console.print(vault_section)

    # Dump config
    # mode='python' preserves SecretStr objects so we can identify and mask them
    # exclude_unset=compact: If compact is True, we exclude defaults. If False (default), we show everything.
    config_dict = config.model_dump(mode='python', exclude_unset=compact)

    # Mask secrets and convert to JSON-safe dict
    masked_config = mask_secrets(config_dict)

    if format.lower() == 'json':
        output = json.dumps(masked_config, indent=2, default=str)
        console.print(Syntax(output, 'json'))
    else:
        # yaml.dump might struggle with some types if not strictly dicts/lists of primitives
        # mask_secrets should handle SecretStr -> str.
        # Other pydantic types (UUID, Path) might need str conversion.
        # Let's ensure everything is friendly.
        output = yaml.dump(masked_config, sort_keys=False)
        console.print(Syntax(output, 'yaml'))


@app.command()
def init(
    ctx: typer.Context,
    path: Annotated[
        plb.Path,
        typer.Option(
            '--path',
            '-p',
            help='Path to write the config file. Defaults to standard user config location.',
            exists=False,
            file_okay=True,
            dir_okay=False,
            writable=True,
        ),
    ] = plb.Path(user_config_dir('memex', appauthor=False)) / 'config.yaml',
):
    """
    Initialize a new Memex configuration.
    """
    # 1. Determine Target Path
    target_path = path if path else ctx.meta.get('config_path')

    typer.echo(f'Initializing Memex configuration at: {target_path}')

    # 2. Load Existing Data (from file + overrides)
    raw_config = ctx.meta.get('raw_config', {})
    # Use Box with dot notation and default_box=True for easy nesting
    config = Box(raw_config, box_dots=True, default_box=True)

    # Helper to check and prompt
    def ensure(key_path: str, prompt_text: str, default: Any = None, **kwargs) -> Any:
        # Attribute access with dots works in Box(box_dots=True)
        # But we need to check if it's "set".
        # An empty Box (result of default_box=True on missing key) is Falsey.
        val = config.get(key_path)

        # If val is an empty Box or None or empty string, we prompt
        if val and not isinstance(val, Box):
            return val

        # Prompt
        new_val = typer.prompt(prompt_text, default=default, **kwargs)
        config[key_path] = new_val
        return new_val

    # 3. Populate Fields
    # Ensure mandatory structures exist
    if not config.server.meta_store.type:
        config.server.meta_store.type = 'postgres'

    typer.echo('\n--- Postgres Metadata Store ---')
    ensure('server.meta_store.instance.host', 'Host', default='localhost')
    ensure('server.meta_store.instance.port', 'Port', default=5432, type=int)
    ensure('server.meta_store.instance.database', 'Database', default='memex')
    ensure('server.meta_store.instance.user', 'User', default='postgres')
    ensure('server.meta_store.instance.password', 'Password', hide_input=True)

    typer.echo('\n--- Fact Extraction ---')
    ensure(
        'server.memory.extraction.model.model',
        'Model Name',
        default='gemini/gemini-3-flash-preview',
    )

    # 4. Write to File
    try:
        # Ensure parent dir exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert Box back to dict for clean YAML dumping
        out_dict = config.to_dict()

        with target_path.open('w') as f:
            yaml.dump(out_dict, f, default_flow_style=False)

        typer.echo(f'\nConfiguration successfully written to {target_path}')
        if path:
            typer.echo(f"Run 'memex --config {target_path} ...' to use this configuration.")
        else:
            typer.echo("You can now run 'memex' commands.")
    except Exception as e:
        logger.error(f'Failed to write configuration: {e}')
        raise typer.Exit(code=1)
