"""Suite runner — orchestrates one suite invocation end-to-end."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import CreateVaultRequest, NoteCreateDTO

from memex_eval.helpers import wait_for_extraction
from memex_eval.judge import Judge
from memex_eval.suite.agents import AgentAnswer, get_backend
from memex_eval.suite.base import (
    LLMJudge,
    RunResult,
    Scenario,
    ScenarioOutcome,
    SetupAction,
    Suite,
    UsefulAtK,
)
from memex_eval.suite.metrics import aggregate_metric_keys, percentile
from memex_eval.suite.setup_actions import get_setup_action

if TYPE_CHECKING:
    from memex_eval.recorders.mlflow_recorder import MLflowRecorder, NullRecorder

logger = logging.getLogger('memex_eval.suite.runner')


def _git_capture(args: list[str]) -> str:
    try:
        return (
            subprocess.check_output(
                ['git', *args], stderr=subprocess.DEVNULL, timeout=5, cwd=str(Path.cwd())
            )
            .decode()
            .strip()
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
        return ''


def _memex_version() -> str:
    try:
        from memex_core.__about__ import __version__

        return str(__version__)
    except Exception:
        return ''


_NOTES_TAG_MAX = 240
_NOTES_OVERFLOW_SUFFIX = '… (see run_notes.md artifact)'

# Param keys consumed by the runner — stripped before the SetupAction's params
# dict is handed to the registered handler so handlers can use those names
# freely in their own ``extra='allow'`` fields. If you add another runner-
# interpreted reserved field on ``SetupAction``, add it here too.
_RUNNER_RESERVED_PARAM_KEYS: frozenset[str] = frozenset({'kind', 'required'})


def _build_notes_tag(notes: str | None) -> str:
    """Build the MLflow ``notes`` tag value from a free-form notes body.

    Returns an empty string when ``notes`` is None or whitespace-only —
    callers should treat that as "skip the tag entirely". Appends the
    overflow suffix only when the tag actually loses content (multi-line
    body OR first line longer than the cap), so trailing whitespace alone
    does not falsely advertise a longer artifact.
    """
    if not notes:
        return ''
    stripped = notes.strip()
    if not stripped:
        return ''
    first_line = stripped.splitlines()[0]
    truncated = first_line[:_NOTES_TAG_MAX]
    content_lost = len(stripped.splitlines()) > 1 or len(first_line) > _NOTES_TAG_MAX
    return truncated + (_NOTES_OVERFLOW_SUFFIX if content_lost else '')


def _extract_judge_revision(lm: Any) -> str | None:
    try:
        entry = lm.history[-1]
        return entry.get('response', {}).get('model') or entry.get('model')
    except (IndexError, AttributeError, KeyError, TypeError):
        return None


async def _setup_vault(api: RemoteMemexAPI, name: str, description: str) -> UUID:
    """Create a vault if it doesn't exist; otherwise truncate it for a fresh slate."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == name:
            with contextlib.suppress(Exception):
                await api.truncate_vault(vault.id)
            return vault.id
    vault = await api.create_vault(CreateVaultRequest(name=name, description=description))
    return vault.id


async def _run_setup_actions(
    api: RemoteMemexAPI, vault_id: UUID, actions: list[SetupAction]
) -> dict[str, Any]:
    """Dispatch each action through the setup-action registry.

    Each handler's optional dict return is auto-prefixed with the handler
    name (e.g. ``snapshot.baseline``) and merged into the per-scenario
    context. The context dict carries:

    - ``<action_name>.<key>`` entries from each handler's return.
    - ``_setup_failures`` — list of ``{kind, error}`` for any action that
      raised. Outcomes that depend on baseline data should check this.
    - ``_required_setup_failed`` — True if any handler with
      ``required=True`` raised; the runner short-circuits the scenario to
      status='error' in this case.
    """
    context: dict[str, Any] = {'_setup_failures': []}
    for action in actions:
        try:
            handler = get_setup_action(action.kind)
        except KeyError as e:
            context['_setup_failures'].append({'kind': action.kind, 'error': str(e)})
            logger.warning('  Setup: %s', e)
            # If the missing handler was declared required at the action
            # level, treat as a required-failure so the scenario short-circuits.
            if getattr(action, 'required', False):
                context['_required_setup_failed'] = True
                break
            continue
        try:
            params = {
                k: v for k, v in action.model_dump().items() if k not in _RUNNER_RESERVED_PARAM_KEYS
            }
            result = await handler.run(api, vault_id, params)
            if isinstance(result, dict):
                # Look up the registered name from the registry so we never
                # depend on cls.name (which can be mutated by replace_*).
                prefix = action.kind + '.'
                for k, v in result.items():
                    key = k if k.startswith(prefix) else f'{prefix}{k}'
                    context[key] = v
        except Exception as e:
            context['_setup_failures'].append({'kind': action.kind, 'error': str(e)})
            if getattr(handler, 'required', False) or getattr(action, 'required', False):
                context['_required_setup_failed'] = True
                # Stop running further actions so we don't mutate vault state
                # after a required snapshot/precondition has failed.
                logger.warning(
                    '  Required setup %s failed; aborting remaining setup actions.',
                    action.kind,
                )
                break
            logger.warning('  Setup action %s failed: %s', action.kind, e)
    return context


