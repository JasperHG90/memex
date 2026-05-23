"""Textual TUI cockpit for the maintenance ledger.

Layout::

    ┌─────────────────────────┬────────────────────────────────────────────┐
    │  Pending proposals      │  Detail                                    │
    │  (sortable list)        │                                            │
    │  • LLM-flagged first    │  TARGET / EXPLANATION / AFFECTED / OPTIONS │
    │                         │                                            │
    │                         │  1) Recommended canned action              │
    │                         │  2) Alternative canned action              │
    │                         │  3) Dismiss                                │
    │                         │  [O] Other  [N] Note  [R] Reverse  [Q]uit │
    └─────────────────────────┴────────────────────────────────────────────┘
     j/k navigate · 1-9 pick · enter execute · ? help                  status

Mirrors the AskUserQuestion shape: numbered options, recommended star, free-form
Other as escape hatch, free-text note alongside.
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static, TextArea

from memex_cli.cockpit.controller import (
    CockpitController,
    CockpitOption,
    CockpitProposal,
    custom_action_options,
    options_for_rule,
)


def _extract_followup(result: dict[str, Any]) -> dict[str, Any] | None:
    """Pull `resolution.followup` from a resolve response, tolerating shapes.

    The new server returns it under `resolution.followup`. Old servers return
    a response with no `resolution` key at all (status-flip-only path).
    """
    resolution = result.get('resolution')
    if not isinstance(resolution, dict):
        return None
    followup = resolution.get('followup')
    if isinstance(followup, dict):
        return followup
    return None


class _ProposalQueueItem(ListItem):
    """Single row in the proposals queue list."""

    def __init__(self, proposal: CockpitProposal) -> None:
        self.proposal = proposal
        badge = 'LLM' if proposal.is_llm_source else 'rule'
        text = (
            f'[{badge}] {proposal.rule_name}\n'
            f'    {proposal.target_type} · {proposal.target_id[:8]}…'
        )
        super().__init__(Label(text))


class _OptionStaticGroup(Static):
    """Rendered menu of canned options for the highlighted proposal."""

    proposal: reactive[CockpitProposal | None] = reactive(None)
    options: reactive[list[CockpitOption]] = reactive(list)

    def render(self) -> str:
        if self.proposal is None:
            return '[dim]No proposal selected.[/dim]'
        lines = [
            '[bold]Pick a remediation — press the digit to commit:[/bold]',
            '[dim](Enter alone commits the ★ Recommended option.)[/dim]',
            '',
        ]
        for i, option in enumerate(self.options, start=1):
            star = ' [yellow]★ Recommended[/yellow]' if option.recommended else ''
            rev = '[green]reversible[/green]' if option.reversible else '[red]forward-only[/red]'
            lines.append(f'  [bold]{i})[/bold] {option.label}{star}')
            lines.append(f'      [dim]{option.summary}[/dim]')
            if option.effect:
                lines.append(f'      [dim]Effect: {option.effect}[/dim]')
            lines.append(f'      [dim]{rev}[/dim]')
            lines.append('')
        lines.append(
            '[dim]Other shortcuts (see footer): o=Other · n=Note · r=Reverse · ?=Help · q=Quit[/dim]'
        )
        return '\n'.join(lines)


class _DetailPanel(VerticalScroll):
    """Right-hand pane with the detail card + action menu."""

    DEFAULT_CSS = """
    _DetailPanel {
        padding: 1 2;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._header = Static(id='detail-header')
        self._body = Static(id='detail-body')
        self._options = _OptionStaticGroup(id='detail-options')
        self._effect = Static('', id='detail-effect')

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body
        yield self._options
        yield self._effect

    def show_proposal(self, proposal: CockpitProposal | None) -> None:
        if proposal is None:
            self._header.update('[dim]Queue empty — nothing to review.[/dim]')
            self._body.update('')
            self._options.proposal = None
            self._options.options = []
            self._effect.update('')
            return
        badge = '[yellow]LLM[/yellow]' if proposal.is_llm_source else '[blue]rule[/blue]'
        header_lines = [
            f'[bold]{proposal.rule_name}[/bold]  {badge}  '
            f'· {proposal.lint_type} / {proposal.target_type}',
            f'[dim]vault {proposal.vault_id or "(global)"} · age {proposal.created_at}[/dim]',
        ]
        if proposal.surprise_score is not None:
            header_lines.append(f'[dim]surprise={proposal.surprise_score:.2f}[/dim]')
        if proposal.polarity_contradiction_prob is not None:
            header_lines.append(
                f'[dim]P(contradiction)={proposal.polarity_contradiction_prob:.3f}[/dim]'
            )
        self._header.update('\n'.join(header_lines))

        body_lines: list[str] = []
        body_lines.append('[bold]TARGET[/bold]')
        body_lines.append(f'  id: {proposal.target_id}')
        if proposal.target_text:
            body_lines.append(f'  text: {proposal.target_text}')
        if proposal.explanation:
            body_lines.append('')
            body_lines.append('[bold]EXPLANATION[/bold]')
            body_lines.append(f'  {proposal.explanation}')
        if proposal.related_unit_ids:
            body_lines.append('')
            body_lines.append(f'[bold]RELATED[/bold]  {len(proposal.related_unit_ids)} units cited')
            for rid in proposal.related_unit_ids[:5]:
                body_lines.append(f'  · {rid}')
            if len(proposal.related_unit_ids) > 5:
                body_lines.append(f'  · … and {len(proposal.related_unit_ids) - 5} more')
        if proposal.suggested_action:
            body_lines.append('')
            body_lines.append(f'[dim]suggested: {proposal.suggested_action}[/dim]')
        self._body.update('\n'.join(body_lines))

        self._options.proposal = proposal
        self._options.options = options_for_rule(proposal.rule_name, proposal.target_type)
        self._effect.update('')

    def show_effect(self, message: str, *, error: bool = False) -> None:
        prefix = '[red]✗[/red]' if error else '[green]✓[/green]'
        self._effect.update(f'{prefix} {message}')

    @property
    def current_options(self) -> list[CockpitOption]:
        return list(self._options.options)


class NoteScreen(ModalScreen[str | None]):
    """Modal that collects a single free-form reviewer note."""

    BINDINGS = [
        Binding('escape', 'dismiss(None)', 'Cancel'),
        Binding('ctrl+s', 'submit', 'Save'),
    ]

    def __init__(self, *, prompt: str, initial: str = '') -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._prompt, id='note-prompt'),
            TextArea(self._initial, id='note-textarea'),
            Label('[dim]Ctrl+S to save · Esc to cancel[/dim]', id='note-help'),
            id='note-modal',
        )

    def action_submit(self) -> None:
        widget = self.query_one('#note-textarea', TextArea)
        text = (widget.text or '').strip() or None
        self.dismiss(text)


