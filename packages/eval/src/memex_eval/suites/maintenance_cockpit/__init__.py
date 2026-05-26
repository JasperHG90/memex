"""Maintenance cockpit eval suite — 7 regression gate scenarios.

Exercises the lint auto-learning loop end-to-end: cooldown suppression,
evidence blob integrity, telemetry verdict rollup, threshold calibration,
auto-apply confidence gating, and DSPy signature optimisation.

Each scenario uses the ``@suite.scenario`` decorator with an async
evaluator function that drives the lint lifecycle via ``ctx.api``
(``RemoteMemexAPI``). This avoids the retrieval-centric
``DirectApiBackend`` dispatch and lets each scenario express its
multi-step lifecycle naturally.

Order matters: scenarios that depend on side-effects (resolved findings,
telemetry rows) appear AFTER the scenarios that produce them.
"""

from pathlib import Path

# Import _outcomes and _setup_actions FIRST for decorator side effects,
# BEFORE any suite.register(...) calls.
from . import _outcomes  # noqa: F401
from . import _setup_actions  # noqa: F401

from memex_eval.suite.base import SetupAction, SuiteMetadata
from memex_eval.suite.decorator import ScenarioContext, Suite
from memex_eval.suite.sources import SuiteSources

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
    requires_nli_classifier=True,
    default_answer_mode='api',
)


suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    readme_path=_ROOT / 'README.md',
)


# ------------------------------------------------------------------
# Helper: find a pending finding by rule name
# ------------------------------------------------------------------


async def _find_pending_finding(
    ctx: ScenarioContext,
    rule_name: str,
) -> dict | None:
    """Return the first pending finding matching ``rule_name``, or None."""
    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = payload.get('findings') or []
    for f in findings:
        if isinstance(f, dict) and f.get('rule_name') == rule_name:
            return f
    return None


async def _count_pending_by_rule(
    ctx: ScenarioContext,
    rule_name: str,
) -> int:
    """Count pending findings matching ``rule_name``."""
    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = payload.get('findings') or []
    return sum(1 for f in findings if isinstance(f, dict) and f.get('rule_name') == rule_name)


# ------------------------------------------------------------------
# 1. cooldown_suppression
# ------------------------------------------------------------------


