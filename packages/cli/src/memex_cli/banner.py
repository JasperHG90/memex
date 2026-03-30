"""
Memex ASCII art banner with gradient coloring and status display.

Displays the MEMEX logo text styled with a coral-to-orange gradient,
followed by status lines showing config, server, and vault info.
"""

from __future__ import annotations

import importlib.metadata

from rich.console import Console
from rich.text import Text

# Pure-ASCII block letters (no Unicode box-drawing — renders cleanly everywhere)
BANNER_LINES: tuple[str, ...] = (
    ' __  __   ___   __  __   ___  __  __',
    '|  \\/  | | __| |  \\/  | | __| \\ \\/ /',
    '| |\\/| | | _|  | |\\/| | | _|   >  < ',
    '|_|  |_| |___| |_|  |_| |___| /_/\\_\\',
)

TAGLINE = 'Long-term memory for LLMs'

# Brand palette: Coral/Rose -> Orange/Amber (matches the Memex brain logo)
GRADIENT_STOPS: tuple[tuple[int, int, int], ...] = (
    (224, 90, 109),  # Coral / rose pink
    (235, 130, 80),  # Mid coral-orange
    (245, 166, 35),  # Orange / amber
)


def _get_version() -> str:
    """Return the installed memex-cli version, or 'dev' if unavailable."""
    try:
        return importlib.metadata.version('memex-cli')
    except importlib.metadata.PackageNotFoundError:
        return 'dev'


def _interpolate_rgb(
    c1: tuple[int, int, int],
    c2: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors at parameter t in [0, 1]."""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _gradient_color(
    t: float,
    stops: tuple[tuple[int, int, int], ...] = GRADIENT_STOPS,
) -> str:
    """Map a parameter t in [0, 1] to an RGB hex color across gradient stops."""
    t = max(0.0, min(1.0, t))
    n_segments = len(stops) - 1
    segment = min(int(t * n_segments), n_segments - 1)
    local_t = (t * n_segments) - segment
    r, g, b = _interpolate_rgb(stops[segment], stops[segment + 1], local_t)
    return f'#{r:02x}{g:02x}{b:02x}'


def build_banner(*, show_version: bool = True) -> Text:
    """Build a Rich Text object containing the banner with gradient styling."""
    max_width = max(len(line) for line in BANNER_LINES)

    text = Text()
    for line_idx, line in enumerate(BANNER_LINES):
        if line_idx > 0:
            text.append('\n')
        for char_idx, char in enumerate(line):
            if char == ' ':
                text.append(char)
            else:
                t = char_idx / max(max_width - 1, 1)
                color = _gradient_color(t)
                text.append(char, style=f'bold {color}')

    return text


def print_banner(
    console: Console,
    *,
    config_path: str | None = None,
    server_url: str | None = None,
    server_connected: bool | None = None,
    vault: str | None = None,
    vault_source: str | None = None,
) -> None:
    """Print the banner and optional status lines if the console is a terminal."""
    if not console.is_terminal:
        return

    banner = build_banner(show_version=False)
    console.print(banner)

    # Subtitle directly below the logo
    console.print(f'[dim]{TAGLINE}[/dim]')

    # Status block with border
    version = _get_version()
    status_lines: list[str] = []
    status_lines.append(f'  Version: [dim]v{version}[/dim]')
    if config_path:
        status_lines.append(f'  Config:  [dim]{config_path}[/dim]')
    if server_url is not None:
        if server_connected is True:
            status = f'[green]\u25cf[/green] {server_url}'
        elif server_connected is False:
            status = f'[red]\u25cf[/red] {server_url} [dim](offline)[/dim]'
        else:
            status = f'[dim]{server_url}[/dim]'
        status_lines.append(f'  Server:  {status}')
    if vault:
        source_hint = f' [dim]({vault_source})[/dim]' if vault_source else ''
        status_lines.append(f'  Vault:   [bold]{vault}[/bold]{source_hint}')

    from rich.panel import Panel

    console.print(Panel('\n'.join(status_lines), border_style='dim', expand=False))

    console.print()
