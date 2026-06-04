"""Interactive prompt loop for ``memex lint review``.

Pure UX layer. Reads pending findings via the same client method as
``memex lint findings``; for each one, renders a per-finding card and
asks the user for a verdict. Resolutions go through the existing
``api.lint_dismiss`` / ``api.lint_resolve`` paths so the audit footprint
(``maintenance_proposals.status`` flip + ``resolved_at`` + ``resolved_by``
columns) is identical to the corresponding direct CLI subcommand.

Defaults to dry-run preview. ``--apply`` is required to write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import logging
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


_VALID_KEYS = {'a', 'd', 's', 'q'}
_DEFAULT_KEY = 's'

_logger = logging.getLogger(__name__)


class LintApplyProto(Protocol):
    """Minimal API surface ``run_review_loop`` writes through.

    Narrower than the full ``RemoteMemexAPI`` so type-checkers catch
    signature drift between this CLI and the API client. Both methods
    return a dict from the resolve/dismiss endpoint; ``run_review_loop``
    ignores the payload and only cares about success vs raised exception.
    """

    async def lint_resolve(
        self,
        finding_id: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]: ...

    async def lint_dismiss(self, finding_id: str) -> dict[str, Any]: ...

    async def lint_preview_action(
        self,
        finding_id: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class ReviewSummary:
    """Verdicts collected from a single review session."""

    accepted: list[str] = field(default_factory=list)
    dismissed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    applied_resolved: list[str] = field(default_factory=list)
    applied_dismissed: list[str] = field(default_factory=list)
    apply_errors: list[tuple[str, str]] = field(default_factory=list)
    quit_early: bool = False

    @property
    def reviewed(self) -> int:
        return len(self.accepted) + len(self.dismissed) + len(self.skipped)


def finding_target_label(finding: dict[str, Any], *, max_len: int = 80) -> str:
    """Best-effort human-readable label for a finding's target.

    A finding's ``target_id`` is an opaque UUID — useless for review on its
    own. Prefer, in order: the server-resolved ``target_label`` (note title /
    entity / mental-model name), then evidence-embedded names, then a
    unit-text snippet, and finally a truncated ``target_id`` so the reviewer
    always sees *something* legible.
    """
    label: Any = finding.get('target_label')
    if not label:
        evidence = finding.get('evidence') or {}
        if isinstance(evidence, dict):
            names = evidence.get('member_canonical_names') or evidence.get('canonical_names')
            if isinstance(names, dict):
                names = list(names.values())
            if isinstance(names, list) and names:
                label = ', '.join(str(n) for n in names)
            else:
                label = evidence.get('entity_name')
    if not label:
        label = finding.get('target_text')
    if not label:
        tid = str(finding.get('target_id') or '?')
        return tid[:8] + '…' if len(tid) > 9 else tid
    collapsed = ' '.join(str(label).split())
    return collapsed if len(collapsed) <= max_len else collapsed[: max_len - 1] + '…'


def _render_finding(console: Console, index: int, total: int, finding: dict[str, Any]) -> None:
    """Render a single finding as a rich panel: header + evidence + suggested action."""
    rule = finding.get('rule_name', '?')
    lint_type = finding.get('lint_type', '?')
    target_type = finding.get('target_type', '?')
    target_id = finding.get('target_id', '?')
    target_text = finding.get('target_text')
    vault_id = finding.get('vault_id') or '(global)'
    created_at = finding.get('created_at', '')
    suggested_action = finding.get('suggested_action', '')
    evidence = finding.get('evidence') or {}

    header = (
        f'[bold]{rule}[/bold]  '
        f'[cyan]{lint_type}[/cyan] / [dim]{target_type}[/dim]  '
        f'[dim]vault={vault_id}[/dim]'
    )

    body = Table.grid(padding=(0, 1))
    body.add_column(style='dim', no_wrap=True)
    body.add_column(overflow='fold')
    body.add_row('id', finding.get('id', '?'))
    body.add_row('target', finding_target_label(finding, max_len=200))
    body.add_row('target_id', str(target_id))
    if target_text:
        body.add_row('text', str(target_text))
    if created_at:
        body.add_row('created', str(created_at))
    if evidence:
        for k, v in evidence.items():
            body.add_row(str(k), str(v))
    if suggested_action:
        body.add_row('action', suggested_action)

    console.print(
        Panel(
            body,
            title=f'[{index}/{total}] {header}',
            title_align='left',
            border_style='yellow',
        )
    )


def _prompt_verdict(console: Console) -> str:
    """Single-keypress verdict prompt. Returns one of {a, d, s, q}.

    Re-prompts on invalid input. Empty input (Enter) defaults to skip.
    """
    while True:
        raw = typer.prompt(
            '[a]ccept / [d]ismiss / [s]kip / [q]uit',
            default=_DEFAULT_KEY,
            show_default=True,
        )
        key = (raw or _DEFAULT_KEY).strip().lower()[:1]
        if key in _VALID_KEYS:
            return key
        console.print('[red]Invalid choice. Pick one of a / d / s / q.[/red]')


def _prefill_for_action(action_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    """Params an action can take from the finding itself — evidence-derived.

    The entity-merge actions are filled from the collapse finding's cluster
    evidence (member ids + suggested winner); ``kv_delete`` defaults to the
    finding target (the KV key); a submitter's ``proposed_action`` params are
    reused verbatim when the reviewer picks the same action.
    """
    evidence = finding.get('evidence') or {}
    if not isinstance(evidence, dict):
        evidence = {}
    if action_id in ('merge_entities', 'collapse_into_new_entity'):
        members = evidence.get('cluster_members') or []
        prefill: dict[str, Any] = {}
        if isinstance(members, list) and members:
            prefill['member_ids'] = [str(m) for m in members]
        if action_id == 'merge_entities':
            winner = evidence.get('suggested_winner_id')
            if winner:
                prefill['winner_id'] = str(winner)
        return prefill
    if action_id == 'kv_delete':
        target = finding.get('target_id')
        return {'key': str(target)} if target else {}
    suggestion = evidence.get('proposed_action')
    if (
        isinstance(suggestion, dict)
        and suggestion.get('action_name') == action_id
        and isinstance(suggestion.get('params'), dict)
    ):
        return dict(suggestion['params'])
    return {}


def _prompt_params_from_schema(
    console: Console,
    schema: dict[str, Any] | None,
    prefill: dict[str, Any],
) -> dict[str, Any]:
    """Collect action params, walking the published JSON schema.

    Prefilled (evidence-derived) values are shown and kept; missing required
    fields are prompted; optional fields prompt with empty-skip. Array-typed
    fields accept comma-separated input. Falls back to the prefill alone
    when the schema is unavailable (offline catalogue).
    """
    params = dict(prefill)
    if not schema:
        return params
    props = schema.get('properties') or {}
    required = set(schema.get('required') or [])
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        if key in params:
            console.print(f'  [dim]{key} = {params[key]!r} (from finding evidence)[/dim]')
            continue
        desc = str(spec.get('description') or '').strip()
        enum = spec.get('enum')
        if isinstance(enum, list) and enum:
            desc = f'{desc} [{" | ".join(str(e) for e in enum)}]'.strip()
        label = f'{key}' + (f' — {desc}' if desc else '')
        is_array = spec.get('type') == 'array'
        if key in required:
            raw = typer.prompt(f'  {label}')
        else:
            raw = typer.prompt(f'  {label} (optional)', default='', show_default=False)
            if raw == '':
                continue
        if is_array:
            params[key] = [part.strip() for part in raw.split(',') if part.strip()]
        else:
            params[key] = raw
    return params


def _action_descriptor_index(
    catalogue: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(catalogue, list) or not catalogue:
        return {}
    return {str(a.get('id')): a for a in catalogue if isinstance(a, dict) and a.get('id')}


def _build_action_choices(
    finding: dict[str, Any],
    catalogue: list[dict[str, Any]] | None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Ordered ``(label, descriptor)`` menu rows for the accept sub-menu.

    Row 0 is always the plain status flip (no action). A submitter's
    ``proposed_action`` follows as the marked suggestion; the rest of the
    catalogue (filtered to this target_type) trails alphabetically.
    """
    choices: list[tuple[str, dict[str, Any] | None]] = [
        ('Resolve only (no action; status flip + note)', None)
    ]
    index = _action_descriptor_index(catalogue)
    target_type = str(finding.get('target_type') or '')
    evidence = finding.get('evidence') or {}
    suggested_id: str | None = None
    if isinstance(evidence, dict):
        suggestion = evidence.get('proposed_action')
        if isinstance(suggestion, dict):
            candidate = str(suggestion.get('action_name') or '')
            if candidate in index:
                suggested_id = candidate
    if suggested_id:
        descriptor = index[suggested_id]
        choices.append((f'{suggested_id} — suggested by submitter', descriptor))
    for action_id in sorted(index):
        if action_id == suggested_id:
            continue
        descriptor = index[action_id]
        types = descriptor.get('applicable_target_types') or []
        if target_type not in types:
            continue
        marker = '' if descriptor.get('reversible') else ' [NOT reversible]'
        choices.append((f'{action_id}{marker}', descriptor))
    return choices


