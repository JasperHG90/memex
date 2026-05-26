"""Lint auto-learning loop — Layer 4: DSPy signature compilation.

Pulls labelled verdicts, compiles a DSPy signature via BootstrapFewShot,
validates against the current champion, and promotes the winner to
``lint_llm_signature``. LLM checks load the latest promoted signature
at startup. The weekly scheduler tick calls ``compile()`` per rule.

Layer 5 (auto-solve) hooks into the scheduler after compilation: when
a compiled signature exists AND the rule's telemetry clears the
confidence + accept_rate thresholds, the scheduler auto-resolves
high-confidence findings via the proposal-action registry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.lint_optimizer')


# Minimum labelled examples before attempting a compile.
MIN_EXAMPLES_FOR_COMPILE = 50
# Champion must beat challenger by this margin to be promoted.
CHAMPION_MARGIN = 0.05
# Max demos per signature (BootstrapFewShot parameter).
MAX_DEMOS = 8


@dataclass(frozen=True)
class CompileResult:
    """Outcome of one ``compile()`` invocation."""

    rule_name: str
    vault_id: UUID | None
    status: str  # 'promoted' | 'rejected' | 'insufficient_data' | 'error'
    new_version: int | None = None
    validation_score: float | None = None
    champion_score: float | None = None
    examples_used: int = 0
    message: str = ''
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SignatureDTO:
    """Read-side projection of ``lint_llm_signature``."""

    id: UUID
    rule_name: str
    vault_id: UUID | None
    version: int
    base_model: str | None
    validation_score: float | None
    validation_examples: int | None
    promoted_at: datetime
    promoted_by: str | None
    superseded_by_version: int | None


_FETCH_LABELLED_SQL = """
    SELECT
        target_id,
        evidence,
        status,
        created_at,
        resolved_at,
        (evidence -> 'resolution' -> 'followup' ->> 'action') AS action_taken,
        (evidence ->> 'surprise_score')::float AS surprise_score
    FROM maintenance_proposals
    WHERE rule_name = :rule_name
      AND status IN ('resolved', 'dismissed')
      AND created_at >= :window_start
      AND (CAST(:vault_id AS uuid) IS NULL OR vault_id = CAST(:vault_id AS uuid))
    ORDER BY resolved_at DESC
    LIMIT :limit
"""

_GET_LATEST_SIGNATURE_SQL = """
    SELECT id::text, rule_name, vault_id::text, version,
           compiled_program, demos, base_model,
           validation_score, validation_examples,
           promoted_at, promoted_by, superseded_by_version
    FROM lint_llm_signature
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND superseded_by_version IS NULL
    ORDER BY version DESC
    LIMIT 1
"""

_LIST_SIGNATURES_SQL = """
    SELECT id::text, rule_name, vault_id::text, version,
           base_model, validation_score, validation_examples,
           promoted_at, promoted_by, superseded_by_version
    FROM lint_llm_signature
    WHERE (CAST(:rule_name AS text) IS NULL OR rule_name = :rule_name)
    ORDER BY rule_name ASC, version DESC
"""

_GET_SIGNATURE_DETAIL_SQL = """
    SELECT id::text, rule_name, vault_id::text, version,
           compiled_program, demos, base_model,
           validation_score, validation_examples,
           promoted_at, promoted_by, superseded_by_version
    FROM lint_llm_signature
    WHERE rule_name = :rule_name
      AND version = :version
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
    LIMIT 1
"""

_INSERT_SIGNATURE_SQL = """
    INSERT INTO lint_llm_signature (
        rule_name, vault_id, version,
        compiled_program, demos, base_model,
        validation_score, validation_examples,
        promoted_by
    )
    VALUES (
        :rule_name, CAST(:vault_id AS uuid), :version,
        CAST(:compiled_program AS jsonb), CAST(:demos AS jsonb), :base_model,
        :validation_score, :validation_examples,
        :promoted_by
    )
"""

_SUPERSEDE_SIGNATURE_SQL = """
    UPDATE lint_llm_signature
    SET superseded_by_version = :new_version
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND superseded_by_version IS NULL
      AND version < :new_version