async def _ingest_sources(
    api: RemoteMemexAPI,
    vault_id_default: UUID,
    vault_map: dict[str | None, UUID],
    suite: Suite,
) -> dict[str, str]:
    """Ingest every source note. Returns {note_key: note_id}."""
    note_id_by_key: dict[str, str] = {}
    for note in suite.sources.notes:
        target_vault_id = vault_map.get(note.vault_name, vault_id_default)
        files_b64 = note.asset_bytes_b64()
        import base64

        dto = NoteCreateDTO(
            name=note.title or note.note_key,
            description=note.description or f'Eval suite source: {note.note_key}',
            content=base64.b64encode(note.content.encode('utf-8')),
            files=files_b64,
            tags=note.tags,
            vault_id=str(target_vault_id),
            note_key=f'eval-{suite.name}-{note.note_key}',
        )
        resp = await api.ingest(dto)
        if hasattr(resp, 'note_id') and resp.note_id:
            note_id_by_key[note.note_key] = str(resp.note_id)
        elif hasattr(resp, 'status') and resp.status == 'skipped':
            # Idempotent skip — find the existing note in THIS vault by name.
            # Suite-prefixed note_key would be more precise but the client lacks
            # a by-key lookup; vault-scoped title search is sufficient because
            # the per-suite vault contains only this suite's notes.
            lookup_name = note.title or note.note_key
            try:
                existing = await api.find_notes_by_title(lookup_name, vault_ids=[target_vault_id])
                for n in existing:
                    if getattr(n, 'name', None) == lookup_name:
                        note_id_by_key[note.note_key] = str(n.id)
                        break
            except Exception as e:
                logger.warning(
                    'idempotent-skip lookup failed for note_key=%r: %s', note.note_key, e
                )
    return note_id_by_key


async def _wait_extraction_per_note(
    api: RemoteMemexAPI,
    note_id_by_key: dict[str, str],
    vault_id: UUID,
    per_note_timeout_s: float = 60.0,
    poll_interval_s: float = 2.0,
) -> dict[str, list[str]]:
    """For each ingested note, poll the new /notes/{id}/memory_units endpoint
    until ≥1 unit appears or per-note timeout. Returns {note_key: [unit_ids]}."""
    out: dict[str, list[str]] = {}
    for note_key, note_id in note_id_by_key.items():
        deadline = time.monotonic() + per_note_timeout_s
        units: list[str] = []
        while time.monotonic() < deadline:
            try:
                rows = await api.list_memory_units_by_note(note_id, vault_id)
                if rows:
                    units = [str(u.id) for u in rows]
                    break
            except Exception as e:
                logger.debug('  list_memory_units_by_note failed for %s: %s', note_key, e)
            await asyncio.sleep(poll_interval_s)
        if not units:
            logger.warning(
                '  Extraction timed out for note_key=%r (note_id=%s); '
                'scenarios referencing this key will status=error',
                note_key,
                note_id,
            )
        out[note_key] = units
    return out


