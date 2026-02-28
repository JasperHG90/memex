"""CLI command to report a bug via GitHub Issues."""

import importlib.metadata
import platform
import sys
import webbrowser
from urllib.parse import urlencode

import typer
from rich.console import Console

console = Console()

GITHUB_ISSUES_URL = 'https://github.com/JasperHG90/memex/issues/new'

app = typer.Typer(
    name='report-bug',
    help='Open a GitHub issue to report a bug.',
    invoke_without_command=True,
)


def _get_memex_version() -> str:
    """Return the installed memex-cli version, or 'unknown' if not found."""
    try:
        return importlib.metadata.version('memex-cli')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def _collect_system_info() -> str:
    """Collect system information for the bug report."""
    memex_version = _get_memex_version()
    python_version = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    os_info = f'{platform.system()} {platform.release()}'
    return f'- Memex version: {memex_version}\n- Python version: {python_version}\n- OS: {os_info}'


def _build_issue_url(system_info: str) -> str:
    """Build a GitHub issue URL with pre-filled template fields."""
    params = {
        'template': 'bug_report.yml',
        'labels': 'bug',
        'environment': system_info,
    }
    return f'{GITHUB_ISSUES_URL}?{urlencode(params)}'


@app.callback(invoke_without_command=True)
def report_bug() -> None:
    """Open a pre-filled GitHub issue to report a bug."""
    system_info = _collect_system_info()
    url = _build_issue_url(system_info)

    console.print('[bold]Opening GitHub issue page...[/bold]')
    console.print(f'[dim]System info:[/dim]\n{system_info}\n')

    if not webbrowser.open(url):
        console.print('[yellow]Could not open browser automatically.[/yellow]')
        console.print(f'Please open this URL manually:\n[cyan]{url}[/cyan]')