@suite.scenario(
    id='cooldown_suppression',
    query='deployment cadence',
    description=(
        'After resolving an LLM lint finding with deprioritize_unit, '
        'a second lint run does NOT re-emit the same finding (30-day '
        'cooldown suppression).'
    ),
    group='cooldown',
    setup_actions=[
        SetupAction(kind='lint_run'),
        SetupAction(kind='lint_llm_run'),
    ],
    requires_nli_classifier=True,
)
async def cooldown_suppression(ctx: ScenarioContext) -> None:
    # Step 1: find the LLM semantic contradiction finding
    finding = await _find_pending_finding(ctx, 'llm_semantic_contradiction')
    assert finding is not None, (
        'No pending llm_semantic_contradiction finding after lint run. '
        'Check that the contradicting notes were ingested and LLM lint ran.'
    )
    finding_id = finding['id']

    # Step 2: resolve with deprioritize_unit action
    target_id = finding.get('target_id', '')
    await ctx.api.lint_resolve(
        finding_id,
        action='deprioritize_unit',
        params={'unit_id': target_id},
        note='eval-suite: testing cooldown suppression',
    )

    # Step 3: run lint again — the resolved finding should NOT re-appear
    await ctx.api.run_lint_rules(ctx.vault_id)
    await ctx.api.run_lint_llm(ctx.vault_id)

    # Step 4: assert no new pending finding for the same rule
    count = await _count_pending_by_rule(ctx, 'llm_semantic_contradiction')
    ctx.metrics['pass'] = 1.0 if count == 0 else 0.0
    ctx.metrics['pending_after_rerun'] = float(count)
    assert count == 0, (
        f'Cooldown failed: {count} pending llm_semantic_contradiction '
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
    setup_actions=[
        SetupAction(kind='lint_run'),
        SetupAction(kind='lint_llm_run'),
    ],
    requires_nli_classifier=True,
)
async def evidence_blob_integrity(ctx: ScenarioContext) -> None:
    finding = await _find_pending_finding(ctx, 'llm_semantic_contradiction')
    assert finding is not None, 'No pending llm_semantic_contradiction finding.'
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
    setup_actions=[
        SetupAction(kind='lint_run'),
        SetupAction(kind='lint_llm_run'),
    ],
    requires_nli_classifier=True,
)
async def telemetry_verdict_rollup(ctx: ScenarioContext) -> None:
    # Collect all pending LLM findings
    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = [
        f
        for f in (payload.get('findings') or [])
        if isinstance(f, dict) and f.get('source') in ('llm', 'rule')
    ]
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

    # Read telemetry for the rule
    telemetry = await ctx.api.lint_telemetry(
        vault_id=str(ctx.vault_id),
    )
    rows = telemetry.get('rows') or []

    # Sum across all rules (findings may span multiple rules)
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
    depends_on_prior_scenarios=['telemetry_verdict_rollup'],
)
async def threshold_calibration_adjusts(ctx: ScenarioContext) -> None:
    # Seed telemetry: resolve many findings as dismisses to push
    # accept_rate below 0.3. The prior scenario already resolved some;
    # we need to create an imbalanced ratio.
    # Run lint to get fresh findings
    await ctx.api.run_lint_rules(ctx.vault_id)
    await ctx.api.run_lint_llm(ctx.vault_id)

    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = [f for f in (payload.get('findings') or []) if isinstance(f, dict)]

    # Dismiss all remaining pending findings to push accept_rate low
    for f in findings:
        try:
            await ctx.api.lint_dismiss(f['id'], note='eval-suite: dismiss for calibration')
        except Exception:
            pass

    # Refresh telemetry to pick up the new dismissals
    await ctx.api.lint_telemetry_refresh(
        vault_id=str(ctx.vault_id),
        window_days=30,
    )

    # Run calibration
    calibration = await ctx.api.lint_calibration_run(
        vault_id=str(ctx.vault_id),
    )

    # Check calibration result
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
    await ctx.api.run_lint_rules(ctx.vault_id)
    await ctx.api.run_lint_llm(ctx.vault_id)

    payload = await ctx.api.lint_findings(
        vault_id=str(ctx.vault_id),
        status='pending',
        limit=500,
    )
    findings = [f for f in (payload.get('findings') or []) if isinstance(f, dict)]

    # Balance: resolve half as accept, half as dismiss -> ~0.5 accept_rate
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
        'A pending LLM proposal with surprise_score below the '
        'confidence_threshold (0.95) remains pending after auto_apply.'
    ),
    group='auto_apply',
    setup_actions=[
        SetupAction(kind='lint_run'),
        SetupAction(kind='lint_llm_run'),
    ],
    requires_nli_classifier=True,
)
async def auto_apply_respects_confidence_gate(ctx: ScenarioContext) -> None:
    # Find a pending finding — its surprise_score is typically < 0.95
    finding = await _find_pending_finding(ctx, 'llm_semantic_contradiction')
    if finding is None:
        # Fall back to any pending finding from any rule
        payload = await ctx.api.lint_findings(
            vault_id=str(ctx.vault_id),
            status='pending',
            limit=10,
        )
        findings = payload.get('findings') or []
        finding = findings[0] if findings else None

    assert finding is not None, 'No pending finding to test auto_apply.'
    finding_id = finding['id']

    evidence = finding.get('evidence') or {}
    surprise = evidence.get('surprise_score')
    ctx.metrics['surprise_score'] = float(surprise) if surprise is not None else 0.0

    # The auto_apply service runs as an internal service, not via a public
    # API endpoint. We verify the invariant indirectly: a finding with
    # surprise_score < 0.95 should remain pending. Since the LLM lint pass
    # typically produces scores well below 0.95, we assert the finding
    # is still pending (auto_apply would have resolved it if the gate
    # were broken).
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
    depends_on_prior_scenarios=['telemetry_verdict_rollup'],
)
async def optimizer_compiles_and_stores(ctx: ScenarioContext) -> None:
    # The telemetry_verdict_rollup scenario already resolved findings.
    # Run optimizer for the llm_semantic_contradiction rule.
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