async def _execute_scenario(
    api: RemoteMemexAPI,
    server_url: str,
    vault_id: UUID,
    scenario: Scenario,
    suite: Suite,
    judge: Judge | None,
    note_key_to_unit_ids: dict[str, list[str]],
    replicate_index: int,
) -> ScenarioOutcome:
    started = time.monotonic()
    answer_mode = suite.answer_mode_for(scenario)

    # Validate gold note_keys resolved before running.
    referenced = scenario.expected.referenced_note_keys()
    unresolved = [k for k in referenced if not note_key_to_unit_ids.get(k)]
    if unresolved:
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={'unresolved_note_keys': unresolved},
            duration_ms=(time.monotonic() - started) * 1000,
            error=f'Ingest produced no units for note_keys={unresolved}',
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )

    # Resolve the answer backend (registered name → instance).
    try:
        backend = get_backend(answer_mode)
    except KeyError as e:
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={},
            duration_ms=(time.monotonic() - started) * 1000,
            error=str(e),
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )

    try:
        scenario_context: dict[str, Any] = {}
        if scenario.setup_actions:
            scenario_context = await _run_setup_actions(api, vault_id, scenario.setup_actions)
            if scenario_context.get('_required_setup_failed'):
                return ScenarioOutcome(
                    scenario_id=scenario.id,
                    status='error',
                    metrics={},
                    actual_summary={'setup_failures': scenario_context.get('_setup_failures', [])},
                    duration_ms=(time.monotonic() - started) * 1000,
                    error='A required setup_action failed; refusing to score',
                    replicate_index=replicate_index,
                    answer_mode=answer_mode,
                )

        # Backend produces an AgentAnswer; outcomes score against it uniformly.
        answer: AgentAnswer = await backend.answer(
            scenario, api=api, vault_id=vault_id, server_url=server_url, judge=judge
        )
        # If the backend reported an error, surface it as scenario error
        # rather than risking a false-pass on an empty answer.
        if answer.error:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='error',
                metrics={},
                actual_summary={'backend_error': answer.error},
                duration_ms=(time.monotonic() - started) * 1000,
                error=f'backend({answer.backend_name}): {answer.error}',
                replicate_index=replicate_index,
                answer_mode=answer_mode,
            )
        metrics = scenario.expected.score(
            answer,
            scenario,
            note_key_to_unit_ids=note_key_to_unit_ids,
            judge=judge,
            context=scenario_context,
        )
        duration_ms = (time.monotonic() - started) * 1000

        # Determine pass/fail status from the metrics dict
        if 'pass' in metrics:
            passed = metrics['pass'] >= 1.0
        else:
            # No explicit pass — pass if any metric > 0 (e.g. recall > 0)
            passed = any(v > 0 for v in metrics.values())

        # Time-budget assertion
        if scenario.max_duration_ms is not None and duration_ms > scenario.max_duration_ms:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='fail',
                metrics={**metrics, 'pass': 0.0},
                actual_summary={'exceeded_max_duration_ms': duration_ms},
                duration_ms=duration_ms,
                error=(
                    f'Exceeded max_duration_ms: '
                    f'{duration_ms:.0f}ms > {scenario.max_duration_ms:.0f}ms'
                ),
                replicate_index=replicate_index,
                answer_mode=answer_mode,
                tokens_in=answer.tokens_in,
                tokens_out=answer.tokens_out,
                cost_usd=answer.cost_usd,
                answer_text=answer.answer_text,
                tool_calls=answer.tool_calls,
            )
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='pass' if passed else 'fail',
            metrics=metrics,
            actual_summary={
                'unit_count': len(answer.units),
                'entity_count': len(answer.entities),
                'lint_findings_count': len(answer.lint_findings),
                'tool_call_count': len(answer.tool_calls),
                'retrieved_unit_id_count': len(answer.retrieved_unit_ids),
                'backend_error': answer.error,
            },
            duration_ms=duration_ms,
            replicate_index=replicate_index,
            answer_mode=answer_mode,
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            cost_usd=answer.cost_usd,
            answer_text=answer.answer_text,
            tool_calls=answer.tool_calls,
        )
    except Exception as e:
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={},
            duration_ms=(time.monotonic() - started) * 1000,
            error=f'{type(e).__name__}: {e}',
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )


def _aggregate_results(
    outcomes: list[ScenarioOutcome],
) -> dict[str, float]:
    """Build the suite_metrics dict logged to MLflow."""
    runnable_outcomes = [o for o in outcomes if o.status != 'skip']
    pass_count = sum(1 for o in outcomes if o.status == 'pass')
    fail_count = sum(1 for o in outcomes if o.status == 'fail')
    error_count = sum(1 for o in outcomes if o.status == 'error')
    skip_count = sum(1 for o in outcomes if o.status == 'skip')

    metrics_only = [o.metrics for o in runnable_outcomes if o.metrics]
    aggregated = aggregate_metric_keys(metrics_only)

    durations = [o.duration_ms for o in runnable_outcomes]
    aggregated['latency_ms.p50'] = percentile(durations, 50)
    aggregated['latency_ms.p95'] = percentile(durations, 95)
    aggregated['latency_ms.mean'] = (sum(durations) / len(durations)) if durations else 0.0

    aggregated['suite.pass_rate'] = (
        pass_count / len(runnable_outcomes) if runnable_outcomes else 0.0
    )
    aggregated['count.scenarios'] = float(len(outcomes))
    aggregated['count.passed'] = float(pass_count)
    aggregated['count.failed'] = float(fail_count)
    aggregated['count.errored'] = float(error_count)
    aggregated['count.skipped'] = float(skip_count)
    return aggregated


