"""Rich terminal output and JSON export for benchmark results."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from memex_eval.metrics import BenchmarkResult, CheckStatus

console = Console()

_STATUS_STYLES = {
    CheckStatus.PASS: ('PASS', 'green'),
    CheckStatus.FAIL: ('FAIL', 'red'),
    CheckStatus.SKIP: ('SKIP', 'yellow'),
    CheckStatus.ERROR: ('ERR', 'red bold'),
}


def print_report(result: BenchmarkResult) -> None:
    """Print a rich terminal report of benchmark results."""
    console.print()
    console.rule('[bold]Memex Quality Benchmark Results[/bold]')
    console.print()

    for group in result.groups:
        _print_group(group)

    _print_summary(result)


def _print_group(group) -> None:
    """Print results for a single scenario group."""
    table = Table(
        title=f'{group.name} — {group.description}',
        show_header=True,
        header_style='bold cyan',
        title_style='bold',
        expand=True,
    )
    table.add_column('Check', style='white', ratio=2)
    table.add_column('Status', justify='center', width=6)
    table.add_column('Query', ratio=2)
    table.add_column('Expected', ratio=2)
    table.add_column('Actual', ratio=2)
    table.add_column('Time', justify='right', width=8)

    for check in group.checks:
        label, style = _STATUS_STYLES.get(check.status, ('???', 'white'))
        status_text = Text(label, style=style)
        expected_str = (
            ', '.join(check.expected) if isinstance(check.expected, list) else check.expected
        )
        actual_display = check.actual[:80] + '...' if len(check.actual) > 80 else check.actual
        table.add_row(
            check.name,
            status_text,
            check.query[:60],
            expected_str[:60],
            actual_display,
            f'{check.duration_ms:.0f}ms',
        )

    console.print(table)

    timing_parts = []
    if group.ingest_duration_ms > 0:
        timing_parts.append(f'Ingest: {group.ingest_duration_ms:.0f}ms')
    if group.reflection_duration_ms > 0:
        timing_parts.append(f'Reflection: {group.reflection_duration_ms:.0f}ms')
    if timing_parts:
        console.print(f'  [dim]{" | ".join(timing_parts)}[/dim]')
    console.print()


def _print_summary(result: BenchmarkResult) -> None:
    """Print aggregate summary."""
    pass_style = 'green' if result.overall_pass_rate >= 0.8 else 'yellow'
    if result.overall_pass_rate < 0.5:
        pass_style = 'red'

    summary = (
        f'[bold]Total:[/bold] {result.total_checks} checks | '
        f'[green]{result.total_passed} passed[/green] | '
        f'[red]{result.total_failed} failed[/red] | '
        f'[yellow]{result.total_skipped} skipped[/yellow] | '
        f'[red]{result.total_errored} errors[/red]\n'
        f'[bold]Pass rate:[/bold] [{pass_style}]{result.overall_pass_rate:.1%}[/{pass_style}] | '
        f'[bold]Duration:[/bold] {result.duration_ms:.0f}ms'
    )

    console.print(Panel(summary, title='Summary', border_style='bold'))
    console.print()


def export_json(result: BenchmarkResult, output_path: str | Path) -> None:
    """Export benchmark results to a JSON file."""
    path = Path(output_path)
    path.write_text(json.dumps(result.to_dict(), indent=2))
    console.print(f'[dim]Results exported to {path}[/dim]')