"""


_ROLLBACK_SIGNATURE_UNSUPERSEDE_SQL = """
    UPDATE lint_llm_signature
    SET superseded_by_version = NULL
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND version = :version
"""

_ROLLBACK_SIGNATURE_SUPERSEDE_LATER_SQL = """
    UPDATE lint_llm_signature
    SET superseded_by_version = -1
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND version > :version
      AND (superseded_by_version IS NULL OR superseded_by_version = -1)
"""


class LintLLMOptimizer(BaseService):
    """DSPy-based signature optimization for LLM lint checks.

    ``compile()`` is the primary entry point: it pulls labelled verdicts
    for a rule, builds training/validation splits, runs the optimizer,
    and promotes or rejects the result.
    """

    async def compile(
        self,
        rule_name: str,
        *,
        vault_id: UUID | None = None,
        window_days: int = 90,
        actor: str = 'system:optimizer',
    ) -> CompileResult:
        """Pull labelled verdicts, compile, validate, promote or reject.

        This method is the scheduler entry point. The actual DSPy compile
        is isolated in ``_run_bootstrap`` so it can be replaced with a
        mock in tests.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)
        v_id = str(vault_id) if vault_id else None

        # 1. Fetch labelled examples.
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_FETCH_LABELLED_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': v_id,
                    'window_start': start,
                    'limit': 200,
                },
            )
            raw_rows = [dict(r) for r in result.mappings().all()]

        compile_warnings: list[str] = []
        if len(raw_rows) < MIN_EXAMPLES_FOR_COMPILE:
            compile_warnings.append(
                f'low_sample_size: have {len(raw_rows)} labelled examples, '
                f'recommended minimum is {MIN_EXAMPLES_FOR_COMPILE}'
            )
            logger.warning(
                'Rule %s: proceeding with %d examples (< %d recommended)',
                rule_name,
                len(raw_rows),
                MIN_EXAMPLES_FOR_COMPILE,
            )

        # 2. Build examples: (target_text_stub, verdict) pairs.
        examples = _build_examples(raw_rows)

        # 3. Temporal train/validation split (80/20).
        # Examples are ordered newest-first (SQL ORDER BY resolved_at DESC).
        # Train on the OLDER 80%, validate on the NEWER 20% — the model's
        # score reflects how well it predicts the operator's current behavior.
        split_idx = int(len(examples) * 0.2)
        validation = examples[:split_idx]  # newest 20%
        train = examples[split_idx:]  # oldest 80%

        if len(validation) < 5:
            compile_warnings.append(
                f'low_validation_set: only {len(validation)} validation examples '
                f'(recommended ≥5). Score may be unreliable.'
            )
        if not train:
            return CompileResult(
                rule_name=rule_name,
                vault_id=vault_id,
                status='insufficient_data',
                examples_used=len(examples),
                message='Zero training examples after split — nothing to compile.',
                warnings=compile_warnings,
            )
        if not validation:
            validation = train[:1]
            compile_warnings.append('no_validation_set: using 1 training example for validation.')

        # 4. Run the optimizer — produces a compiled program + demos.
        try:
            compiled_program, demos, new_score = await self._run_bootstrap(
                rule_name, train, validation
            )
        except Exception as exc:
            logger.exception('DSPy compile failed for rule %s', rule_name)
            return CompileResult(
                rule_name=rule_name,
                vault_id=vault_id,
                status='error',
                message=str(exc),
                warnings=compile_warnings,
            )

        # 5. Load champion.
        async with self.metastore.session() as session:
            champ_row = (
                (
                    await session.execute(
                        text(_GET_LATEST_SIGNATURE_SQL),
                        {'rule_name': rule_name, 'vault_id': v_id},
                    )
                )
                .mappings()
                .first()
            )

        champion_score = float(champ_row['validation_score']) if champ_row else -1.0
        current_version = int(champ_row['version']) if champ_row else 0

        # 6. Champion-vs-challenger gate.
        # No champion → always promote (first compile).
        # New beats champion by margin → promote.
        # Scores tied AND new has more data → promote (more data = more reliable).
        # Otherwise → reject.
        no_champion = champ_row is None
        beats_margin = new_score >= champion_score + CHAMPION_MARGIN
        tied_with_more_data = abs(new_score - champion_score) < CHAMPION_MARGIN and len(
            examples
        ) > (champ_row['validation_examples'] or 0 if champ_row else 0)
        should_promote = no_champion or beats_margin or tied_with_more_data

        if not should_promote:
            return CompileResult(
                rule_name=rule_name,
                vault_id=vault_id,
                status='rejected',
                validation_score=new_score,
                champion_score=champion_score,
                examples_used=len(examples),
                message=(
                    f'New ({new_score:.3f}) did not beat champion '
                    f'({champion_score:.3f}) + margin ({CHAMPION_MARGIN}).'
                ),
                warnings=compile_warnings,
            )

        # 7. Promote.
        new_version = current_version + 1
        async with self.metastore.session() as session:
            await session.execute(
                text(_SUPERSEDE_SIGNATURE_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'new_version': new_version},
            )
            await session.execute(
                text(_INSERT_SIGNATURE_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': v_id,
                    'version': new_version,
                    'compiled_program': json.dumps(compiled_program),
                    'demos': json.dumps(demos),
                    'base_model': 'default',
                    'validation_score': new_score,
                    'validation_examples': len(validation),
                    'promoted_by': actor,
                },
            )
            await session.commit()

        logger.info(
            'Promoted signature v%d for rule %s (score: %.3f → %.3f)',
            new_version,
            rule_name,
            champion_score,
            new_score,
        )

        return CompileResult(
            rule_name=rule_name,
            vault_id=vault_id,
            status='promoted',
            new_version=new_version,
            validation_score=new_score,
            champion_score=champion_score,
            examples_used=len(examples),
            message=f'Promoted v{new_version} (score: {new_score:.3f}).',
            warnings=compile_warnings,
        )

    async def _run_bootstrap(
        self,
        rule_name: str,
        train: list[dict[str, Any]],
        validation: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
        """Run DSPy BootstrapFewShot to produce a compiled program.

        Returns (compiled_program_dict, demos_list, validation_score).

        The actual DSPy invocation requires ``dspy`` + a language model
        configured in the environment. When DSPy is not installed or the
        LM is unavailable, this falls back to a simple majority-class
        baseline that still produces a valid score — so the
        champion-vs-challenger gate is always exercisable.
        """
        try:
            import dspy

            # Build the metric function for BootstrapFewShot.
            def verdict_match(example: dspy.Example, pred: Any, trace: Any = None) -> bool:
                return getattr(pred, 'verdict', '') == example.verdict

            class VerdictPredictor(dspy.Signature):
                """Given a lint finding's target text, rule, and any prior reviewer rationale, predict the operator verdict."""

                target_text: str = dspy.InputField()
                rule_name: str = dspy.InputField()
                surprise_score: float = dspy.InputField()
                rationale: str = dspy.InputField(
                    desc='Reviewer note explaining why they accepted/dismissed (empty if none)',
                )
                verdict: str = dspy.OutputField(desc='One of: accept, no_op, dismiss')

            teleprompter = dspy.BootstrapFewShot(
                metric=verdict_match,
                max_bootstrapped_demos=MAX_DEMOS,
                max_rounds=1,
            )
            trainset = [
                dspy.Example(
                    target_text=ex.get('target_text', ''),
                    rule_name=rule_name,
                    surprise_score=ex.get('surprise_score', 0.0),
                    rationale=ex.get('rationale', ''),
                    verdict=ex['verdict'],
                ).with_inputs('target_text', 'rule_name', 'surprise_score', 'rationale')
                for ex in train
            ]

            student = dspy.Predict(VerdictPredictor)
            compiled = teleprompter.compile(student, trainset=trainset)

            # Validate.
            correct = 0
            for ex in validation:
                try:
                    pred = compiled(
                        target_text=ex.get('target_text', ''),
                        rule_name=rule_name,
                        surprise_score=ex.get('surprise_score', 0.0),
                        rationale=ex.get('rationale', ''),
                    )
                    if getattr(pred, 'verdict', '') == ex['verdict']:
                        correct += 1
                except Exception:
                    pass
            val_score = correct / len(validation) if validation else 0.0

            # Serialise the compiled program's demos.
            demos_list = []
            if hasattr(compiled, 'demos'):
                for demo in compiled.demos:
                    demos_list.append(
                        {
                            'target_text': getattr(demo, 'target_text', ''),
                            'rule_name': getattr(demo, 'rule_name', ''),
                            'surprise_score': getattr(demo, 'surprise_score', 0.0),
                            'rationale': getattr(demo, 'rationale', ''),
                            'verdict': getattr(demo, 'verdict', ''),
                        }
                    )

            return ({'type': 'dspy_bootstrap', 'version': 1}, demos_list, val_score)

        except ImportError:
            logger.warning('DSPy not installed; using majority-class baseline.')
            # Fallback: majority-class baseline.
            from collections import Counter

            counts = Counter(ex['verdict'] for ex in train)
            majority = counts.most_common(1)[0][0] if counts else 'dismiss'
            correct = sum(1 for ex in validation if ex['verdict'] == majority)
            val_score = correct / len(validation) if validation else 0.0
            return (
                {'type': 'majority_baseline', 'majority_class': majority},
                [],
                val_score,
            )

    async def get_signature_detail(
        self,
        rule_name: str,
        version: int,
        *,
        vault_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Fetch full signature detail including ``demos`` and ``compiled_program``.

        Returns a dict with all columns or ``None`` if no matching row exists.
        Used by the CLI ``memex lint signatures show`` and ``diff`` commands.
        """
        v_id = str(vault_id) if vault_id else None
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_GET_SIGNATURE_DETAIL_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'version': version},
            )
            row = result.mappings().first()
        if row is None:
            return None
        r = dict(row)
        # Normalise JSON columns that may come back as strings.
        for col in ('compiled_program', 'demos'):
            val = r.get(col)
            if isinstance(val, str):
                try:
                    r[col] = json.loads(val)
                except json.JSONDecodeError:
                    pass
        return r

    async def list_signatures(
        self,
        *,
        rule_name: str | None = None,
    ) -> list[SignatureDTO]:
        """List all signature versions across rules."""
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_LIST_SIGNATURES_SQL),
                {'rule_name': rule_name},
            )
            rows = result.mappings().all()
        return [
            SignatureDTO(
                id=UUID(r['id']) if isinstance(r['id'], str) else r['id'],
                rule_name=r['rule_name'],
                vault_id=UUID(r['vault_id']) if r.get('vault_id') else None,
                version=int(r['version']),
                base_model=r.get('base_model'),
                validation_score=r.get('validation_score'),
                validation_examples=r.get('validation_examples'),
                promoted_at=r['promoted_at'],
                promoted_by=r.get('promoted_by'),
                superseded_by_version=r.get('superseded_by_version'),
            )
            for r in rows
        ]

    async def rollback_signature(
        self,
        rule_name: str,
        version: int,
        *,
        vault_id: UUID | None = None,
    ) -> bool:
        """Rollback to a specific signature version.

        Marks all versions after ``version`` as superseded and
        un-supersedes the target. Same pattern as calibration rollback.
        """
        v_id = str(vault_id) if vault_id else None
        async with self.metastore.session() as session:
            # Mark everything after `version` as superseded.
            await session.execute(
                text(_ROLLBACK_SIGNATURE_SUPERSEDE_LATER_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'version': version},
            )
            # Un-supersede the target version.
            result = await session.execute(
                text(_ROLLBACK_SIGNATURE_UNSUPERSEDE_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'version': version},
            )
            await session.commit()
        return bool(result.rowcount)


def _build_examples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw proposal rows into training examples including the reviewer's rationale."""
    from memex_core.services.lint_learning import classify_verdict

    examples: list[dict[str, Any]] = []
    for row in rows:
        verdict = classify_verdict(row)
        if verdict == 'legacy':
            continue
        evidence = row.get('evidence') or {}
        target_id = row.get('target_id', '')
        surprise = row.get('surprise_score')
        target_text = (
            evidence.get('explanation') or evidence.get('target_text') or str(target_id)[:200]
        )
        resolution = evidence.get('resolution') or {}
        note = ''
        if isinstance(resolution, dict):
            note = str(resolution.get('note') or '')[:500]
        examples.append(
            {
                'target_text': str(target_text)[:2000],
                'verdict': verdict,
                'surprise_score': float(surprise) if surprise is not None else 0.0,
                'rationale': note,
            }
        )
    return examples
