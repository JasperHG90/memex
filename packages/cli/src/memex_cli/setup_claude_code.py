"""
Setup command for Claude Code integration.

Generates all configuration files needed to use Memex as a long-term
memory backend inside Claude Code sessions.
"""

import json
import logging
import pathlib as plb
import stat
from importlib import resources
from typing import Any, Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel

import memex_cli.templates as _templates
from memex_cli.utils import async_command
from memex_common.client import RemoteMemexAPI
from memex_common.config import MemexConfig

logger = logging.getLogger('memex_cli.setup')
console = Console()

app = typer.Typer(name='setup', help='Setup integrations.', no_args_is_help=True)


@app.callback()
def setup_callback():
    """Setup integrations."""


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

CLAUDE_MD_MARKER = '<!-- MEMEX CLAUDE CODE INTEGRATION -->'

_TEMPLATE_PKG = resources.files(_templates)

_HOOK_TEMPLATES: list[tuple[str, str]] = [
    ('on_session_start.sh', 'hooks/on_session_start.sh'),
    ('on_pre_compact.sh', 'hooks/on_pre_compact.sh'),
    ('on_session_end.sh', 'hooks/on_session_end.sh'),
]

_PROJECT_DIR_PLACEHOLDER = '__PROJECT_DIR__'


def _load_template(name: str) -> str:
    """Read a bundled template file from the ``memex_cli.templates`` package."""
    return (_TEMPLATE_PKG / name).read_text(encoding='utf-8')


def _load_hook_template(name: str, project_dir: plb.Path) -> str:
    """Load a hook template and replace the project-dir placeholder."""
    content = _load_template(f'hooks/{name}')
    return content.replace(_PROJECT_DIR_PLACEHOLDER, str(project_dir))


def _mcp_server_entry(vault: str) -> dict:
    """Build the MCP server config entry for Memex."""
    return {
        'type': 'stdio',
        'command': 'uv',
        'args': ['run', 'memex', 'mcp', 'run'],
        'env': {
            'MEMEX_SERVER__ACTIVE_VAULT': vault,
        },
    }


# ---------------------------------------------------------------------------
# Hooks helpers
# ---------------------------------------------------------------------------


