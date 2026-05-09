"""Typer CLI for memex-eval: `memex-eval suite <sub>`, `memex-eval locomo <sub>`, `memex-eval longmemeval <sub>`."""

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
# Suite subcommands (the new framework — see docs/how-to/evaluation-suite.md)
# ---------------------------------------------------------------------------


suite_app = typer.Typer(
    name='suite',
    help='Run, list, validate, and track evaluation suites (optional MLflow).',
    no_args_is_help=True,
)
app.add_typer(suite_app, name='suite')


def _read_notes_file(path: str) -> str:
    """Read --notes-file with helpful errors instead of raw stack traces."""
    from pathlib import Path as _NP

    p = _NP(path)
    if not p.is_file():
        console.print(f'[red]--notes-file not found: {path}[/red]')
        raise typer.Exit(code=2)
    try:
        return p.read_text(encoding='utf-8')
    except UnicodeDecodeError as e:
        console.print(f'[red]--notes-file {path!r} is not valid UTF-8: {e}[/red]')
        raise typer.Exit(code=2) from None
    except OSError as e:
        console.print(f'[red]Could not read --notes-file {path!r}: {e}[/red]')
        raise typer.Exit(code=2) from None


def _resolve_overrides_to_env(
    overrides: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Translate dotted-path key=value overrides to MEMEX env-var form.

    Returns ``(override_dict, env_dict)`` where ``override_dict`` is the
    raw user input (logged as MLflow params) and ``env_dict`` is the env
    overlay shape — kept for callers that want to spawn a server with the
    override applied.
    """
    out_overrides: dict[str, str] = {}
    out_env: dict[str, str] = {}
    for ov in overrides:
        if '=' not in ov:
            raise typer.BadParameter(f'--override must be KEY=VALUE; got {ov!r}')
        key, value = ov.split('=', 1)
        out_overrides[key.strip()] = value.strip()
        env_key = 'MEMEX_' + key.strip().upper().replace('.', '__')
        out_env[env_key] = value.strip()
    return out_overrides, out_env


@suite_app.command('list')
def suite_list(
    json_output: bool = typer.Option(False, '--json', help='Machine-readable output.'),
) -> None:
    """List every discoverable suite with metadata."""
    from memex_eval.suite import discover_suites

    suites = discover_suites()
    if json_output:
        import json as _json

        payload = [
            {
                'name': s.metadata.name,
                'version': s.metadata.suite_version,
                'schema_version': s.metadata.schema_version,
                'tags': s.metadata.tags,
                'primary_metrics': s.metadata.primary_metrics,
                'requires_llm_judge': s.metadata.requires_llm_judge,
                'default_answer_mode': s.metadata.default_answer_mode,
                'scenario_count': len(s.scenarios),
            }
            for s in suites
        ]
        console.print(_json.dumps(payload, indent=2))
        return

    if not suites:
        console.print('[yellow]No suites discovered under memex_eval.suites.[/yellow]')
        return

    from rich.table import Table

    table = Table(title='Evaluation Suites', show_lines=True)
    for col in (
        'Name',
        'Version',
        'Tags',
        'Primary metrics',
        'Backend',
        'Scenarios',
        'LLM?',
    ):
        table.add_column(col)
    for s in suites:
        table.add_row(
            s.metadata.name,
            s.metadata.suite_version,
            ','.join(s.metadata.tags) or '-',
            ','.join(s.metadata.primary_metrics) or '-',
            s.metadata.default_answer_mode,
            str(len(s.scenarios)),
            'yes' if s.metadata.requires_llm_judge else 'no',
        )
    console.print(table)


@suite_app.command('show')
def suite_show(
    name: str = typer.Argument(..., help='Suite name.'),
    scenarios_only: bool = typer.Option(False, '--scenarios-only'),
    metadata_only: bool = typer.Option(False, '--metadata-only'),
) -> None:
    """Render a suite's README + scenarios summary for inspection."""
    from memex_eval.suite import load_suite, SuiteNotFound

    try:
        suite = load_suite(name)
    except SuiteNotFound as e:
        console.print(f'[red]{e}[/red]')
        raise typer.Exit(code=1) from None

    if not scenarios_only:
        console.rule(f'[bold]{suite.metadata.name}[/bold]')
        console.print(f'Version: {suite.metadata.suite_version}')
        console.print(f'Description: {suite.metadata.description}')
        console.print(f'Tags: {", ".join(suite.metadata.tags) or "-"}')
        console.print(f'Default backend: {suite.metadata.default_answer_mode}')
        console.print(f'Components: {", ".join(suite.metadata.components_under_test) or "-"}')
        console.print(f'Knobs: {", ".join(suite.metadata.knobs) or "-"}')
        if suite.readme_path and suite.readme_path.is_file():
            console.print('')
            console.print(suite.readme_path.read_text())

    if not metadata_only:
        console.rule('Scenarios')
        for sc in suite.scenarios:
            console.print(
                f'  • [bold]{sc.id}[/bold] '
                f'({sc.expected.type}, top_k={sc.top_k}, '
                f'mode={sc.answer_mode or suite.metadata.default_answer_mode})'
            )
            console.print(f'      {sc.description}')


@suite_app.command('validate')
def suite_validate(
    name: str | None = typer.Argument(None, help='Suite name. Omit with --all.'),
    all_suites: bool = typer.Option(False, '--all'),
) -> None:
    """Validate a suite (or all) loads cleanly without running it."""
    from memex_eval.suite import discover_suite_names, load_suite, SuiteNotFound

    if all_suites:
        names = discover_suite_names()
    elif name:
        names = [name]
    else:
        console.print('[red]Provide a suite name or pass --all.[/red]')
        raise typer.Exit(code=1)

    failed = 0
    for n in names:
        try:
            suite = load_suite(n)
            console.print(
                f'[green]✓[/green] {n} (v{suite.metadata.suite_version}, '
                f'{len(suite.scenarios)} scenarios)'
            )
        except SuiteNotFound as e:
            console.print(f'[red]✗[/red] {n}: {e}')
            failed += 1
        except Exception as e:
            console.print(f'[red]✗[/red] {n}: {type(e).__name__}: {e}')
            failed += 1
    if failed:
        raise typer.Exit(code=1)


@suite_app.command('run')
def suite_run(
    name: str | None = typer.Argument(None, help='Suite name. Omit with --all.'),
    all_suites: bool = typer.Option(False, '--all', help='Run every discoverable suite serially.'),
    server: str = typer.Option(
        DEFAULT_SERVER, '--server', '-s', envvar='MEMEX_EVAL_DEFAULT_SERVER'
    ),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str | None = typer.Option(
        None, '--mlflow-experiment', envvar='MEMEX_EVAL_MLFLOW_EXPERIMENT'
    ),
    mlflow_run_name: str | None = typer.Option(None, '--mlflow-run-name'),
    answer_mode: str | None = typer.Option(
        None,
        '--answer-mode',
        help='Override Suite.default_answer_mode for this run (api / claude-code / hermes / custom).',
    ),
    overrides: list[str] = typer.Option(
        [], '--override', help='Repeatable. KEY=VALUE for MLflow params (logged only).'
    ),
    replicates: int = typer.Option(1, '--replicates', min=1, max=20),
    seed: int | None = typer.Option(None, '--seed'),
    no_llm_judge: bool = typer.Option(False, '--no-llm-judge'),
    judge_model: str | None = typer.Option(None, '--judge-model', envvar='EVAL_JUDGE_MODEL'),
    output: str | None = typer.Option(None, '--output', '-o'),
    notes: str | None = typer.Option(
        None,
        '--notes',
        help=(
            'Free-form description of the change being evaluated. Uploaded to MLflow as '
            'the run_notes.md artifact + a truncated `notes` tag for filtering. Use this '
            'to record what changed in the code so a 6-month-old run is interpretable.'
        ),
    ),
    notes_file: str | None = typer.Option(
        None,
        '--notes-file',
        help='Read the notes body from a file (mutually exclusive with --notes).',
    ),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
) -> None:
    """Run a suite (or all) once."""
    _setup_logging(verbose)
    from memex_eval.recorders.mlflow_recorder import get_recorder
    from memex_eval.suite import discover_suite_names, load_suite, SuiteNotFound
    from memex_eval.suite.runner import run_suite

    if all_suites:
        names = discover_suite_names()
    elif name:
        names = [name]
    else:
        console.print('[red]Provide a suite name or pass --all.[/red]')
        raise typer.Exit(code=1)

    if notes and notes_file:
        console.print('[red]Pass either --notes or --notes-file, not both.[/red]')
        raise typer.Exit(code=2)
    if notes_file:
        notes = _read_notes_file(notes_file)

    cfg_overrides, _env = _resolve_overrides_to_env(overrides)
    if cfg_overrides:
        console.print(
            '[yellow]warning:[/yellow] --override on `suite run` is logged to MLflow '
            'only — the running server is NOT restarted with these values. Restart '
            'your server with the desired knob set in env or YAML, then re-run.'
        )

    any_failure = False
    for n in names:
        try:
            suite = load_suite(n)
        except SuiteNotFound as e:
            console.print(f'[red]✗[/red] {n}: {e}')
            any_failure = True
            continue

        # Optional per-run answer-mode override (modifies suite metadata in-place).
        if answer_mode:
            suite.metadata.default_answer_mode = answer_mode

        experiment = (
            mlflow_experiment or f'memex-suite-{suite.name}-v{suite.metadata.schema_version}'
        )
        recorder = get_recorder(
            mlflow_uri=mlflow_uri,
            mlflow_experiment=experiment,
            mlflow_run_name=mlflow_run_name,
        )
        try:
            result = asyncio.run(
                run_suite(
                    suite,
                    server_url=server,
                    config_overrides=cfg_overrides,
                    judge_model=judge_model,
                    use_llm_judge=not no_llm_judge,
                    replicates=replicates,
                    seed=seed,
                    recorder=recorder,
                    notes=notes,
                )
            )
        except KeyboardInterrupt:
            console.print('[yellow]Run interrupted.[/yellow]')
            raise typer.Exit(code=130) from None

        passed = result.total_passed
        failed = result.total_failed
        errored = result.total_errored
        skipped = result.total_skipped
        xfailed = result.total_xfailed
        xpassed = result.total_xpassed
        console.rule(f'[bold]{suite.name}[/bold]')
        line = f'  passed={passed} failed={failed} errored={errored} skipped={skipped}'
        if xfailed or xpassed:
            line += f' xfailed={xfailed} xpassed={xpassed}'
        console.print(line)
        for k, v in sorted(result.suite_metrics.items()):
            console.print(f'  {k}: {v:.4f}' if isinstance(v, float) else f'  {k}: {v}')

        if output:
            from pathlib import Path as _P

            _P(output).write_text(result.model_dump_json(indent=2))

        if failed > 0 or errored > 0:
            any_failure = True

    if any_failure:
        raise typer.Exit(code=1)


@suite_app.command('backends')
def suite_backends() -> None:
    """List all registered answer backends."""
    from memex_eval.suite import list_backends

    for n in list_backends():
        console.print(f'  • {n}')


@suite_app.command('history')
def suite_history(
    name: str = typer.Argument(..., help='Suite name.'),
    metric: str = typer.Option(..., '--metric', help='MLflow metric key.'),
    since_git_rev: str = typer.Option(
        ..., '--since-git-rev', help='Git commit/branch/HEAD~N — range is <since>..HEAD'
    ),
    mlflow_uri: str | None = typer.Option(None, '--mlflow-uri', envvar='MLFLOW_TRACKING_URI'),
    mlflow_experiment: str | None = typer.Option(None, '--mlflow-experiment'),
    limit: int = typer.Option(100, '--limit', min=1, max=1000),
    json_output: bool = typer.Option(False, '--json'),
) -> None:
    """Tabulate a metric across MLflow runs filtered by git commit range."""
    import subprocess as _sp

    from memex_eval.suite import load_suite

    suite = load_suite(name)
    experiment = mlflow_experiment or f'memex-suite-{suite.name}-v{suite.metadata.schema_version}'
    # Resolve git commit set
    try:
        proc = _sp.run(
            ['git', 'rev-list', '--reverse', f'{since_git_rev}..HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
    except _sp.CalledProcessError as e:
        console.print(f'[red]git rev-list failed: {e.stderr}[/red]')
        raise typer.Exit(code=1) from None
    sha_set = {sha for sha in proc.stdout.split() if sha}

    try:
        import mlflow
    except ImportError:
        console.print(
            '[red]suite history requires mlflow.[/red] '
            'Install with: [yellow]uv add memex-eval[mlflow][/yellow]'
        )
        raise typer.Exit(code=1) from None

    if mlflow_uri:
        mlflow.set_tracking_uri(mlflow_uri)
    runs_df = mlflow.search_runs(experiment_names=[experiment], max_results=limit)
    if runs_df.empty:
        console.print(f'[yellow]No runs in experiment {experiment}[/yellow]')
        return

    rows = []
    for _, run in runs_df.iterrows():
        sha = run.get('params.git.sha', '')
        if sha not in sha_set:
            continue
        rows.append(
            {
                'git_sha_short': sha[:8] if sha else '',
                'start_time': str(run.get('start_time', '')),
                metric: run.get(f'metrics.{metric}'),
                'suite.version': run.get('params.suite.version'),
            }
        )

    if json_output:
        import json as _json

        console.print(_json.dumps(rows, indent=2, default=str))
        return

    from rich.table import Table

    table = Table(title=f'{name} — {metric} over {since_git_rev}..HEAD')
    for col in ('git_sha', 'start_time', metric, 'suite.version'):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r['git_sha_short'],
            r['start_time'],
            f'{r[metric]:.4f}' if isinstance(r[metric], float) else str(r[metric]),
            str(r['suite.version']),
        )
    console.print(table)


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
