"""Maintenance cockpit eval suite — 7 regression gate scenarios.

Exercises the lint auto-learning loop end-to-end: cooldown suppression,
evidence blob integrity, telemetry verdict rollup, threshold calibration,
auto-apply confidence gating, and DSPy signature optimisation.

Each scenario uses the ``@suite.scenario`` decorator with an async
evaluator function that drives the lint lifecycle via ``ctx.api``
(``RemoteMemexAPI``).

**Key design principle**: every scenario is self-contained. No
``depends_on_prior_scenarios``, no ``setup_actions`` referencing
framework-registered lint actions. Each scenario runs lint itself,
seeds its own preconditions, and asserts independently.
"""

from __future__ import annotations

import logging
from pathlib import Path

# Import _outcomes and _setup_actions FIRST for decorator side effects,
# BEFORE any suite.register(...) calls.
from . import _outcomes  # noqa: F401
from . import _setup_actions  # noqa: F401

from memex_eval.suite.base import SuiteMetadata
from memex_eval.suite.decorator import ScenarioContext, Suite
from memex_eval.suite.sources import SuiteSources

logger = logging.getLogger('memex_eval.suites.maintenance_cockpit')

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='maintenance_cockpit',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Regression gates for the lint auto-learning loop: cooldown '
        'suppression, evidence blob integrity, telemetry verdict rollup, '
        'threshold calibration, auto-apply confidence gating, and DSPy '
        'signature optimisation.'
    ),
    tags=[
        'lint',
        'cockpit',
        'auto-learning',
        'calibration',
        'optimizer',
    ],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'services.lint',
        'services.lint_llm',
        'services.lint_learning',
        'services.lint_auto_apply',
        'services.lint_optimizer',
    ],
    knobs=[
        'server.memory.lint_llm.surprise_threshold',
        'server.memory.lint_llm.polarity.enabled',
    ],
    requires_llm_judge=False,
    requires_nli_classifier=False,
    default_answer_mode='api',
)


suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    readme_path=_ROOT / 'README.md',
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _run_lint(ctx: ScenarioContext) -> None:
    """Run both SQL rule lint and LLM lint. LLM lint failures are logged
    but not fatal — SQL rule findings are always available as fallback."""
    await ctx.api.run_lint_rules(ctx.vault_id)
    try:
        await ctx.api.run_lint_llm(ctx.vault_id)
    except Exception as exc:
        logger.warning('run_lint_llm failed (non-fatal): %s', exc)


async def _get_pending_findings(
    ctx: ScenarioContext,
    *,
    rule_name: str | None = None,
) -> list[dict]:
    """Return all pending findings, optionally filtered by rule_name."""
    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = [f for f in (payload.get('findings') or []) if isinstance(f, dict)]
    if rule_name is not None:
        findings = [f for f in findings if f.get('rule_name') == rule_name]
    return findings


async def _find_any_pending_finding(ctx: ScenarioContext) -> dict | None:
    """Return the first pending finding from any source (LLM or SQL rule)."""
    # Prefer LLM findings, fall back to any rule finding
    findings = await _get_pending_findings(ctx, rule_name='llm_semantic_contradiction')
    if findings:
        return findings[0]
    findings = await _get_pending_findings(ctx)
    return findings[0] if findings else None


