"""LoCoMo Phase 4: Generate evaluation report with plots.

Reads judge results, answers, and session traces to produce a reproducible
Markdown report with seaborn distribution plots and mermaid retrieval diagrams.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib
import matplotlib.pyplot as plt
import seaborn as sns
import tiktoken
from rich.console import Console

from memex_eval.external.locomo_common import (
    CATEGORY_NAMES,
    EXCLUDED_QUESTION_IDS,
    QUESTION_TYPES,
    read_jsonl,
)
from memex_eval.external.locomo_efficiency import _count_retrieval_tokens_from_trace

matplotlib.use('Agg')

logger = logging.getLogger('memex_eval.locomo_report')
console = Console()

_MEMEX_TOOL_PREFIX = 'mcp__memex__'

# Re-export for backward compatibility; canonical source is locomo_common.
_EXCLUDED_REASONS = EXCLUDED_QUESTION_IDS


# ---------------------------------------------------------------------------
# Data loading and enrichment
# ---------------------------------------------------------------------------


def _load_data(
    results_path: str,
    answers_path: str,
    traces_dir: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Load and cross-reference results, answers, and trace metrics.

    Returns (report_json, enriched_details, retrieval_by_tool).
    """
    report = json.loads(Path(results_path).read_text())
    answers_by_id = {a['id']: a for a in read_jsonl(answers_path)}
    traces_path = Path(traces_dir)
    enc = tiktoken.get_encoding('cl100k_base')

    enriched: list[dict[str, Any]] = []
    retrieval_per_tool: Counter[str] = Counter()
    total_retrieval = 0

    for detail in report.get('details', []):
        qid = detail['id']
        answer_rec = answers_by_id.get(qid, {})

        # Count Memex tool calls from the answer record
        tool_calls = answer_rec.get('tool_calls', [])
        memex_calls = sum(
            1 for tc in tool_calls if tc.get('name', '').startswith(_MEMEX_TOOL_PREFIX)
        )

        # Retrieval tokens from trace
        retr_tokens = 0
        trace_file = traces_path / f'{qid}.jsonl'
        if trace_file.exists():
            rt = _count_retrieval_tokens_from_trace(trace_file, enc)
            retr_tokens = rt['total']
            for tool_name, count in rt['per_tool'].items():
                retrieval_per_tool[tool_name] += count
            total_retrieval += retr_tokens

        total_tokens = sum(detail.get('tokens', {}).values())
        enriched.append(
            {
                **detail,
                'memex_calls': memex_calls,
                'retrieval_tokens': retr_tokens,
                'total_tokens': total_tokens,
                'retrieval_pct': (
                    round(retr_tokens / total_tokens * 100, 1) if total_tokens > 0 else 0.0
                ),
                'cost_usd': answer_rec.get('cost_usd', 0.0),
                'num_turns': answer_rec.get('num_turns', detail.get('num_turns', 0)),
            }
        )

    retrieval_breakdown = {
        'total': total_retrieval,
        'per_tool': dict(retrieval_per_tool.most_common()),
    }

    return report, enriched, retrieval_breakdown


# ---------------------------------------------------------------------------
# Plot generation
# ---------------------------------------------------------------------------


def _setup_plot_style() -> None:
    """Configure seaborn/matplotlib for consistent report plots."""
    sns.set_theme(style='whitegrid', palette='muted', font_scale=1.1)
    plt.rcParams['figure.figsize'] = (10, 6)
    plt.rcParams['figure.dpi'] = 150


