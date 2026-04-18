"""Typer CLI for memex-eval: `memex-eval run`, `memex-eval longmemeval ...`."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import typer
from rich.console import Console

app = typer.Typer(
    name='memex-eval',
    help='Quality benchmarks for the Memex memory system.',
    no_args_is_help=True,
)
console = Console()

DEFAULT_SERVER = 'http://localhost:8001/api/v1/'


@app.command()
def run(
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    group: str | None = typer.Option(
        None, '--group', '-g', help='Run only a specific scenario group.'
    ),
    no_llm_judge: bool = typer.Option(
        False, '--no-llm-judge', help='Skip LLM-judged checks (deterministic only).'
    ),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    output: str | None = typer.Option(None, '--output', '-o', help='Export results to JSON file.'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Run the internal quality benchmark against a Memex server."""
    _setup_logging(verbose)

    from memex_eval.internal.runner import run_benchmark
    from memex_eval.report import export_json, print_report

    result = asyncio.run(
        run_benchmark(
            server_url=server,
            group_filter=group,
            use_llm_judge=not no_llm_judge,
            judge_model=judge_model,
        )
    )

    print_report(result)

    if output:
        export_json(result, output)

    if result.total_failed > 0 or result.total_errored > 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# LongMemEval subcommands
# ---------------------------------------------------------------------------

longmemeval_app = typer.Typer(
    name='longmemeval',
    help='Run the LongMemEval external benchmark (xiaowu0162/longmemeval).',
    no_args_is_help=True,
)
app.add_typer(longmemeval_app, name='longmemeval')


@longmemeval_app.command('ingest')
def longmemeval_ingest_cmd(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LongMemEval dataset directory or file.'
    ),
    variant: str = typer.Option(
        's',
        '--variant',
        help=('Dataset variant: "s" (default, matches agentmemory baseline), "oracle", or "m".'),
    ),
    run_id: str = typer.Option(
        ..., '--run-id', help='Identifier for this run (used in the per-run vault name).'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    question_limit: int | None = typer.Option(
        None, '--questions', '-n', help='Limit ingest to the first N questions.'
    ),
    clean: bool = typer.Option(False, '--clean', help='Delete existing notes and re-ingest.'),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only). Logs the computed hash.',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 0: Ingest LongMemEval sessions into a dedicated vault."""
    _setup_logging(verbose)

    from memex_eval.external.longmemeval_ingest import ingest_longmemeval

    asyncio.run(
        ingest_longmemeval(
            server_url=server,
            dataset_path=dataset_path,
            variant=variant,
            run_id=run_id,
            question_limit=question_limit,
            clean=clean,
            allow_unpinned_checksum=allow_unpinned_checksum,
        )
    )


@longmemeval_app.command('answer')
def longmemeval_answer_cmd(
    dataset_path: str = typer.Option(..., '--dataset-path', '-d'),
    variant: str = typer.Option('s', '--variant'),
    run_id: str = typer.Option(..., '--run-id'),
    output: str = typer.Option('hypotheses.jsonl', '--output', '-o'),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s'),
    method: str = typer.Option(
        'claude-code',
        '--method',
        help='Answer-module driver. One of "claude-code" or "gemini-cli".',
    ),
    plugin_dir: str | None = typer.Option(
        None,
        '--plugin-dir',
        help=(
            'Path to the memex Claude Code plugin directory. Defaults to '
            'packages/claude-code-plugin/ in this repo, or $MEMEX_CLAUDE_PLUGIN_DIR.'
        ),
    ),
    question_limit: int | None = typer.Option(None, '--questions', '-n'),
    subagent_timeout_s: float = typer.Option(
        300.0,
        '--subagent-timeout-s',
        help='Per-question timeout for the Claude Code subagent (seconds).',
    ),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only). Logs the computed hash.',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Phase 2: Answer questions and emit hypotheses JSONL."""
    _setup_logging(verbose)

    from memex_eval.external.longmemeval_answer import AnswerMethod, answer_questions

    asyncio.run(
        answer_questions(
            server_url=server,
            dataset_path=dataset_path,
            variant=variant,
            run_id=run_id,
            output_path=output,
            method=AnswerMethod(method),
            plugin_dir=plugin_dir,
            question_limit=question_limit,
            subagent_timeout_s=subagent_timeout_s,
            allow_unpinned_checksum=allow_unpinned_checksum,
        )
    )