async def run_suite(
    suite: Suite,
    server_url: str,
    *,
    config_overrides: dict[str, str] | None = None,
    judge_model: str | None = None,
    use_llm_judge: bool = True,
    replicates: int = 1,
    seed: int | None = None,
    recorder: 'MLflowRecorder | NullRecorder | None' = None,
    extra_tags: dict[str, str] | None = None,
    extra_params: dict[str, str] | None = None,
    notes: str | None = None,
) -> RunResult:
    """Run one suite end-to-end.

    Args:
        notes: Free-form description of the change being evaluated. Persisted
            on ``RunResult.notes`` and uploaded to MLflow as the
            ``run_notes.md`` artifact + a truncated ``notes`` tag for
            UI-side filtering. The full text lives in the artifact.

    Logs to ``recorder`` if provided. Returns the full ``RunResult``.
    """
    config_overrides = dict(config_overrides or {})
    extra_tags = dict(extra_tags or {})
    extra_params = dict(extra_params or {})
    actual_seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
    random.seed(actual_seed)
    try:
        import numpy

        numpy.random.seed(actual_seed % (2**32 - 1))
    except Exception:
        pass

    started_at = dt.datetime.now(dt.timezone.utc)
    run_id = uuid.uuid4().hex
    run_id_short = run_id[:8]
    vault_name = f'eval-suite-{suite.name}-{run_id_short}'

    # Judge setup + probe
    judge: Judge | None = None
    judge_model_probe: dict[str, Any] | None = None
    judge_model_value: str | None = None
    if use_llm_judge and any(
        isinstance(s.expected, (LLMJudge, UsefulAtK)) for s in suite.scenarios
    ):
        try:
            judge = Judge(model=judge_model)
            judge_model_value = judge.lm.model
            with contextlib.suppress(Exception):
                judge.judge_correctness('probe', 'probe', 'probe')
            judge_kwargs = getattr(judge.lm, 'kwargs', None)
            judge_temp = (
                judge_kwargs.get('temperature', 0.0) if isinstance(judge_kwargs, dict) else 0.0
            )
            judge_model_probe = {
                'model': judge_model_value,
                'revision': _extract_judge_revision(judge.lm),
                'temperature': judge_temp,
            }
        except Exception as e:
            logger.warning('Judge unavailable: %s', e)
            judge = None

    if recorder is None:
        from memex_eval.recorders.mlflow_recorder import NullRecorder

        recorder = NullRecorder()

    sources_hash = suite.sources.content_hash()
    git_sha = _git_capture(['rev-parse', 'HEAD'])
    git_branch = _git_capture(['rev-parse', '--abbrev-ref', 'HEAD'])
    memex_v = _memex_version()

    outcomes: list[ScenarioOutcome] = []
    note_key_to_unit_ids: dict[str, list[str]] = {}
    config_snapshot: dict[str, Any] = {}
    embedding_model = ''
    reranker_model = ''

    try:
        async with httpx.AsyncClient(base_url=server_url, timeout=180.0) as client:
            api = RemoteMemexAPI(client)

            # Capture config snapshot (best-effort — admin auth may block)
            try:
                config_snapshot = await api.get_system_config()
                # Resolve embedding/reranker model identity from snapshot
                emb = config_snapshot.get('server', {}).get('memory', {}).get('embedding', {})
                rer = config_snapshot.get('server', {}).get('memory', {}).get('reranker', {})
                embedding_model = str(emb.get('model') or emb.get('type') or '')
                reranker_model = str(rer.get('model') or rer.get('type') or '')
            except Exception as e:
                logger.warning('Could not fetch /system/config: %s', e)

            # Vault setup
            default_vault_id = await _setup_vault(
                api, vault_name, f'Eval suite vault: {suite.name}'
            )
            vault_map: dict[str | None, UUID] = {None: default_vault_id}
            extra_vault_names = {n.vault_name for n in suite.sources.notes if n.vault_name}
            extra_vault_names |= {s.vault_name for s in suite.scenarios if s.vault_name}
            for name in extra_vault_names:
                if name is None:
                    continue
                vault_map[name] = await _setup_vault(
                    api, f'{vault_name}-{name}', f'Eval extra vault: {name}'
                )

            try:
                # Ingest source notes
                note_id_by_key = await _ingest_sources(api, default_vault_id, vault_map, suite)

                # Wait for extraction (vault-wide stable signal first). Skip
                # entirely when no notes were ingested — otherwise we burn the
                # full retry budget on a vault that has nothing to extract.
                if note_id_by_key:
                    with contextlib.suppress(Exception):
                        await wait_for_extraction(
                            api,
                            default_vault_id,
                            poll_interval=2.0,
                            poll_timeout=120.0,
                            stable_ticks_required=2,
                            max_consecutive_errors=5,
                        )

                # Per-note unit-id resolution (also serves as per-note extraction wait)
                note_key_to_unit_ids = await _wait_extraction_per_note(
                    api, note_id_by_key, default_vault_id
                )

                # Run scenarios
                for scenario in suite.scenarios:
                    sc_vault_id = vault_map.get(scenario.vault_name, default_vault_id)
                    for replicate in range(replicates):
                        if (
                            isinstance(scenario.expected, (LLMJudge, UsefulAtK))
                            and not use_llm_judge
                        ):
                            outcomes.append(
                                ScenarioOutcome(
                                    scenario_id=scenario.id,
                                    status='skip',
                                    replicate_index=replicate,
                                    answer_mode=suite.answer_mode_for(scenario),
                                )
                            )
                            continue
                        outcome = await _execute_scenario(
                            api,
                            server_url,
                            sc_vault_id,
                            scenario,
                            suite,
                            judge,
                            note_key_to_unit_ids,
                            replicate,
                        )
                        outcomes.append(outcome)
            finally:
                # Best-effort cleanup: delete the temp vault(s) so we don't leak state.
                with contextlib.suppress(Exception):
                    await api.delete_vault(default_vault_id)
                for name, vid in vault_map.items():
                    if name is None:
                        continue
                    with contextlib.suppress(Exception):
                        await api.delete_vault(vid)
    except KeyboardInterrupt:
        logger.warning('Run interrupted by user')
        raise

    finished_at = dt.datetime.now(dt.timezone.utc)
    suite_metrics = _aggregate_results(outcomes)
    answer_modes_used = sorted({o.answer_mode for o in outcomes if o.answer_mode})
    # Aggregate cost/tokens for agent-mode runs (zero for direct API).
    suite_metrics['cost.total_usd'] = sum(o.cost_usd for o in outcomes)
    suite_metrics['tokens.total_in'] = float(sum(o.tokens_in for o in outcomes))
    suite_metrics['tokens.total_out'] = float(sum(o.tokens_out for o in outcomes))

    result = RunResult(
        suite_name=suite.name,
        suite_version=suite.metadata.suite_version,
        schema_version=suite.metadata.schema_version,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        config_snapshot=config_snapshot,
        config_overrides=config_overrides,
        sources_hash=sources_hash,
        git_sha=git_sha,
        git_branch=git_branch,
        memex_version=memex_v,
        judge_model=judge_model_value,
        judge_model_probe=judge_model_probe,
        seed=actual_seed,
        embedding_model=embedding_model,
        reranker_model=reranker_model,
        vault_name=vault_name,
        answer_modes=answer_modes_used,
        replicates=replicates,
        notes=notes,
        scenario_outcomes=outcomes,
        suite_metrics=suite_metrics,
        note_key_to_unit_ids=note_key_to_unit_ids,
    )

    # MLflow logging
    _log_to_recorder(result, suite, recorder, extra_tags, extra_params)
    return result