async def _accept_with_action(
    console: Console,
    finding: dict[str, Any],
    finding_id: str,
    catalogue: list[dict[str, Any]] | None,
    *,
    apply: bool,
    api: LintApplyProto,
) -> tuple[bool, str | None]:
    """Accept flow: action sub-menu → params → preview gate → resolve.

    Returns ``(applied_ok, error)``; ``applied_ok`` is True in dry-run too
    (the verdict was collected; nothing was written).
    """
    choices = _build_action_choices(finding, catalogue)
    if len(choices) > 1:
        console.print('[bold]Resolve with:[/bold]')
        for i, (label, _) in enumerate(choices):
            console.print(f'  [{i}] {label}')
        raw = await asyncio.to_thread(typer.prompt, 'action #', default='0', show_default=True)
        try:
            picked = choices[int(str(raw).strip() or '0')]
        except (ValueError, IndexError):
            console.print('[red]Invalid pick — falling back to plain resolve.[/red]')
            picked = choices[0]
    else:
        picked = choices[0]

    _, descriptor = picked
    action_id: str | None = None
    params: dict[str, Any] | None = None
    if descriptor is not None:
        action_id = str(descriptor.get('id'))
        prefill = _prefill_for_action(action_id, finding)
        schema = descriptor.get('params_schema')
        params = await asyncio.to_thread(_prompt_params_from_schema, console, schema, prefill)
        if not descriptor.get('reversible'):
            preview_text: str | None = None
            try:
                payload = await api.lint_preview_action(finding_id, action=action_id, params=params)
                preview_text = str(payload.get('preview') or '')
            except Exception as e:  # noqa: BLE001 - preview is advisory
                console.print(f'[dim]preview unavailable: {e}[/dim]')
            if preview_text:
                console.print(Panel(preview_text, border_style='red', title='blast radius'))
            confirmed = await asyncio.to_thread(
                typer.confirm,
                f'{action_id} is NOT reversible. Execute?',
                False,
            )
            if not confirmed:
                console.print('[dim]Cancelled — falling back to plain resolve.[/dim]')
                action_id = None
                params = None

    if not apply:
        chosen = action_id or 'resolve-only'
        console.print(f'[dim]dry-run: would resolve {finding_id} via {chosen}[/dim]')
        return True, None
    try:
        if action_id is None:
            await api.lint_resolve(finding_id)
        else:
            await api.lint_resolve(finding_id, action=action_id, params=params)
        suffix = f' (action={action_id})' if action_id else ''
        console.print(f'[green]resolved:[/green] {finding_id}{suffix}')
        return True, None
    except Exception as e:
        _logger.exception(
            'lint_review.apply_failed: finding_id=%s verdict=accept action=%s',
            finding_id,
            action_id,
        )
        console.print(f'[red]apply failed for {finding_id}:[/red] {e}')
        return False, str(e)