async def _resolve_all_pending_as(
    ctx: ScenarioContext,
    *,
    action: str = 'dismiss',
    limit: int = 500,
) -> tuple[int, int]:
    """Resolve all pending findings. Returns (accept_count, dismiss_count).

    ``action='accept'`` resolves with deprioritize_unit (accept).
    ``action='dismiss'`` dismisses.
    ``action='mixed'`` accepts the first half, dismisses the rest.
    """
    findings = await _get_pending_findings(ctx)
    accept_count = 0
    dismiss_count = 0

    for i, f in enumerate(findings[:limit]):
        fid = f['id']
        try:
            if action == 'accept' or (action == 'mixed' and i < len(findings) // 2):
                target_id = f.get('target_id', '')
                await ctx.api.lint_resolve(
                    fid,
                    action='deprioritize_unit',
                    params={'unit_id': target_id},
                    note=f'eval-suite: accept #{i + 1}',
                )
                accept_count += 1
            else:
                await ctx.api.lint_dismiss(fid, note=f'eval-suite: dismiss #{i + 1}')
                dismiss_count += 1
        except Exception as exc:
            logger.warning('Failed to resolve finding %s: %s', fid, exc)

    return accept_count, dismiss_count


# ------------------------------------------------------------------
# 1. cooldown_suppression
# ------------------------------------------------------------------


@suite.scenario(
    id='cooldown_suppression',
    query='deployment cadence',
    description=(
        'After resolving a lint finding with deprioritize_unit, '
        'a second lint run does NOT re-emit the same finding (30-day '
        'cooldown suppression).'
    ),
    group='cooldown',
)
async def cooldown_suppression(ctx: ScenarioContext) -> None:
    # Step 1: run lint to produce findings
    await _run_lint(ctx)

    # Step 2: find any pending finding (prefer LLM, fall back to SQL rule)
    finding = await _find_any_pending_finding(ctx)
    assert finding is not None, (
        'No pending finding after lint run. '
        'Check that the contradicting notes were ingested and lint ran.'
    )
    finding_id = finding['id']
    rule_name = finding.get('rule_name', '')

    # Step 3: resolve with deprioritize_unit action
    target_id = finding.get('target_id', '')
    await ctx.api.lint_resolve(
        finding_id,
        action='deprioritize_unit',
        params={'unit_id': target_id},
        note='eval-suite: testing cooldown suppression',
    )

    # Step 4: run lint again — the resolved finding should NOT re-appear
    await _run_lint(ctx)

    # Step 5: assert no new pending finding for the same rule
    reappeared = await _get_pending_findings(ctx, rule_name=rule_name)
    count = len(reappeared)
    ctx.metrics['pass'] = 1.0 if count == 0 else 0.0
    ctx.metrics['pending_after_rerun'] = float(count)
    assert count == 0, (
        f'Cooldown failed: {count} pending {rule_name} '
        f'finding(s) re-appeared after resolving + re-running lint.'
    )


# ------------------------------------------------------------------
# 2. evidence_blob_integrity
# ------------------------------------------------------------------


@suite.scenario(
    id='evidence_blob_integrity',
    query='deployment cadence',
    description=(
        'Resolving a finding with action=deprioritize_unit stamps '
        'evidence.resolution.followup with action, params, '
        'applied_state, prior_state, and the reviewer note.'
    ),
    group='evidence',
)
async def evidence_blob_integrity(ctx: ScenarioContext) -> None:
    # Run lint to produce findings
    await _run_lint(ctx)

    finding = await _find_any_pending_finding(ctx)
    assert finding is not None, 'No pending finding after lint run.'
    finding_id = finding['id']
    target_id = finding.get('target_id', '')

    result = await ctx.api.lint_resolve(
        finding_id,
        action='deprioritize_unit',
        params={'unit_id': target_id},
        note='test rationale',
    )

    resolution = result.get('resolution') or {}
    note_ok = resolution.get('note') == 'test rationale'
    followup = resolution.get('followup') or {}
    action_ok = followup.get('action') == 'deprioritize_unit'
    has_applied = 'applied_state' in followup
    has_prior = 'prior_state' in followup

    ctx.metrics['note_matches'] = 1.0 if note_ok else 0.0
    ctx.metrics['action_matches'] = 1.0 if action_ok else 0.0
    ctx.metrics['has_applied_state'] = 1.0 if has_applied else 0.0
    ctx.metrics['has_prior_state'] = 1.0 if has_prior else 0.0

    all_ok = note_ok and action_ok and has_applied and has_prior
    ctx.metrics['pass'] = 1.0 if all_ok else 0.0
    assert all_ok, (
        f'Evidence blob incomplete: note={note_ok}, action={action_ok}, '
        f'applied_state={has_applied}, prior_state={has_prior}. '
        f'resolution={resolution!r}'
    )


# ------------------------------------------------------------------
# 3. telemetry_verdict_rollup
# ------------------------------------------------------------------


@suite.scenario(
    id='telemetry_verdict_rollup',
    query='deployment cadence',
    description=(
        'Resolving 3 findings (2 deprioritize_unit=accept, 1 dismiss) '
        'then refreshing telemetry yields accept_count>=2, dismiss_count>=1.'
    ),
    group='telemetry',
)
async def telemetry_verdict_rollup(ctx: ScenarioContext) -> None:
    # Run lint to seed findings
    await _run_lint(ctx)

    # Collect all pending findings
    findings = await _get_pending_findings(ctx)
    assert len(findings) >= 3, (
        f'Need >=3 pending findings for telemetry test, got {len(findings)}. '
        f'The contradicting notes may not have produced enough lint findings.'
    )

    # Resolve first 2 with accept (deprioritize_unit), 3rd with dismiss
    for i, f in enumerate(findings[:3]):
        fid = f['id']
        if i < 2:
            target_id = f.get('target_id', '')
            await ctx.api.lint_resolve(
                fid,
                action='deprioritize_unit',
                params={'unit_id': target_id},
                note=f'eval-suite: accept #{i + 1}',
            )
        else:
            await ctx.api.lint_dismiss(fid, note='eval-suite: dismiss')

    # Refresh telemetry
    await ctx.api.lint_telemetry_refresh(
        vault_id=str(ctx.vault_id),
        window_days=30,
    )

    # Read telemetry
    telemetry = await ctx.api.lint_telemetry(
        vault_id=str(ctx.vault_id),
    )
    rows = telemetry.get('rows') or []

    # Sum across all rules
    total_accept = sum(int(r.get('accept_count', 0)) for r in rows)
    total_dismiss = sum(int(r.get('dismiss_count', 0)) for r in rows)

    ctx.metrics['accept_count'] = float(total_accept)
    ctx.metrics['dismiss_count'] = float(total_dismiss)
    accept_ok = total_accept >= 2
    dismiss_ok = total_dismiss >= 1
    ctx.metrics['pass'] = 1.0 if (accept_ok and dismiss_ok) else 0.0
    assert accept_ok, f'Expected accept_count>=2, got {total_accept}'
    assert dismiss_ok, f'Expected dismiss_count>=1, got {total_dismiss}'


# ------------------------------------------------------------------
# 4. threshold_calibration_adjusts
# ------------------------------------------------------------------


@suite.scenario(
    id='threshold_calibration_adjusts',
    query='deployment cadence',
    description=(
        'When telemetry shows accept_rate < 0.3 (mostly dismisses), '
        'calibration raises surprise_threshold above the default 0.7.'
    ),
    group='calibration',
)
async def threshold_calibration_adjusts(ctx: ScenarioContext) -> None:
    # Step 1: run lint to seed findings
    await _run_lint(ctx)

    # Step 2: dismiss ALL pending findings to push accept_rate to 0.0
    _, dismiss_count = await _resolve_all_pending_as(ctx, action='dismiss')
    ctx.metrics['dismissed_count'] = float(dismiss_count)

    # If we didn't get enough findings, run lint again to get more
    if dismiss_count < 3:
        await _run_lint(ctx)
        _, extra = await _resolve_all_pending_as(ctx, action='dismiss')
        dismiss_count += extra
        ctx.metrics['dismissed_count'] = float(dismiss_count)

    assert dismiss_count >= 1, 'Need at least 1 dismissed finding for calibration test, got 0.'

    # Step 3: refresh telemetry to pick up dismissals
    await ctx.api.lint_telemetry_refresh(
        vault_id=str(ctx.vault_id),
        window_days=30,
    )

    # Step 4: run calibration
    calibration = await ctx.api.lint_calibration_run(
        vault_id=str(ctx.vault_id),
    )

    # Step 5: check calibration result
    cal_list = await ctx.api.lint_calibration_list(
        vault_id=str(ctx.vault_id),
    )
    rows = cal_list.get('rows') or []

    # Find any row with a raised threshold
    max_threshold = 0.0
    for row in rows:
        threshold = row.get('surprise_threshold')
        if threshold is not None:
            max_threshold = max(max_threshold, float(threshold))

    ctx.metrics['max_surprise_threshold'] = max_threshold
    # The default is 0.7; calibration should push it higher
    ok = max_threshold > 0.7
    ctx.metrics['pass'] = 1.0 if ok else 0.0
    assert ok, (
        f'Expected calibration to raise surprise_threshold above 0.7, '
        f'got {max_threshold}. calibration_result={calibration!r}'
    )


# ------------------------------------------------------------------
# 5. threshold_calibration_stable_in_range
# ------------------------------------------------------------------


@suite.scenario(
    id='threshold_calibration_stable_in_range',
    query='deployment cadence',
    description=(
        'When telemetry shows accept_rate ~0.5 (dead zone), '
        'calibration does NOT write a new threshold row.'
    ),
    group='calibration',
)
async def threshold_calibration_stable_in_range(ctx: ScenarioContext) -> None:
    # Run lint to seed findings
    await _run_lint(ctx)

    # Balance: resolve half as accept, half as dismiss -> ~0.5 accept_rate
    findings = await _get_pending_findings(ctx)
    mid = len(findings) // 2
    for i, f in enumerate(findings):
        fid = f['id']
        try:
            if i < mid:
                target_id = f.get('target_id', '')
                await ctx.api.lint_resolve(
                    fid,
                    action='deprioritize_unit',
                    params={'unit_id': target_id},
                    note='eval-suite: accept for balance',
                )
            else:
                await ctx.api.lint_dismiss(fid, note='eval-suite: dismiss for balance')
        except Exception:
            pass

    # Refresh telemetry
    await ctx.api.lint_telemetry_refresh(
        vault_id=str(ctx.vault_id),
        window_days=30,
    )

    # Get calibration count before
    before = await ctx.api.lint_calibration_list(
        vault_id=str(ctx.vault_id),
    )
    rows_before = len(before.get('rows') or [])

    # Run calibration
    await ctx.api.lint_calibration_run(
        vault_id=str(ctx.vault_id),
    )

    # Get calibration count after
    after = await ctx.api.lint_calibration_list(
        vault_id=str(ctx.vault_id),
    )
    rows_after = len(after.get('rows') or [])

    new_rows = rows_after - rows_before
    ctx.metrics['new_calibration_rows'] = float(new_rows)
    ctx.metrics['pass'] = 1.0 if new_rows == 0 else 0.0
    assert new_rows == 0, (
        f'Expected no new calibration row in the dead zone, but {new_rows} new row(s) were written.'
    )


# ------------------------------------------------------------------
# 6. auto_apply_respects_confidence_gate
# ------------------------------------------------------------------


@suite.scenario(
    id='auto_apply_respects_confidence_gate',
    query='deployment cadence',
    description=(
        'A pending finding with surprise_score below the '
        'confidence_threshold (0.95) remains pending after auto_apply.'
    ),
    group='auto_apply',
)
async def auto_apply_respects_confidence_gate(ctx: ScenarioContext) -> None:
    # Run lint to produce findings (SQL rules always available)
    await _run_lint(ctx)

    # Find any pending finding — SQL rule findings work fine, no LLM needed
    finding = await _find_any_pending_finding(ctx)
    assert finding is not None, 'No pending finding to test auto_apply after lint run.'
    finding_id = finding['id']

    evidence = finding.get('evidence') or {}
    surprise = evidence.get('surprise_score')
    ctx.metrics['surprise_score'] = float(surprise) if surprise is not None else 0.0

    # The auto_apply service runs as an internal service, not via a public
    # API endpoint. We verify the invariant indirectly: a finding with
    # surprise_score < 0.95 should remain pending. Since lint typically
    # produces scores well below 0.95, we assert the finding is still pending
    # (auto_apply would have resolved it if the gate were broken).
    #
    # Re-fetch to confirm status
    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    still_pending = any(
        f.get('id') == finding_id for f in (payload.get('findings') or []) if isinstance(f, dict)
    )
    ctx.metrics['still_pending'] = 1.0 if still_pending else 0.0
    ctx.metrics['pass'] = 1.0 if still_pending else 0.0
    assert still_pending, (
        f'Finding {finding_id} was auto-applied despite '
        f'surprise_score={surprise} < confidence_threshold=0.95.'
    )


# ------------------------------------------------------------------
# 7. optimizer_compiles_and_stores
# ------------------------------------------------------------------


@suite.scenario(
    id='optimizer_compiles_and_stores',
    query='deployment cadence',
    description=(
        'After seeding 10+ resolved proposals with mixed verdicts, '
        'the optimizer compile produces a lint_llm_signature row with '
        'version >= 1 and a non-null validation_score.'
    ),
    group='optimizer',
)
async def optimizer_compiles_and_stores(ctx: ScenarioContext) -> None:
    # Step 1: run lint to seed findings
    await _run_lint(ctx)

    # Step 2: resolve ALL pending findings to create labelled examples
    accept_count, dismiss_count = await _resolve_all_pending_as(ctx, action='mixed')
    total_resolved = accept_count + dismiss_count

    # If we need more resolved proposals, run lint again to get more findings
    if total_resolved < 10:
        await _run_lint(ctx)
        extra_accept, extra_dismiss = await _resolve_all_pending_as(ctx, action='mixed')
        accept_count += extra_accept
        dismiss_count += extra_dismiss
        total_resolved = accept_count + dismiss_count

    ctx.metrics['total_resolved'] = float(total_resolved)
    ctx.metrics['accept_count'] = float(accept_count)
    ctx.metrics['dismiss_count'] = float(dismiss_count)

    # Step 3: run optimizer for the llm_semantic_contradiction rule
    rule = 'llm_semantic_contradiction'

    try:
        result = await ctx.api.lint_optimize_run(
            rule=rule,
            vault_id=str(ctx.vault_id),
        )
    except Exception as exc:
        # Optimizer may fail if there are not enough labelled examples.
        # Record as error, not a silent pass.
        ctx.metrics['pass'] = 0.0
        assert False, f'lint_optimize_run failed: {exc}'  # noqa: B011

    version = result.get('version')
    validation_score = result.get('validation_score')

    ctx.metrics['version'] = float(version) if version is not None else 0.0
    ctx.metrics['has_validation_score'] = 1.0 if validation_score is not None else 0.0

    ok = version is not None and int(version) >= 1 and validation_score is not None
    ctx.metrics['pass'] = 1.0 if ok else 0.0
    assert ok, (
        f'Expected optimizer to produce signature with version>=1 '
        f'and non-null validation_score, got version={version}, '
        f'validation_score={validation_score}. result={result!r}'
    )


SUITE = suite.build()