def _build_hooks_config(
    project_dir: plb.Path,
    *,
    include_session_end: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Build the ``hooks`` section for ``settings.local.json``."""
    hooks_dir = project_dir / '.claude' / 'hooks' / 'memex'

    hooks: dict[str, list[dict[str, Any]]] = {
        'SessionStart': [
            {
                'matcher': 'startup',
                'hooks': [
                    {
                        'type': 'command',
                        'command': str(hooks_dir / 'on_session_start.sh'),
                    },
                ],
            },
        ],
        'PreCompact': [
            {
                'hooks': [
                    {
                        'type': 'command',
                        'command': str(hooks_dir / 'on_pre_compact.sh'),
                    },
                ],
            },
        ],
    }

    if include_session_end:
        hooks['SessionEnd'] = [
            {
                'hooks': [
                    {
                        'type': 'command',
                        'command': str(hooks_dir / 'on_session_end.sh'),
                    },
                ],
            },
        ]

    return hooks


def _merge_settings_local(
    settings_path: plb.Path,
    hooks_config: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Read existing ``settings.local.json``, merge the ``hooks`` key, and return."""
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            logger.warning('Malformed %s — creating fresh settings.', settings_path.name)
            data = {}
    else:
        data = {}

    data['hooks'] = hooks_config
    return data


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@app.command('claude-code')
@async_command
async def setup_claude_code(
    ctx: typer.Context,
    project_dir: Annotated[
        plb.Path,
        typer.Option(
            '--project-dir',
            '-p',
            help='Target project directory. Defaults to current working directory.',
        ),
    ] = plb.Path('.'),
    vault: Annotated[
        str | None,
        typer.Option(
            '--vault',
            '-v',
            help='Vault name to use. Defaults to the active vault from Memex config.',
        ),
    ] = None,
    server_url: Annotated[
        str | None,
        typer.Option(
            '--server-url',
            help='Memex server URL override for the health check.',
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            '--force',
            '-f',
            help='Overwrite existing skill files.',
        ),
    ] = False,
    no_claude_md: Annotated[
        bool,
        typer.Option(
            '--no-claude-md',
            help='Skip CLAUDE.md modifications.',
        ),
    ] = False,
    no_hooks: Annotated[
        bool,
        typer.Option(
            '--no-hooks',
            help='Skip hook generation.',
        ),
    ] = False,
    with_session_tracking: Annotated[
        bool,
        typer.Option(
            '--with-session-tracking',
            help='Include SessionEnd hook for session tracking.',
        ),
    ] = False,
):
    """
    Configure Claude Code to use Memex as its long-term memory backend.

    Generates MCP server config, slash-command skills (/remember, /recall),
    hooks for session lifecycle events, and optionally appends
    memory-integration instructions to CLAUDE.md.
    """
    project_dir = project_dir.resolve()
    config: MemexConfig | None = ctx.obj

    # --- Resolve vault name ---------------------------------------------------
    vault_name = vault or (config.server.active_vault if config else 'global')

    # --- Health check ---------------------------------------------------------
    check_url = server_url or (config.server_url if config else None)
    if check_url:
        console.print(f'[dim]Checking Memex server at {check_url} …[/dim]')
        try:
            async with httpx.AsyncClient(
                base_url=f'{check_url.rstrip("/")}/api/v1/', timeout=10.0
            ) as client:
                api = RemoteMemexAPI(client)
                vaults = await api.list_vaults()
                vault_names = [v.name for v in vaults]
                if vault_name not in vault_names:
                    console.print(
                        f'[yellow]Warning:[/yellow] Vault "{vault_name}" not found on server. '
                        f'Available: {", ".join(vault_names)}'
                    )
                else:
                    console.print(
                        f'[green]✓[/green] Server reachable, vault "{vault_name}" exists.'
                    )
        except Exception as e:
            console.print(
                f'[yellow]Warning:[/yellow] Could not reach Memex server ({e}). '
                'Continuing with file generation.'
            )
    else:
        console.print('[yellow]Warning:[/yellow] No server URL configured — skipping health check.')

    created: list[str] = []

    # --- 1. Skill files -------------------------------------------------------
    skills_dir = project_dir / '.claude' / 'skills'

    for skill_name, template_file in [
        ('remember', 'remember_skill.md'),
        ('recall', 'recall_skill.md'),
    ]:
        skill_content = _load_template(template_file)
        skill_path = skills_dir / skill_name / 'SKILL.md'
        if skill_path.exists() and not force:
            console.print(
                f'[dim]Skipping {skill_path.relative_to(project_dir)} (already exists, use --force to overwrite)[/dim]'
            )
        else:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill_content)
            created.append(str(skill_path.relative_to(project_dir)))
            console.print(f'[green]✓[/green] Created {skill_path.relative_to(project_dir)}')

    # --- 2. .mcp.json ---------------------------------------------------------
    mcp_path = project_dir / '.mcp.json'
    if mcp_path.exists():
        mcp_data = json.loads(mcp_path.read_text())
    else:
        mcp_data = {}

    servers = mcp_data.setdefault('mcpServers', {})
    servers['memex'] = _mcp_server_entry(vault_name)
    mcp_path.write_text(json.dumps(mcp_data, indent=2) + '\n')
    created.append(str(mcp_path.relative_to(project_dir)))
    console.print(f'[green]✓[/green] Updated {mcp_path.relative_to(project_dir)}')

    # --- 3. CLAUDE.md ---------------------------------------------------------
    if not no_claude_md:
        claude_md_section = _load_template('claude_md_section.md')
        claude_md_path = project_dir / 'CLAUDE.md'
        if claude_md_path.exists():
            existing = claude_md_path.read_text()
        else:
            existing = ''

        if CLAUDE_MD_MARKER in existing:
            console.print(
                '[dim]Skipping CLAUDE.md (Memex section already present, use --force to overwrite)[/dim]'
            )
            if force:
                # Remove old section and re-append
                before, _, _ = existing.partition(CLAUDE_MD_MARKER)
                existing = before.rstrip('\n')
                claude_md_path.write_text(existing + claude_md_section)
                created.append('CLAUDE.md')
                console.print('[green]✓[/green] Updated CLAUDE.md (replaced existing section)')
        else:
            claude_md_path.write_text(existing.rstrip('\n') + claude_md_section)
            created.append('CLAUDE.md')
            console.print('[green]✓[/green] Appended Memex integration section to CLAUDE.md')
    else:
        console.print('[dim]Skipping CLAUDE.md (--no-claude-md)[/dim]')

    # --- 4. Hooks -------------------------------------------------------------
    hooks_enabled = False
    if not no_hooks:
        hooks_dir = project_dir / '.claude' / 'hooks' / 'memex'
        state_dir = hooks_dir / '.state'
        state_dir.mkdir(parents=True, exist_ok=True)

        hook_scripts = [
            ('on_session_start.sh', True),
            ('on_pre_compact.sh', True),
            ('on_session_end.sh', with_session_tracking),
        ]

        for script_name, include in hook_scripts:
            if not include:
                continue
            script_path = hooks_dir / script_name
            if script_path.exists() and not force:
                console.print(
                    f'[dim]Skipping {script_path.relative_to(project_dir)} '
                    '(already exists, use --force to overwrite)[/dim]'
                )
            else:
                content = _load_hook_template(script_name, project_dir)
                script_path.write_text(content)
                script_path.chmod(
                    script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )
                created.append(str(script_path.relative_to(project_dir)))
                console.print(f'[green]✓[/green] Created {script_path.relative_to(project_dir)}')

        # Merge hooks config into settings.local.json
        hooks_config = _build_hooks_config(project_dir, include_session_end=with_session_tracking)
        settings_path = project_dir / '.claude' / 'settings.local.json'
        settings_data = _merge_settings_local(settings_path, hooks_config)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings_data, indent=2) + '\n')
        created.append(str(settings_path.relative_to(project_dir)))
        console.print(f'[green]✓[/green] Updated {settings_path.relative_to(project_dir)}')
        hooks_enabled = True
    else:
        console.print('[dim]Skipping hooks (--no-hooks)[/dim]')

    # --- Summary --------------------------------------------------------------
    if hooks_enabled:
        hook_names = 'SessionStart + PreCompact'
        if with_session_tracking:
            hook_names += ' + SessionEnd'
        hooks_status = f'Hooks: Enabled ({hook_names})'
    else:
        hooks_status = 'Hooks: Disabled'

    console.print()
    console.print(
        Panel(
            '\n'.join(
                [
                    f'[bold]Vault:[/bold]  {vault_name}',
                    f'[bold]Files:[/bold]  {len(created)} created/updated',
                    f'[bold]{hooks_status}[/bold]',
                    '',
                    'Next steps:',
                    '  1. Start (or restart) Claude Code in this project',
                    '  2. Try [bold cyan]/remember[/bold cyan] and [bold cyan]/recall[/bold cyan]',
                ]
            ),
            title='[bold green]Memex + Claude Code setup complete[/bold green]',
            expand=False,
        )
    )