@longmemeval_app.command('judge')
def longmemeval_judge_cmd(
    dataset_path: str = typer.Option(..., '--dataset-path', '-d'),
    variant: str = typer.Option('s', '--variant'),
    hypotheses: str = typer.Option('hypotheses.jsonl', '--hypotheses'),
    output: str = typer.Option('judgments.jsonl', '--output', '-o'),
    judge_model: str | None = typer.Option(None, '--judge-model'),
    cache: str | None = typer.Option(
        None, '--cache', help='JSON file with cached judge responses (for offline runs).'
    ),
    traces_dir: str | None = typer.Option(
        None,
        '--traces-dir',
        help='Directory with per-question trace JSONL files for retrieval containment judging.',
    ),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only). Logs the computed hash.',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Phase 3: Judge hypotheses against ground-truth answers."""
    _setup_logging(verbose)

    from pathlib import Path

    from memex_eval.external.longmemeval_judge import judge_hypotheses

    resolved_traces = traces_dir or str(Path(hypotheses).parent / 'traces')

    asyncio.run(
        judge_hypotheses(
            dataset_path=dataset_path,
            variant=variant,
            hypotheses_path=hypotheses,
            output_path=output,
            judge_model=judge_model,
            cache_path=cache,
            allow_unpinned_checksum=allow_unpinned_checksum,
            traces_dir=resolved_traces,
        )
    )


@longmemeval_app.command('report')
def longmemeval_report_cmd(
    judgments: str = typer.Option('judgments.jsonl', '--judgments'),
    output_dir: str = typer.Option('report', '--output-dir', '-o'),
    run_id: str | None = typer.Option(None, '--run-id'),
    variant: str = typer.Option('s', '--variant'),
    dataset_path: str | None = typer.Option(
        None, '--dataset-path', '-d', help='Optional, used to embed dataset SHA-256 in report.'
    ),
    hypotheses_path: str | None = typer.Option(
        None, '--hypotheses', help='Path to hypotheses.jsonl (auto-detected if omitted).'
    ),
    traces_dir: str | None = typer.Option(
        None, '--traces-dir', help='Directory with per-question trace JSONL files.'
    ),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only).',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Phase 4: Aggregate judgments into a report with efficiency analysis."""
    _setup_logging(verbose)

    from pathlib import Path

    from memex_eval.external.longmemeval_report import generate_report

    resolved_hypotheses = hypotheses_path or judgments.replace(
        'judgments.jsonl', 'hypotheses.jsonl'
    )
    resolved_traces = traces_dir or str(Path(judgments).parent / 'traces')

    generate_report(
        judgments_path=judgments,
        output_dir=output_dir,
        run_id=run_id,
        variant=variant,
        dataset_path=dataset_path,
        hypotheses_path=resolved_hypotheses,
        traces_dir=resolved_traces,
        allow_unpinned_checksum=allow_unpinned_checksum,
    )


@longmemeval_app.command('parse-trace')
def longmemeval_parse_trace_cmd(
    traces_path: str = typer.Argument(
        help='Path to a trace JSONL file or directory of trace files.'
    ),
    dataset_path: str | None = typer.Option(
        None,
        '--dataset-path',
        '-d',
        help='Dataset file to compute recall against gold session IDs.',
    ),
    variant: str = typer.Option('s', '--variant'),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only).',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Parse trace files and show per-question retrieval breakdown."""
    _setup_logging(verbose)

    from pathlib import Path

    from memex_eval.external.longmemeval_trace_parser import (
        compute_recall,
        format_question_breakdown,
        parse_trace,
        parse_traces_dir,
    )

    p = Path(traces_path)

    # Load gold data if dataset provided
    gold_map: dict[str, list[str]] = {}
    category_map: dict[str, str] = {}
    if dataset_path:
        from memex_eval.external.longmemeval_common import _load_variant

        questions = _load_variant(
            Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum
        )
        gold_map = {q.question_id: q.answer_session_ids for q in questions}
        category_map = {q.question_id: q.category.value for q in questions}

    # Parse traces
    if p.is_dir():
        traces = parse_traces_dir(p)
    elif p.is_file():
        traces = [parse_trace(p)]
    else:
        console.print(f'[red]Not found: {traces_path}[/red]')
        raise typer.Exit(code=1)

    if not traces:
        console.print('[dim]No trace files found.[/dim]')
        raise typer.Exit(code=0)

    # Output per-question breakdown
    total_gold = 0
    total_found = 0

    for trace in traces:
        gold = gold_map.get(trace.question_id, [])
        cat = category_map.get(trace.question_id, '')
        metrics = compute_recall(trace, gold, category=cat) if gold else None
        print(format_question_breakdown(trace, metrics))
        print()

        if metrics:
            total_gold += len(metrics.gold_session_ids)
            total_found += len(metrics.found_session_ids)

    # Summary
    if total_gold > 0:
        overall_recall = total_found / total_gold
        console.print(
            f'[bold]Overall Recall@3: {total_found}/{total_gold} ({overall_recall:.3f})[/bold]'
        )
    else:
        console.print('[dim]No gold session IDs available for recall computation.[/dim]')


