"""
Memex ASCII art banner with gradient coloring.

Displays the MEMEX logo text styled with a purple-to-cyan gradient using Rich.
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

# Multi-stop gradient: Purple -> Blurple -> Cyan
GRADIENT_STOPS: tuple[tuple[int, int, int], ...] = (
    (138, 43, 226),  # Purple
    (88, 101, 242),  # Blurple
    (0, 191, 255),  # Cyan
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
    version = _get_version() if show_version else None

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

    # Tagline
    text.append('\n')
    tagline = f'{TAGLINE}  v{version}' if version else TAGLINE
    # Center tagline under the banner
    padding = max((max_width - len(tagline)) // 2, 0)
    text.append(' ' * padding)
    text.append(tagline, style='dim')

    return text


def print_banner(console: Console) -> None:
    """Print the banner if the console is attached to a terminal."""
    if not console.is_terminal:
        return
    banner = build_banner()
    console.print(banner)
    console.print()
