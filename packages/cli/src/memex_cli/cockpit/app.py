"""Textual TUI cockpit for the maintenance ledger.

Three-mode UX: LIST -> REVIEW -> NOTE.

LIST  — browse the queue; Space multi-selects; Enter opens REVIEW.
REVIEW — pick an action from the action list; Enter confirms / opens NOTE.
NOTE  — inline TextArea for an optional reviewer note; Enter submits.
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Rule,
    Static,
    TextArea,
)

from memex_cli.cockpit.controller import (
    CockpitController,
    CockpitOption,
    CockpitProposal,
    options_for_contradiction,
    options_for_rule,
)


def _extract_followup(result: dict[str, Any]) -> dict[str, Any] | None:
    resolution = result.get('resolution')
    if not isinstance(resolution, dict):
        return None
    followup = resolution.get('followup')
    if isinstance(followup, dict):
        return followup
    return None


# ---------------------------------------------------------------------------
# Queue item
# ---------------------------------------------------------------------------


class _ProposalQueueItem(ListItem):
    """Single row in the proposals queue list, with a multi-select checkbox."""

    def __init__(self, proposal: CockpitProposal) -> None:
        self.proposal = proposal
        self.checked: bool = False
        badge = 'LLM' if proposal.is_llm_source else 'rule'
        self._text_base = (
            f'[{badge}] {proposal.rule_name}\n'
            f'    {proposal.target_type} · {proposal.target_id[:8]}…'
        )
        super().__init__(Label(self._render_label()))

    def _render_label(self) -> str:
        mark = '[bold green]✓[/bold green] ' if self.checked else '  '
        return f'{mark}{self._text_base}'

    def toggle(self) -> None:
        self.checked = not self.checked
        self.query_one(Label).update(self._render_label())


# ---------------------------------------------------------------------------
# Action-list item
# ---------------------------------------------------------------------------


class _ActionListItem(ListItem):
    def __init__(self, option: CockpitOption, *, highlighted: bool = False) -> None:
        self.option = option
        super().__init__(Label(self._render_label(highlighted)))

    def _render_label(self, highlighted: bool = False) -> str:
        cursor = '[bold]▸[/bold] ' if highlighted else '  '
        star = ' [yellow]★[/yellow]' if self.option.recommended else '  '
        rev_glyph = '[dim]↩[/dim]' if self.option.reversible else '[dim]⏎[/dim]'
        label = f'[bold]{self.option.label}[/bold]' if highlighted else self.option.label
        return f'{cursor}{label}{star}  {rev_glyph}'

    def set_highlighted(self, highlighted: bool) -> None:
        try:
            self.query_one(Label).update(self._render_label(highlighted))
        except Exception:  # noqa: BLE001
            pass  # widget not yet mounted


# ---------------------------------------------------------------------------
# Inline note widget (Submit on Enter, newline on Shift+Enter)
# ---------------------------------------------------------------------------


class _NoteInput(TextArea):
    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Cancelled(Message):
        pass

    def _on_key(self, event: Any) -> None:  # type: ignore[override]
        if event.key == 'enter':
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text.strip()))
        elif event.key == 'escape':
            event.prevent_default()
            event.stop()
            self.post_message(self.Cancelled())


# ---------------------------------------------------------------------------
# Modal screens retained from the old design
# ---------------------------------------------------------------------------


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding('escape', 'dismiss(None)', 'Close'),
        Binding('q', 'dismiss(None)', 'Close'),
        Binding('question_mark', 'dismiss(None)', 'Close'),
    ]

    def compose(self) -> ComposeResult:
        body = (
            '[bold]Cockpit keybindings[/bold]\n'
            '\n'
            '[bold underline]LIST mode[/bold underline]\n'
            '  [bold]↑ / ↓[/bold]       navigate the queue\n'
            '  [bold]Enter[/bold]       open proposal in REVIEW mode\n'
            '  [bold]Space[/bold]       toggle multi-select checkbox\n'
            '  [bold]Shift+↑/↓[/bold]  toggle-select + move cursor\n'
            '  [bold]Esc[/bold]         deselect all\n'
            '  [bold]F5[/bold]          refresh queue from the server\n'
            '  [bold]r[/bold]           reverse a previously-resolved finding\n'
            '  [bold]?[/bold]           this help\n'
            '  [bold]q[/bold]           quit\n'
            '\n'
            '[bold underline]REVIEW mode[/bold underline]\n'
            '  [bold]↑ / ↓[/bold]       navigate action list\n'
            '  [bold]Enter[/bold]       confirm action (opens note area)\n'
            '  [bold]n[/bold]           toggle note area\n'
            '  [bold]Esc[/bold]         back to LIST\n'
            '\n'
            '[bold underline]NOTE mode[/bold underline]\n'
            '  [bold]Enter[/bold]       submit verdict\n'
            '  [bold]Shift+Enter[/bold] newline in note\n'
            '  [bold]Esc[/bold]         cancel note\n'
            '\n'
            '[bold underline]ACTION EFFECTS[/bold underline]\n'
        )
        # Append action effect details from all known options
        from memex_cli.cockpit.controller import _DEFAULT_OPTIONS_BY_RULE

        effect_lines: list[str] = []
        seen: set[str] = set()
        for options in _DEFAULT_OPTIONS_BY_RULE.values():
            for opt in options:
                key = opt.action_id or opt.label
                if key in seen:
                    continue
                seen.add(key)
                if opt.effect:
                    effect_lines.append(f'  [bold]{opt.label}[/bold]')
                    effect_lines.append(f'    [dim]{opt.effect}[/dim]')
        body += '\n'.join(effect_lines)
        body += '\n\n[dim]Esc or q to close this help.[/dim]'
        yield Vertical(Static(body, id='help-body'), id='help-modal')


class ReverseScreen(ModalScreen[str | None]):
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


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class ProposalCockpitApp(App):
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
    #queue-pane.dimmed {
        opacity: 0.4;
    }
    #detail-pane {
        width: 1fr;
        border: solid $primary;
        padding: 1 2;
    }
    #action-section {
        display: none;
    }
    #action-section.visible {
        display: block;
    }
    #action-list ListItem {
        padding: 0;
        margin: 0;
        height: 1;
    }
    #action-rule {
        color: $text-muted;
    }
    #note-section {
        display: none;
    }
    #note-section.visible {
        display: block;
    }
    #note-input {
        height: 4;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding('q', 'quit', 'Quit'),
        Binding('question_mark', 'help', 'Help', show=True, key_display='?'),
        Binding('f5', 'refresh', 'Refresh'),
        Binding('r', 'reverse', 'Reverse'),
    ]

    proposals: reactive[list[CockpitProposal]] = reactive(list, init=False)
    mode: reactive[str] = reactive('list', init=False)

    def __init__(self, controller: CockpitController, *, limit: int = 50) -> None:
        super().__init__()
        self._controller = controller
        self._limit = limit
        self._pending_note: str | None = None
        self._selected_option: CockpitOption | None = None
        self._batch_targets: list[CockpitProposal] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Vertical(
                Label('[bold]Pending (0)[/bold]', id='queue-label'),
                ListView(id='queue-list'),
                id='queue-pane',
            ),
            VerticalScroll(
                Static(id='detail-header'),
                Static(id='detail-body'),
                Vertical(
                    Rule(line_style='heavy', id='action-rule'),
                    ListView(id='action-list'),
                    Static(id='action-detail'),
                    id='action-section',
                ),
                Vertical(
                    Label('[dim]NOTE (optional — Enter to submit, Esc to cancel)[/dim]'),
                    _NoteInput(id='note-input'),
                    id='note-section',
                ),
                Static(id='status-bar'),
                id='detail-pane',
            ),
            id='cockpit-root',
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.title = 'Memex Maintenance Cockpit'
        await self._refresh_queue()

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    def watch_mode(self, old: str, new: str) -> None:
        queue_pane = self.query_one('#queue-pane')
        action_section = self.query_one('#action-section')
        note_section = self.query_one('#note-section')

        if new == 'list':
            queue_pane.remove_class('dimmed')
            action_section.remove_class('visible')
            note_section.remove_class('visible')
            self._pending_note = None
            self._selected_option = None
            self._batch_targets = []
            self.query_one('#queue-list', ListView).focus()
            self._update_footer()
        elif new in ('review', 'batch'):
            queue_pane.add_class('dimmed')
            action_section.add_class('visible')
            note_section.remove_class('visible')
            self._pending_note = None
            self.query_one('#action-list', ListView).focus()
            self._update_footer()
            self.call_after_refresh(lambda: action_section.scroll_visible(animate=False))
        elif new == 'note':
            note_section.add_class('visible')
            note_input = self.query_one('#note-input', _NoteInput)
            note_input.clear()
            note_input.focus()
            self._update_footer()

        self._update_subtitle()

    def _update_subtitle(self) -> None:
        count = len(self.proposals)
        selected = self._count_selected()
        mode_label = self.mode.upper()
        if self.mode == 'batch':
            mode_label = f'BATCH ({selected} selected)'
        self.sub_title = f'{mode_label} · {count} pending'

    def _update_footer(self) -> None:
        footer = self.query_one(Footer)
        footer.refresh()

    def _count_selected(self) -> int:
        queue = self.query_one('#queue-list', ListView)
        count = 0
        for child in queue.children:
            if isinstance(child, _ProposalQueueItem) and child.checked:
                count += 1
        return count

    def _selected_proposals(self) -> list[CockpitProposal]:
        queue = self.query_one('#queue-list', ListView)
        result: list[CockpitProposal] = []
        for child in queue.children:
            if isinstance(child, _ProposalQueueItem) and child.checked:
                result.append(child.proposal)
        return result

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    async def _refresh_queue(self) -> None:
        proposals = await self._controller.fetch_pending(limit=self._limit)
        self.proposals = proposals
        queue = self.query_one('#queue-list', ListView)
        queue.clear()
        for proposal in proposals:
            queue.append(_ProposalQueueItem(proposal))
        self.query_one('#queue-label', Label).update(f'[bold]Pending ({len(proposals)})[/bold]')
        if proposals:
            queue.index = 0
            self._show_proposal_preview(proposals[0])
        else:
            self._show_empty_queue()
        self.mode = 'list'

    def _show_empty_queue(self) -> None:
        self.query_one('#detail-header', Static).update(
            '[dim]Queue empty — all proposals reviewed.[/dim]'
        )
        self.query_one('#detail-body', Static).update('')
        self.query_one('#status-bar', Static).update('')

    def _show_proposal_preview(self, proposal: CockpitProposal) -> None:
        self.query_one('#detail-header', Static).update(self._render_header(proposal))

        body_lines: list[str] = []

        if proposal.rule_name == 'llm_semantic_contradiction':
            body_lines.extend(self._build_contradiction_body(proposal))
        else:
            body_lines.append(f'[bold]TARGET[/bold]  [dim cyan]{proposal.target_id[:8]}[/dim cyan]')
            if proposal.target_text:
                body_lines.append(f' {proposal.target_text}')

        if proposal.explanation:
            body_lines.append('')
            body_lines.append(f' {proposal.explanation}')

        is_contradiction = proposal.rule_name == 'llm_semantic_contradiction'
        if proposal.related_unit_ids and not is_contradiction:
            body_lines.append('')
            body_lines.append(f'[dim]related: {len(proposal.related_unit_ids)} units cited[/dim]')
        if proposal.suggested_action:
            body_lines.append('')
            body_lines.append(f'[dim]suggested: {proposal.suggested_action}[/dim]')
        body_lines.append('')
        body_lines.append('[dim]' + '─' * 72 + '[/dim]')
        self.query_one('#detail-body', Static).update('\n'.join(body_lines))

    def _render_header(self, proposal: CockpitProposal) -> str:
        badge = '[yellow]LLM[/yellow]' if proposal.is_llm_source else '[blue]rule[/blue]'
        vault_short = (proposal.vault_id or '(global)')[:8]
        line1 = (
            f' [bold]{proposal.rule_name}[/bold]  {badge}'
            f'  [dim]· {proposal.lint_type} / {proposal.target_type}[/dim]'
        )
        right_parts: list[str] = []
        if proposal.surprise_score is not None:
            right_parts.append(f'surprise={proposal.surprise_score:.2f}')
        if proposal.polarity_contradiction_prob is not None:
            right_parts.append(f'P(contra) {proposal.polarity_contradiction_prob:.3f}')
        right = f'  [dim]{" · ".join(right_parts)}[/dim]' if right_parts else ''
        line2 = f' [dim]vault {vault_short} · {proposal.created_at or "?"}[/dim]{right}'
        return f'{line1}\n{line2}'

    def _build_contradiction_body(self, proposal: CockpitProposal) -> list[str]:
        lines: list[str] = []
        lines.append(f' [bold]TARGET[/bold]   [dim cyan]{proposal.target_id[:8]}[/dim cyan]')
        if proposal.target_text:
            lines.append(f' {proposal.target_text}')
        else:
            lines.append(f' [dim]{proposal.target_id}[/dim]')

        if proposal.related_unit_ids:
            contra_id = proposal.related_unit_ids[0]
            lines.append('')
            lines.append('     [dim]vs.[/dim]')
            lines.append('')
            lines.append(f' [bold]RELATED[/bold]  [dim cyan]{contra_id[:8]}[/dim cyan]')
            lines.append(' [dim]loading…[/dim]')
            self._fetch_contradiction_text(proposal, contra_id)
        return lines

    def _fetch_contradiction_text(self, proposal: CockpitProposal, contra_id: str) -> None:
        self.run_worker(
            self._fetch_contradiction_text_async(proposal, contra_id),
            name='fetch_contra_text',
        )

    async def _fetch_contradiction_text_async(
        self, proposal: CockpitProposal, contra_id: str
    ) -> None:
        texts = await self._controller.fetch_unit_texts([contra_id])
        contra_text = texts.get(contra_id)

        body_lines: list[str] = []
        body_lines.append(f' [bold]TARGET[/bold]   [dim cyan]{proposal.target_id[:8]}[/dim cyan]')
        if proposal.target_text:
            body_lines.append(f' {proposal.target_text}')
        else:
            body_lines.append(f' [dim]{proposal.target_id}[/dim]')

        body_lines.append('')
        body_lines.append('     [dim]vs.[/dim]')
        body_lines.append('')
        body_lines.append(f' [bold]RELATED[/bold]  [dim cyan]{contra_id[:8]}[/dim cyan]')
        if contra_text:
            body_lines.append(f' {contra_text}')
        else:
            body_lines.append(' [dim](text not loaded)[/dim]')

        if proposal.explanation:
            body_lines.append('')
            body_lines.append('[dim]' + '─' * 50 + '[/dim]')
            body_lines.append(f'[dim italic] {proposal.explanation}[/dim italic]')

        remaining = proposal.related_unit_ids[1:] if proposal.related_unit_ids else []
        if remaining:
            body_lines.append('')
            body_lines.append(f'[dim]related: {len(remaining)} additional units cited[/dim]')
        if proposal.suggested_action:
            body_lines.append('')
            body_lines.append(f'[dim]suggested: {proposal.suggested_action}[/dim]')
        body_lines.append('')
        body_lines.append('[dim]' + '─' * 72 + '[/dim]')

        self.query_one('#detail-body', Static).update('\n'.join(body_lines))

    # ------------------------------------------------------------------
    # Detail panel: populate actions
    # ------------------------------------------------------------------

    def _populate_actions(self, proposal: CockpitProposal) -> None:
        if proposal.rule_name == 'llm_semantic_contradiction':
            options = options_for_contradiction(proposal)
        else:
            options = options_for_rule(proposal.rule_name, proposal.target_type)
        action_list = self.query_one('#action-list', ListView)
        action_list.clear()
        for i, opt in enumerate(options):
            action_list.append(_ActionListItem(opt, highlighted=(i == 0)))
        if options:
            action_list.index = 0
            self._show_action_detail(options[0])
        else:
            self.query_one('#action-detail', Static).update('[dim]No actions available.[/dim]')

    def _populate_batch_actions(self, proposals: list[CockpitProposal]) -> None:
        if not proposals:
            return
        option_sets = [
            {o.action_id for o in options_for_rule(p.rule_name, p.target_type)} for p in proposals
        ]
        common_ids = option_sets[0]
        for s in option_sets[1:]:
            common_ids = common_ids & s

        ref = proposals[0]
        all_options = options_for_rule(ref.rule_name, ref.target_type)
        filtered = [o for o in all_options if o.action_id in common_ids]

        action_list = self.query_one('#action-list', ListView)
        action_list.clear()
        for i, opt in enumerate(filtered):
            action_list.append(_ActionListItem(opt, highlighted=(i == 0)))
        if filtered:
            action_list.index = 0
            self._show_action_detail(filtered[0])

        self.query_one('#detail-header', Static).update(
            f'[bold]BATCH — {len(proposals)} proposals selected[/bold]'
        )
        rule_names = {p.rule_name for p in proposals}
        self.query_one('#detail-body', Static).update(
            f'Rules: {", ".join(sorted(rule_names))}\nCommon actions: {len(filtered)}'
        )

    def _show_action_detail(self, option: CockpitOption) -> None:
        rev = '[green]reversible[/green]' if option.reversible else '[red]permanent[/red]'
        summary = option.summary
        current = self._current_proposal()
        if (
            current
            and option.action_id == 'deprioritize_unit'
            and current.rule_name == 'llm_semantic_contradiction'
        ):
            short_id = current.target_id[:8]
            summary = f'Suppress TARGET {short_id}; related units stay active.'
        self.query_one('#action-detail', Static).update(f' [dim]{summary}  {rev}[/dim]')
        # Update cursor glyphs on all action items
        action_list = self.query_one('#action-list', ListView)
        for child in action_list.children:
            if isinstance(child, _ActionListItem):
                is_current = child.option is option
                child.set_highlighted(is_current)

    def _show_status(self, message: str, *, error: bool = False) -> None:
        if error:
            self.query_one('#status-bar', Static).update(f' [red]✗ {message}[/red]')
        else:
            self.query_one('#status-bar', Static).update(f' [dim]✓ {message}[/dim]')

    # ------------------------------------------------------------------
    # Current proposal helper
    # ------------------------------------------------------------------

    def _current_proposal(self) -> CockpitProposal | None:
        queue = self.query_one('#queue-list', ListView)
        idx = queue.index
        if idx is None or idx < 0 or idx >= len(self.proposals):
            return None
        return self.proposals[idx]

    def _current_action(self) -> CockpitOption | None:
        action_list = self.query_one('#action-list', ListView)
        idx = action_list.index
        if idx is None:
            return None
        items = list(action_list.children)
        if 0 <= idx < len(items):
            item = items[idx]
            if isinstance(item, _ActionListItem):
                return item.option
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == 'queue-list' and self.mode == 'list':
            item = event.item
            if isinstance(item, _ProposalQueueItem):
                self._show_proposal_preview(item.proposal)
        elif event.list_view.id == 'action-list' and self.mode in ('review', 'batch'):
            if isinstance(event.item, _ActionListItem):
                self._show_action_detail(event.item.option)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == 'queue-list' and self.mode == 'list':
            self._enter_review_mode()
        elif event.list_view.id == 'action-list' and self.mode in ('review', 'batch'):
            self._enter_note_mode()

    def _on__note_input_submitted(self, event: _NoteInput.Submitted) -> None:
        self._pending_note = event.text or None
        self._submit_verdict()

    def _on__note_input_cancelled(self, event: _NoteInput.Cancelled) -> None:
        self.query_one('#note-section').remove_class('visible')
        self.query_one('#action-list', ListView).focus()
        self.mode = 'review' if not self._batch_targets else 'batch'
        self._update_footer()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def on_key(self, event: Any) -> None:  # type: ignore[override]
        if self.mode == 'note':
            return

        if self.mode == 'list':
            self._handle_list_key(event)
        elif self.mode in ('review', 'batch'):
            self._handle_review_key(event)

    def _handle_list_key(self, event: Any) -> None:  # type: ignore[override]
        key = event.key
        if key == 'space':
            event.prevent_default()
            event.stop()
            self._toggle_current_item()
        elif key == 'shift+up':
            event.prevent_default()
            event.stop()
            self._toggle_current_item()
            queue = self.query_one('#queue-list', ListView)
            queue.action_cursor_up()
        elif key == 'shift+down':
            event.prevent_default()
            event.stop()
            self._toggle_current_item()
            queue = self.query_one('#queue-list', ListView)
            queue.action_cursor_down()
        elif key == 'escape':
            event.prevent_default()
            event.stop()
            self._deselect_all()

    def _handle_review_key(self, event: Any) -> None:  # type: ignore[override]
        key = event.key
        if key == 'n':
            event.prevent_default()
            event.stop()
            self._toggle_note_area()
        elif key == 'escape':
            event.prevent_default()
            event.stop()
            self.mode = 'list'

    # ------------------------------------------------------------------
    # LIST mode actions
    # ------------------------------------------------------------------

    def _toggle_current_item(self) -> None:
        queue = self.query_one('#queue-list', ListView)
        idx = queue.index
        if idx is None:
            return
        items = list(queue.children)
        if 0 <= idx < len(items):
            item = items[idx]
            if isinstance(item, _ProposalQueueItem):
                item.toggle()
        self._update_subtitle()

    def _deselect_all(self) -> None:
        queue = self.query_one('#queue-list', ListView)
        for child in queue.children:
            if isinstance(child, _ProposalQueueItem) and child.checked:
                child.toggle()
        self._update_subtitle()

    # ------------------------------------------------------------------
    # REVIEW mode
    # ------------------------------------------------------------------

    def _enter_review_mode(self) -> None:
        selected = self._selected_proposals()
        if selected:
            self._batch_targets = selected
            self._populate_batch_actions(selected)
            self.mode = 'batch'
        else:
            proposal = self._current_proposal()
            if proposal is None:
                return
            self._batch_targets = []
            self._show_proposal_preview(proposal)
            self._populate_actions(proposal)
            self.mode = 'review'

    def _enter_note_mode(self) -> None:
        self._selected_option = self._current_action()
        if self._selected_option is None:
            return
        self.mode = 'note'

    def _toggle_note_area(self) -> None:
        note_section = self.query_one('#note-section')
        if note_section.has_class('visible'):
            note_section.remove_class('visible')
            self.query_one('#action-list', ListView).focus()
            self._update_footer()
        else:
            note_section.add_class('visible')
            note_input = self.query_one('#note-input', _NoteInput)
            note_input.clear()
            note_input.focus()
            self.mode = 'note'

    # ------------------------------------------------------------------
    # Verdict submission
    # ------------------------------------------------------------------

    def _submit_verdict(self) -> None:
        option = self._selected_option
        if option is None:
            return
        if self._batch_targets:
            self.run_worker(
                self._submit_batch_async(self._batch_targets, option, self._pending_note),
                exclusive=True,
                name='batch_verdict',
            )
        else:
            proposal = self._current_proposal()
            if proposal is None:
                return
            self.run_worker(
                self._submit_single_async(proposal, option, self._pending_note),
                exclusive=True,
                name='single_verdict',
            )

    async def _submit_single_async(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        note: str | None,
    ) -> None:
        try:
            result = await self._controller.resolve(
                proposal,
                option,
                note=note,
                params=option.params,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_status(f'Action failed: {exc}', error=True)
            self.mode = 'list'
            return
        status = result.get('status', 'unknown')
        action_id = option.action_id or 'dismiss'

        if option.verb == 'resolve' and action_id != 'no_op':
            followup = _extract_followup(result)
            if followup is None or followup.get('action') != action_id:
                self._show_status(
                    f'{action_id} → {status}, BUT the server did NOT run the '
                    'action (no resolution.followup in response). Server is likely '
                    'on a pre-cockpit build.',
                    error=True,
                )
                await self._refresh_queue()
                return

        self._show_status(f'{action_id} → resolved')
        await self._refresh_queue()

    async def _submit_batch_async(
        self,
        proposals: list[CockpitProposal],
        option: CockpitOption,
        note: str | None,
    ) -> None:
        ok = 0
        fail = 0
        for proposal in proposals:
            try:
                await self._controller.resolve(proposal, option, note=note)
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
        parts: list[str] = []
        if ok:
            parts.append(f'{ok} resolved')
        if fail:
            parts.append(f'{fail} failed')
        self._show_status(', '.join(parts), error=bool(fail))
        await self._refresh_queue()

    # ------------------------------------------------------------------
    # App-level actions
    # ------------------------------------------------------------------

    async def action_refresh(self) -> None:
        await self._refresh_queue()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reverse(self) -> None:
        self.run_worker(self._reverse_async(), exclusive=True, name='reverse')

    async def _reverse_async(self) -> None:
        finding_id = await self.push_screen_wait(ReverseScreen())
        if not finding_id:
            return
        try:
            result = await self._controller.reverse(finding_id)
        except Exception as exc:  # noqa: BLE001
            self._show_status(f'Reverse failed: {exc}', error=True)
            return
        summary = result.get('reversal') or result.get('effective_action') or 'ok'
        self._show_status(f'Reversed {finding_id[:8]}… ({summary}).')
