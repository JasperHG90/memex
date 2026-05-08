"""Typer CLI for memex-eval: `memex-eval internal run`, `memex-eval locomo <sub>`, `memex-eval longmemeval <sub>`."""

from __future__ import annotations

import asyncio
import logging
import warnings
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


def _make_recorder(
    mlflow_uri: str | None,
    mlflow_experiment: str,
    mlflow_run_name: str | None,
):
    """Build a recorder from CLI options, returning NullRecorder if disabled."""
    from memex_eval.recorders import get_recorder

    return get_recorder(
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
    )


# ---------------------------------------------------------------------------
# Internal benchmark
# ---------------------------------------------------------------------------

internal_app = typer.Typer(
    name='internal',
    help='Internal quality benchmark.',
    no_args_is_help=True,
)
app.add_typer(internal_app, name='internal')


@internal_app.command('run')
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
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
        help='MLflow experiment name.',
    ),
    mlflow_run_name: str | None = typer.Option(
        None,
        '--mlflow-run-name',
        help='Override default MLflow run name.',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Run the internal quality benchmark against a Memex server."""
    _setup_logging(verbose)

    from memex_eval.internal.runner import run_benchmark
    from memex_eval.report import print_report, export_json

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'internal',
                'server_url': server,
                'group_filter': group or 'all',
                'use_llm_judge': str(not no_llm_judge),
                'judge_model': judge_model or 'default',
            }
        )

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
            recorder.log_artifact(output)

        # Log metrics from BenchmarkResult.to_dict()
        summary = result.to_dict()['summary']
        recorder.log_metrics(
            {
                'summary.pass_rate': summary['pass_rate'],
                'summary.duration_ms': summary.get('duration_ms', 0),
            }
        )
        for group_data in result.to_dict()['groups']:
            prefix = f'groups.{group_data["name"]}'
            recorder.log_metrics(
                {
                    f'{prefix}.pass_rate': group_data['pass_rate'],
                    f'{prefix}.passed': group_data['passed'],
                    f'{prefix}.failed': group_data['failed'],
                    f'{prefix}.ingest_duration_ms': group_data.get('ingest_duration_ms', 0),
                    f'{prefix}.reflection_duration_ms': group_data.get('reflection_duration_ms', 0),
                }
            )

    if result.total_failed > 0 or result.total_errored > 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# LoCoMo benchmark
# ---------------------------------------------------------------------------

locomo_app = typer.Typer(
    name='locomo',
    help='LoCoMo benchmark: ingest, export, answer, judge, report, efficiency.',
    no_args_is_help=True,
)
app.add_typer(locomo_app, name='locomo')


@locomo_app.command('ingest')
def locomo_ingest_cmd(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LoCoMo dataset directory.'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    conversation: int = typer.Option(0, '--conversation', '-c', help='Conversation index (0-9).'),
    clean: bool = typer.Option(False, '--clean', help='Delete existing notes and re-ingest.'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 0: Ingest LoCoMo conversation sessions into Memex."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_ingest import ingest_locomo

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'ingest',
                'conversation_index': conversation,
            }
        )
        asyncio.run(
            ingest_locomo(
                server_url=server,
                dataset_path=dataset_path,
                conversation_index=conversation,
                clean=clean,
            )
        )


@locomo_app.command('export')
def locomo_export_cmd(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LoCoMo dataset directory.'
    ),
    output: str = typer.Option('questions.jsonl', '--output', '-o', help='Output JSONL file.'),
    limit: int | None = typer.Option(
        None, '--limit', '-n', help='Randomly sample this many QA pairs.'
    ),
    seed: int = typer.Option(42, '--seed', help='Random seed for sampling.'),
    conversation: int = typer.Option(0, '--conversation', '-c', help='Conversation index (0-9).'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 1: Export LoCoMo questions to JSONL."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_export import export_questions

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'export',
                'conversation_index': conversation,
            }
        )
        export_questions(
            dataset_path=dataset_path,
            output=output,
            limit=limit,
            seed=seed,
            conversation_index=conversation,
        )
        recorder.log_artifact(output)


@locomo_app.command('answer')
def locomo_answer_cmd(
    method: str = typer.Option(
        'claude-code',
        '--method',
        '-m',
        help='Answer method: "claude-code" or "gemini-cli".',
    ),
    questions: str = typer.Option(
        'questions.jsonl', '--questions', '-q', help='Input questions JSONL.'
    ),
    output: str = typer.Option('answers.jsonl', '--output', '-o', help='Output answers JSONL.'),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 2: Answer LoCoMo questions using a curated CLI agent."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_answer import AnswerMethod, answer_questions

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'answer',
                'method': method,
            }
        )
        answer_questions(
            method=AnswerMethod(method),
            questions_path=questions,
            output_path=output,
            server_url=server,
        )
        recorder.log_artifact(output)


@locomo_app.command('judge')
def locomo_judge_cmd(
    questions: str = typer.Option(
        'questions.jsonl', '--questions', '-q', help='Input questions JSONL.'
    ),
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    output: str = typer.Option('report.json', '--output', '-o', help='Output report JSON.'),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 3: Judge LoCoMo answers and produce a graded report."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_judge import judge_answers

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'judge',
                'judge_model': judge_model or 'default',
            }
        )
        asyncio.run(
            judge_answers(
                questions_path=questions,
                answers_path=answers,
                output_path=output,
                judge_model=judge_model,
            )
        )
        recorder.log_artifact(output)


@locomo_app.command('report')
def locomo_report_cmd(
    results: str = typer.Option(
        'results.json', '--results', '-r', help='Input judge results JSON.'
    ),
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    traces_dir: str = typer.Option(
        'traces', '--traces-dir', '-t', help='Directory with trace JSONL files.'
    ),
    output_dir: str = typer.Option(
        'report', '--output-dir', '-o', help='Output directory for report and plots.'
    ),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 4: Generate evaluation report with plots from judge results and traces."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_report import generate_report

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'report',
            }
        )
        generate_report(
            results_path=results,
            answers_path=answers,
            traces_dir=traces_dir,
            output_dir=output_dir,
        )


@locomo_app.command('efficiency')
def locomo_efficiency_cmd(
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    traces_dir: str = typer.Option(
        ..., '--traces-dir', '-t', help='Directory with trace JSONL files.'
    ),
    output: str = typer.Option('efficiency.json', '--output', '-o', help='Output efficiency JSON.'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Analyze LoCoMo answer efficiency: latency, tokens, tool usage, retrieval cost."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_efficiency import analyze_efficiency

    recorder = _make_recorder(mlflow_uri, mlflow_experiment, mlflow_run_name)

    with recorder:
        recorder.start_run()
        recorder.log_params(
            {
                'benchmark': 'locomo',
                'phase': 'efficiency',
            }
        )
        analyze_efficiency(
            answers_path=answers,
            output_path=output,
            traces_dir=traces_dir,
        )
        recorder.log_artifact(output)


# ---------------------------------------------------------------------------
# Deprecated LoCoMo aliases (backward compat — remove in next major version)
# ----------------------------------------------------------------------------


@app.command('locomo-ingest', deprecated=True)
def locomo_ingest_deprecated(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LoCoMo dataset directory.'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    conversation: int = typer.Option(0, '--conversation', '-c', help='Conversation index (0-9).'),
    clean: bool = typer.Option(False, '--clean', help='Delete existing notes and re-ingest.'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo ingest'."""
    warnings.warn(
        "'locomo-ingest' is deprecated, use 'locomo ingest'", DeprecationWarning, stacklevel=2
    )
    locomo_ingest_cmd(
        dataset_path=dataset_path,
        server=server,
        conversation=conversation,
        clean=clean,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


@app.command('locomo-export', deprecated=True)
def locomo_export_deprecated(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LoCoMo dataset directory.'
    ),
    output: str = typer.Option('questions.jsonl', '--output', '-o', help='Output JSONL file.'),
    limit: int | None = typer.Option(
        None, '--limit', '-n', help='Randomly sample this many QA pairs.'
    ),
    seed: int = typer.Option(42, '--seed', help='Random seed for sampling.'),
    conversation: int = typer.Option(0, '--conversation', '-c', help='Conversation index (0-9).'),
    mlflow_uri: str | None = typer.Option(
        None,
        '--mlflow-uri',
        envvar='MLFLOW_TRACKING_URI',
        help='Optional MLflow tracking URI.',
    ),
    mlflow_experiment: str = typer.Option(
        'memex-eval',
        '--mlflow-experiment',
        envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT',
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo export'."""
    warnings.warn(
        "'locomo-export' is deprecated, use 'locomo export'", DeprecationWarning, stacklevel=2
    )
    locomo_export_cmd(
        dataset_path=dataset_path,
        output=output,
        limit=limit,
        seed=seed,
        conversation=conversation,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


@app.command('locomo-answer', deprecated=True)
def locomo_answer_deprecated(
    method: str = typer.Option('claude-code', '--method', '-m', help='Answer method.'),
    questions: str = typer.Option(
        'questions.jsonl', '--questions', '-q', help='Input questions JSONL.'
    ),
    output: str = typer.Option('answers.jsonl', '--output', '-o', help='Output answers JSONL.'),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str = typer.Option(
        'memex-eval', '--mlflow-experiment', envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT'
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo answer'."""
    warnings.warn(
        "'locomo-answer' is deprecated, use 'locomo answer'", DeprecationWarning, stacklevel=2
    )
    locomo_answer_cmd(
        method=method,
        questions=questions,
        output=output,
        server=server,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


@app.command('locomo-judge', deprecated=True)
def locomo_judge_deprecated(
    questions: str = typer.Option(
        'questions.jsonl', '--questions', '-q', help='Input questions JSONL.'
    ),
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    output: str = typer.Option('report.json', '--output', '-o', help='Output report JSON.'),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str = typer.Option(
        'memex-eval', '--mlflow-experiment', envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT'
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo judge'."""
    warnings.warn(
        "'locomo-judge' is deprecated, use 'locomo judge'", DeprecationWarning, stacklevel=2
    )
    locomo_judge_cmd(
        questions=questions,
        answers=answers,
        output=output,
        judge_model=judge_model,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


@app.command('locomo-report', deprecated=True)
def locomo_report_deprecated(
    results: str = typer.Option(
        'results.json', '--results', '-r', help='Input judge results JSON.'
    ),
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    traces_dir: str = typer.Option(
        'traces', '--traces-dir', '-t', help='Directory with trace JSONL files.'
    ),
    output_dir: str = typer.Option(
        'report', '--output-dir', '-o', help='Output directory for report and plots.'
    ),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str = typer.Option(
        'memex-eval', '--mlflow-experiment', envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT'
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo report'."""
    warnings.warn(
        "'locomo-report' is deprecated, use 'locomo report'", DeprecationWarning, stacklevel=2
    )
    locomo_report_cmd(
        results=results,
        answers=answers,
        traces_dir=traces_dir,
        output_dir=output_dir,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


@app.command('locomo-efficiency', deprecated=True)
def locomo_efficiency_deprecated(
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    traces_dir: str = typer.Option(
        ..., '--traces-dir', '-t', help='Directory with trace JSONL files.'
    ),
    output: str = typer.Option('efficiency.json', '--output', '-o', help='Output efficiency JSON.'),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str = typer.Option(
        'memex-eval', '--mlflow-experiment', envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT'
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Deprecated: use 'memex-eval locomo efficiency'."""
    warnings.warn(
        "'locomo-efficiency' is deprecated, use 'locomo efficiency'",
        DeprecationWarning,
        stacklevel=2,
    )
    locomo_efficiency_cmd(
        answers=answers,
        traces_dir=traces_dir,
        output=output,
        mlflow_uri=mlflow_uri,
        mlflow_experiment=mlflow_experiment,
        mlflow_run_name=mlflow_run_name,
        verbose=verbose,
    )


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
    mode: str = typer.Option(
        'agent',
        '--mode',
        help=(
            'Retrieval configuration: "agent" (default, full Claude Code subagent), '
            '"note-only" (direct memex_note_search + LLM synthesis), or '
            '"memory-only" (direct memex_memory_search + LLM synthesis). '
            'Non-agent modes retry once with expand_query=true on abstention.'
        ),
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

    from memex_eval.external.longmemeval_answer import AnswerMethod, AnswerMode, answer_questions

    asyncio.run(
        answer_questions(
            server_url=server,
            dataset_path=dataset_path,
            variant=variant,
            run_id=run_id,
            output_path=output,
            method=AnswerMethod(method),
            mode=AnswerMode(mode),
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


@longmemeval_app.command('compare')
def longmemeval_compare_cmd(
    mode_run: list[str] = typer.Option(
        ...,
        '--mode-run',
        help=(
            'Labelled mode run, repeatable. Format: "<label>:<run-dir>". '
            'Example: --mode-run agent:./run-agent --mode-run note-only:./run-note. '
            'Each run dir must contain judgments.jsonl and hypotheses.jsonl.'
        ),
    ),
    output_dir: str = typer.Option('compare-report', '--output-dir', '-o'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Compare multiple mode runs (agent vs note-only vs memory-only)."""
    _setup_logging(verbose)

    from pathlib import Path

    from memex_eval.external.longmemeval_compare import generate_comparison_report

    parsed: list[tuple[str, Path]] = []
    for spec in mode_run:
        if ':' not in spec:
            raise typer.BadParameter(
                f'Invalid --mode-run spec {spec!r}. Expected "<label>:<run-dir>".'
            )
        label, run_dir = spec.split(':', 1)
        rd = Path(run_dir)
        if not rd.is_dir():
            raise typer.BadParameter(f'Run dir does not exist: {rd}')
        parsed.append((label, rd))

    summary = generate_comparison_report(parsed, Path(output_dir))
    console.print('[bold green]Comparison report written to[/bold green]', output_dir)
    console.print_json(data=summary)


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

    gold_map: dict[str, list[str]] = {}
    category_map: dict[str, str] = {}
    if dataset_path:
        from memex_eval.external.longmemeval_common import _load_variant

        questions = _load_variant(
            Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum
        )
        gold_map = {q.question_id: q.answer_session_ids for q in questions}
        category_map = {q.question_id: q.category.value for q in questions}

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
    # Quiet noisy libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('dspy').setLevel(logging.WARNING)
