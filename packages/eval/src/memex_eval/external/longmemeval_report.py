"""LongMemEval Phase 4: Generate evaluation report with plots.

Ported from locomo_report.py — reads judge results, answers/hypotheses, and
session traces to produce a reproducible Markdown report with seaborn
distribution plots and mermaid retrieval diagrams.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
import tiktoken
from rich.console import Console
from rich.table import Table

from memex_eval.external.longmemeval_common import (
    CATEGORY_NAMES,
    LongMemEvalCategory,
    LongMemEvalCategoryResult,
    LongMemEvalHypothesis,
    LongMemEvalJudgment,
    LongMemEvalReport,
    _load_variant,
    dataset_sha256,
    read_jsonl,
)
from memex_eval.external.longmemeval_efficiency import _count_retrieval_tokens_from_trace
from memex_eval.external.longmemeval_trace_parser import (
    RecallMetrics,
    compute_batch_recall,
    parse_traces_dir,
)

matplotlib.use('Agg')

logger = logging.getLogger('memex_eval.longmemeval_report')
console = Console()

_MEMEX_TOOL_PREFIX = 'mcp__memex__'

# Category ordering for tables and plots
_CATEGORY_ORDER = list(LongMemEvalCategory)


# ---------------------------------------------------------------------------
# Aggregation (preserved from the original module)
# ---------------------------------------------------------------------------


def _aggregate(
    judgments: list[LongMemEvalJudgment],
) -> tuple[list[LongMemEvalCategoryResult], float, float, float]:
    """Compute per-category results plus abstention precision/recall."""
    by_cat: dict[LongMemEvalCategory, list[LongMemEvalJudgment]] = {}
    for j in judgments:
        by_cat.setdefault(j.category, []).append(j)

    per_category: list[LongMemEvalCategoryResult] = []
    for category, items in by_cat.items():
        n = len(items)
        n_correct = sum(1 for j in items if j.correct)
        per_category.append(
            LongMemEvalCategoryResult(
                category=category,
                n_questions=n,
                n_correct=n_correct,
                accuracy=round(n_correct / n, 4) if n else 0.0,
            )
        )

    overall_accuracy = (
        round(sum(1 for j in judgments if j.correct) / len(judgments), 4) if judgments else 0.0
    )

    abstention_questions = [j for j in judgments if j.is_abstention]
    abstention_correct = [j for j in abstention_questions if j.correct]
    abstention_recall = (
        round(len(abstention_correct) / len(abstention_questions), 4)
        if abstention_questions
        else 0.0
    )

    abstained_hyps = [j for j in judgments if j.is_abstention_hypothesis]
    abstention_precision = (
        round(sum(1 for j in abstained_hyps if j.is_abstention) / len(abstained_hyps), 4)
        if abstained_hyps
        else 0.0
    )

    return per_category, overall_accuracy, abstention_precision, abstention_recall


# ---------------------------------------------------------------------------
# Data loading and enrichment
# ---------------------------------------------------------------------------


def _load_data(
    judgments: list[LongMemEvalJudgment],
    hypotheses: list[LongMemEvalHypothesis],
    traces_dir: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cross-reference judgments, hypotheses, and trace metrics.

    Returns (enriched_details, retrieval_by_tool).
    """
    hyp_by_id = {h.question_id: h for h in hypotheses}
    traces_path = Path(traces_dir)
    enc = tiktoken.get_encoding('cl100k_base')

    enriched: list[dict[str, Any]] = []
    retrieval_per_tool: Counter[str] = Counter()
    total_retrieval = 0

    for j in judgments:
        qid = j.question_id
        hyp = hyp_by_id.get(qid)

        # Build base detail record from judgment
        detail: dict[str, Any] = {
            'id': qid,
            'category': j.category.value,
            'score': 1.0 if j.correct else 0.0,
            'question': hyp.hypothesis[:120] if hyp else '',
            'reasoning': j.judge_reasoning[:120] if j.judge_reasoning else '',
            'is_abstention': j.is_abstention,
        }

        if hyp:
            duration_s = hyp.latency_ms / 1000.0
            total_tokens = hyp.input_tokens + hyp.output_tokens
            tool_calls = hyp.tool_calls
            memex_calls = sum(1 for tc in tool_calls if tc.name.startswith(_MEMEX_TOOL_PREFIX))
            detail.update(
                {
                    'duration_s': round(duration_s, 1),
                    'tokens': {'input': hyp.input_tokens, 'output': hyp.output_tokens},
                    'total_tokens': total_tokens,
                    'num_turns': hyp.num_turns,
                    'cost_usd': hyp.cost_usd or 0.0,
                    'memex_calls': memex_calls,
                    'tool_calls': [{'name': tc.name, 'input': tc.input} for tc in tool_calls],
                }
            )
        else:
            detail.update(
                {
                    'duration_s': 0.0,
                    'tokens': {'input': 0, 'output': 0},
                    'total_tokens': 0,
                    'num_turns': 0,
                    'cost_usd': 0.0,
                    'memex_calls': 0,
                    'tool_calls': [],
                }
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

        detail['retrieval_tokens'] = retr_tokens
        detail['retrieval_pct'] = (
            round(retr_tokens / detail['total_tokens'] * 100, 1)
            if detail['total_tokens'] > 0
            else 0.0
        )

        enriched.append(detail)

    retrieval_breakdown = {
        'total': total_retrieval,
        'per_tool': dict(retrieval_per_tool.most_common()),
    }

    return enriched, retrieval_breakdown


# ---------------------------------------------------------------------------
# Retrieval path classification
# ---------------------------------------------------------------------------

_ENTITY_TOOLS = {
    'mcp__memex__memex_list_entities',
    'mcp__memex__memex_get_entity_mentions',
    'mcp__memex__memex_get_entity_cooccurrences',
    'mcp__memex__memex_get_entities',
}

_DEEP_TOOLS = {
    'mcp__memex__memex_get_page_indices',
    'mcp__memex__memex_get_nodes',
}


def _classify_retrieval_path(detail: dict[str, Any]) -> str:
    """Classify a question's retrieval path based on its tool call sequence."""
    tool_calls = detail.get('tool_calls', [])
    tool_names = [tc.get('name', '') for tc in tool_calls]
    memex_names = [n for n in tool_names if n.startswith(_MEMEX_TOOL_PREFIX)]

    if not memex_names:
        return 'simple'

    # Count occurrences of key tools
    memory_search_count = sum(1 for n in memex_names if n == 'mcp__memex__memex_memory_search')
    note_search_count = sum(1 for n in memex_names if n == 'mcp__memex__memex_note_search')
    has_entity = any(n in _ENTITY_TOOLS for n in memex_names)
    has_deep = any(n in _DEEP_TOOLS for n in memex_names)
    has_note_search = note_search_count > 0

    # Exhaustive: multiple rounds of memory_search or note_search
    if memory_search_count > 1 or note_search_count > 1:
        return 'exhaustive'

    # Deep + entity
    if has_deep and has_entity:
        return 'deep_entity'

    # Deep verification
    if has_deep and has_note_search:
        return 'deep'

    # Two-stage + entity
    if has_note_search and has_entity:
        return 'two_stage_entity'

    # Simple + entity
    if has_entity and not has_note_search and not has_deep:
        return 'simple_entity'

    # Two-stage: memory_search + note_search only
    if has_note_search:
        return 'two_stage'

    return 'simple'


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

    # Category order for consistent axis
    cat_order = [CATEGORY_NAMES.get(c, c.value) for c in _CATEGORY_ORDER]

    def _cat_label(d: dict[str, Any]) -> str:
        cat_val = d.get('category', '')
        try:
            cat = LongMemEvalCategory(cat_val)
            return CATEGORY_NAMES.get(cat, cat_val)
        except ValueError:
            return cat_val

    # 1. Token usage by category
    fig, ax = plt.subplots()
    data = [{'Category': _cat_label(d), 'Total Tokens': d['total_tokens']} for d in details]
    if data:
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
    data = [{'Category': _cat_label(d), 'Duration (s)': d['duration_s']} for d in details]
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
        for d in details
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
        for d in details
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
    turns = [d['num_turns'] for d in details if d.get('num_turns', 0) > 0]
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
### Two-stage path

Memory search and note search provide sufficient context to answer directly.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#55A868,color:white
```

### Two-stage + entity path

Memory and note search augmented with entity exploration.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[list_entities] --> D[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#DD8452,color:white
    style D fill:#55A868,color:white
```

### Deep verification path

Full two-speed reading: search, then drill into specific note sections.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> E[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style E fill:#55A868,color:white
```

### Deep + entity path

Adds entity exploration to deep verification.

```mermaid
graph TD
    E1[list_entities] --> E2[get_entity_mentions]
    E2 --> A[memory_search]
    A --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> F[Answer]
    style E1 fill:#DD8452,color:white
    style E2 fill:#DD8452,color:white
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style F fill:#55A868,color:white
```

### Simple + entity path

A single search round with entity exploration. No deep reading needed.

```mermaid
graph LR
    A[memory_search] --> B[list_entities] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#DD8452,color:white
    style C fill:#55A868,color:white
```

### Exhaustive path

Multiple rounds of searching and reading across different notes and queries.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> E{Sufficient?}
    E -- No --> F[Refined memory_search]
    F --> G[note_search]
    G --> H[get_page_indices]
    H --> I[get_nodes]
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


def _generate_markdown(
    report: LongMemEvalReport,
    details: list[dict[str, Any]],
    retrieval_breakdown: dict[str, Any],
    plots_dir_rel: str,
    plot_files: list[str],
    recall_metrics: list[RecallMetrics] | None = None,
) -> str:
    """Generate the full Markdown report with three evaluation axes."""
    lines: list[str] = []

    def w(line: str = '') -> None:
        lines.append(line)

    total_cost = sum(d.get('cost_usd', 0) for d in details)
    total_duration = sum(d.get('duration_s', 0) for d in details)
    total_tokens_all = sum(d.get('total_tokens', 0) for d in details)
    total_retr = retrieval_breakdown['total']

    n_correct = sum(1 for d in details if d['score'] == 1.0)
    n_wrong = sum(1 for d in details if d['score'] == 0.0)
    n_scored = len(details)

    # --- Header ---
    w('# LongMemEval Evaluation Report')
    w()
    w(
        f'> Run: `{report.run_id}` | Variant: `{report.variant}` '
        f'| Model: `{report.answer_model_fingerprint}` '
        f'| Judge: `{report.judge_model_fingerprint}`'
    )
    w()

    # --- Summary ---
    w('## Summary')
    w()
    w('| Metric | Value |')
    w('|---|---|')
    w(f'| Questions (scored) | {n_scored} |')
    w(f'| Overall Accuracy | **{report.overall_accuracy:.3f}** |')
    w(f'| Correct answers | {n_correct} ({n_correct / n_scored * 100:.1f}%) |')
    w(f'| Wrong answers | {n_wrong} ({n_wrong / n_scored * 100:.1f}%) |')
    w(f'| Abstention precision | {report.abstention_precision:.3f} |')
    w(f'| Abstention recall | {report.abstention_recall:.3f} |')
    w(f'| Total cost | ${total_cost:.2f} |')
    w(f'| Total duration | {total_duration / 60:.1f} min |')
    if report.dataset_sha256:
        w(f'| Dataset SHA-256 | `{report.dataset_sha256[:16]}...` |')
    w()

    # ===================================================================
    # AXIS 1: Retrieval Recall
    # ===================================================================
    w('## 1. Retrieval Recall')
    w()
    w('Measures whether memex surfaces the right evidence in the first 3 calls per tool type.')
    w('Cap: first 3 calls each for memory_search, note_search, survey.')
    w()

    if recall_metrics:
        # Filter to questions that have gold session IDs
        with_gold = [m for m in recall_metrics if m.gold_session_ids]

        if with_gold:
            total_gold = sum(len(m.gold_session_ids) for m in with_gold)
            total_found = sum(len(m.found_session_ids) for m in with_gold)
            overall_recall = total_found / total_gold if total_gold > 0 else 0.0

            w('| Question | Category | Gold Sessions | Found | Recall@3 |')
            w('|---|---|---|---|---|')
            for m in sorted(with_gold, key=lambda x: x.question_id):
                cat_label = m.category or ''
                w(
                    f'| {m.question_id} | {cat_label} '
                    f'| {len(m.gold_session_ids)} '
                    f'| {len(m.found_session_ids)} '
                    f'| {m.recall:.3f} |'
                )
            w(
                f'| **Overall** | | **{total_gold}** '
                f'| **{total_found}** | **{overall_recall:.3f}** |'
            )
            w()

            # Per-tool recall
            tool_stats: dict[str, dict[str, int]] = {}
            for m in with_gold:
                for tool_name, info in m.per_tool.items():
                    if tool_name not in tool_stats:
                        tool_stats[tool_name] = {
                            'capped_calls': 0,
                            'total_results': 0,
                            'gold_found': 0,
                        }
                    tool_stats[tool_name]['capped_calls'] += info['capped_calls']
                    tool_stats[tool_name]['total_results'] += info['total_results']
                    tool_stats[tool_name]['gold_found'] += len(info['gold_found'])

            if tool_stats:
                w('Per-tool recall:')
                w()
                w('| Tool | Calls (capped) | Total Results | Gold Found | Recall |')
                w('|---|---|---|---|---|')
                for tool_name in sorted(tool_stats.keys()):
                    s = tool_stats[tool_name]
                    short = tool_name.replace('mcp__memex__memex_', '')
                    recall_val = s['gold_found'] / total_gold if total_gold > 0 else 0.0
                    w(
                        f'| `{short}` | {s["capped_calls"]} '
                        f'| {s["total_results"]} '
                        f'| {s["gold_found"]} | {recall_val:.3f} |'
                    )
                w()
        else:
            w('No questions with gold session IDs available for recall computation.')
            w()
    else:
        w('No trace data or dataset available for recall computation.')
        w()

    # ===================================================================
    # AXIS 2: Answer Quality
    # ===================================================================
    w('## 2. Answer Quality')
    w()
    w(
        f'Measures end-to-end answer correctness. '
        f'Model: `{report.answer_model_fingerprint}`. '
        f'Judge: `{report.judge_model_fingerprint}`.'
    )
    w()

    # Accuracy by category
    w('### Accuracy by category')
    w()
    w('| Category | Count | Correct | Accuracy |')
    w('|---|---|---|---|')

    for cat in _CATEGORY_ORDER:
        cat_details = [d for d in details if d.get('category') == cat.value]
        if not cat_details:
            continue
        cat_correct = sum(1 for d in cat_details if d['score'] == 1.0)
        accuracy = round(cat_correct / len(cat_details), 3) if cat_details else 0.0
        name = CATEGORY_NAMES.get(cat, cat.value)
        w(f'| {name} | {len(cat_details)} | {cat_correct} | **{accuracy:.3f}** |')

    w(f'| **Overall** | **{n_scored}** | **{n_correct}** | **{report.overall_accuracy:.3f}** |')
    w()

    # Per-question detail
    w('### Per-question detail')
    w()
    w('| ID | Category | Score | Reasoning |')
    w('|---|---|---|---|')

    for d in sorted(details, key=lambda x: x['id']):
        qid = d['id']
        cat_val = d.get('category', '')
        try:
            cat = LongMemEvalCategory(cat_val)
            cat_name = CATEGORY_NAMES.get(cat, cat_val)
        except ValueError:
            cat_name = cat_val
        score_str = f'{d["score"]:.1f}'
        reasoning = d.get('reasoning', '')[:120]
        w(f'| {qid} | {cat_name.lower()} | {score_str} | {reasoning} |')
    w()

    # Errors
    errors = [d for d in details if d['score'] == 0.0]
    if errors:
        w(f'### Wrong answers ({len(errors)})')
        w()
        w('| ID | Category | Issue |')
        w('|---|---|---|')
        for d in errors:
            cat_val = d.get('category', '')
            try:
                cat = LongMemEvalCategory(cat_val)
                cat_name = CATEGORY_NAMES.get(cat, cat_val)
            except ValueError:
                cat_name = cat_val
            reasoning = d.get('reasoning', '')[:120]
            w(f'| {d["id"]} | {cat_name} | {reasoning} |')
        w()

    # ===================================================================
    # AXIS 3: Token Efficiency
    # ===================================================================
    w('## 3. Token Efficiency')
    w()
    w('Measures total token spend across the full subagent session (uncapped -- all calls count).')
    w()

    # Token breakdown
    w('### Token breakdown')
    w()
    w('| Metric | Value |')
    w('|---|---|')
    w(f'| Total tokens (all) | {total_tokens_all:,} |')
    retr_pct = round(total_retr / total_tokens_all * 100, 1) if total_tokens_all > 0 else 0.0
    w(f'| Retrieval tokens (Memex) | {total_retr:,} (**{retr_pct}%** of total) |')
    w(f'| Agent overhead tokens | {total_tokens_all - total_retr:,} ({100 - retr_pct:.1f}%) |')
    retr_values = [d['retrieval_tokens'] for d in details if d['retrieval_tokens'] > 0]
    if retr_values:
        w(f'| Retrieval tokens/question (mean) | {round(statistics.mean(retr_values)):,} |')
        w(f'| Retrieval tokens/question (median) | {round(statistics.median(retr_values)):,} |')
    w()

    # Retrieval by tool
    per_tool = retrieval_breakdown.get('per_tool', {})
    if per_tool:
        tool_call_counts: Counter[str] = Counter()
        for d in details:
            for tc in d.get('tool_calls', []):
                name = tc.get('name', '')
                if name.startswith(_MEMEX_TOOL_PREFIX):
                    tool_call_counts[name] += 1

        w('### Retrieval by tool')
        w()
        w('| Tool | Tokens | Share | Calls | Calls/q |')
        w('|---|---|---|---|---|')
        for tool_name, tokens in per_tool.items():
            share = round(tokens / total_retr * 100, 1) if total_retr > 0 else 0.0
            short = tool_name.replace('mcp__memex__memex_', '')
            calls = tool_call_counts.get(tool_name, 0)
            per_q = round(calls / n_scored, 1) if n_scored else 0.0
            w(f'| `{short}` | {tokens:,} | {share}% | {calls} | {per_q} |')
        w()

    if 'retrieval_token_breakdown.png' in plot_files:
        w(f'![Retrieval token breakdown]({plots_dir_rel}/retrieval_token_breakdown.png)')
        w()

    # Efficiency by category
    w('### Efficiency by category')
    w()
    w('| Category | Duration | Turns | Total Tokens | Retr Tokens | Retr % | Memex Calls |')
    w('|---|---|---|---|---|---|---|')

    for cat in _CATEGORY_ORDER:
        cat_d = [d for d in details if d.get('category') == cat.value]
        if not cat_d:
            continue
        name = CATEGORY_NAMES.get(cat, cat.value)
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

    # Resource usage
    w('### Resource usage')
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

    # Per-question efficiency detail
    w('### Per-question efficiency')
    w()
    w('| ID | Category | Dur | Turns | Total Tok | Retr Tok | Retr % | Memex# | Cost |')
    w('|---|---|---|---|---|---|---|---|---|')

    for d in sorted(details, key=lambda x: x['id']):
        qid = d['id']
        cat_val = d.get('category', '')
        try:
            cat = LongMemEvalCategory(cat_val)
            cat_name = CATEGORY_NAMES.get(cat, cat_val)
        except ValueError:
            cat_name = cat_val

        w(
            f'| {qid} | {cat_name.lower()} | {d["duration_s"]:.1f}s '
            f'| {d["num_turns"]} | {d["total_tokens"]:,} | {d["retrieval_tokens"]:,} '
            f'| {d["retrieval_pct"]}% | {d["memex_calls"]} '
            f'| ${d.get("cost_usd", 0):.2f} |'
        )
    w()

    # Tool usage patterns
    w('### Tool usage patterns')
    w()

    toolsearch_calls = sum(
        1 for d in details for tc in d.get('tool_calls', []) if tc.get('name') == 'ToolSearch'
    )
    toolsearch_multi = sum(
        1
        for d in details
        if sum(1 for tc in d.get('tool_calls', []) if tc.get('name') == 'ToolSearch') > 1
    )
    ts_per_q = round(toolsearch_calls / n_scored, 1) if n_scored else 0.0

    entity_questions = sum(
        1
        for d in details
        if any(tc.get('name', '') in _ENTITY_TOOLS for tc in d.get('tool_calls', []))
    )
    entity_pct = round(entity_questions / n_scored * 100) if n_scored else 0

    w('| Metric | Value |')
    w('|---|---|')
    w(
        f'| ToolSearch calls/question | {ts_per_q} '
        f'({toolsearch_calls} total, {toolsearch_multi} questions with >1) |'
    )
    w(f'| Entity exploration | {entity_questions}/{n_scored} questions ({entity_pct}%) |')
    w()

    memex_tool_counts: Counter[str] = Counter()
    for d in details:
        for tc in d.get('tool_calls', []):
            name = tc.get('name', '')
            if name.startswith(_MEMEX_TOOL_PREFIX):
                memex_tool_counts[name] += 1

    if memex_tool_counts:
        w('### Memex tool call distribution')
        w()
        w('| Tool | Total Calls | Calls/q |')
        w('|---|---|---|')
        for tool_name, count in memex_tool_counts.most_common():
            short = tool_name.replace('mcp__memex__memex_', '')
            per_q = round(count / n_scored, 1) if n_scored else 0.0
            w(f'| `{short}` | {count} | {per_q} |')
        w()

    # Distribution plots
    if plot_files:
        w('### Distribution plots')
        w()
        for pf in plot_files:
            if pf == 'retrieval_token_breakdown.png':
                continue
            label = pf.replace('.png', '').replace('_', ' ').title()
            w(f'![{label}]({plots_dir_rel}/{pf})')
            w()

    # Retrieval paths
    w('### Retrieval paths')
    w()

    path_counts: Counter[str] = Counter()
    path_details: dict[str, list[dict[str, Any]]] = {}
    for d in details:
        path = _classify_retrieval_path(d)
        path_counts[path] += 1
        path_details.setdefault(path, []).append(d)

    path_labels = {
        'simple': 'Simple',
        'two_stage': 'Two-stage',
        'two_stage_entity': 'Two-stage + entity',
        'deep': 'Deep verification',
        'deep_entity': 'Deep + entity',
        'simple_entity': 'Simple + entity',
        'exhaustive': 'Exhaustive',
    }
    path_order = [
        'two_stage',
        'two_stage_entity',
        'deep',
        'deep_entity',
        'simple_entity',
        'exhaustive',
        'simple',
    ]

    w('| Pattern | Count | Share | Avg Score | Avg Tools | Avg Duration | Avg Cost |')
    w('|---|---|---|---|---|---|---|')
    for key in path_order:
        cnt = path_counts.get(key, 0)
        if cnt == 0:
            continue
        pct = round(cnt / n_scored * 100) if n_scored > 0 else 0
        pd_list = path_details.get(key, [])
        avg_score = round(statistics.mean([d['score'] for d in pd_list]), 2)
        avg_tools = round(statistics.mean([len(d.get('tool_calls', [])) for d in pd_list]), 1)
        avg_dur_val = round(statistics.mean([d['duration_s'] for d in pd_list]))
        avg_cost = round(statistics.mean([d.get('cost_usd', 0) for d in pd_list]), 2)
        w(
            f'| {path_labels[key]} | {cnt} | {pct}% | {avg_score} '
            f'| {avg_tools} | {avg_dur_val}s | ${avg_cost} |'
        )
    w()
    w(_RETRIEVAL_PATHS_MERMAID)

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------


def _print_summary(report: LongMemEvalReport) -> None:
    console.print()
    console.rule('[bold]LongMemEval Report[/bold]')
    console.print(
        f'[dim]run={report.run_id} | variant={report.variant} | '
        f'questions={report.total_questions}[/dim]'
    )
    console.print()
    table = Table(show_header=True, header_style='bold cyan')
    table.add_column('Category', style='white')
    table.add_column('N', justify='right')
    table.add_column('Correct', justify='right')
    table.add_column('Accuracy', justify='right')
    for cat_result in sorted(report.per_category, key=lambda c: c.category.value):
        acc = cat_result.accuracy
        style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
        table.add_row(
            CATEGORY_NAMES.get(cat_result.category, cat_result.category.value),
            str(cat_result.n_questions),
            str(cat_result.n_correct),
            f'[{style}]{acc:.3f}[/{style}]',
        )
    overall_style = (
        'green'
        if report.overall_accuracy >= 0.7
        else ('yellow' if report.overall_accuracy >= 0.4 else 'red')
    )
    table.add_row(
        '[bold]Overall[/bold]',
        f'[bold]{report.total_questions}[/bold]',
        f'[bold]{sum(c.n_correct for c in report.per_category)}[/bold]',
        f'[bold {overall_style}]{report.overall_accuracy:.3f}[/bold {overall_style}]',
        end_section=True,
    )
    console.print(table)
    console.print(
        f'Abstention: precision={report.abstention_precision:.3f} '
        f'| recall={report.abstention_recall:.3f}'
    )
    console.print()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    judgments_path: str,
    output_dir: str,
    *,
    run_id: str | None = None,
    variant: str = 's',
    dataset_path: str | None = None,
    hypotheses_path: str | None = None,
    traces_dir: str | None = None,
    answer_model_fingerprint: str = '',
    judge_model_fingerprint: str = '',
    allow_unpinned_checksum: bool = False,
) -> str:
    """Generate the full LongMemEval evaluation report with plots.

    Args:
        judgments_path: Path to judgments.jsonl from Phase 3 (judge).
        output_dir: Directory for output report and plots.
        run_id: Run identifier.
        variant: Dataset variant.
        dataset_path: Optional, used to embed dataset SHA-256 and compute recall.
        hypotheses_path: Path to hypotheses.jsonl from Phase 2.
        traces_dir: Directory containing per-question trace JSONL files.
        answer_model_fingerprint: Model fingerprint for answers.
        judge_model_fingerprint: Model fingerprint for judge.
        allow_unpinned_checksum: Bypass SHA-256 pin requirement for dataset.

    Returns:
        Path to the generated report Markdown file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    plots_dir = out / 'plots'

    # Load judgments
    judgments_raw = read_jsonl(judgments_path)
    if not judgments_raw:
        raise ValueError(f'No judgments found in {judgments_path}')
    judgments = [LongMemEvalJudgment(**r) for r in judgments_raw]

    # Load hypotheses
    hypotheses: list[LongMemEvalHypothesis] = []
    if hypotheses_path and Path(hypotheses_path).exists():
        hypotheses = [LongMemEvalHypothesis(**r) for r in read_jsonl(hypotheses_path)]

    # Infer fingerprints from data if not supplied
    if not answer_model_fingerprint and hypotheses:
        fps = [h.answer_model_fingerprint for h in hypotheses if h.answer_model_fingerprint]
        answer_model_fingerprint = statistics.mode(fps) if fps else ''
    if not judge_model_fingerprint:
        fps = [j.judge_model_fingerprint for j in judgments if j.judge_model_fingerprint]
        judge_model_fingerprint = statistics.mode(fps) if fps else ''

    # Build the structured report
    per_category, overall, abst_p, abst_r = _aggregate(judgments)
    sha = dataset_sha256(Path(dataset_path), variant) if dataset_path else ''

    # Compute total cost from hypotheses
    total_cost_usd = sum(h.cost_usd or 0.0 for h in hypotheses) if hypotheses else None

    report = LongMemEvalReport(
        run_id=run_id or str(uuid4())[:8],
        variant=variant,
        total_questions=len(judgments),
        overall_accuracy=overall,
        per_category=per_category,
        abstention_precision=abst_p,
        abstention_recall=abst_r,
        answer_model_fingerprint=answer_model_fingerprint,
        judge_model_fingerprint=judge_model_fingerprint,
        total_cost_usd=total_cost_usd,
        dataset_sha256=sha,
    )

    # Resolve traces dir
    resolved_traces_dir = traces_dir or str(out / 'traces')

    # Load and enrich data
    console.print('[bold]Loading data...[/bold]')
    details, retrieval_breakdown = _load_data(judgments, hypotheses, resolved_traces_dir)

    # Compute retrieval recall (Axis 1) if dataset + traces available
    recall_metrics: list[RecallMetrics] | None = None
    if dataset_path and Path(resolved_traces_dir).is_dir():
        try:
            questions = _load_variant(
                Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum
            )
            gold_map = {q.question_id: q.answer_session_ids for q in questions}
            category_map = {q.question_id: q.category.value for q in questions}
            traces = parse_traces_dir(Path(resolved_traces_dir))
            computed = compute_batch_recall(traces, gold_map, category_map)
            n_with_gold = sum(1 for m in computed if m.gold_session_ids)
            console.print(
                f'  Computed recall@3 for {len(computed)} traces ({n_with_gold} with gold IDs)'
            )
            recall_metrics = computed
        except Exception:
            logger.warning('Failed to compute retrieval recall', exc_info=True)

    # Generate plots
    console.print('[bold]Generating plots...[/bold]')
    plot_files = _generate_plots(details, retrieval_breakdown, plots_dir)
    console.print(f'  {len(plot_files)} plots -> {plots_dir}')

    # Generate markdown
    console.print('[bold]Generating report...[/bold]')
    md = _generate_markdown(
        report,
        details,
        retrieval_breakdown,
        'plots',
        plot_files,
        recall_metrics=recall_metrics,
    )

    # Write outputs
    results_path = out / 'results.json'
    results_data = report.model_dump()
    results_path.write_text(json.dumps(results_data, indent=2, default=str))

    md_path = out / 'longmemeval_report.md'
    md_path.write_text(md)

    # Terminal output
    _print_summary(report)
    console.print(f'\n[bold green]Report -> {md_path}[/bold green]')
    return str(md_path)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def aggregate_for_test(judgments: list[LongMemEvalJudgment]) -> dict[str, Any]:
    """Convenience wrapper used by tests to exercise aggregation directly."""
    per_category, overall, abst_p, abst_r = _aggregate(judgments)
    return {
        'overall_accuracy': overall,
        'per_category': [c.model_dump() for c in per_category],
        'abstention_precision': abst_p,
        'abstention_recall': abst_r,
    }