@longmemeval_app.command('run')
def longmemeval_run_cmd(
    dataset_path: str = typer.Option(..., '--dataset-path', '-d'),
    variant: str = typer.Option('s', '--variant'),
    run_id: str | None = typer.Option(
        None, '--run-id', help='Identifier for this run (auto-generated if omitted).'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s'),
    output_dir: str = typer.Option('./longmemeval-run', '--output-dir', '-o'),
    method: str = typer.Option(
        'claude-code', '--method', help='Answer driver: "claude-code" | "gemini-cli".'
    ),
    plugin_dir: str | None = typer.Option(
        None,
        '--plugin-dir',
        help='Path to the memex Claude Code plugin. Defaults to packages/claude-code-plugin/.',
    ),
    judge_model: str | None = typer.Option(None, '--judge-model'),
    cache: str | None = typer.Option(None, '--cache'),
    question_limit: int | None = typer.Option(None, '--questions', '-n'),
    subagent_timeout_s: float = typer.Option(300.0, '--subagent-timeout-s'),
    allow_unpinned_checksum: bool = typer.Option(
        False,
        '--allow-unpinned-checksum',
        help='Bypass the dataset SHA-256 pin requirement (dev only). Logs the computed hash.',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """End-to-end: ingest -> answer -> judge -> report."""
    _setup_logging(verbose)

    from pathlib import Path

    from memex_eval.external.longmemeval_answer import AnswerMethod, answer_questions
    from memex_eval.external.longmemeval_ingest import ingest_longmemeval
    from memex_eval.external.longmemeval_judge import judge_hypotheses
    from memex_eval.external.longmemeval_report import generate_report

    rid = run_id or uuid4().hex[:8]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    hypotheses = out / 'hypotheses.jsonl'
    judgments = out / 'judgments.jsonl'

    async def _pipeline() -> None:
        await ingest_longmemeval(
            server_url=server,
            dataset_path=dataset_path,
            variant=variant,
            run_id=rid,
            question_limit=question_limit,
            allow_unpinned_checksum=allow_unpinned_checksum,
        )
        await answer_questions(
            server_url=server,
            dataset_path=dataset_path,
            variant=variant,
            run_id=rid,
            output_path=str(hypotheses),
            method=AnswerMethod(method),
            plugin_dir=plugin_dir,
            question_limit=question_limit,
            subagent_timeout_s=subagent_timeout_s,
            allow_unpinned_checksum=allow_unpinned_checksum,
        )
        await judge_hypotheses(
            dataset_path=dataset_path,
            variant=variant,
            hypotheses_path=str(hypotheses),
            output_path=str(judgments),
            judge_model=judge_model,
            cache_path=cache,
            allow_unpinned_checksum=allow_unpinned_checksum,
            traces_dir=str(out / 'traces'),
        )

    asyncio.run(_pipeline())
    generate_report(
        judgments_path=str(judgments),
        output_dir=str(out),
        run_id=rid,
        variant=variant,
        dataset_path=dataset_path,
        hypotheses_path=str(hypotheses),
        traces_dir=str(out / 'traces'),
        allow_unpinned_checksum=allow_unpinned_checksum,
    )


def _setup_logging(verbose: bool) -> None:
    """Configure logging for the benchmark run."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)-8s %(name)s — %(message)s',
        datefmt='%H:%M:%S',
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('dspy').setLevel(logging.WARNING)
