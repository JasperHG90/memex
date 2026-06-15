"""Shared Textual theme + palette for the Memex curation cockpits.

One visual language across the lint Maintenance Cockpit and the procedure-review
curation cockpit — a deep-ink "brass instrument console": warm readout text on a
slate base, brass as the single bold signal (procedures / headings / focus), teal
secondary (strategy / structure), and instrument status colours
(green = published/healthy, amber = draft/pending, rust = deprecated/error).

Status is always carried by a LABEL as well as colour — never colour alone — so
the design stays legible without colour (the one accessibility rule that survives
the jump from web to terminal).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.theme import Theme

if TYPE_CHECKING:
    from textual.app import App

# --- palette (hex; reused in inline rich markup so badges match the chrome) ---
INK = '#0e1419'  # deep ink-slate base
SURFACE = '#161d26'  # panel surface
PANEL = '#1c2733'  # raised panel
READOUT = '#e6e0d4'  # warm instrument-readout off-white
BRASS = '#d9a23b'  # primary signal — procedures, headings, focus
TEAL = '#54b0a6'  # secondary — strategy / structure
COPPER = '#cf8e5c'  # accent — app scope, warm highlights
SAGE = '#7bbf6a'  # success / published
AMBER = '#e2a93f'  # warning / draft / pending
RUST = '#d4604c'  # error / deprecated

# semantic aliases the cockpits render with
PROC = BRASS
STRAT = TEAL
STATUS = {'published': SAGE, 'draft': AMBER, 'deprecated': RUST}
_CTX = {'global': SAGE, 'project': TEAL, 'app': COPPER}


MEMEX_CONSOLE = Theme(
    name='memex-console',
    primary=BRASS,
    secondary=TEAL,
    accent=COPPER,
    foreground=READOUT,
    background=INK,
    surface=SURFACE,
    panel=PANEL,
    success=SAGE,
    warning=AMBER,
    error=RUST,
    dark=True,
    variables={
        'block-cursor-background': BRASS,
        'block-cursor-foreground': INK,
        'block-cursor-text-style': 'b',
        'input-cursor-background': BRASS,
        'input-cursor-foreground': INK,
        'footer-key-foreground': BRASS,
        'footer-description-foreground': READOUT,
    },
)


def context_color(ctx: str) -> str:
    """Pin-context colour: global=sage, project=teal, app=copper."""
    if ctx == 'global':
        return _CTX['global']
    return _CTX['project'] if ctx.startswith('project') else _CTX['app']


def status_chip(status: str) -> str:
    """A label+colour status chip — never colour alone (e.g. ``(draft)``)."""
    color = STATUS.get(status, READOUT)
    return f'[{color}]({status})[/]'


def install(app: 'App') -> None:
    """Register + activate the console theme on a Textual app."""
    app.register_theme(MEMEX_CONSOLE)
    app.theme = 'memex-console'


__all__ = [
    'AMBER',
    'BRASS',
    'COPPER',
    'INK',
    'MEMEX_CONSOLE',
    'PROC',
    'READOUT',
    'RUST',
    'SAGE',
    'STRAT',
    'TEAL',
    'context_color',
    'install',
    'status_chip',
]