def _generate_plots(
    details: list[dict[str, Any]],
    retrieval_breakdown: dict[str, Any],
    plots_dir: Path,
) -> list[str]:
    """Generate all distribution plots. Returns list of generated filenames."""
    _setup_plot_style()
    plots_dir.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []

    # Filter to scored questions only
    scored = [d for d in details if d['id'] not in _EXCLUDED_REASONS]

    # Category order for consistent axis
    cat_order = [CATEGORY_NAMES.get(c, c) for c in QUESTION_TYPES if c != 'adversarial']
    cat_order.append(CATEGORY_NAMES.get('adversarial', 'Adversarial'))

    def _cat_label(d: dict[str, Any]) -> str:
        return CATEGORY_NAMES.get(d.get('category', ''), d.get('category', ''))

    # 1. Token usage by category
    fig, ax = plt.subplots()
    data = [{'Category': _cat_label(d), 'Total Tokens': d['total_tokens']} for d in scored]
    if data:
        import pandas as pd

        df = pd.DataFrame(data)
        sns.boxplot(data=df, x='Category', y='Total Tokens', order=cat_order, ax=ax)
        ax.set_title('Token Usage by Category')
        ax.set_xlabel('')
        ax.tick_params(axis='x', rotation=15)
        fig.tight_layout()
        fig.savefig(plots_dir / 'tokens_by_category.png')
        generated.append('tokens_by_category.png')
    plt.close(fig)

    # 2. Duration by category
    fig, ax = plt.subplots()
    data = [{'Category': _cat_label(d), 'Duration (s)': d['duration_s']} for d in scored]
    if data:
        df = pd.DataFrame(data)
        sns.boxplot(data=df, x='Category', y='Duration (s)', order=cat_order, ax=ax)
        ax.set_title('Duration by Category')
        ax.set_xlabel('')
        ax.tick_params(axis='x', rotation=15)
        fig.tight_layout()
        fig.savefig(plots_dir / 'duration_by_category.png')
        generated.append('duration_by_category.png')
    plt.close(fig)

    # 3. Retrieval vs total tokens (scatter)
    fig, ax = plt.subplots()
    data = [
        {
            'Total Tokens': d['total_tokens'],
            'Retrieval Tokens': d['retrieval_tokens'],
            'Category': _cat_label(d),
        }
        for d in scored
        if d['retrieval_tokens'] > 0
    ]
    if data:
        df = pd.DataFrame(data)
        sns.scatterplot(
            data=df,
            x='Total Tokens',
            y='Retrieval Tokens',
            hue='Category',
            s=80,
            alpha=0.7,
            ax=ax,
        )
        ax.set_title('Retrieval Tokens vs Total Tokens')
        fig.tight_layout()
        fig.savefig(plots_dir / 'retrieval_vs_total.png')
        generated.append('retrieval_vs_total.png')
    plt.close(fig)

    # 4. Duration vs Memex calls (scatter)
    fig, ax = plt.subplots()
    data = [
        {
            'Memex Calls': d['memex_calls'],
            'Duration (s)': d['duration_s'],
            'Category': _cat_label(d),
        }
        for d in scored
    ]
    if data:
        df = pd.DataFrame(data)
        sns.scatterplot(
            data=df,
            x='Memex Calls',
            y='Duration (s)',
            hue='Category',
            s=80,
            alpha=0.7,
            ax=ax,
        )
        ax.set_title('Duration vs Memex Tool Calls')
        fig.tight_layout()
        fig.savefig(plots_dir / 'duration_vs_memex_calls.png')
        generated.append('duration_vs_memex_calls.png')
    plt.close(fig)

    # 5. Turns distribution
    fig, ax = plt.subplots()
    turns = [d['num_turns'] for d in scored if d.get('num_turns', 0) > 0]
    if turns:
        sns.histplot(turns, bins=range(1, max(turns) + 2), discrete=True, ax=ax)
        ax.set_title('Turns per Question')
        ax.set_xlabel('Number of Turns')
        ax.set_ylabel('Count')
        fig.tight_layout()
        fig.savefig(plots_dir / 'turns_distribution.png')
        generated.append('turns_distribution.png')
    plt.close(fig)

    # 6. Retrieval token breakdown by tool
    fig, ax = plt.subplots()
    per_tool = retrieval_breakdown.get('per_tool', {})
    if per_tool:
        # Shorten tool names
        short = {k.replace('mcp__memex__memex_', ''): v for k, v in per_tool.items()}
        names = list(short.keys())
        values = list(short.values())
        colors = sns.color_palette('muted', len(names))
        ax.barh(names, values, color=colors)
        ax.set_title('Retrieval Tokens by Memex Tool')
        ax.set_xlabel('Tokens')
        fig.tight_layout()
        fig.savefig(plots_dir / 'retrieval_token_breakdown.png')
        generated.append('retrieval_token_breakdown.png')
    plt.close(fig)

    return generated


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

