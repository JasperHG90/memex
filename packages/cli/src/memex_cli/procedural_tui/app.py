"""Textual curation cockpit for the procedural plane (``memex procedure review``).

Browse-first, ported from the spike-7 design (`.claude/reports/spikes/
2026-06-07-experiential/spike7`). Two screens, cockpit idioms:

* **Browse / pins** — every procedure & strategy listed on open, each row
  carrying its per-context pin badges and Memory-Worth score. The right pane
  previews the ASSEMBLED briefing for the active context chain
  (``global → project:<id> → app:<consumer>``), with the 10-pin cap shown live.
  Pins are CONTEXT BINDINGS, not a boolean: the same entry can be pinned for
  ``global``, ``project:<id>``, and/or ``app:<agent>`` independently.
* **Versions** — the non-destructive version ledger for one entry: list +
  unified diff vs the active version + rollback (writes a new version).
* **Edit** — direct edit of trigger + body as a NEW version (changing the
  trigger re-embeds; identity is immutable; saving stamps the editor).

Keys: ``/`` filter · ``c`` cycle pin-context · ``p`` pin/unpin · ``v`` versions
· ``e`` edit · ``q`` quit.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any
from uuid import UUID

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static, TextArea

from memex_cli.procedural_tui.controller import (
    PIN_CAP_PER_CONTEXT,
    ProceduralCurationController,
    build_chain,
)

logger = logging.getLogger('memex.cli.procedural_tui')


def _ctx_color(ctx: str) -> str:
    if ctx == 'global':
        return 'green'
    return 'cyan' if ctx.startswith('project') else 'magenta'


def _anchor(entry: Any) -> str:
    verb = getattr(entry, 'verb', None) or ''
    context = getattr(entry, 'context', None) or ''
    return f'{verb}:{context}'.strip(':') or '—'


def _mw(entry: Any) -> str:
    """Memory-Worth display — success/total enactments, or ``—`` for strategies
    and never-used entries."""
    if getattr(entry, 'kind', '') == 'strategy':
        return '—'
    uses = getattr(entry, 'uses', 0) or 0
    if uses == 0:
        return '—'
    succ = getattr(entry, 'success_count', 0) or 0
    fail = getattr(entry, 'failure_count', 0) or 0
    return f'{succ}/{uses}' + (' ✓' if succ >= fail else '')


class _ConfirmRollback(ModalScreen[bool]):
    BINDINGS = [Binding('y', 'yes', 'yes'), Binding('n,escape', 'no', 'no')]

    def __init__(self, version: int) -> None:
        super().__init__()
        self._version = version

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f'[b]Roll back to v{self._version}?[/b]'),
            Label('[dim]Non-destructive: writes a NEW version with the old body.[/dim]'),
            Label('[yellow]y[/yellow] confirm   [yellow]n[/yellow] cancel'),
            id='confirm-box',
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class VersionScreen(Screen):
    """Version ledger + unified diff vs the active version (§18.8)."""

    BINDINGS = [
        Binding('r', 'rollback', 'rollback'),
        Binding('b,escape', 'back', 'back'),
        Binding('q', 'quit_app', 'quit'),
    ]

    def __init__(self, controller: ProceduralCurationController, entry_id: str, name: str) -> None:
        super().__init__()
        self._controller = controller
        self._entry_id = entry_id
        self._name = name
        self._versions: list[Any] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id='vleft'):
                yield Static('[b]versions[/b] [dim](newest first)[/dim]', markup=True)
                yield ListView(id='version-list')
            with VerticalScroll(id='vright'):
                yield Static(id='diff', markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f'procedure review — versions · {self._name}'
        self.sub_title = 'diff vs active · r rollback (non-destructive) · b back'
        await self._load()

    async def _load(self) -> None:
        try:
            self._versions = await self._controller.list_versions(UUID(self._entry_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the TUI
            self.query_one('#diff', Static).update(f'could not load versions: {exc}')
            return
        lv = self.query_one('#version-list', ListView)
        await lv.clear()
        for i, v in enumerate(self._versions):
            active = '  [green](active)[/green]' if i == 0 else ''
            await lv.append(
                ListItem(Label(f'v{v.version}{active}', markup=True), id=f'ver-{v.version}')
            )
        if len(self._versions) >= 2:
            self._show_diff(self._versions[1])
        elif self._versions:
            self.query_one('#diff', Static).update('Only one version — nothing to diff yet.')
        else:
            self.query_one('#diff', Static).update('No version history for this entry.')

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if not (event.item and event.item.id and self._versions):
            return
        ver = int(event.item.id.removeprefix('ver-'))
        match = next((v for v in self._versions if v.version == ver), None)
        if match is not None:
            self._show_diff(match)

    def _show_diff(self, older: Any) -> None:
        active = self._versions[0]
        out = Text()
        out.append(f'unified diff: v{older.version} → v{active.version} (active)\n\n', style='bold')
        if older.version == active.version:
            out.append('(this is the active version)\n', style='dim')
            self.query_one('#diff', Static).update(out)
            return

        def _render(v: Any) -> list[str]:
            title = getattr(v, 'title', '') or ''
            trigger = getattr(v, 'trigger', '') or ''
            body = getattr(v, 'body', '') or ''
            return f'title: {title}\ntrigger: {trigger}\n\n{body}'.splitlines()

        for line in difflib.unified_diff(
            _render(older), _render(active), f'v{older.version}', f'v{active.version}', lineterm=''
        ):
            if line.startswith('+') and not line.startswith('+++'):
                style = 'green'
            elif line.startswith('-') and not line.startswith('---'):
                style = 'red'
            elif line.startswith('@@'):
                style = 'cyan'
            else:
                style = 'dim'
            out.append(line + '\n', style=style)
        self.query_one('#diff', Static).update(out)

    async def action_rollback(self) -> None:
        lv = self.query_one('#version-list', ListView)
        item = lv.highlighted_child
        if item is None or item.id is None or not self._versions:
            return
        version = int(item.id.removeprefix('ver-'))

        async def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await self._controller.rollback(UUID(self._entry_id), version)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity='error')
                return
            self.notify(f'Rolled back: wrote a new version with the v{version} body.')
            await self._load()

        await self.app.push_screen(_ConfirmRollback(version), _done)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()


class EditScreen(Screen):
    """Direct edit = a NEW VERSION write, never in-place (§18.8 / spike 7b).

    Changing the trigger re-embeds on save. Identity (kind/scope/verb/context)
    is NOT editable here — renaming is a separate op. Saving stamps the editor,
    so a later re-derivation proposes instead of silently overwriting (§18.6).
    """

    BINDINGS = [
        Binding('ctrl+s', 'save', 'save (new version)', priority=True),
        Binding('escape', 'cancel', 'cancel', priority=True),
    ]

    def __init__(self, controller: ProceduralCurationController, entry: Any) -> None:
        super().__init__()
        self._controller = controller
        self._entry = entry
        self._orig_trigger = getattr(entry, 'trigger', '') or ''

    def compose(self) -> ComposeResult:
        e = self._entry
        yield Header(show_clock=False)
        with Vertical(id='edit-wrap'):
            yield Static(
                f'[b]identity[/b]  [yellow]{_anchor(e)}[/yellow] '
                f'[dim]({getattr(e, "scope", "")}) — not editable here; renaming moves '
                'the anchor (a separate op)[/dim]',
                markup=True,
            )
            yield Static(
                '[b]trigger[/b] [dim](when_to_use — changing it RE-EMBEDS on save)[/dim]',
                markup=True,
            )
            yield Input(value=self._orig_trigger, id='edit-trigger')
            yield Static(
                '[b]body[/b] [dim](steps — quantitative anchors are load-bearing; '
                'keep numbers verbatim)[/dim]',
                markup=True,
            )
            yield TextArea(getattr(e, 'body', '') or '', id='edit-body')
            yield Static('', id='edit-status', markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f'procedure review — edit · {getattr(self._entry, "title", "")}'
        self.sub_title = 'ctrl+s save as new version · escape cancel'

    async def action_save(self) -> None:
        body = self.query_one('#edit-body', TextArea).text.rstrip()
        trigger = self.query_one('#edit-trigger', Input).value.strip()
        try:
            updated = await self._controller.save_edit(
                UUID(str(self._entry.id)), trigger=trigger, body=body
            )
        except Exception as exc:  # noqa: BLE001
            self.query_one('#edit-status', Static).update(f'[red]save failed: {exc}[/red]')
            return
        bits = [
            f'[green]✓ saved v{getattr(updated, "version", "?")}[/green] '
            '[dim](old version kept — diff/rollback available)[/dim]'
        ]
        if trigger != self._orig_trigger:
            bits.append('[cyan]trigger changed → re-embedded[/cyan]')
        bits.append(
            '[magenta]origin: authored — re-derivation now proposes instead of '
            'overwriting[/magenta]'
        )
        self.query_one('#edit-status', Static).update('  ·  '.join(bits))

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ProceduralCurationApp(App[None]):
    """Browse-first curation cockpit over a :class:`ProceduralCurationController`."""

    CSS = """
    #left { width: 62%; }
    #right { width: 38%; border-left: solid $primary 30%; padding: 1 2; }
    #vleft { width: 22%; }
    #vright { border-left: solid $primary 30%; padding: 1 2; }
    #context-bar { padding: 0 1; height: 2; }
    #edit-wrap { padding: 1 2; }
    #edit-body { height: 12; margin: 0 0 1 0; }
    #edit-status { height: 2; }
    #confirm-box {
        align: center middle; background: $surface; border: thick $warning;
        padding: 2 4; width: 64; height: 9; margin: 6 12;
    }
    ListView { scrollbar-gutter: stable; }
    """

    BINDINGS = [
        Binding('slash', 'focus_filter', 'filter'),
        Binding('c', 'cycle_context', 'pin-context'),
        Binding('p', 'toggle_pin', 'pin/unpin'),
        Binding('v', 'versions', 'versions'),
        Binding('e', 'edit', 'edit'),
        Binding('q', 'quit', 'quit'),
    ]

    def __init__(
        self,
        controller: ProceduralCurationController,
        *,
        project_id: str | None = None,
        app_identity: str | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._chain = build_chain(project_id, app_identity)
        # Default the pin target to the most-specific context in the chain.
        self._active_context = self._chain[-1]
        self._filter = ''
        self._entries: list[Any] = []
        self._pins: dict[str, list[str]] = {}  # context_key -> [entry_id (str)]

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id='left'):
                yield Static(self._context_bar(), id='context-bar', markup=True)
                yield Input(placeholder='/ filter by name or trigger…', id='filter')
                yield ListView(id='entries')
            with VerticalScroll(id='right'):
                yield Static('', id='preview', markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = 'procedure review — briefing pins'
        self.sub_title = 'pins are CONTEXT BINDINGS: global / project:<id> / app:<agent>'
        await self._reload()

    # -------------------------------------------------------------------- data
    async def _reload(self) -> None:
        try:
            self._entries = await self._controller.list_entries()
            self._pins = {}
            for ctx in self._chain:
                pins = await self._controller.list_pins(ctx)
                self._pins[ctx] = [str(p.entry_id) for p in pins]
        except Exception as exc:  # noqa: BLE001 — surface as a toast, keep the TUI alive
            self.notify(str(exc), severity='error')
            return
        await self._refresh_list()

    def _assembled(self) -> list[str]:
        """Layered assembly: global → project → app, deduped, capped."""
        out: list[str] = []
        for ctx in self._chain:
            for eid in self._pins.get(ctx, []):
                if eid not in out:
                    out.append(eid)
        return out[:PIN_CAP_PER_CONTEXT]

    # ----------------------------------------------------------------- render
    def _context_bar(self) -> str:
        parts = []
        for ctx in self._chain:
            n = len(self._pins.get(ctx, []))
            cell = f'{ctx} ({n})'
            parts.append(
                f'[reverse] {cell} [/reverse]' if ctx == self._active_context else f' {cell} '
            )
        cycle = '   [dim](c to cycle)[/dim]' if len(self._chain) > 1 else ''
        return '[b]pin context:[/b] ' + ' '.join(parts) + cycle

    def _row(self, e: Any) -> str:
        eid = str(e.id)
        pinned_in = [c for c in self._chain if eid in self._pins.get(c, [])]
        glyph = '📌' if pinned_in else '  '
        badges = ''.join(f'[{_ctx_color(c)}]●[/]' for c in pinned_in)
        kbadge = '[yellow]P[/yellow]' if e.kind == 'procedure' else '[blue]S[/blue]'
        trig = (getattr(e, 'trigger', '') or '')[:60]
        status = '' if e.status == 'published' else f' [dim]({e.status})[/dim]'
        return (
            f'{glyph} {kbadge} [b]{e.title}[/b] [dim]({e.scope})[/dim]{status} '
            f'— {trig}  [dim]{_mw(e)}[/dim] {badges}'
        )

    async def _refresh_list(self) -> None:
        self.query_one('#context-bar', Static).update(self._context_bar())
        lv = self.query_one('#entries', ListView)
        await lv.clear()
        flt = self._filter.lower()
        for e in self._entries:
            hay = (e.title + ' ' + (getattr(e, 'trigger', '') or '')).lower()
            if flt and flt not in hay:
                continue
            await lv.append(ListItem(Label(self._row(e), markup=True), id=f'row-{e.id}'))
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        by_id = {str(e.id): e for e in self._entries}
        assembled = self._assembled()
        lines = [
            '[b]Assembled briefing preview[/b]',
            f'[dim]consumer chain: {" → ".join(self._chain)}[/dim]',
            '',
        ]
        for i, eid in enumerate(assembled, 1):
            e = by_id.get(eid)
            if e is None:
                continue
            src = next(c for c in self._chain if eid in self._pins.get(c, []))
            trig = (getattr(e, 'trigger', '') or '')[:42]
            color = _ctx_color(src)
            lines.append(
                f'{i:>2}. [b]{e.title}[/b] [dim]— {trig}…[/dim] [{color}]({src})[/{color}]'
            )
        if not assembled:
            lines.append(
                '[dim]No pins yet — highlight an entry on the left and press '
                '[b]p[/b] to pin it into the active context.[/dim]'
            )
        n = len(assembled)
        bar = '█' * n + '░' * (PIN_CAP_PER_CONTEXT - n)
        total = sum(len(v) for v in self._pins.values())
        cap_color = 'red' if n >= PIN_CAP_PER_CONTEXT else 'green'
        lines += [
            '',
            f'pins {n}/{PIN_CAP_PER_CONTEXT}  [{cap_color}]{bar}[/]',
            f'[dim]{total} bindings across contexts; remaining briefing slots fill '
            'MW-ranked.[/dim]',
        ]
        self.query_one('#preview', Static).update('\n'.join(lines))

    # ----------------------------------------------------------------- helpers
    def _highlighted_entry(self) -> Any | None:
        lv = self.query_one('#entries', ListView)
        item = lv.highlighted_child
        if item is None or item.id is None:
            return None
        eid = item.id.removeprefix('row-')
        return next((e for e in self._entries if str(e.id) == eid), None)

    # ----------------------------------------------------------------- actions
    def action_focus_filter(self) -> None:
        self.query_one('#filter', Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'filter':
            self._filter = event.value
            await self._refresh_list()

    async def action_cycle_context(self) -> None:
        i = self._chain.index(self._active_context)
        self._active_context = self._chain[(i + 1) % len(self._chain)]
        await self._refresh_list()

    async def action_toggle_pin(self) -> None:
        e = self._highlighted_entry()
        if e is None:
            self.notify('Highlight an entry first', severity='warning')
            return
        eid = str(e.id)
        ctx = self._active_context
        try:
            if eid in self._pins.get(ctx, []):
                await self._controller.unpin(UUID(eid), ctx)
                self.notify(f'Unpinned from {ctx}')
            else:
                await self._controller.pin(UUID(eid), ctx)
                self.notify(f'Pinned into {ctx}')
        except Exception as exc:  # noqa: BLE001 — incl. the 10/10 cap 422
            self.notify(str(exc), severity='error')
        await self._reload()

    async def action_versions(self) -> None:
        e = self._highlighted_entry()
        if e is None:
            self.notify('Highlight an entry first', severity='warning')
            return
        await self.push_screen(VersionScreen(self._controller, str(e.id), e.title))

    async def action_edit(self) -> None:
        e = self._highlighted_entry()
        if e is None:
            self.notify('Highlight an entry first', severity='warning')
            return
        try:
            full = await self._controller.get(UUID(str(e.id)))
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity='error')
            return
        await self.push_screen(EditScreen(self._controller, full))

    def action_quit(self) -> None:
        self.exit()


__all__ = ['ProceduralCurationApp']
