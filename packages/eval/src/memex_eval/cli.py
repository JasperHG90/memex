"""Typer CLI for memex-eval: `memex-eval run`, `memex-eval longmemeval`, etc."""

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


@app.command()
def longmemeval(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LongMemEval dataset directory.'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    output: str | None = typer.Option(None, '--output', '-o', help='Export results to JSON file.'),
    limit: int | None = typer.Option(
        None, '--limit', '-n', help='Limit number of questions to evaluate.'
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Run the LongMemEval benchmark against a Memex server."""
    _setup_logging(verbose)

    from memex_eval.external.longmemeval import run_longmemeval

    result = asyncio.run(
        run_longmemeval(
            dataset_path=dataset_path,
            server_url=server,
            judge_model=judge_model,
            limit=limit,
        )
    )

    from memex_eval.external.longmemeval import print_longmemeval_report

    print_longmemeval_report(result)

    if output:
        import json
        from pathlib import Path

        Path(output).write_text(json.dumps(result, indent=2))
        console.print(f'[dim]Results exported to {output}[/dim]')


@app.command()
def locomo(
    dataset_path: str = typer.Option(
        ..., '--dataset-path', '-d', help='Path to the LoCoMo dataset directory.'
    ),
    server: str = typer.Option(DEFAULT_SERVER, '--server', '-s', help='Memex API server URL.'),
    judge_model: str | None = typer.Option(
        None, '--judge-model', help='Override the LLM judge model.'
    ),
    output: str | None = typer.Option(None, '--output', '-o', help='Export results to JSON file.'),
    limit: int | None = typer.Option(
        None, '--limit', '-n', help='Limit number of conversations to evaluate.'
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v', help='Enable verbose logging.'),
) -> None:
    """Run the LoCoMo benchmark against a Memex server."""
    _setup_logging(verbose)

    from memex_eval.external.locomo import run_locomo

    result = asyncio.run(
        run_locomo(
            dataset_path=dataset_path,
            server_url=server,
            judge_model=judge_model,
            limit=limit,
        )
    )

    from memex_eval.external.locomo import print_locomo_report

    print_locomo_report(result)

    if output:
        import json
        from pathlib import Path

        Path(output).write_text(json.dumps(result, indent=2))
        console.print(f'[dim]Results exported to {output}[/dim]')


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