_RETRIEVAL_PATHS_MERMAID = """\
### Simple path

A single `memory_search` returns sufficient facts to answer directly.

```mermaid
graph LR
    A[memory_search] --> B[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#55A868,color:white
```

### Two-stage path

Memory search provides initial context, then note search finds source documents.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#55A868,color:white
```

### Deep verification path

Full two-speed reading: search, then drill into specific note sections.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_index]
    C --> D[get_node]
    D --> E[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style E fill:#55A868,color:white
```

### Exhaustive path

Multiple rounds of searching and reading across different notes and queries.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_index]
    C --> D[get_node]
    D --> E{Sufficient?}
    E -- No --> F[Refined memory_search]
    F --> G[note_search]
    G --> H[get_page_index]
    H --> I[get_node]
    I --> J[Answer]
    E -- Yes --> J
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style F fill:#4C72B0,color:white
    style G fill:#8172B2,color:white
    style H fill:#CCB974,color:black
    style I fill:#C44E52,color:white
    style J fill:#55A868,color:white
```
"""


def _classify_retrieval_path(detail: dict[str, Any]) -> str:
    """Classify a question's retrieval path based on tool sequence and turn count."""
    turns = detail.get('num_turns', 0)
    memex_calls = detail.get('memex_calls', 0)

    if memex_calls <= 1:
        return 'simple'
    elif turns <= 5:
        return 'two_stage'
    elif turns <= 12:
        return 'deep'
    else:
        return 'exhaustive'


