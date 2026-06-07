"""Textual TUI cockpit for the maintenance ledger.

Four-mode UX: LIST -> REVIEW -> NOTE, LIST -> DETAIL.

LIST   — browse the queue; Space multi-selects; Enter opens REVIEW; d opens DETAIL.
REVIEW — pick an action from the action list; Enter confirms / opens NOTE.
NOTE   — inline TextArea for an optional reviewer note; Enter submits.
DETAIL — drill-down view of a finding's memory units: metadata, lineage, source note.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape as _esc

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
    DISMISS_OPTION,
    FLAG_OPTION,
    UnitDetail,
    UnitLineage,
    UnitMeta,
    action_is_reversible,
    options_for_proposal,
    recommended_resolve_option,
)

logger = logging.getLogger('memex.cli.cockpit')


# Batch sentinel: applies each proposal's OWN recommended action (with its
# proposal-specific params) rather than one shared option — so an inbox route in
# a mixed batch still migrates to its top vault. Never sent to the server; the
# batch submitter translates it per proposal.
_BATCH_RECOMMENDED_ACTION_ID = '__recommended__'
_BATCH_RECOMMENDED_OPTION = CockpitOption(
    action_id=_BATCH_RECOMMENDED_ACTION_ID,
    label='Accept recommended action (per proposal)',
    summary=(
        "Apply each selected proposal's own recommended action — e.g. route "
        'each inbox note to its top vault, deprioritize each contradiction '
        'target. Findings with no actionable default are skipped.'
    ),
    effect="Per-proposal: the recommended mutation for each finding's rule.",
    reversible=False,
    recommended=True,
)
_BATCH_NO_OP_OPTION = CockpitOption(
    action_id='no_op',
    label='Mark reviewed (no mutation)',
    summary='Flip every selected finding to resolved without mutating its target.',
    effect='No mutation. Status flips to resolved.',
    reversible=True,
)


def _extract_followup(result: dict[str, Any]) -> dict[str, Any] | None:
    resolution = result.get('resolution')
    if not isinstance(resolution, dict):
        return None
    followup = resolution.get('followup')
    if isinstance(followup, dict):
        return followup
    return None


def _format_unit_meta_line(meta: UnitMeta) -> str:
    """Build a dim Rich-markup line showing created date, source note, and status."""
    parts: list[str] = []
    if meta.date:
        parts.append(f'created: {meta.date}')
    if meta.note_name:
        parts.append(f'note: {meta.note_name}')
    if meta.status:
        parts.append(f'status: {meta.status}')
    if not parts:
        return ''
    return ' [dim]  ' + '  ·  '.join(parts) + '[/dim]'


# Keys that are already rendered elsewhere in the preview (entity_name is shown
# prominently for orphan_mental_model; the others are internal / structural).
_EVIDENCE_SKIP_KEYS = frozenset(
    {
        'entity_name',
        'explanation',
        'related_unit_ids',
        'surprise_score',
        'polarity_contradiction_prob',
        'resolution',
        'components',
    }
)

# Human-friendly labels for common evidence keys.
_EVIDENCE_LABELS: dict[str, str] = {
    'last_refreshed': 'Last refreshed',
    'observation_count': 'Observations',
    'linked_active_units': 'Active linked units',
    'mw_score': 'MW score',
    'success_co_count': 'Successes',
    'failure_co_count': 'Failures',
    'last_outcome_age_days': 'Last outcome age (days)',
    'risk_class': 'Risk class',
    'created_at': 'Created',
    'composite_score': 'Composite score',
    'component_range': 'Component range',
    'flag_reason': 'Flag reason',
    'importance': 'Importance',
    'intent_class': 'Intent class',
    'contradicts_count': 'Contradicts count',
    'contradicts_credibility_sum': 'Contradicts credibility',
    'orphan_link_count': 'Orphan links',
    'claim_type': 'Claim type',
    'link_count': 'Link count',
    'entity_id': 'Entity ID',
}


def _format_evidence_line(evidence: dict[str, Any]) -> str:
    """Build a dim Rich-markup line from evidence fields.

    Skips keys that are rendered elsewhere (entity_name, explanation, etc.)
    and formats the remaining scalar values as a dot-separated line.
    """
    parts: list[str] = []
    for key, value in evidence.items():
        if key in _EVIDENCE_SKIP_KEYS:
            continue
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        label = _EVIDENCE_LABELS.get(key, key.replace('_', ' ').capitalize())
        # Format floats to 2 decimal places for readability.
        if isinstance(value, float):
            formatted = f'{value:.2f}'
        else:
            formatted = str(value)
            # Truncate ISO timestamps to date-only for readability.
            if len(formatted) > 10 and 'T' in formatted:
                formatted = formatted[:10]
        parts.append(f'{label}: {formatted}')
    if not parts:
        return ''
    return ' [dim]' + '  ·  '.join(parts) + '[/dim]'


# ---------------------------------------------------------------------------
# Queue item
# ---------------------------------------------------------------------------


class _ProposalQueueItem(ListItem):
    """Single row in the proposals queue list, with a multi-select checkbox."""

    def __init__(self, proposal: CockpitProposal) -> None:
        self.proposal = proposal
        self.checked: bool = False
        badge = 'LLM' if proposal.is_llm_source else 'rule'
        self._badge = badge
        super().__init__(Label(self._render_label()))

    def _render_label(self) -> str:
        mark = '[bold green]✓[/bold green] ' if self.checked else '  '
        flag = '[yellow]⚑[/yellow] ' if self.proposal.is_flagged else ''
        return (
            f'{mark}{flag}[{self._badge}] {self.proposal.rule_name}\n'
            f'    {self.proposal.target_type} · {_esc(self.proposal.target_display)}'
        )

    def toggle(self) -> None:
        self.checked = not self.checked
        self.query_one(Label).update(self._render_label())

    def refresh_label(self) -> None:
        """Re-render the label (e.g. after a flag toggle)."""
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


class _CollapseMemberItem(ListItem):
    """One entity in the entity-collapse selection list.

    Holds its own include/exclude + winner state so the reviewer can pick
    exactly which entities merge into the winner.
    """

    def __init__(
        self,
        member_id: str,
        display_name: str,
        *,
        included: bool = True,
        is_winner: bool = False,
        highlighted: bool = False,
    ) -> None:
        self.member_id = member_id
        self.member_name = display_name
        self.included = included
        self.is_winner = is_winner
        super().__init__(Label(self._render_label(highlighted)))

    def _render_label(self, highlighted: bool = False) -> str:
        cursor = '[bold]▸[/bold] ' if highlighted else '  '
        box = '[green]✓[/green]' if self.included else '[red]✗[/red]'
        safe_name = _esc(self.member_name)
        name = f'[bold]{safe_name}[/bold]' if highlighted else safe_name
        if self.is_winner:
            tag = '  [yellow]★ winner[/yellow]'
        elif self.included:
            tag = '  [dim]→ merge[/dim]'
        else:
            tag = '  [dim]kept separate[/dim]'
        return f'{cursor}{box} {name}  [dim]{self.member_id[:8]}[/dim]{tag}'

    def refresh_label(self, highlighted: bool = False) -> None:
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
            '  [bold]d[/bold]           drill-down into unit DETAIL mode\n'
            '  [bold]Space[/bold]       toggle multi-select checkbox\n'
            '  [bold]Shift+↑/↓[/bold]  toggle-select + move cursor\n'
            '  [bold]Esc[/bold]         deselect all\n'
            '  [bold]F5[/bold]          refresh queue from the server\n'
            '  [bold]f[/bold]           toggle flag on highlighted finding\n'
            '  [bold]r[/bold]           reverse a previously-resolved finding\n'
            '  [bold]?[/bold]           this help\n'
            '  [bold]q[/bold]           quit\n'
            '\n'
            '[bold underline]DETAIL mode[/bold underline]\n'
            '  [bold]Tab / ↑ / ↓[/bold] cycle between units in the finding\n'
            '  [bold]s[/bold]           view source note text\n'
            '  [bold]Esc[/bold]         back to LIST\n'
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
        body += '\n\n[dim][Esc]/[q] Close[/dim]'
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
            Label('[dim][Enter] Submit · [Esc] Cancel[/dim]', id='reverse-help'),
            id='reverse-modal',
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or '').strip()
        if not text:
            self.dismiss(None)
            return
        try:
            UUID(text)
        except ValueError:
            self.query_one('#reverse-help', Label).update(
                '[red]Not a valid finding_id (UUID). Esc to cancel.[/red]'
            )
            return
        self.dismiss(text)


class ConfirmIrreversibleScreen(ModalScreen[bool]):
    """Blast-radius confirmation for forward-only catalogue actions.

    Rendered before executing any action the catalogue marks
    ``reversible=False`` — the preview text comes live from the server
    (``POST /lint/findings/{id}/preview``) so the reviewer sees what would
    actually be destroyed, not a canned description.
    """

    BINDINGS = [
        Binding('y', 'dismiss(True)', 'Execute'),
        Binding('n', 'dismiss(False)', 'Cancel'),
        Binding('escape', 'dismiss(False)', 'Cancel'),
    ]

    def __init__(self, action_id: str, preview_text: str) -> None:
        super().__init__()
        self._action_id = action_id
        self._preview_text = preview_text

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                f'[bold red]{self._action_id} is NOT reversible.[/bold red]',
                id='confirm-title',
            ),
            Static(self._preview_text, id='confirm-preview'),
            Label('[dim][y] Execute · [n]/[Esc] Cancel[/dim]', id='confirm-help'),
            id='confirm-modal',
        )


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
        Binding('f5', 'refresh', 'Refresh', key_display='F5'),
        Binding('f', 'flag', 'Flag'),
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
        # DETAIL mode state
        self._viewing_source_note: bool = False
        self._detail_unit_ids: list[str] = []
        self._detail_unit_index: int = 0
        # Set when the highlighted finding targets a note directly (target_id is
        # a note id, not a unit id) so DETAIL renders the note, not a unit.
        self._detail_note_id: str | None = None
        # COLLAPSE mode state (entity_collapse_cluster member selection).
        self._collapse_proposal: CockpitProposal | None = None
        self._collapse_winner_id: str | None = None
        # When True the note input is collecting the NEW canonical name for a
        # collapse-into-new-entity merge, not a reviewer note.
        self._collapse_new_name_pending: bool = False

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
            self._detail_unit_ids = []
            self._detail_unit_index = 0
            self._detail_note_id = None
            self._viewing_source_note = False
            self._collapse_proposal = None
            self._collapse_winner_id = None
            self.query_one('#queue-list', ListView).focus()
            self._update_footer()
        elif new in ('review', 'batch', 'collapse'):
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
        elif new == 'detail':
            queue_pane.add_class('dimmed')
            action_section.remove_class('visible')
            note_section.remove_class('visible')
            self._update_footer()

        self._update_subtitle()

    def _update_subtitle(self) -> None:
        count = len(self.proposals)
        selected = self._count_selected()
        mode_label = self.mode.upper()
        if self.mode == 'batch':
            mode_label = f'BATCH ({selected} selected)'
        elif self.mode == 'detail':
            n = len(self._detail_unit_ids)
            idx = self._detail_unit_index + 1
            mode_label = f'DETAIL (unit {idx}/{n})' if n > 1 else 'DETAIL'
        elif self.mode == 'collapse':
            mode_label = 'COLLAPSE'
        self.sub_title = f'{mode_label} · {count} pending'

    def _update_footer(self) -> None:
        footer = self.query_one(Footer)
        footer.refresh()
        hints = {
            'list': '[d] Detail  [Enter] Review  [f] Flag  [F5] Refresh  [?] Help  [q] Quit',
            'review': '[↑↓] Navigate  [Enter] Confirm  [n] Note  [Esc] Back  [q] Quit',
            'note': '[Enter] Submit  [Shift+Enter] Newline  [Esc] Cancel',
            'detail': '[s] View note  [Tab] Cycle units  [Esc] Back  [q] Quit',
            'collapse': (
                '[Space] in/out  [w] winner  [a] apply  [n] new entity  [x] dismiss  [Esc] Cancel'
            ),
        }
        hint = hints.get(self.mode, '')
        self.query_one('#status-bar', Static).update(f' [dim]{hint}[/dim]')

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
        try:
            proposals = await self._controller.fetch_pending(limit=self._limit)
        except Exception as exc:  # noqa: BLE001
            # Drop to LIST first so watch_mode's footer update fires before we
            # paint the error — otherwise a refresh failing from DETAIL/REVIEW
            # would overwrite our retry hint with the normal LIST hint.
            queue = self.query_one('#queue-list', ListView)
            queue.clear()
            self.proposals = []
            self.mode = 'list'
            self._show_load_error(exc)
            return
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

    def _show_load_error(self, exc: Exception) -> None:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            detail = 'the server did not respond in time'
        else:
            detail = str(exc) or exc.__class__.__name__
        self.query_one('#queue-label', Label).update('[bold red]Failed to load queue[/bold red]')
        self.query_one('#detail-header', Static).update(
            f'[red]Could not load the proposal queue: {_esc(detail)}.[/red]'
        )
        self.query_one('#detail-body', Static).update(
            '[dim]Check the server is reachable, then press [bold]F5[/bold] to retry.[/dim]'
        )
        self.query_one('#status-bar', Static).update(' [dim][F5] Retry  [q] Quit[/dim]')

    def _show_empty_queue(self) -> None:
        self.query_one('#detail-header', Static).update(
            '[dim]Queue empty — all proposals reviewed.[/dim]'
        )
        self.query_one('#detail-body', Static).update('')
        self.query_one('#status-bar', Static).update('')

    def _show_proposal_preview(self, proposal: CockpitProposal) -> None:
        self.run_worker(
            self._show_proposal_preview_async(proposal),
            name='show_preview',
            exclusive=True,
        )

    async def _show_proposal_preview_async(self, proposal: CockpitProposal) -> None:
        header = await self._render_header(proposal)
        self.query_one('#detail-header', Static).update(header)

        body_lines: list[str] = []

        if proposal.rule_name == 'llm_semantic_contradiction':
            body_lines.extend(self._build_contradiction_body(proposal))
        elif proposal.rule_name == 'entity_collapse_cluster':
            body_lines.extend(self._build_entity_collapse_body(proposal))
        else:
            label_suffix = (
                f'  [white]{proposal.target_label}[/white]' if proposal.target_label else ''
            )
            body_lines.append(
                f'[bold]TARGET[/bold]  [dim cyan]{proposal.target_id[:8]}[/dim cyan]{label_suffix}'
            )

            # For orphan_mental_model, show the entity name prominently.
            entity_name = proposal.raw_evidence.get('entity_name')
            if entity_name:
                body_lines.append(f'[bold]Entity: "{_esc(str(entity_name))}"[/bold]')

            # Fetch and display unit metadata for the target.
            target_meta = await self._controller.fetch_unit_metadata([proposal.target_id])
            meta = target_meta.get(proposal.target_id)
            if meta:
                meta_line = _format_unit_meta_line(meta)
                if meta_line:
                    body_lines.append(meta_line)
            if proposal.target_text:
                body_lines.append(f' {proposal.target_text}')

            # Show evidence fields as a dim metadata line for all rule types.
            evidence_line = _format_evidence_line(proposal.raw_evidence)
            if evidence_line:
                body_lines.append('')
                body_lines.append(evidence_line)

        if proposal.explanation:
            body_lines.append('')
            body_lines.append('[dim]' + '─' * 50 + '[/dim]')
            body_lines.append('[bold]EXPLANATION[/bold]')
            body_lines.append(f'[dim italic] {proposal.explanation}[/dim italic]')

        is_contradiction = proposal.rule_name == 'llm_semantic_contradiction'
        if proposal.related_unit_ids and not is_contradiction:
            n = len(proposal.related_unit_ids)
            unit_word = 'unit' if n == 1 else 'units'
            body_lines.append('')
            body_lines.append(f'[dim]related: {n} {unit_word} cited[/dim]')
        if proposal.suggested_action:
            body_lines.append('')
            body_lines.append(f'[dim]suggested: {_esc(proposal.suggested_action)}[/dim]')
        body_lines.append('')
        body_lines.append('[dim]' + '─' * 72 + '[/dim]')
        self.query_one('#detail-body', Static).update('\n'.join(body_lines))

    async def _render_header(self, proposal: CockpitProposal) -> str:
        badge = '[yellow]LLM[/yellow]' if proposal.is_llm_source else '[blue]rule[/blue]'
        if proposal.vault_id:
            vault_label = await self._controller.resolve_vault_name(proposal.vault_id)
        else:
            vault_label = '(global)'
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
        line2 = f' [dim]vault {vault_label} · {proposal.created_at or "?"}[/dim]{right}'
        return f'{line1}\n{line2}'

    def _build_entity_collapse_body(self, proposal: CockpitProposal) -> list[str]:
        """Detail body for an entity-collapse proposal.

        Lists EVERY member entity by name and marks the winner, so a reviewer
        can actually see which entities get merged away — the member names live
        in ``evidence.member_canonical_names`` (a dict the generic evidence
        renderer drops).
        """
        ev = proposal.raw_evidence if isinstance(proposal.raw_evidence, dict) else {}
        names = ev.get('member_canonical_names')
        if not isinstance(names, dict):
            names = {}
        members = ev.get('cluster_members')
        if not isinstance(members, list) or not members:
            members = list(names.keys())
        winner_id = str(ev.get('suggested_winner_id') or proposal.target_id)
        winner_name = _esc(str(names.get(winner_id) or proposal.target_label or winner_id[:8]))

        lines: list[str] = []
        lines.append(
            f'[bold]MERGE {len(members)} entities[/bold] → winner [green]"{winner_name}"[/green]'
        )
        lines.append('')
        for mid in members:
            mid = str(mid)
            nm = _esc(str(names.get(mid))) if names.get(mid) else '[dim](name unavailable)[/dim]'
            if mid == winner_id:
                lines.append(
                    f'  [green]★[/green] [white]{nm}[/white]  [dim]{mid[:8]} · winner[/dim]'
                )
            else:
                lines.append(
                    f'  [red]→[/red] [white]{nm}[/white]  [dim]{mid[:8]} · merged into winner[/dim]'
                )

        vaults = ev.get('vaults_affected')
        if isinstance(vaults, list) and vaults:
            lines.append('')
            lines.append(f'[dim]Affects {len(vaults)} vault(s).[/dim]')
        pmin, pmax = ev.get('pair_min_similarity'), ev.get('pair_max_similarity')
        try:
            if pmin is not None and pmax is not None:
                lines.append(f'[dim]Pairwise similarity {float(pmin):.2f}–{float(pmax):.2f}.[/dim]')
        except (TypeError, ValueError):
            pass
        return lines

    def _build_contradiction_body(self, proposal: CockpitProposal) -> list[str]:
        lines: list[str] = []
        label_suffix = f'  [white]{proposal.target_label}[/white]' if proposal.target_label else ''
        lines.append(
            f' [bold]TARGET[/bold]   [dim cyan]{proposal.target_id[:8]}[/dim cyan]{label_suffix}'
        )
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
            exclusive=True,
        )

    async def _fetch_contradiction_text_async(
        self, proposal: CockpitProposal, contra_id: str
    ) -> None:
        texts = await self._controller.fetch_unit_texts([contra_id])
        contra_text = texts.get(contra_id)

        # Fetch metadata for both the target and related unit.
        both_ids = [proposal.target_id, contra_id]
        all_meta = await self._controller.fetch_unit_metadata(both_ids)
        target_meta = all_meta.get(proposal.target_id)
        contra_meta = all_meta.get(contra_id)

        body_lines: list[str] = []
        label_suffix = f'  [white]{proposal.target_label}[/white]' if proposal.target_label else ''
        body_lines.append(
            f' [bold]TARGET[/bold]   [dim cyan]{proposal.target_id[:8]}[/dim cyan]{label_suffix}'
        )
        if target_meta:
            meta_line = _format_unit_meta_line(target_meta)
            if meta_line:
                body_lines.append(meta_line)
        if proposal.target_text:
            body_lines.append(f' {proposal.target_text}')
        else:
            body_lines.append(f' [dim]{proposal.target_id}[/dim]')

        body_lines.append('')
        body_lines.append('     [dim]vs.[/dim]')
        body_lines.append('')
        body_lines.append(f' [bold]RELATED[/bold]  [dim cyan]{contra_id[:8]}[/dim cyan]')
        if contra_meta:
            meta_line = _format_unit_meta_line(contra_meta)
            if meta_line:
                body_lines.append(meta_line)
        if contra_text:
            body_lines.append(f' {contra_text}')
        else:
            body_lines.append(' [dim](text not loaded)[/dim]')

        if proposal.explanation:
            body_lines.append('')
            body_lines.append('[dim]' + '─' * 50 + '[/dim]')
            body_lines.append('[bold]EXPLANATION[/bold]')
            body_lines.append(f'[dim italic] {proposal.explanation}[/dim italic]')

        remaining = proposal.related_unit_ids[1:] if proposal.related_unit_ids else []
        if remaining:
            n = len(remaining)
            unit_word = 'unit' if n == 1 else 'units'
            body_lines.append('')
            body_lines.append(f'[dim]{n} more related {unit_word} not shown[/dim]')
        if proposal.suggested_action:
            body_lines.append('')
            body_lines.append(f'[dim]suggested: {_esc(proposal.suggested_action)}[/dim]')
        body_lines.append('')
        body_lines.append('[dim]' + '─' * 72 + '[/dim]')

        self.query_one('#detail-body', Static).update('\n'.join(body_lines))

    # ------------------------------------------------------------------
    # Detail panel: populate actions
    # ------------------------------------------------------------------

    def _populate_actions(self, proposal: CockpitProposal) -> None:
        options = options_for_proposal(proposal)
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
        # A single shared option cannot carry per-proposal params (e.g. each
        # inbox route's target_vault_id), and the old intersection over
        # ``options_for_rule`` never even included the dynamic route /
        # contradiction actions — so a batch could never route. Offer verb-level
        # batch actions instead: "accept recommended" fans out to each
        # proposal's own option at submit time; no-op / flag / dismiss apply
        # uniformly because they need no params.
        n_actionable = sum(1 for p in proposals if recommended_resolve_option(p) is not None)
        options: list[CockpitOption] = []
        if n_actionable:
            options.append(_BATCH_RECOMMENDED_OPTION)
        options.extend([_BATCH_NO_OP_OPTION, FLAG_OPTION, DISMISS_OPTION])

        action_list = self.query_one('#action-list', ListView)
        action_list.clear()
        for i, opt in enumerate(options):
            action_list.append(_ActionListItem(opt, highlighted=(i == 0)))
        action_list.index = 0
        self._show_action_detail(options[0])

        self.query_one('#detail-header', Static).update(
            f'[bold]BATCH — {len(proposals)} proposals selected[/bold]'
        )
        rule_names = {p.rule_name for p in proposals}
        self.query_one('#detail-body', Static).update(
            f'Rules: {", ".join(sorted(rule_names))}\n'
            f'{n_actionable}/{len(proposals)} have a recommended action'
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
        elif event.list_view.id == 'action-list' and self.mode == 'collapse':
            self._refresh_collapse_items()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == 'queue-list' and self.mode == 'list':
            self._enter_review_mode()
        elif event.list_view.id == 'action-list' and self.mode == 'collapse':
            # In collapse mode Enter toggles the highlighted member (same as
            # Space) — apply is the explicit [a] key, never Enter, so a stray
            # Enter can't commit a merge.
            self._toggle_collapse_current()
        elif event.list_view.id == 'action-list' and self.mode in ('review', 'batch'):
            self._enter_note_mode()

    def _on__note_input_submitted(self, event: _NoteInput.Submitted) -> None:
        if self._collapse_new_name_pending:
            self._collapse_new_name_pending = False
            self.query_one('#note-section').remove_class('visible')
            new_name = (event.text or '').strip()
            proposal = self._collapse_proposal
            if not new_name or proposal is None:
                self._show_status('Merge cancelled — a non-empty name is required.', error=True)
                self.mode = 'collapse'
                self._update_footer()
                return
            self.run_worker(
                self._apply_collapse_new_async(proposal, new_name, self._collapse_included_ids()),
                exclusive=True,
                name='apply_collapse_new',
            )
            return
        self._pending_note = event.text or None
        self._submit_verdict()

    def _on__note_input_cancelled(self, event: _NoteInput.Cancelled) -> None:
        self.query_one('#note-section').remove_class('visible')
        if self._collapse_new_name_pending:
            self._collapse_new_name_pending = False
            self.mode = 'collapse'
            self._update_footer()
            return
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
        elif self.mode == 'collapse':
            self._handle_collapse_key(event)
        elif self.mode in ('review', 'batch'):
            self._handle_review_key(event)
        elif self.mode == 'detail':
            self._handle_detail_key(event)

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
        elif key == 'f':
            event.prevent_default()
            event.stop()
            self._toggle_flag_current()
        elif key == 'd':
            event.prevent_default()
            event.stop()
            self._enter_detail_mode()

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
    # COLLAPSE mode (entity_collapse_cluster member selection)
    # ------------------------------------------------------------------

    def _enter_collapse_mode(self, proposal: CockpitProposal) -> None:
        ev = proposal.raw_evidence if isinstance(proposal.raw_evidence, dict) else {}
        names = ev.get('member_canonical_names')
        if not isinstance(names, dict):
            names = {}
        members = ev.get('cluster_members')
        if not isinstance(members, list) or not members:
            members = list(names.keys())
        member_ids = [str(m) for m in members]
        if not member_ids:
            # Degenerate finding (no members to review) — don't strand the user
            # in an empty selection screen.
            self._show_status('This cluster has no members to review.', error=True)
            self.mode = 'list'
            return
        # ``or`` fallback is safe here: entity ids are always non-empty UUID
        # strings, so a present suggested_winner_id is never falsy.
        winner = str(ev.get('suggested_winner_id') or proposal.target_id)
        if winner not in member_ids:
            winner = member_ids[0]

        self._collapse_proposal = proposal
        self._collapse_winner_id = winner or None

        action_list = self.query_one('#action-list', ListView)
        action_list.clear()
        for i, mid in enumerate(member_ids):
            action_list.append(
                _CollapseMemberItem(
                    mid,
                    names.get(mid) or '(name unavailable)',
                    included=True,
                    is_winner=(mid == winner),
                    highlighted=(i == 0),
                )
            )
        if member_ids:
            action_list.index = 0
        self.mode = 'collapse'
        self._render_collapse_detail()

    def _collapse_items(self) -> list[_CollapseMemberItem]:
        return [
            c
            for c in self.query_one('#action-list', ListView).children
            if isinstance(c, _CollapseMemberItem)
        ]

    def _collapse_included_ids(self) -> list[str]:
        return [it.member_id for it in self._collapse_items() if it.included]

    def _collapse_member_name(self, member_id: str | None) -> str:
        for it in self._collapse_items():
            if it.member_id == member_id:
                return it.member_name
        return (member_id or '?')[:8]

    def _refresh_collapse_items(self) -> None:
        action_list = self.query_one('#action-list', ListView)
        for i, it in enumerate(self._collapse_items()):
            it.refresh_label(highlighted=(i == action_list.index))

    def _render_collapse_detail(self) -> None:
        items = self._collapse_items()
        included = [it for it in items if it.included]
        winner_name = _esc(self._collapse_member_name(self._collapse_winner_id))
        lines = [
            '[bold]SELECT ENTITIES TO MERGE[/bold]',
            '',
            f'Winner: [green]{winner_name}[/green]',
            f'Merging [bold]{len(included)}[/bold] of {len(items)} entities '
            'into the winner; the rest stay separate.',
            '',
            '[dim][Space] include/exclude · [w] set winner · [a] apply · '
            '[x] dismiss · [Esc] cancel[/dim]',
        ]
        self.query_one('#detail-header', Static).update('[bold]─── ENTITY COLLAPSE ───[/bold]')
        self.query_one('#detail-body', Static).update('\n'.join(lines))

    def _collapse_current_item(self) -> _CollapseMemberItem | None:
        action_list = self.query_one('#action-list', ListView)
        items = self._collapse_items()
        idx = action_list.index
        if idx is None or not (0 <= idx < len(items)):
            return None
        return items[idx]

    def _toggle_collapse_current(self) -> None:
        current = self._collapse_current_item()
        if current is None:
            return
        if current.is_winner and current.included:
            self._show_status(
                'Cannot exclude the winner — set a different winner first.', error=True
            )
            return
        current.included = not current.included
        self._refresh_collapse_items()
        self._render_collapse_detail()

    def _set_collapse_winner(self) -> None:
        current = self._collapse_current_item()
        if current is None:
            return
        current.included = True  # the winner is always part of the merge
        self._collapse_winner_id = current.member_id
        for it in self._collapse_items():
            it.is_winner = it.member_id == current.member_id
        self._refresh_collapse_items()
        self._render_collapse_detail()

    def _handle_collapse_key(self, event: Any) -> None:
        key = event.key
        if key == 'space':
            event.prevent_default()
            event.stop()
            self._toggle_collapse_current()
        elif key == 'w':
            event.prevent_default()
            event.stop()
            self._set_collapse_winner()
        elif key == 'a':
            event.prevent_default()
            event.stop()
            self._apply_collapse()
        elif key == 'n':
            event.prevent_default()
            event.stop()
            self._collapse_into_new()
        elif key == 'x':
            event.prevent_default()
            event.stop()
            self._dismiss_collapse()
        elif key == 'escape':
            event.prevent_default()
            event.stop()
            self.mode = 'list'

    def _apply_collapse(self) -> None:
        proposal = self._collapse_proposal
        if proposal is None:
            return
        included = self._collapse_included_ids()
        if len(included) < 2:
            self._show_status('Select at least 2 entities (a winner + one to merge).', error=True)
            return
        if self._collapse_winner_id not in included:
            self._show_status('The winner must be one of the selected entities.', error=True)
            return
        winner_id = self._collapse_winner_id
        winner_name = self._collapse_member_name(winner_id)
        self.run_worker(
            self._apply_collapse_async(proposal, winner_id, included, winner_name),
            exclusive=True,
            name='apply_collapse',
        )

    async def _apply_collapse_async(
        self,
        proposal: CockpitProposal,
        winner_id: str,
        member_ids: list[str],
        winner_name: str,
    ) -> None:
        try:
            await self._controller.apply_entity_collapse(
                proposal.finding_id, winner_id=winner_id, member_ids=member_ids
            )
        except Exception:  # noqa: BLE001
            logger.exception('entity-collapse apply failed for finding %s', proposal.finding_id)
            # Generic UI message — the full detail (which may contain internal
            # error strings) goes to the log, not the status bar.
            self._show_status('Collapse failed — see logs for details.', error=True)
            self.mode = 'list'
            return
        self._show_status(f'Merged {len(member_ids)} entities into "{winner_name}".')
        await self._refresh_queue()

    def _collapse_into_new(self) -> None:
        """Merge the selected members into a freshly named entity.

        Opens the note input to collect the new canonical name; submission
        resolves the finding through the ``collapse_into_new_entity``
        catalogue action with the selected member subset. NOT reversible —
        same caveat as the winner merge.
        """
        proposal = self._collapse_proposal
        if proposal is None:
            return
        included = self._collapse_included_ids()
        if len(included) < 2:
            self._show_status('Select at least 2 entities to merge into a new one.', error=True)
            return
        self._collapse_new_name_pending = True
        note_section = self.query_one('#note-section')
        note_section.add_class('visible')
        note_input = self.query_one('#note-input', _NoteInput)
        note_input.clear()
        note_input.focus()
        self.mode = 'note'
        self._show_status('Type the NEW canonical name; [Enter] merges, [Esc] cancels.')

    async def _apply_collapse_new_async(
        self,
        proposal: CockpitProposal,
        new_name: str,
        member_ids: list[str],
    ) -> None:
        params = {'new_canonical_name': new_name, 'member_ids': member_ids}
        if not await self._confirm_if_irreversible(
            proposal.finding_id, 'collapse_into_new_entity', params
        ):
            self._show_status('Cancelled — merge into new entity not confirmed.')
            self.mode = 'collapse'
            return
        option = CockpitOption(
            action_id='collapse_into_new_entity',
            label='Collapse into a new entity',
            summary='Create a new entity and fold the selected members onto it.',
            effect='Members hard-deleted; counters/links/models fold onto the new entity.',
            reversible=False,
        )
        try:
            await self._controller.resolve(
                proposal,
                option,
                note=None,
                params={'new_canonical_name': new_name, 'member_ids': member_ids},
            )
        except Exception:  # noqa: BLE001
            logger.exception('collapse-into-new-entity failed for finding %s', proposal.finding_id)
            self._show_status('Merge into new entity failed — see logs for details.', error=True)
            self.mode = 'list'
            return
        self._show_status(f'Merged {len(member_ids)} entities into new "{new_name}".')
        await self._refresh_queue()

    def _dismiss_collapse(self) -> None:
        proposal = self._collapse_proposal
        if proposal is None:
            return
        self.run_worker(
            self._dismiss_collapse_async(proposal), exclusive=True, name='dismiss_collapse'
        )

    async def _dismiss_collapse_async(self, proposal: CockpitProposal) -> None:
        try:
            await self._controller.resolve(proposal, DISMISS_OPTION, note=None)
        except Exception:  # noqa: BLE001
            logger.exception('entity-collapse dismiss failed for finding %s', proposal.finding_id)
            self._show_status('Dismiss failed — see logs for details.', error=True)
            self.mode = 'list'
            return
        self._show_status('Dismissed.')
        await self._refresh_queue()

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

    def _toggle_flag_current(self) -> None:
        proposal = self._current_proposal()
        if proposal is None:
            return
        self.run_worker(
            self._toggle_flag_async(proposal),
            exclusive=False,
            name='toggle_flag',
        )

    async def _toggle_flag_async(self, proposal: CockpitProposal) -> None:
        try:
            result = await self._controller.flag_finding(proposal.finding_id)
        except Exception as exc:  # noqa: BLE001
            self._show_status(f'Flag toggle failed: {exc}', error=True)
            return
        flagged = result.get('flagged', False)
        proposal.flagged_at = result.get('flagged_at')
        # Update the queue item label to reflect the new flag state.
        queue = self.query_one('#queue-list', ListView)
        for child in queue.children:
            if isinstance(child, _ProposalQueueItem) and child.proposal is proposal:
                child.refresh_label()
                break
        verb = 'Flagged' if flagged else 'Unflagged'
        self._show_status(f'{verb} {proposal.finding_id[:8]}…')

    # ------------------------------------------------------------------
    # DETAIL mode
    # ------------------------------------------------------------------

    def _enter_detail_mode(self) -> None:
        """Enter DETAIL drill-down for the currently highlighted finding."""
        proposal = self._current_proposal()
        if proposal is None:
            return
        self._viewing_source_note = False
        # Note-target findings (e.g. inbox routing) carry a NOTE id in target_id,
        # not a unit id — render the note directly instead of a unit lookup that
        # would 404.
        if proposal.target_type == 'note':
            self._detail_note_id = proposal.target_id
            self._detail_unit_ids = []
            self._detail_unit_index = 0
            self.mode = 'detail'
            self._load_note_detail()
            return
        self._detail_note_id = None
        # Collect all unit IDs from the finding: target first, then related.
        unit_ids: list[str] = [proposal.target_id]
        for rid in proposal.related_unit_ids:
            if rid not in unit_ids:
                unit_ids.append(rid)
        self._detail_unit_ids = unit_ids
        self._detail_unit_index = 0
        self.mode = 'detail'
        self._load_detail_for_current_unit()

    def _load_note_detail(self) -> None:
        if not self._detail_note_id:
            return
        self.query_one('#detail-header', Static).update(
            f'[bold]─── NOTE DETAIL: {self._detail_note_id[:8]} ───[/bold]'
        )
        self.query_one('#detail-body', Static).update('[dim]loading…[/dim]')
        self.run_worker(
            self._load_note_detail_async(self._detail_note_id),
            name='load_note_detail',
            exclusive=True,
        )

    async def _load_note_detail_async(self, note_id: str) -> None:
        data = await self._controller.fetch_note_detail(note_id)
        self._render_note_detail_panel(note_id, data)

    def _render_note_detail_panel(
        self, note_id: str, data: tuple[str | None, str | None] | None
    ) -> None:
        self.query_one('#detail-header', Static).update(
            f'[bold]─── NOTE DETAIL: {note_id[:8]} ───[/bold]'
        )
        if data is None:
            self.query_one('#detail-body', Static).update(
                f'[red]Could not load note {note_id}[/red]'
            )
            return
        title, text = data
        lines: list[str] = []
        # Escape note content — titles/bodies may contain [..] that Rich would
        # otherwise parse as markup tags.
        lines.append(f'  [bold]TITLE[/bold]    {_esc(title) if title else "[dim](untitled)[/dim]"}')
        lines.append(f'  [bold]NOTE ID[/bold]  {note_id}')
        lines.append('')
        lines.append('[dim]' + '─' * 60 + '[/dim]')
        lines.append('[bold]TEXT[/bold]')
        lines.append('')
        lines.append(f'  {_esc(text)}' if text else '  [dim](no text)[/dim]')
        lines.append('')
        lines.append('[dim]' + '─' * 60 + '[/dim]')
        lines.append('  [bold][Esc][/bold] Back to list')
        self.query_one('#detail-body', Static).update('\n'.join(lines))

    def _handle_detail_key(self, event: Any) -> None:
        key = event.key
        if key == 'escape':
            event.prevent_default()
            event.stop()
            if self._viewing_source_note:
                self._viewing_source_note = False
                self._load_detail_for_current_unit()
            else:
                self.mode = 'list'
        elif key == 'tab' and len(self._detail_unit_ids) > 1:
            event.prevent_default()
            event.stop()
            self._detail_unit_index = (self._detail_unit_index + 1) % len(self._detail_unit_ids)
            self._load_detail_for_current_unit()
            self._update_subtitle()
        elif key == 'up' and len(self._detail_unit_ids) > 1:
            event.prevent_default()
            event.stop()
            self._detail_unit_index = (self._detail_unit_index - 1) % len(self._detail_unit_ids)
            self._load_detail_for_current_unit()
            self._update_subtitle()
        elif key == 'down' and len(self._detail_unit_ids) > 1:
            event.prevent_default()
            event.stop()
            self._detail_unit_index = (self._detail_unit_index + 1) % len(self._detail_unit_ids)
            self._load_detail_for_current_unit()
            self._update_subtitle()
        elif key == 's':
            event.prevent_default()
            event.stop()
            self._fetch_source_note_text()

    def _load_detail_for_current_unit(self) -> None:
        if not self._detail_unit_ids:
            return
        unit_id = self._detail_unit_ids[self._detail_unit_index]
        short_id = unit_id[:8]
        n = len(self._detail_unit_ids)
        idx = self._detail_unit_index + 1
        nav = f'  (unit {idx}/{n})' if n > 1 else ''
        self.query_one('#detail-header', Static).update(
            f'[bold]─── UNIT DETAIL: {short_id} ───[/bold]{nav}'
        )
        self.query_one('#detail-body', Static).update('[dim]loading…[/dim]')
        self.run_worker(
            self._load_detail_async(unit_id),
            name='load_detail',
            exclusive=True,
        )

    async def _load_detail_async(self, unit_id: str) -> None:
        detail = await self._controller.get_unit_detail(unit_id)
        lineage = await self._controller.get_unit_lineage(unit_id)
        self._render_detail_panel(unit_id, detail, lineage)

    def _render_detail_panel(
        self,
        unit_id: str,
        detail: UnitDetail | None,
        lineage: UnitLineage,
    ) -> None:
        short_id = unit_id[:8]
        n = len(self._detail_unit_ids)
        idx = self._detail_unit_index + 1
        nav = f'  (unit {idx}/{n})' if n > 1 else ''
        self.query_one('#detail-header', Static).update(
            f'[bold]─── UNIT DETAIL: {short_id} ───[/bold]{nav}'
        )

        if detail is None:
            self.query_one('#detail-body', Static).update(
                f'[red]Could not load unit {unit_id}[/red]'
            )
            return

        lines: list[str] = []

        # --- Metadata block ---
        lines.append(f'  [bold]STATUS[/bold]    {detail.status or "unknown"}')
        if detail.is_deprioritized:
            lines.append('            [yellow]deprioritized[/yellow]')
        if detail.created_at:
            lines.append(f'  [bold]CREATED[/bold]   {detail.created_at}')
        if detail.fact_type:
            lines.append(f'  [bold]TYPE[/bold]      {detail.fact_type}')
        if detail.confidence is not None:
            lines.append(f'  [bold]CONFID.[/bold]   {detail.confidence:.2f}')

        # Source note
        if detail.note_key or detail.note_id:
            note_label = detail.note_key or detail.note_id or '?'
            created = f' (created: {detail.note_created_at})' if detail.note_created_at else ''
            lines.append(f'  [bold]SOURCE[/bold]    note: {note_label}{created}')
        if detail.chunk_index:
            lines.append(f'  [bold]CHUNK[/bold]     {detail.chunk_index} in source note')
        if detail.entities:
            lines.append(f'  [bold]ENTITIES[/bold]  {", ".join(detail.entities)}')

        # --- Full text block ---
        lines.append('')
        lines.append('[dim]' + '─' * 60 + '[/dim]')
        lines.append('[bold]FULL TEXT[/bold]')
        lines.append('')
        lines.append(f'  {detail.text}')

        # --- Lineage block ---
        lines.append('')
        lines.append('[dim]' + '─' * 60 + '[/dim]')
        lines.append('[bold]LINEAGE[/bold]')
        lines.append('')
        if lineage.upstream:
            for uid, label in lineage.upstream:
                lines.append(f'  [dim]upstream:[/dim]  unit {uid} — "{label}"')
        else:
            lines.append('  [dim]upstream:   (none)[/dim]')
        if lineage.downstream:
            for uid, label in lineage.downstream:
                lines.append(f'  [dim]downstream:[/dim] unit {uid} — "{label}"')
        else:
            lines.append('  [dim]downstream: (none)[/dim]')

        # --- Actions block ---
        lines.append('')
        lines.append('[dim]' + '─' * 60 + '[/dim]')
        lines.append('[bold]ACTIONS[/bold]')
        lines.append('')
        action_parts: list[str] = ['  [bold][s][/bold] View source note']
        if len(self._detail_unit_ids) > 1:
            action_parts.append('  [bold][Tab/↑/↓][/bold] Navigate units')
        action_parts.append('  [bold][Esc][/bold] Back to list')
        lines.append('  '.join(action_parts))

        self.query_one('#detail-body', Static).update('\n'.join(lines))

    def _fetch_source_note_text(self) -> None:
        """Fetch and display the source note text for the current detail unit."""
        if not self._detail_unit_ids:
            return
        unit_id = self._detail_unit_ids[self._detail_unit_index]
        self.run_worker(
            self._fetch_source_note_text_async(unit_id),
            name='fetch_source_note',
            exclusive=True,
        )

    async def _fetch_source_note_text_async(self, unit_id: str) -> None:
        detail = await self._controller.get_unit_detail(unit_id)
        if detail is None or detail.note_id is None:
            self._show_status('No source note linked to this unit.', error=True)
            return
        note_text = await self._controller.fetch_note_text(detail.note_id)
        if note_text is None:
            self._show_status('Could not load source note text.', error=True)
            return

        self._viewing_source_note = True
        note_label = detail.note_key or detail.note_id

        self.query_one('#detail-header', Static).update(
            f'[bold]─── SOURCE NOTE: {note_label} ───[/bold]'
        )
        body = self.query_one('#detail-body', Static)
        body.update(RichMarkdown(note_text))

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
            if proposal.rule_name == 'entity_collapse_cluster':
                # Interactive member selection instead of the dismiss-only menu.
                self._enter_collapse_mode(proposal)
                return
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

    async def _flag_one(self, proposal: CockpitProposal) -> tuple[bool, dict[str, Any] | None]:
        """Toggle the flag bookmark for one proposal. Returns (ok, result)."""
        try:
            result = await self._controller.flag_finding(proposal.finding_id)
        except Exception:  # noqa: BLE001
            return False, None
        proposal.flagged_at = result.get('flagged_at')
        return True, result

    async def _resolve_one(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        note: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Execute a non-flag verdict for one proposal. Returns (result, error)."""
        try:
            result = await self._controller.resolve(
                proposal, option, note=note, params=option.params
            )
            return result, None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    async def _submit_single_async(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        note: str | None,
    ) -> None:
        # Flag is orthogonal — call the flag endpoint and stay in LIST mode
        # without removing the finding from the queue.
        if option.verb == 'flag':
            ok, result = await self._flag_one(proposal)
            if not ok:
                self._show_status('Flag toggle failed.', error=True)
                self.mode = 'list'
                return
            # Update the queue item label.
            queue = self.query_one('#queue-list', ListView)
            for child in queue.children:
                if isinstance(child, _ProposalQueueItem) and child.proposal is proposal:
                    child.refresh_label()
                    break
            verb = 'Flagged' if (result or {}).get('flagged', False) else 'Unflagged'
            self._show_status(f'{verb} {proposal.finding_id[:8]}…')
            self.mode = 'list'
            return

        if option.verb == 'resolve' and not await self._confirm_if_irreversible(
            proposal.finding_id, option.action_id, option.params
        ):
            self._show_status('Cancelled — irreversible action not confirmed.')
            self.mode = 'list'
            return

        result, error = await self._resolve_one(proposal, option, note)
        if error is not None:
            self._show_status(f'Action failed: {error}', error=True)
            self.mode = 'list'
            return
        assert result is not None
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
        if option.verb == 'flag':
            for proposal in proposals:
                flag_ok, _ = await self._flag_one(proposal)
                if flag_ok:
                    ok += 1
                else:
                    fail += 1
            parts: list[str] = []
            if ok:
                parts.append(f'{ok} flagged')
            if fail:
                parts.append(f'{fail} failed')
            self._show_status(', '.join(parts), error=bool(fail))
            await self._refresh_queue()
            return
        skipped = 0
        needs_review = 0
        for proposal in proposals:
            # For "accept recommended", resolve each proposal with ITS OWN option
            # so proposal-specific params (e.g. an inbox route's target_vault_id)
            # are not dropped. For the uniform verbs (no_op / dismiss) the shared
            # option carries no params and applies to every proposal as-is.
            if option.action_id == _BATCH_RECOMMENDED_ACTION_ID:
                per_option = recommended_resolve_option(proposal)
                if per_option is None:
                    skipped += 1
                    continue
            else:
                per_option = option
            # Irreversible actions never execute from a batch — each one needs
            # the single-review blast-radius confirmation.
            if (
                per_option.verb == 'resolve'
                and per_option.action_id
                and action_is_reversible(per_option.action_id) is False
            ):
                needs_review += 1
                continue
            _, error = await self._resolve_one(proposal, per_option, note)
            if error is None:
                ok += 1
            else:
                fail += 1
        parts = []
        if ok:
            parts.append(f'{ok} resolved')
        if skipped:
            parts.append(f'{skipped} skipped (no recommended action)')
        if needs_review:
            parts.append(f'{needs_review} skipped (irreversible — review singly)')
        if fail:
            parts.append(f'{fail} failed')
        self._show_status(', '.join(parts), error=bool(fail))
        await self._refresh_queue()

    async def _confirm_if_irreversible(
        self,
        finding_id: str,
        action_id: str,
        params: dict[str, Any] | None,
    ) -> bool:
        """Blast-radius confirm gate; True when execution may proceed.

        Reversible / unknown-to-catalogue actions pass through (the server
        re-validates everything); forward-only actions fetch the live
        preview and require an explicit [y].
        """
        if not action_id or action_is_reversible(action_id) is not False:
            return True
        preview = await self._controller.preview_action(
            finding_id, action_id=action_id, params=params
        )
        text = preview or (
            f'{action_id} cannot be undone (live preview unavailable — '
            'the server may predate the preview endpoint).'
        )
        confirmed = await self.push_screen_wait(ConfirmIrreversibleScreen(action_id, text))
        return bool(confirmed)

    # ------------------------------------------------------------------
    # App-level actions
    # ------------------------------------------------------------------

    async def action_refresh(self) -> None:
        await self._refresh_queue()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_flag(self) -> None:
        if self.mode == 'list':
            self._toggle_flag_current()

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
