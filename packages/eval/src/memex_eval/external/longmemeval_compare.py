"""Compare multiple LongMemEval runs side-by-side (agent vs note-only vs memory-only).

Takes N labelled run directories (each containing ``judgments.jsonl`` and
``hypotheses.jsonl``) and produces a comparison markdown report covering:

- Accuracy + interpretation breakdown per mode
- Retrieval recall@K against gold session IDs
- Token/cost/latency efficiency
- Per-question disagreement table (which modes got each question right)

Intended to isolate retrieval quality from agent quality by running the
same questions through three retrieval configurations.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger('memex_eval.longmemeval_compare')


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _dedupe_by_qid(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep the first record per question_id."""
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        qid = r.get('question_id')
        if qid and qid not in out:
            out[qid] = r
    return out


def _summarize_run(
    label: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Compute summary stats for a single mode run."""
    judgments = _dedupe_by_qid(_load_jsonl(run_dir / 'judgments.jsonl'))
    hypotheses = _dedupe_by_qid(_load_jsonl(run_dir / 'hypotheses.jsonl'))

    if not judgments:
        logger.warning('No judgments found at %s', run_dir / 'judgments.jsonl')

    n = len(judgments)
    correct = sum(1 for j in judgments.values() if j.get('correct'))

    interpretations: Counter[str] = Counter()
    for j in judgments.values():
        interpretations[j.get('interpretation') or 'unknown'] += 1

    retrieval_found = sum(1 for j in judgments.values() if j.get('retrieval_contains_answer'))

    # Efficiency aggregates
    latencies = [float(h.get('latency_ms') or 0) / 1000.0 for h in hypotheses.values()]
    total_tokens = [
        int(h.get('input_tokens') or 0) + int(h.get('output_tokens') or 0)
        for h in hypotheses.values()
    ]
    costs = [float(h.get('cost_usd') or 0.0) for h in hypotheses.values()]

    return {
        'label': label,
        'run_dir': str(run_dir),
        'n_questions': n,
        'accuracy': correct / n if n else 0.0,
        'correct': correct,
        'retrieval_recall': retrieval_found / n if n else 0.0,
        'retrieval_found': retrieval_found,
        'interpretations': dict(interpretations),
        'avg_latency_s': statistics.mean(latencies) if latencies else 0.0,
        'median_latency_s': statistics.median(latencies) if latencies else 0.0,
        'avg_total_tokens': statistics.mean(total_tokens) if total_tokens else 0.0,
        'total_cost_usd': sum(costs),
        'avg_cost_usd': statistics.mean(costs) if costs else 0.0,
        'judgments': judgments,
        'hypotheses': hypotheses,
    }


def generate_comparison_report(
    mode_runs: list[tuple[str, Path]],
    output_dir: Path,
) -> dict[str, Any]:
    """Generate a comparison markdown report across mode runs.

    Args:
        mode_runs: list of ``(label, run_dir)`` tuples. ``run_dir`` must
            contain ``judgments.jsonl`` and ``hypotheses.jsonl``.
        output_dir: where to write the report.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries = [_summarize_run(label, rd) for label, rd in mode_runs]

    # Collect all qids seen across runs, and compute per-question correctness
    all_qids: set[str] = set()
    for s in summaries:
        all_qids.update(s['judgments'].keys())
    qids_sorted = sorted(all_qids)

    # Build report
    lines: list[str] = []
    lines.append('# LongMemEval Mode Comparison\n')
    lines.append(f'Comparing {len(summaries)} modes across {len(qids_sorted)} questions.\n')

    # --- Accuracy table ---
    lines.append('## Accuracy\n')
    lines.append('| Mode | N | Correct | Accuracy | Retrieval recall |')
    lines.append('|---|---:|---:|---:|---:|')
    for s in summaries:
        lines.append(
            f'| {s["label"]} | {s["n_questions"]} | {s["correct"]} | '
            f'{s["accuracy"]:.1%} | {s["retrieval_recall"]:.1%} |'
        )
    lines.append('')

    # --- Interpretation breakdown ---
    lines.append('## Interpretation breakdown\n')
    all_interps = sorted({k for s in summaries for k in s['interpretations'].keys()})
    header = '| Mode | ' + ' | '.join(all_interps) + ' |'
    sep = '|---|' + '|'.join(['---:'] * len(all_interps)) + '|'
    lines.append(header)
    lines.append(sep)
    for s in summaries:
        row = [s['label']] + [str(s['interpretations'].get(k, 0)) for k in all_interps]
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # --- Efficiency ---
    lines.append('## Efficiency\n')
    lines.append(
        '| Mode | Avg latency (s) | Median latency (s) | Avg total tokens | Avg cost (USD) | Total cost (USD) |'
    )
    lines.append('|---|---:|---:|---:|---:|---:|')
    for s in summaries:
        lines.append(
            f'| {s["label"]} | {s["avg_latency_s"]:.1f} | {s["median_latency_s"]:.1f} '
            f'| {s["avg_total_tokens"]:,.0f} | ${s["avg_cost_usd"]:.4f} '
            f'| ${s["total_cost_usd"]:.2f} |'
        )
    lines.append('')

    # --- Per-question disagreement ---
    lines.append('## Per-question correctness\n')
    header_cells = ['QID'] + [s['label'] for s in summaries]
    lines.append('| ' + ' | '.join(header_cells) + ' |')
    lines.append('|' + '|'.join(['---'] * len(header_cells)) + '|')
    for qid in qids_sorted:
        row = [qid]
        for s in summaries:
            j = s['judgments'].get(qid)
            if j is None:
                row.append('—')
            elif j.get('correct'):
                row.append('✓')
            elif j.get('retrieval_contains_answer'):
                row.append('model-fail')
            else:
                row.append('retr-fail')
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # --- Disagreement summary ---
    lines.append('## Where modes disagree\n')
    disagree_all_wrong: list[str] = []
    disagree_mixed: list[tuple[str, list[str]]] = []
    agree_correct: list[str] = []

    for qid in qids_sorted:
        results = []
        for s in summaries:
            j = s['judgments'].get(qid)
            results.append(bool(j and j.get('correct')))
        if all(results):
            agree_correct.append(qid)
        elif not any(results):
            disagree_all_wrong.append(qid)
        else:
            modes_correct = [s['label'] for s, r in zip(summaries, results) if r]
            disagree_mixed.append((qid, modes_correct))

    lines.append(f'- All modes correct: **{len(agree_correct)}** questions')
    lines.append(f'- All modes wrong: **{len(disagree_all_wrong)}** questions')
    lines.append(f'- Modes disagree: **{len(disagree_mixed)}** questions\n')

    if disagree_mixed:
        lines.append('### Questions where only some modes got it right')
        for qid, modes in disagree_mixed[:50]:
            lines.append(f'- `{qid}` — correct in: {", ".join(modes)}')
        if len(disagree_mixed) > 50:
            lines.append(f'- (... and {len(disagree_mixed) - 50} more)')
        lines.append('')

    report_path = output_dir / 'comparison_report.md'
    report_path.write_text('\n'.join(lines))

    # JSON summary for programmatic consumption
    json_summary = {
        'modes': [
            {k: v for k, v in s.items() if k not in ('judgments', 'hypotheses')} for s in summaries
        ],
        'n_questions_total': len(qids_sorted),
        'agreement': {
            'all_correct': len(agree_correct),
            'all_wrong': len(disagree_all_wrong),
            'mixed': len(disagree_mixed),
        },
    }
    (output_dir / 'comparison.json').write_text(json.dumps(json_summary, indent=2))

    return json_summary
