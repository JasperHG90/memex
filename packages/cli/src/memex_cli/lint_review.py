"""F7 — interactive prompt loop for ``memex lint review``.

Pure UX layer. Reads pending findings via the same client method as
``memex lint findings``; for each one, renders a per-finding card and
asks the user for a verdict. Resolutions go through the existing
``api.lint_dismiss`` / ``api.lint_resolve`` paths so the audit footprint
(``maintenance_proposals.status`` flip + ``resolved_at`` + ``resolved_by``
columns) is identical to the corresponding direct CLI subcommand.

Defaults to dry-run preview. ``--apply`` is required to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


_VALID_KEYS = {'a', 'd', 's', 'q'}
_DEFAULT_KEY = 's'


class LintApplyProto(Protocol):
    """Minimal API surface ``run_review_loop`` writes through.

    Narrower than the full ``RemoteMemexAPI`` so type-checkers catch
    signature drift between this CLI and the API client. Both methods
    return a dict from the resolve/dismiss endpoint; ``run_review_loop``
    ignores the payload and only cares about success vs raised exception.
    """

    async def lint_resolve(self, finding_id: str) -> dict[str, Any]: ...

    async def lint_dismiss(self, finding_id: str) -> dict[str, Any]: ...


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


def _render_finding(console: Console, index: int, total: int, finding: dict[str, Any]) -> None:
    """Render a single finding as a rich panel: header + evidence + suggested action."""
    rule = finding.get('rule_name', '?')
    lint_type = finding.get('lint_type', '?')
    target_type = finding.get('target_type', '?')
    target_id = finding.get('target_id', '?')
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
    body.add_row('target', str(target_id))
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


async def run_review_loop(
    findings: list[dict[str, Any]],
    *,
    apply: bool,
    api: LintApplyProto,
    console: Console,
) -> ReviewSummary:
    """Walk the user through ``findings``; collect verdicts; optionally apply.

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
        verdict = _prompt_verdict(console)
        finding_id = finding.get('id', '')

        if verdict == 'q':
            summary.quit_early = True
            console.print('[dim]Quitting review.[/dim]')
            break
        if verdict == 's':
            summary.skipped.append(finding_id)
            continue
        if verdict == 'a':
            summary.accepted.append(finding_id)
            if apply:
                try:
                    await api.lint_resolve(finding_id)
                    summary.applied_resolved.append(finding_id)
                    console.print(f'[green]resolved:[/green] {finding_id}')
                except Exception as e:
                    summary.apply_errors.append((finding_id, str(e)))
                    console.print(f'[red]apply failed for {finding_id}:[/red] {e}')
            continue
        if verdict == 'd':
            summary.dismissed.append(finding_id)
            if apply:
                try:
                    await api.lint_dismiss(finding_id)
                    summary.applied_dismissed.append(finding_id)
                    console.print(f'[yellow]dismissed:[/yellow] {finding_id}')
                except Exception as e:
                    summary.apply_errors.append((finding_id, str(e)))
                    console.print(f'[red]apply failed for {finding_id}:[/red] {e}')
            continue

    return summary


def render_summary(console: Console, summary: ReviewSummary, *, apply: bool) -> None:
    """Print a final summary table and a per-mode footer."""
    table = Table(title='lint review summary')
    table.add_column('verdict')
    table.add_column('count', justify='right')
    table.add_row('accepted (would resolve)', str(len(summary.accepted)))
    table.add_row('dismissed (would dismiss)', str(len(summary.dismissed)))
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