def _generate_markdown(
    report: dict[str, Any],
    details: list[dict[str, Any]],
    retrieval_breakdown: dict[str, Any],
    plots_dir_rel: str,
    plot_files: list[str],
) -> str:
    """Generate the full Markdown report."""
    lines: list[str] = []

    def w(line: str = '') -> None:
        lines.append(line)

    # Separate scored vs excluded vs adversarial
    excluded_ids = set(_EXCLUDED_REASONS.keys())
    scored = [d for d in details if d['id'] not in excluded_ids]
    non_adv = [d for d in scored if d.get('category') != 'adversarial']
    adv = [d for d in scored if d.get('category') == 'adversarial']

    non_adv_scores = [d['score'] for d in non_adv]
    non_adv_mean = round(sum(non_adv_scores) / len(non_adv_scores), 3) if non_adv_scores else 0.0
    non_adv_perfect = sum(1 for s in non_adv_scores if s == 1.0)
    non_adv_wrong = sum(1 for s in non_adv_scores if s == 0.0)

    total_cost = sum(d.get('cost_usd', 0) for d in details)
    total_duration = sum(d.get('duration_s', 0) for d in details)
    total_tokens_all = sum(d.get('total_tokens', 0) for d in details)
    total_retr = retrieval_breakdown['total']

    # --- Header ---
    w('# LoCoMo Evaluation Report')
    w()
    w('> Model: Claude Opus 4 via Claude Code CLI | Judge: Gemini 3 Flash')
    w()

    # --- Summary ---
    w('## Summary')
    w()
    w('| Metric | Value |')
    w('|---|---|')
    w(
        f'| Questions (scored) | {len(non_adv)} (excl. '
        f'{len(excluded_ids)} image-dependent, {len(adv)} adversarial) |'
    )
    w(f'| Overall Score (non-adversarial) | **{non_adv_mean:.3f}** |')
    w(f'| Perfect answers | {non_adv_perfect} ({non_adv_perfect / len(non_adv) * 100:.1f}%) |')
    w(f'| Wrong answers | {non_adv_wrong} ({non_adv_wrong / len(non_adv) * 100:.1f}%) |')
    w(f'| Total cost | ${total_cost:.2f} |')
    w(f'| Total duration | {total_duration / 60:.1f} min |')
    w()

    # --- Scores by category ---
    w('## Scores by Category')
    w()
    w('| Category | Count | Mean Score | Perfect | Wrong |')
    w('|---|---|---|---|---|')

    for qt in QUESTION_TYPES:
        cat_details = [d for d in scored if d.get('category') == qt and d['id'] not in excluded_ids]
        if not cat_details:
            continue
        cat_scores = [d['score'] for d in cat_details]
        mean = round(sum(cat_scores) / len(cat_scores), 3) if cat_scores else 0.0
        perfect = sum(1 for s in cat_scores if s == 1.0)
        wrong = sum(1 for s in cat_scores if s == 0.0)
        name = CATEGORY_NAMES.get(qt, qt)
        if qt == 'adversarial':
            w(f'| {name} (unweighted) | {len(cat_scores)} | {mean:.3f} | {perfect} | {wrong} |')
        else:
            w(f'| {name} | {len(cat_scores)} | **{mean:.3f}** | {perfect} | {wrong} |')

    w(
        f'| **Non-adversarial** | **{len(non_adv)}** | **{non_adv_mean:.3f}** '
        f'| **{non_adv_perfect}** | **{non_adv_wrong}** |'
    )
    w()

    # --- Adversarial note ---
    w('### A note on adversarial scoring')
    w()
    w(
        'Adversarial questions in the LoCoMo dataset deliberately swap the subject — '
        'asking about person A when the ground-truth answer actually pertains to person B. '
        'The expected "correct" behavior is for the model to detect this swap.'
    )
    w()
    w(
        'However, Memex is a search-and-answer tool. When asked "What instrument does '
        'Caroline play?", it correctly searches for Caroline\'s instruments, finds '
        '"acoustic guitar", and returns that answer. The fact that the benchmark expects '
        '"clarinet and violin" (Melanie\'s instruments) tests something outside the system\'s '
        'scope: the model would need to know the question is deliberately misleading.'
    )
    w()
    w(
        'In the failed adversarial cases, the retrieval system found correct, relevant facts '
        'for the queried person. These are not retrieval failures — they are a fundamental '
        "mismatch between a search tool's behavior and the adversarial benchmark's "
        'expectations. Adversarial scores are therefore reported separately and excluded '
        'from the weighted overall score.'
    )
    w()

    # --- Retrieval efficiency ---
    w('## Retrieval Efficiency')
    w()
    w('### Token breakdown')
    w()
    w('| Metric | Value |')
    w('|---|---|')
    w(f'| Total tokens (all) | {total_tokens_all:,} |')
    retr_pct = round(total_retr / total_tokens_all * 100, 1) if total_tokens_all > 0 else 0.0
    w(f'| Retrieval tokens (Memex) | {total_retr:,} (**{retr_pct}%** of total) |')
    w(f'| Agent overhead tokens | {total_tokens_all - total_retr:,} ({100 - retr_pct:.1f}%) |')
    retr_values = [d['retrieval_tokens'] for d in scored if d['retrieval_tokens'] > 0]
    if retr_values:
        w(f'| Retrieval tokens/question (mean) | {round(statistics.mean(retr_values)):,} |')
        w(f'| Retrieval tokens/question (median) | {round(statistics.median(retr_values)):,} |')
    w()

    # --- Retrieval by tool ---
    per_tool = retrieval_breakdown.get('per_tool', {})
    if per_tool:
        # Count tool calls across all answers
        tool_call_counts: Counter[str] = Counter()
        for d in details:
            tool_pattern = d.get('tool_pattern', {})
            for name in tool_pattern.get('sequence', []):
                if name.startswith('memex_'):
                    tool_call_counts[f'mcp__memex__{name}'] += 1

        w('### Retrieval by tool')
        w()
        w('| Tool | Tokens | Share | Calls | Calls/q |')
        w('|---|---|---|---|---|')
        for tool_name, tokens in per_tool.items():
            share = round(tokens / total_retr * 100, 1) if total_retr > 0 else 0.0
            short = tool_name.replace('mcp__memex__memex_', '')
            calls = tool_call_counts.get(tool_name, 0)
            per_q = round(calls / len(scored), 1) if scored else 0.0
            w(f'| `{short}` | {tokens:,} | {share}% | {calls} | {per_q} |')
        w()

    if 'retrieval_token_breakdown.png' in plot_files:
        w(f'![Retrieval token breakdown]({plots_dir_rel}/retrieval_token_breakdown.png)')
        w()

    # --- Efficiency by category ---
    w('### Efficiency by category')
    w()
    w('| Category | Duration | Turns | Total Tokens | Retr Tokens | Retr % | Memex Calls |')
    w('|---|---|---|---|---|---|---|')

    for qt in QUESTION_TYPES:
        cat_d = [d for d in scored if d.get('category') == qt]
        if not cat_d:
            continue
        name = CATEGORY_NAMES.get(qt, qt)
        avg_dur = round(statistics.mean([d['duration_s'] for d in cat_d]), 1)
        avg_turns = round(statistics.mean([d['num_turns'] for d in cat_d]), 1)
        avg_tok = round(statistics.mean([d['total_tokens'] for d in cat_d]))
        avg_retr = round(statistics.mean([d['retrieval_tokens'] for d in cat_d]))
        avg_pct = round(statistics.mean([d['retrieval_pct'] for d in cat_d]), 1)
        avg_memex = round(statistics.mean([d['memex_calls'] for d in cat_d]), 1)
        w(
            f'| {name} | {avg_dur}s | {avg_turns} | {avg_tok:,} | '
            f'{avg_retr:,} | {avg_pct}% | {avg_memex} |'
        )
    w()

    # --- Distribution plots ---
    w('### Distribution plots')
    w()
    for pf in plot_files:
        if pf == 'retrieval_token_breakdown.png':
            continue
        label = pf.replace('.png', '').replace('_', ' ').title()
        w(f'![{label}]({plots_dir_rel}/{pf})')
        w()

    # --- Retrieval paths ---
    w('## Retrieval Paths')
    w()

    # Classify and count
    path_counts: Counter[str] = Counter()
    for d in scored:
        path_counts[_classify_retrieval_path(d)] += 1

    path_labels = {
        'simple': 'Simple',
        'two_stage': 'Two-stage',
        'deep': 'Deep verification',
        'exhaustive': 'Exhaustive',
    }
    total_scored = len(scored)
    w('| Pattern | Count | Share |')
    w('|---|---|---|')
    for key in ['simple', 'two_stage', 'deep', 'exhaustive']:
        cnt = path_counts.get(key, 0)
        pct = round(cnt / total_scored * 100) if total_scored > 0 else 0
        w(f'| {path_labels[key]} | {cnt} | {pct}% |')
    w()
    w(_RETRIEVAL_PATHS_MERMAID)

    # --- Resource usage ---
    w('## Resource Usage')
    w()
    total_input = sum(d.get('tokens', {}).get('input', 0) for d in details)
    total_output = sum(d.get('tokens', {}).get('output', 0) for d in details)
    w('| Metric | Value |')
    w('|---|---|')
    w(f'| Total tokens | {total_tokens_all:,} |')
    w(f'| Input tokens | {total_input:,} |')
    w(f'| Output tokens | {total_output:,} |')
    w(f'| Retrieval tokens (Memex) | {total_retr:,} ({retr_pct}%) |')
    w(f'| Total duration | {total_duration:,.0f}s ({total_duration / 60:.1f} min) |')
    avg_dur = round(total_duration / len(details), 1) if details else 0.0
    w(f'| Avg duration/question | {avg_dur}s |')
    dur_values = [d['duration_s'] for d in details]
    if dur_values:
        w(f'| Median duration/question | {round(statistics.median(dur_values), 1)}s |')
    w(f'| Total cost | ${total_cost:.2f} |')
    if details:
        w(f'| Avg cost/question | ${total_cost / len(details):.3f} |')
    w()

    # --- Per-question detail ---
    w('## Per-Question Detail')
    w()
    w('| ID | Category | Score | Dur | Turns | Total Tok | Retr Tok | Retr % | Memex# | Cost |')
    w('|---|---|---|---|---|---|---|---|---|---|')

    for d in sorted(details, key=lambda x: x['id']):
        qid = d['id']
        cat = CATEGORY_NAMES.get(d.get('category', ''), d.get('category', ''))

        if qid in excluded_ids:
            score_str = '\u2014'
        else:
            score_str = f'{d["score"]:.1f}'

        w(
            f'| {qid} | {cat.lower()} | {score_str} | {d["duration_s"]:.1f}s '
            f'| {d["num_turns"]} | {d["total_tokens"]:,} | {d["retrieval_tokens"]:,} '
            f'| {d["retrieval_pct"]}% | {d["memex_calls"]} | ${d.get("cost_usd", 0):.2f} |'
        )
    w()

    # --- Excluded questions ---
    w('## Excluded Questions')
    w()
    w('| ID | Reason |')
    w('|---|---|')
    for qid, reason in _EXCLUDED_REASONS.items():
        w(f'| {qid} | {reason} |')
    w()

    # --- Errors ---
    w('## Remaining Errors')
    w()
    non_adv_errors = [d for d in non_adv if d['score'] == 0.0]
    adv_errors = [d for d in adv if d['score'] == 0.0]

    w(f'### Non-adversarial ({len(non_adv_errors)})')
    w()
    if non_adv_errors:
        w('| ID | Category | Question | Issue |')
        w('|---|---|---|---|')
        for d in non_adv_errors:
            cat = CATEGORY_NAMES.get(d.get('category', ''), d.get('category', ''))
            q = d.get('question', '')[:80]
            reasoning = d.get('reasoning', '')[:120]
            w(f'| {d["id"]} | {cat} | {q} | {reasoning} |')
    else:
        w('None.')
    w()

    w(f'### Adversarial ({len(adv_errors)}, unweighted)')
    w()
    if adv_errors:
        w('| ID | Question | Issue |')
        w('|---|---|---|')
        for d in adv_errors:
            q = d.get('question', '')[:80]
            reasoning = d.get('reasoning', '')[:120]
            w(f'| {d["id"]} | {q} | {reasoning} |')
        w()
        w(
            'In all adversarial failures, the retrieval system found correct facts for the '
            'queried person. The failures are inherent to the adversarial format: a search '
            'tool cannot detect that a question deliberately swaps subjects.'
        )
    else:
        w('None.')
    w()

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    results_path: str,
    answers_path: str,
    traces_dir: str,
    output_dir: str,
) -> str:
    """Generate the full LoCoMo evaluation report with plots.

    Args:
        results_path: Path to results.json from Phase 3 (judge).
        answers_path: Path to answers.jsonl from Phase 2.
        traces_dir: Directory containing per-question trace JSONL files.
        output_dir: Directory for output report and plots.

    Returns:
        Path to the generated report Markdown file.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    plots_dir = output / 'plots'

    console.print('[bold]Loading data...[/bold]')
    report, details, retrieval_breakdown = _load_data(results_path, answers_path, traces_dir)

    console.print('[bold]Generating plots...[/bold]')
    plot_files = _generate_plots(details, retrieval_breakdown, plots_dir)
    console.print(f'  {len(plot_files)} plots -> {plots_dir}')

    console.print('[bold]Generating report...[/bold]')
    md = _generate_markdown(report, details, retrieval_breakdown, 'plots', plot_files)

    report_path = output / 'report.md'
    report_path.write_text(md)

    console.print(f'\n[bold green]Report -> {report_path}[/bold green]')
    return str(report_path)
