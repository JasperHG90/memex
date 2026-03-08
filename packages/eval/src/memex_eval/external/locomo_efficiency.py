"""LoCoMo efficiency analysis: latency, token cost, tool usage patterns.

Analyzes answer records and session trace files to produce distribution
metrics for duration, tokens, turns, tool calls, and retrieval token cost.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import tiktoken
from rich.console import Console
from rich.table import Table

from memex_eval.external.locomo_common import read_jsonl

logger = logging.getLogger('memex_eval.locomo_efficiency')
console = Console()

_MEMEX_TOOL_PREFIX = 'mcp__memex__'


def _distribution(values: list[float]) -> dict[str, float]:
    """Compute distribution stats for a list of numeric values."""
    if not values:
        return {
            'mean': 0.0,
            'median': 0.0,
            'p25': 0.0,
            'p75': 0.0,
            'min': 0.0,
            'max': 0.0,
            'std': 0.0,
        }
    q = statistics.quantiles(values, n=4) if len(values) >= 2 else [values[0]] * 3
    return {
        'mean': round(statistics.mean(values), 2),
        'median': round(statistics.median(values), 2),
        'p25': round(q[0], 2),
        'p75': round(q[2], 2),
        'min': round(min(values), 2),
        'max': round(max(values), 2),
        'std': round(statistics.stdev(values), 2) if len(values) >= 2 else 0.0,
    }


def _count_retrieval_tokens_from_trace(trace_path: Path, enc: tiktoken.Encoding) -> dict[str, Any]:
    """Parse a session trace JSONL and count tokens in Memex tool results.

    Returns a dict with total retrieval tokens and per-tool-type breakdown.
    """
    per_tool: Counter[str] = Counter()
    total = 0

    try:
        lines = trace_path.read_text().strip().splitlines()
    except Exception:
        logger.warning('Failed to read trace %s', trace_path)
        return {'total': 0, 'per_tool': {}}

    # Build a map of tool_use_id -> tool_name from assistant messages
    tool_id_to_name: dict[str, str] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        msg_type = obj.get('type')

        # Collect tool_use blocks from assistant messages
        if msg_type == 'assistant':
            for block in obj.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    tool_name = block.get('name', '')
                    tool_id = block.get('id', '')
                    if tool_id and tool_name.startswith(_MEMEX_TOOL_PREFIX):
                        tool_id_to_name[tool_id] = tool_name

        # Count tokens in tool results that match Memex tools
        if msg_type == 'user':
            for block in obj.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tool_use_id = block.get('tool_use_id', '')
                    tool_name = tool_id_to_name.get(tool_use_id)
                    if not tool_name:
                        continue

                    # Extract text content from the result
                    content = block.get('content', '')
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get('type') == 'text':
                                text_parts.append(part.get('text', ''))
                        content = '\n'.join(text_parts)
                    elif not isinstance(content, str):
                        content = str(content)

                    tokens = len(enc.encode(content))
                    per_tool[tool_name] += tokens
                    total += tokens

    return {'total': total, 'per_tool': dict(per_tool)}


def analyze_efficiency(
    answers_path: str,
    output_path: str,
    traces_dir: str,
) -> dict[str, Any]:
    """Analyze efficiency metrics from answers and session traces.

    Args:
        answers_path: Path to answers.jsonl from Phase 2.
        output_path: Path to write the efficiency report JSON.
        traces_dir: Directory containing per-question trace JSONL files.

    Returns:
        The full efficiency report dict.
    """
    answers = read_jsonl(answers_path)
    if not answers:
        raise ValueError(f'No answers found in {answers_path}')

    traces_path = Path(traces_dir)

    # Initialize tiktoken encoder
    enc = tiktoken.get_encoding('cl100k_base')

    # Collect per-question metrics
    durations: list[float] = []
    total_tokens_list: list[float] = []
    num_turns_list: list[float] = []
    num_tool_calls_list: list[float] = []
    retrieval_tokens_list: list[float] = []
    tool_counter: Counter[str] = Counter()
    tool_per_question: Counter[str] = Counter()
    retrieval_per_tool: Counter[str] = Counter()
    n_questions = len(answers)
    n_with_traces = 0

    for a in answers:
        qid = a['id']
        duration = a.get('duration_s', 0.0)
        tokens = a.get('tokens', {})
        input_tokens = tokens.get('input', 0)
        output_tokens = tokens.get('output', 0)
        tool_calls = a.get('tool_calls', [])

        durations.append(duration)
        total_tokens_list.append(input_tokens + output_tokens)
        num_turns_list.append(a.get('num_turns', 0))
        num_tool_calls_list.append(len(tool_calls))

        # Tool frequency
        for tc in tool_calls:
            name = tc.get('name', '')
            tool_counter[name] += 1
            tool_per_question[name] += 1  # will divide later

        # Retrieval tokens from trace
        trace_file = traces_path / f'{qid}.jsonl'
        if trace_file.exists():
            n_with_traces += 1
            rt = _count_retrieval_tokens_from_trace(trace_file, enc)
            retrieval_tokens_list.append(rt['total'])
            for tool_name, count in rt['per_tool'].items():
                retrieval_per_tool[tool_name] += count
        else:
            retrieval_tokens_list.append(0)

    # Distribution metrics
    distributions = {
        'duration_s': _distribution(durations),
        'total_tokens': _distribution(total_tokens_list),
        'num_turns': _distribution(num_turns_list),
        'num_tool_calls': _distribution(num_tool_calls_list),
        'retrieval_tokens': _distribution(retrieval_tokens_list),
    }

    # Tool call analysis
    tool_frequency = dict(tool_counter.most_common())
    tool_mean_per_question = {
        name: round(count / n_questions, 2) for name, count in tool_per_question.most_common()
    }

    # Retrieval token breakdown
    total_retrieval = sum(retrieval_tokens_list)
    total_all_tokens = sum(total_tokens_list)
    retrieval_breakdown = {
        'total_retrieval_tokens': total_retrieval,
        'per_tool': dict(retrieval_per_tool.most_common()),
        'retrieval_to_total_ratio': (
            round(total_retrieval / total_all_tokens, 4) if total_all_tokens > 0 else 0.0
        ),
        'questions_with_traces': n_with_traces,
    }

    report: dict[str, Any] = {
        'benchmark': 'LoCoMo-Efficiency',
        'answers_file': answers_path,
        'traces_dir': traces_dir,
        'n_questions': n_questions,
        'distributions': distributions,
        'tool_frequency': tool_frequency,
        'tool_mean_per_question': tool_mean_per_question,
        'retrieval_breakdown': retrieval_breakdown,
    }

    # Write report
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(report, indent=2, default=str))

    # Print terminal summary
    _print_report(report)

    return report


def _print_report(report: dict[str, Any]) -> None:
    """Print a rich terminal summary of the efficiency report."""
    console.print()
    console.rule('[bold]LoCoMo Efficiency Report[/bold]')
    console.print()
    console.print(f'[dim]{report["n_questions"]} questions analyzed[/dim]')
    console.print()

    # Distribution table
    dist_table = Table(show_header=True, header_style='bold cyan', title='Distribution Metrics')
    dist_table.add_column('Metric', style='white')
    for stat in ['mean', 'median', 'p25', 'p75', 'min', 'max', 'std']:
        dist_table.add_column(stat, justify='right')

    dist_labels = {
        'duration_s': 'Duration (s)',
        'total_tokens': 'Total Tokens',
        'num_turns': 'Num Turns',
        'num_tool_calls': 'Num Tool Calls',
        'retrieval_tokens': 'Retrieval Tokens',
    }

    for key, label in dist_labels.items():
        d = report['distributions'][key]
        fmt = '.1f' if key == 'duration_s' else ',.0f'
        dist_table.add_row(
            label,
            *[f'{d[s]:{fmt}}' for s in ['mean', 'median', 'p25', 'p75', 'min', 'max', 'std']],
        )

    console.print(dist_table)
    console.print()

    # Tool frequency table
    tool_table = Table(show_header=True, header_style='bold cyan', title='Tool Call Frequency')
    tool_table.add_column('Tool', style='white')
    tool_table.add_column('Total Calls', justify='right')
    tool_table.add_column('Mean/Question', justify='right')

    for tool_name, count in sorted(
        report['tool_frequency'].items(), key=lambda x: x[1], reverse=True
    ):
        mean = report['tool_mean_per_question'].get(tool_name, 0)
        tool_table.add_row(tool_name, str(count), f'{mean:.2f}')

    console.print(tool_table)
    console.print()

    # Retrieval breakdown
    rb = report['retrieval_breakdown']
    console.print('[bold]Retrieval Token Breakdown[/bold]')
    console.print(f'  Total retrieval tokens: {rb["total_retrieval_tokens"]:,}')
    console.print(f'  Retrieval/total ratio:  {rb["retrieval_to_total_ratio"]:.2%}')
    console.print(f'  Questions with traces:  {rb["questions_with_traces"]}')

    if rb['per_tool']:
        console.print('  Per tool:')
        for tool_name, tokens in sorted(rb['per_tool'].items(), key=lambda x: x[1], reverse=True):
            console.print(f'    {tool_name}: {tokens:,}')

    console.print()
