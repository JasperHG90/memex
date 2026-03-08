"""Typer CLI for memex-eval: `memex-eval run`, `memex-eval locomo-*`, etc."""

from __future__ import annotations

import asyncio
import logging

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
    from memex_eval.report import print_report, export_json

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

    # Exit with non-zero if any checks failed
    if result.total_failed > 0 or result.total_errored > 0:
        raise typer.Exit(code=1)


@app.command('locomo-export')
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
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 1: Export LoCoMo questions to JSONL."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_export import export_questions

    export_questions(
        dataset_path=dataset_path,
        output=output,
        limit=limit,
        seed=seed,
        conversation_index=conversation,
    )


@app.command('locomo-answer')
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
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 2: Answer LoCoMo questions using a curated CLI agent."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_answer import AnswerMethod, answer_questions

    answer_questions(
        method=AnswerMethod(method),
        questions_path=questions,
        output_path=output,
        server_url=server,
    )


@app.command('locomo-judge')
def locomo_judge_cmd(
    questions: str = typer.Option(
        'questions.jsonl', '--questions', '-q', help='Input questions JSONL.'
    ),
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    output: str = typer.Option('report.json', '--output', '-o', help='Output report JSON.'),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Phase 3: Judge LoCoMo answers and produce a graded report."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_judge import judge_answers

    asyncio.run(
        judge_answers(
            questions_path=questions,
            answers_path=answers,
            output_path=output,
            judge_model=judge_model,
        )
    )


@app.command('locomo-efficiency')
def locomo_efficiency_cmd(
    answers: str = typer.Option('answers.jsonl', '--answers', '-a', help='Input answers JSONL.'),
    traces_dir: str = typer.Option(
        ..., '--traces-dir', '-t', help='Directory with trace JSONL files.'
    ),
    output: str = typer.Option('efficiency.json', '--output', '-o', help='Output efficiency JSON.'),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Analyze LoCoMo answer efficiency: latency, tokens, tool usage, retrieval cost."""
    _setup_logging(verbose)

    from memex_eval.external.locomo_efficiency import analyze_efficiency

    analyze_efficiency(
        answers_path=answers,
        output_path=output,
        traces_dir=traces_dir,
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