class OtherScreen(ModalScreen[tuple[str, str | None, str | None] | None]):
    """Modal that captures a free-form intent and maps it to a canned action.

    Returns a tuple `(action_id, reason, note)` on submit or None on cancel.
    `reason` is forwarded to the action as `params.reason` when the action
    accepts a `reason` field; `note` is stored at evidence.resolution.note.

    Flow:

    1. Modal opens with focus on the text input. Type your free-form
       description.
    2. Press Enter to advance — focus moves to the action list (text is
       captured). Use arrows to highlight, Enter again to commit.
    3. Esc at any time cancels without writing.
    """

    BINDINGS = [
        Binding('escape', 'dismiss(None)', 'Cancel'),
    ]

    def __init__(self, *, options: list[CockpitOption]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        rows: list[Any] = [
            Label(
                'Describe what you would like to happen, then press Enter to advance '
                'to the action picker.',
                id='other-prompt',
            ),
            Input(placeholder='Free-form description…', id='other-text'),
            Label('Map to one of (arrows + Enter):', id='other-map-label'),
        ]
        items: list[ListItem] = []
        for option in self._options:
            rev = 'reversible' if option.reversible else 'forward-only'
            items.append(ListItem(Label(f'{option.action_id} — {option.label} ({rev})')))
        items.append(ListItem(Label('cancel — abandon')))
        rows.append(ListView(*items, id='other-list'))
        rows.append(
            Label(
                '[dim]Enter on input → advance · Enter on list → commit · Esc cancels[/dim]',
                id='other-help',
            )
        )
        yield Vertical(*rows, id='other-modal')

    def on_mount(self) -> None:
        # Focus the input so the user can start typing immediately.
        self.query_one('#other-text', Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter on the text field moves focus into the action list rather
        # than dismissing the modal — the user almost certainly wants to
        # pick a mapping next, not commit blind.
        list_view = self.query_one('#other-list', ListView)
        if list_view.index is None and self._options:
            list_view.index = 0
        list_view.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_view = self.query_one('#other-list', ListView)
        idx = list_view.index
        if idx is None:
            return
        if idx >= len(self._options):
            self.dismiss(None)
            return
        option = self._options[idx]
        text_input = self.query_one('#other-text', Input)
        free_text = (text_input.value or '').strip() or None
        self.dismiss((option.action_id, free_text, free_text))


class HelpScreen(ModalScreen[None]):
    """Quick-reference modal for cockpit keybindings."""

    BINDINGS = [
        Binding('escape', 'dismiss(None)', 'Close'),
        Binding('q', 'dismiss(None)', 'Close'),
        Binding('question_mark', 'dismiss(None)', 'Close'),
    ]

    def compose(self) -> ComposeResult:
        body = (
            '[bold]Cockpit keybindings[/bold]\n'
            '\n'
            '  [bold]j / k / ↓ / ↑[/bold]   navigate the queue\n'
            '  [bold]1 – 9[/bold]           commit the numbered remediation\n'
            '  [bold]Enter[/bold]           commit the ★ Recommended remediation\n'
            '  [bold]o[/bold]               Other — free-form text mapped to a canned action\n'
            '  [bold]n[/bold]               stage a reviewer note (saved on next verdict)\n'
            '  [bold]r[/bold]               reverse a previously-resolved finding\n'
            '  [bold]F5[/bold]              refresh queue from the server\n'
            '  [bold]?[/bold]               this help\n'
            '  [bold]q[/bold]               quit\n'
            '\n'
            '[dim]Esc or q to close · ? to reopen.[/dim]'
        )
        yield Vertical(Static(body, id='help-body'), id='help-modal')


class ReverseScreen(ModalScreen[str | None]):
    """Modal that asks for a resolved-finding-id to reverse."""

    BINDINGS = [
        Binding('escape', 'dismiss(None)', 'Cancel'),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                'Reverse a previously-resolved proposal. Paste its finding_id:',
                id='reverse-prompt',
            ),
            Input(placeholder='UUID…', id='reverse-input'),
            Label('[dim]Enter to submit · Esc to cancel[/dim]', id='reverse-help'),
            id='reverse-modal',
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or '').strip()
        self.dismiss(text or None)


class _ProposalResolved(Message):
    """Sent after a successful verdict so the queue can refresh."""

    def __init__(self, finding_id: str, summary: str) -> None:
        super().__init__()
        self.finding_id = finding_id
        self.summary = summary


class ProposalCockpitApp(App):
    """Top-level Textual application."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #cockpit-root {
        height: 1fr;
    }
    #queue-pane {
        width: 36;
        border: solid $secondary;
    }
    #detail-pane {
        width: 1fr;
        border: solid $primary;
    }
    """

    BINDINGS = [
        Binding('q', 'quit', 'Quit'),
        Binding('question_mark', 'help', 'Help', show=True, key_display='?'),
        Binding('j', 'cursor_down', 'Down', show=False),
        Binding('k', 'cursor_up', 'Up', show=False),
        Binding('down', 'cursor_down', 'Down', show=False),
        Binding('up', 'cursor_up', 'Up', show=False),
        Binding('enter', 'pick_recommended', 'Recommended', priority=True),
        Binding('n', 'add_note', 'Note'),
        Binding('o', 'other_action', 'Other'),
        Binding('r', 'reverse', 'Reverse'),
        Binding('f5', 'refresh', 'Refresh'),
        # Priority on digit bindings so they fire even when the queue ListView
        # has focus. Without priority, focus-chain key handling could swallow
        # them in some Textual versions.
        Binding('1', 'pick(1)', 'Pick #', priority=True, show=True),
        Binding('2', 'pick(2)', 'Pick #', priority=True, show=False),
        Binding('3', 'pick(3)', 'Pick #', priority=True, show=False),
        Binding('4', 'pick(4)', 'Pick #', priority=True, show=False),
        Binding('5', 'pick(5)', 'Pick #', priority=True, show=False),
        Binding('6', 'pick(6)', 'Pick #', priority=True, show=False),
        Binding('7', 'pick(7)', 'Pick #', priority=True, show=False),
        Binding('8', 'pick(8)', 'Pick #', priority=True, show=False),
        Binding('9', 'pick(9)', 'Pick #', priority=True, show=False),
    ]

    proposals: reactive[list[CockpitProposal]] = reactive(list, init=False)

    def __init__(self, controller: CockpitController, *, limit: int = 50) -> None:
        super().__init__()
        self._controller = controller
        self._limit = limit
        self._queue = ListView(id='queue-list')
        self._detail = _DetailPanel()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Vertical(
                Label('[bold]Pending proposals[/bold]'),
                self._queue,
                id='queue-pane',
            ),
            Vertical(self._detail, id='detail-pane'),
            id='cockpit-root',
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.title = 'Memex Maintenance Cockpit'
        await self._refresh_queue()

    async def _refresh_queue(self) -> None:
        proposals = await self._controller.fetch_pending(limit=self._limit)
        self.proposals = proposals
        self._queue.clear()
        for proposal in proposals:
            self._queue.append(_ProposalQueueItem(proposal))
        if proposals:
            self._queue.index = 0
            self._detail.show_proposal(proposals[0])
        else:
            self._detail.show_proposal(None)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view is not self._queue:
            return
        item = event.item
        if isinstance(item, _ProposalQueueItem):
            self._detail.show_proposal(item.proposal)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter on a queue item commits the recommended option for that
        # proposal — equivalent to pressing the digit of the recommended row.
        if event.list_view is not self._queue:
            return
        self.action_pick_recommended()

    def _current_proposal(self) -> CockpitProposal | None:
        idx = self._queue.index
        if idx is None or idx < 0 or idx >= len(self.proposals):
            return None
        return self.proposals[idx]

    def _option(self, index: int) -> CockpitOption | None:
        options = self._detail.current_options
        if 1 <= index <= len(options):
            return options[index - 1]
        return None

    async def action_cursor_down(self) -> None:
        self._queue.action_cursor_down()

    async def action_cursor_up(self) -> None:
        self._queue.action_cursor_up()

    async def action_refresh(self) -> None:
        await self._refresh_queue()

    def action_pick(self, index_str: str) -> None:
        try:
            idx = int(index_str)
        except (ValueError, TypeError):
            return
        option = self._option(idx)
        proposal = self._current_proposal()
        if option is None or proposal is None:
            return
        # `push_screen_wait` requires a Textual worker — run the verdict
        # cycle as an exclusive worker so the modal `await` is legal.
        self._run_verdict_worker(proposal, option, None)

    def action_pick_recommended(self) -> None:
        """Pick the option marked `recommended=True` on the highlighted proposal.

        Falls back to the first non-dismiss option if no option is marked
        recommended (rare — the per-rule map should always declare one).
        Wired to Enter and surfaced in the footer so the cockpit has a clear
        default-commit affordance, not just numbered picks.
        """
        proposal = self._current_proposal()
        if proposal is None:
            return
        options = self._detail.current_options
        chosen: CockpitOption | None = None
        for option in options:
            if option.recommended:
                chosen = option
                break
        if chosen is None:
            for option in options:
                if option.action_id:  # skip the dismiss sentinel for default-Enter
                    chosen = option
                    break
        if chosen is None:
            return
        self._run_verdict_worker(proposal, chosen, None)

    def action_add_note(self) -> None:
        proposal = self._current_proposal()
        if proposal is None:
            return
        self._stage_note_worker(proposal)

    def action_other_action(self) -> None:
        proposal = self._current_proposal()
        if proposal is None:
            return
        catalogue = custom_action_options(proposal.target_type)
        if not catalogue:
            self._detail.show_effect(
                'No actions apply to this target_type. Use Dismiss or [N]ote.',
                error=True,
            )
            return
        self._other_action_worker(proposal, catalogue)

    def action_reverse(self) -> None:
        self._reverse_worker()

    # Worker-decorated helpers — push_screen_wait + controller calls live
    # here because `push_screen_wait(...)` requires `get_current_worker()`
    # to succeed. Action handlers fire-and-forget into these.

    def _run_verdict_worker(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        params: dict[str, Any] | None,
    ) -> None:
        self.run_worker(
            self._run_verdict_async(proposal, option, params),
            exclusive=True,
            name='run_verdict',
        )

    def _stage_note_worker(self, proposal: CockpitProposal) -> None:
        self.run_worker(self._stage_note_async(proposal), exclusive=True, name='stage_note')

    def _other_action_worker(
        self,
        proposal: CockpitProposal,
        catalogue: list[CockpitOption],
    ) -> None:
        self.run_worker(
            self._other_action_async(proposal, catalogue),
            exclusive=True,
            name='other_action',
        )

    def _reverse_worker(self) -> None:
        self.run_worker(self._reverse_async(), exclusive=True, name='reverse')

    async def _run_verdict_async(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        params: dict[str, Any] | None,
    ) -> None:
        # Offer the user a chance to attach a note (Esc to skip).
        note = await self.push_screen_wait(
            NoteScreen(prompt='Optional reviewer note (Ctrl+S to save · Esc to skip):'),
        )
        try:
            result = await self._controller.resolve(
                proposal,
                option,
                note=note,
                params=params,
            )
        except Exception as exc:  # noqa: BLE001
            self._detail.show_effect(f'Action failed: {exc}', error=True)
            return
        status = result.get('status', 'unknown')
        action_id = option.action_id or 'dismiss'
        # When the cockpit asks for a canned action but the server's response
        # has no `resolution.followup` block, the server is on the pre-cockpit
        # code path — it accepted the body, flipped status, and ignored the
        # action. Surface that loudly so the user doesn't believe a deprio /
        # archive ran when it actually didn't.
        if option.verb == 'resolve' and action_id != 'no_op':
            followup = _extract_followup(result)
            if followup is None or followup.get('action') != action_id:
                self._detail.show_effect(
                    f'{action_id} → {status}, BUT the server did NOT run the action '
                    '(no resolution.followup in response). The server is likely on a '
                    'pre-cockpit build; deploy this branch to make canned actions fire.',
                    error=True,
                )
                await self._refresh_queue()
                return
        self._detail.show_effect(f'{action_id} → {status}.  Refreshing queue…')
        await self._refresh_queue()

    async def _stage_note_async(self, proposal: CockpitProposal) -> None:
        note = await self.push_screen_wait(
            NoteScreen(prompt='Add a reviewer note (will be saved on next verdict):'),
        )
        if note:
            self._detail.show_effect(f'Note staged. Pick an option to commit. ({note!r})')

    async def _other_action_async(
        self,
        proposal: CockpitProposal,
        catalogue: list[CockpitOption],
    ) -> None:
        result = await self.push_screen_wait(OtherScreen(options=catalogue))
        if result is None:
            return
        action_id, reason, note = result
        if not action_id or action_id == 'cancel':
            return
        synthetic = CockpitOption(
            action_id=action_id,
            label=f'(Other) {action_id}',
            summary=reason or '',
            effect='',
            reversible=False,
            verb='resolve',
        )
        params: dict[str, Any] = {}
        if reason:
            params['reason'] = reason
        try:
            result_payload = await self._controller.resolve(
                proposal,
                synthetic,
                note=note,
                params=params or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._detail.show_effect(f'Action failed: {exc}', error=True)
            return
        status = result_payload.get('status', 'unknown')
        self._detail.show_effect(f'{action_id} → {status}.  Refreshing queue…')
        await self._refresh_queue()

    async def _reverse_async(self) -> None:
        finding_id = await self.push_screen_wait(ReverseScreen())
        if not finding_id:
            return
        try:
            result = await self._controller.reverse(finding_id)
        except Exception as exc:  # noqa: BLE001
            self._detail.show_effect(f'Reverse failed: {exc}', error=True)
            return
        summary = result.get('reversal') or result.get('effective_action') or 'ok'
        self._detail.show_effect(f'Reversed {finding_id[:8]}… ({summary}).')

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