def _log_to_recorder(
    result: RunResult,
    suite: Suite,
    recorder: 'MLflowRecorder | NullRecorder',
    extra_tags: dict[str, str],
    extra_params: dict[str, str],
) -> None:
    import json
    import os
    import tempfile

    # Build params per the §6 schema
    base_params: dict[str, Any] = {
        'suite.name': result.suite_name,
        'suite.version': result.suite_version,
        'suite.schema_version': result.schema_version,
        'suite.sources_hash': result.sources_hash,
        'suite.answer_modes': ','.join(result.answer_modes) or 'api',
        'git.sha': result.git_sha,
        'git.branch': result.git_branch,
        'memex.version': result.memex_version,
        'release.version': os.environ.get('MEMEX_RELEASE_VERSION', ''),
        'judge.model': result.judge_model or '',
        'judge.model_revision': (result.judge_model_probe or {}).get('revision') or '',
        'judge.temperature': str((result.judge_model_probe or {}).get('temperature') or 0.0),
        'embedding.model_id': result.embedding_model,
        'reranker.model_id': result.reranker_model,
        'seed': str(result.seed),
        'replicates': str(result.replicates),
        'vault.name': result.vault_name,
    }
    base_params = {k: str(v) for k, v in base_params.items() if v not in (None, '')}

    # Knob params (allowlist from suite.metadata.knobs)
    knob_params: dict[str, str] = {}
    for knob in suite.metadata.knobs[:30]:
        # Resolve value from config_snapshot (dotted-path lookup)
        value = result.config_snapshot
        try:
            for part in knob.split('.'):
                value = value[part]
            knob_params[f'knob.{knob}'] = str(value)
        except (KeyError, TypeError):
            knob_params[f'knob.{knob}'] = '<unresolved>'

    override_params = {
        f'override.{k}': str(v) for k, v in list(result.config_overrides.items())[:20]
    }

    tags: dict[str, str] = {
        'suite.name': result.suite_name,
        'schema_version': result.schema_version,
        'suite.tags': ','.join(suite.metadata.tags),
        'components': ','.join(suite.metadata.components_under_test),
        **extra_tags,
    }

    # User-supplied change-description notes — short summary as a tag (for
    # UI filtering), full body uploaded as an artifact below. MLflow tag
    # values cap at 5000 chars; we keep it well under for readability.
    notes_tag = _build_notes_tag(result.notes)
    if notes_tag:
        tags['notes'] = notes_tag

    # mlflow.parentRunId belongs in tags, not params — extract from extra_params
    # so the MLflow UI properly nests this run under its sweep parent.
    parent_run_id = extra_params.pop('mlflow.parentRunId', None)
    is_nested = bool(parent_run_id)
    if parent_run_id:
        tags['mlflow.parentRunId'] = parent_run_id

    all_params = {**base_params, **knob_params, **override_params, **extra_params}

    metrics = {k: float(v) for k, v in result.suite_metrics.items() if isinstance(v, (int, float))}

    start_kwargs: dict[str, Any] = {'tags': tags}
    if is_nested:
        start_kwargs['nested'] = True
    recorder.start_run(**start_kwargs)
    try:
        recorder.log_params(all_params)
        for k, tag_v in tags.items():
            with contextlib.suppress(Exception):
                if hasattr(recorder, 'set_tag'):
                    recorder.set_tag(k, tag_v)
        recorder.log_metrics(metrics, step=0)

        # Artifacts
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            run_result_path = tmpdir / 'run_result.json'
            # Pydantic's mode='json' handles UUID/datetime/Path automatically;
            # ``default=`` is a json.dumps kwarg, not model_dump_json's.
            run_result_path.write_text(result.model_dump_json(indent=2))
            recorder.log_artifact(run_result_path)

            # Defense-in-depth: re-redact even though the server already did.
            # If a self-hosted memex is older / lacks redaction, we still avoid leaking secrets.
            from memex_common.redaction import redact

            cfg_path = tmpdir / 'config_snapshot.json'
            cfg_path.write_text(json.dumps(redact(result.config_snapshot), indent=2, default=str))
            recorder.log_artifact(cfg_path)

            if suite.readme_path and suite.readme_path.is_file():
                with contextlib.suppress(Exception):
                    recorder.log_artifact(suite.readme_path)

            # User-supplied change notes — full text as its own artifact.
            if result.notes and result.notes.strip():
                notes_path = tmpdir / 'run_notes.md'
                notes_path.write_text(result.notes)
                recorder.log_artifact(notes_path)

            # Snapshot the source notes (and any binary assets) so a 6-month-old
            # run is reproducible from the artifact alone — plan §6.
            with contextlib.suppress(Exception):
                snapshot_dir = tmpdir / 'sources'
                snapshot_dir.mkdir()
                for note in suite.sources.notes:
                    if note.path.is_file():
                        (snapshot_dir / note.path.name).write_bytes(note.path.read_bytes())
                    for asset_name, asset_path in note.assets.items():
                        if asset_path.is_file():
                            asset_target = snapshot_dir / 'assets' / asset_name
                            asset_target.parent.mkdir(parents=True, exist_ok=True)
                            asset_target.write_bytes(asset_path.read_bytes())
                recorder.log_artifact(snapshot_dir)
    finally:
        recorder.end_run()


__all__ = ['run_suite']
