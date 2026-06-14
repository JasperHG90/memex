"""Textual app for procedural-plane curation.

Single screen, three panes:
  • left  — the active context's pin chain (position-ordered, with a
    N/10 capacity badge);
  • mid   — search results (entries to pin);
  • right — context-sensitive detail: briefing preview / version ledger
    / unified diff.

Bindings:
  /  focus search        p  pin selected hit into active context
  u  unpin selected pin  b  briefing preview for the assembled chain
  v  versions of the selected entry   d  diff last two versions
  r  rollback selected entry to a version (confirm; non-destructive)
  c  edit the active context key       q  quit
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from memex_cli.procedural_tui.controller import (
    ProceduralCurationController,
    build_chain,
    unified_version_diff,
    validate_context_key,
)

logger = logging.getLogger('memex.cli.procedural_tui')


class _ConfirmRollback(ModalScreen[bool]):
    """Yes/no modal for a rollback (non-destructive — re-applies a
    snapshot as a NEW version)."""

    BINDINGS = [
        Binding('y', 'confirm', 'Yes'),
        Binding('n,escape', 'cancel', 'No'),
    ]

    def __init__(self, entry_id: str, version: int) -> None:
        super().__init__()
        self._entry_id = entry_id
        self._version = version

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f'Roll back entry {self._entry_id[:8]} to v{self._version}?'),
            Label('This is non-destructive — it re-applies that snapshot as a NEW version.'),
            Label('[y] confirm   [n] cancel'),
            id='confirm-box',
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProceduralCurationApp(App[None]):
    """Curation TUI over a :class:`ProceduralCurationController`."""

    CSS = """
    #panes { height: 1fr; }
    #pins, #results, #detail { width: 1fr; border: round $primary; padding: 0 1; }
    #search { dock: top; }
    .pane-title { text-style: bold; color: $accent; }
    .full { color: $error; }
    """

    BINDINGS = [
        Binding('slash', 'focus_search', 'Search'),
        Binding('p', 'pin', 'Pin'),
        Binding('u', 'unpin', 'Unpin'),
        Binding('b', 'briefing', 'Briefing'),
        Binding('v', 'versions', 'Versions'),
        Binding('d', 'diff', 'Diff'),
        Binding('r', 'rollback', 'Rollback'),
        Binding('c', 'edit_context', 'Context'),
        Binding('q', 'quit', 'Quit'),
    ]

    context_key: reactive[str] = reactive('global')

    def __init__(
        self,
        controller: ProceduralCurationController,
        *,
        project_id: str | None = None,
        app_identity: str | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._project_id = project_id
        self._app_identity = app_identity
        self._results: list[Any] = []
        self._pins: list[Any] = []
        self._versions: list[Any] = []

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder='Search procedures/strategies to pin…', id='search')
        with Horizontal(id='panes'):
            with VerticalScroll(id='pins'):
                yield Static('Pin chain', classes='pane-title', id='pins-title')
                yield ListView(id='pins-list')
            with VerticalScroll(id='results'):
                yield Static('Search results', classes='pane-title')
                yield ListView(id='results-list')
            with VerticalScroll(id='detail'):
                yield Static('Detail', classes='pane-title', id='detail-title')
                yield Static('', id='detail-body')
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh_pins()

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    async def _refresh_pins(self) -> None:
        try:
            state = await self._controller.context_state(self.context_key)
            self._pins = await self._controller.list_pins(self.context_key)
        except Exception as exc:  # noqa: BLE001 — surface as a toast
            self.notify(str(exc), severity='error')
            return
        title = self.query_one('#pins-title', Static)
        cap = state.capacity_label
        title.update(f'Pin chain — {self.context_key}  [{cap}]')
        title.set_class(state.is_full, 'full')
        lv = self.query_one('#pins-list', ListView)
        await lv.clear()
        for pin in self._pins:
            await lv.append(
                ListItem(Label(f'[{pin.position}] {str(pin.entry_id)[:8]}  {pin.pinned_by or ""}'))
            )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one('#search', Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != 'search':
            return
        try:
            self._results = await self._controller.search(event.value)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity='error')
            return
        lv = self.query_one('#results-list', ListView)
        await lv.clear()
        for entry in self._results:
            anchor = f'{entry.verb or ""}:{entry.context or ""}'.strip(':')
            await lv.append(ListItem(Label(f'{entry.kind}/{entry.scope}/{anchor}  {entry.title}')))
        self.notify(f'{len(self._results)} result(s)')

    def _selected_result(self) -> Any | None:
        lv = self.query_one('#results-list', ListView)
        idx = lv.index
        if idx is None or idx >= len(self._results):
            return None
        return self._results[idx]

    def _selected_pin(self) -> Any | None:
        lv = self.query_one('#pins-list', ListView)
        idx = lv.index
        if idx is None or idx >= len(self._pins):
            return None
        return self._pins[idx]

    async def action_pin(self) -> None:
        entry = self._selected_result()
        if entry is None:
            self.notify('Select a search result first', severity='warning')
            return
        try:
            await self._controller.pin(UUID(str(entry.id)), self.context_key)
        except Exception as exc:  # noqa: BLE001 — incl. the 10/10 cap 422
            self.notify(str(exc), severity='error')
            return
        self.notify(f'Pinned into {self.context_key}')
        await self._refresh_pins()

    async def action_unpin(self) -> None:
        pin = self._selected_pin()
        if pin is None:
            self.notify('Select a pin first', severity='warning')
            return
        try:
            removed = await self._controller.unpin(UUID(str(pin.entry_id)), self.context_key)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity='error')
            return
        self.notify('Unpinned' if removed else 'No pin removed')
        await self._refresh_pins()

    async def action_briefing(self) -> None:
        chain = build_chain(self._project_id, self._app_identity)
        try:
            cards = await self._controller.briefing_preview(chain)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity='error')
            return
        lines = [f'Briefing chain: {" → ".join(chain)}', '']
        for card in getattr(cards, 'cards', []):
            e = card.entry
            lines.append(f'• [{card.context_key}] {e.kind}/{e.title}')
            lines.append(f'    {e.summary}')
        self.query_one('#detail-title', Static).update('Briefing preview')
        self.query_one('#detail-body', Static).update('\n'.join(lines) or '(no pinned cards)')

    async def action_versions(self) -> None:
        entry = self._selected_result() or self._selected_pin()
        eid = getattr(entry, 'id', None) or getattr(entry, 'entry_id', None)
        if eid is None:
            self.notify('Select an entry first', severity='warning')
            return
        try:
            self._versions = await self._controller.list_versions(UUID(str(eid)))
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity='error')
            return
        lines = [f'Versions of {str(eid)[:8]} (newest first):', '']
        for v in self._versions:
            reason = f'  ({v.edit_reason})' if v.edit_reason else ''
            lines.append(f'v{v.version}  {v.created_at}{reason}')
        self.query_one('#detail-title', Static).update('Version ledger')
        self.query_one('#detail-body', Static).update('\n'.join(lines) or '(no versions yet)')

    async def action_diff(self) -> None:
        if len(self._versions) < 2:
            self.notify('Load versions (v) on an entry with ≥2 versions first', severity='warning')
            return
        # _versions is newest-first; diff the two newest.
        newer, older = self._versions[0], self._versions[1]
        diff = unified_version_diff(older, newer)
        self.query_one('#detail-title', Static).update(f'Diff v{older.version}→v{newer.version}')
        self.query_one('#detail-body', Static).update(diff or '(identical)')

    async def action_rollback(self) -> None:
        if not self._versions:
            self.notify('Load versions (v) first, then rollback', severity='warning')
            return
        target = self._versions[-1]  # oldest in the loaded window
        eid = target.entry_id

        async def _after(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await self._controller.rollback(UUID(str(eid)), target.version)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity='error')
                return
            self.notify(f'Rolled back to v{target.version} (as a new version)')
            await self.action_versions()

        await self.push_screen(_ConfirmRollback(str(eid), target.version), _after)

    async def action_edit_context(self) -> None:
        inp = self.query_one('#search', Input)
        inp.placeholder = 'Type a context key (global | project:<id> | app:<id>) then Enter'
        inp.value = self.context_key
        inp.focus()
        # A submitted value that validates becomes the active context.
        self._context_edit_mode = True

    async def on_input_changed(self, event: Input.Changed) -> None:
        # Live-validate a context edit without committing.
        if getattr(self, '_context_edit_mode', False) and event.input.id == 'search':
            try:
                validate_context_key(event.value)
            except ValueError:
                pass


__all__ = ['ProceduralCurationApp']