async def run_review_loop(
    findings: list[dict[str, Any]],
    *,
    apply: bool,
    api: LintApplyProto,
    console: Console,
    catalogue: list[dict[str, Any]] | None = None,
) -> ReviewSummary:
    """Walk the user through ``findings``; collect verdicts; optionally apply.

    ``catalogue`` is the closed proposal-action catalogue from
    ``GET /lint/actions``; when present, accepting a finding opens an
    action sub-menu (with schema-driven params prompts and a blast-radius
    preview gate on irreversible actions). Without it, accept degrades to
    the plain status flip.

    Per-finding errors when ``apply=True`` are captured into ``summary.apply_errors``
    and the loop continues — one bad finding shouldn't abort the session.
    """
    summary = ReviewSummary()
    total = len(findings)
    if total == 0:
        console.print('[dim]No pending findings to review.[/dim]')
        return summary

    for idx, finding in enumerate(findings, start=1):
        _render_finding(console, idx, total, finding)
        finding_id = finding.get('id') or ''
        if not finding_id:
            console.print(f'[red]Finding at position {idx} is missing ``id`` — skipping.[/red]')
            summary.skipped.append('')
            continue
        verdict = await asyncio.to_thread(_prompt_verdict, console)

        if verdict == 'q':
            summary.quit_early = True
            console.print('[dim]Quitting review.[/dim]')
            break
        if verdict == 's':
            summary.skipped.append(finding_id)
            continue
        if verdict == 'a':
            summary.accepted.append(finding_id)
            applied_ok, error = await _accept_with_action(
                console, finding, finding_id, catalogue, apply=apply, api=api
            )
            if apply:
                if applied_ok:
                    summary.applied_resolved.append(finding_id)
                elif error is not None:
                    summary.apply_errors.append((finding_id, error))
            continue
        if verdict == 'd':
            summary.dismissed.append(finding_id)
            if apply:
                try:
                    await api.lint_dismiss(finding_id)
                    summary.applied_dismissed.append(finding_id)
                    console.print(f'[yellow]dismissed:[/yellow] {finding_id}')
                except Exception as e:
                    _logger.exception(
                        'lint_review.apply_failed: finding_id=%s verdict=dismiss',
                        finding_id,
                    )
                    summary.apply_errors.append((finding_id, str(e)))
                    console.print(f'[red]apply failed for {finding_id}:[/red] {e}')
            continue

    return summary


def render_summary(console: Console, summary: ReviewSummary, *, apply: bool) -> None:
    """Print a final summary table and a per-mode footer."""
    table = Table(title='lint review summary')
    table.add_column('verdict')
    table.add_column('count', justify='right')
    accept_label = 'accepted' if apply else 'accepted (would resolve)'
    dismiss_label = 'dismissed' if apply else 'dismissed (would dismiss)'
    table.add_row(accept_label, str(len(summary.accepted)))
    table.add_row(dismiss_label, str(len(summary.dismissed)))
    table.add_row('skipped', str(len(summary.skipped)))
    if apply:
        table.add_row('applied (resolved)', str(len(summary.applied_resolved)))
        table.add_row('applied (dismissed)', str(len(summary.applied_dismissed)))
        if summary.apply_errors:
            table.add_row('apply errors', str(len(summary.apply_errors)))
    console.print(table)

    if not apply:
        would = len(summary.accepted) + len(summary.dismissed)
        console.print(
            f'[dim]Dry-run: would have applied {would} actions. Re-run with --apply to write.[/dim]'
        )
    if summary.quit_early:
        console.print('[dim]Session ended early via quit.[/dim]')
