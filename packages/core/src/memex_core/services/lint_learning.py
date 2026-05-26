"""Lint auto-learning loop — Layers 2 + 3.

Layer 2 (telemetry): reads ``maintenance_proposals`` and aggregates
resolved rows into ``lint_rule_telemetry``.

Layer 3 (threshold calibration): reads telemetry, adjusts per-rule
emission thresholds in ``lint_rule_calibration``. LLM checks read
the latest unsuperseded row per (rule_name, vault_id) at emission time
instead of the static config default.

Verdict classification:

* ``accept``  — ``status='resolved'`` and ``evidence.resolution.followup.action``
  is set and the action_id is NOT ``no_op``. The operator picked a canned
  remediation; the rule's signal was useful.
* ``no_op``   — ``status='resolved'`` and ``followup.action = 'no_op'``.
  The operator reviewed but chose not to mutate state.
* ``dismiss`` — ``status='dismissed'``. Operator judged the rule wrong.
* ``legacy``  — ``status='resolved'`` but no ``resolution.followup`` block.
  Pre-cockpit rows; cannot be classified accept vs no_op. Counted
  separately so operators can see how much history is unlabelled.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from memex_core.services.base import BaseService


@dataclass(frozen=True)
class LintRuleTelemetryDTO:
    """Read-side projection of ``lint_rule_telemetry``."""

    rule_name: str
    vault_id: UUID | None
    window_start: datetime
    window_end: datetime
    accept_count: int
    no_op_count: int
    dismiss_count: int
    legacy_count: int
    median_surprise: float | None
    median_time_to_resolve_seconds: int | None
    refreshed_at: datetime

    @property
    def total_count(self) -> int:
        return self.accept_count + self.no_op_count + self.dismiss_count + self.legacy_count

    @property
    def labelled_count(self) -> int:
        """Verdicts the loop CAN learn from — accept / no_op / dismiss."""
        return self.accept_count + self.no_op_count + self.dismiss_count

    @property
    def accept_rate(self) -> float | None:
        """Fraction of labelled verdicts where the operator engaged positively.

        Both ``accept`` (canned action ran) AND ``no_op`` (operator reviewed
        and acknowledged) count as positive engagement. Only ``dismiss``
        counts as negative — the operator said the rule was wrong.

        This matters because rules like ``sensitive_unreviewed_unit`` have
        ``no_op`` as their primary intended action. Counting ``no_op`` as
        neutral would make those rules show 0% accept rate, causing the
        calibration loop to suppress rules the operator IS using.
        """
        if self.labelled_count == 0:
            return None
        return (self.accept_count + self.no_op_count) / self.labelled_count


@dataclass(frozen=True)
class RefreshResult:
    """Summary returned by ``refresh_telemetry``."""

    rows_written: int
    rules_seen: int
    proposals_aggregated: int
    window_start: datetime
    window_end: datetime
    vault_id: UUID | None
    warnings: list[str] = field(default_factory=list)


# Resolves a single row of `maintenance_proposals` into one of
# {accept, no_op, dismiss, legacy} for the verdict counters. Centralised so
# the unit test can exercise the classification logic against synthetic
# dicts without a Postgres roundtrip.
def classify_verdict(row: dict[str, Any]) -> str:
    status = row.get('status')
    if status == 'dismissed':
        return 'dismiss'
    if status != 'resolved':
        # Pending rows aren't aggregated; defensive return otherwise.
        return 'legacy'
    evidence = row.get('evidence') or {}
    if not isinstance(evidence, dict):
        return 'legacy'
    resolution = evidence.get('resolution')
    if not isinstance(resolution, dict):
        return 'legacy'
    followup = resolution.get('followup')
    if not isinstance(followup, dict):
        return 'legacy'
    action = followup.get('action')
    if not action:
        return 'legacy'
    if action == 'no_op':
        return 'no_op'
    return 'accept'


# Raw aggregate query: returns the list of resolved proposals for a window
# with enough fields to classify each verdict and compute medians. We do
# the classification client-side rather than in SQL — JSON path queries
# across versions of Postgres + asyncpg get hairy, and the row counts
# involved here (single-vault windows) are well under 10k typically.
_FETCH_SQL = """
    SELECT
        rule_name,
        vault_id::text AS vault_id,
        status,
        evidence,
        created_at,
        resolved_at,
        (evidence ->> 'surprise_score')::float AS surprise_score
    FROM maintenance_proposals
    WHERE created_at >= :window_start
      AND created_at < :window_end
      AND status IN ('resolved', 'dismissed')
      AND (:vault_id IS NULL OR vault_id = CAST(:vault_id AS uuid))
"""


_UPSERT_SQL = """
    INSERT INTO lint_rule_telemetry (
        rule_name, vault_id, window_start, window_end,
        accept_count, no_op_count, dismiss_count, legacy_count,
        median_surprise, median_time_to_resolve_seconds, refreshed_at
    )
    VALUES (
        :rule_name, CAST(:vault_id AS uuid), :window_start, :window_end,
        :accept_count, :no_op_count, :dismiss_count, :legacy_count,
        :median_surprise, :median_time_to_resolve_seconds, now()
    )
    ON CONFLICT (rule_name, vault_id, window_start) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        accept_count = EXCLUDED.accept_count,
        no_op_count = EXCLUDED.no_op_count,
        dismiss_count = EXCLUDED.dismiss_count,
        legacy_count = EXCLUDED.legacy_count,
        median_surprise = EXCLUDED.median_surprise,
        median_time_to_resolve_seconds = EXCLUDED.median_time_to_resolve_seconds,
        refreshed_at = now()
"""


# Variant of the upsert for the global rollup row (vault_id IS NULL). The
# composite PK includes vault_id, and Postgres treats NULL as distinct
# from NULL for uniqueness — so a plain ON CONFLICT clause does not
# match the existing global row. We DELETE the prior global row first
# inside the same transaction.
_DELETE_GLOBAL_SQL = """
    DELETE FROM lint_rule_telemetry
    WHERE rule_name = :rule_name
      AND vault_id IS NULL
      AND window_start = :window_start
"""


_INSERT_GLOBAL_SQL = """
    INSERT INTO lint_rule_telemetry (
        rule_name, vault_id, window_start, window_end,
        accept_count, no_op_count, dismiss_count, legacy_count,
        median_surprise, median_time_to_resolve_seconds, refreshed_at
    )
    VALUES (
        :rule_name, NULL, :window_start, :window_end,
        :accept_count, :no_op_count, :dismiss_count, :legacy_count,
        :median_surprise, :median_time_to_resolve_seconds, now()
    )
"""


_SELECT_SQL = """
    SELECT
        rule_name,
        vault_id::text AS vault_id,
        window_start,
        window_end,
        accept_count,
        no_op_count,
        dismiss_count,
        legacy_count,
        median_surprise,
        median_time_to_resolve_seconds,
        refreshed_at
    FROM lint_rule_telemetry
    WHERE (CAST(:rule_name AS text) IS NULL OR rule_name = :rule_name)
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND :include_global = TRUE AND vault_id IS NULL)
        OR (CAST(:vault_id AS uuid) IS NOT NULL AND vault_id = CAST(:vault_id AS uuid))
        OR (CAST(:vault_id AS uuid) IS NULL AND :include_global = FALSE)
      )
    ORDER BY window_end DESC, rule_name ASC
"""


class LintLearningService(BaseService):
    """Read + refresh the lint telemetry rollups and Layer 3 calibration.

    Refresh is idempotent: re-running with the same window writes the
    same row contents. Vault-scoped refreshes write per-vault rows AND
    the global (vault_id NULL) row for the same rule keys.
    """

    DEFAULT_WINDOW_DAYS = 30

    async def refresh_telemetry(
        self,
        *,
        vault_id: UUID | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> RefreshResult:
        """Aggregate resolved proposals into ``lint_rule_telemetry`` rows.

        Args:
            vault_id: when supplied, the rollup is scoped to this vault
                AND the global cross-vault row is also refreshed. When
                None, only the global row is refreshed (across all
                vaults' resolved findings).
            window_days: rolling window length. Defaults to 30 days.
            now: window end. Defaults to ``now(UTC)``; primarily for tests.
        """
        if window_days <= 0:
            raise ValueError('window_days must be positive')
        end = now or datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)

        async with self.metastore.session() as session:
            result = await session.execute(
                text(_FETCH_SQL),
                {
                    'window_start': start,
                    'window_end': end,
                    'vault_id': str(vault_id) if vault_id is not None else None,
                },
            )
            rows = [dict(row) for row in result.mappings().all()]

        # Two rollups in lock-step: per-vault (if vault_id supplied) and
        # global. The per-vault uses ON CONFLICT (rule, vault, window) DO
        # UPDATE; the global uses DELETE + INSERT because NULL-vault PK
        # is "distinct from NULL" in Postgres.
        per_vault: dict[str, _Bucket] = {}
        global_buckets: dict[str, _Bucket] = {}
        for row in rows:
            rule = row.get('rule_name')
            if not rule:
                continue
            verdict = classify_verdict(row)
            ttr = _resolve_ttr(row)
            row_vault = row.get('vault_id')
            global_buckets.setdefault(rule, _Bucket()).observe(
                verdict, row.get('surprise_score'), ttr
            )
            if vault_id is not None and row_vault == str(vault_id):
                per_vault.setdefault(rule, _Bucket()).observe(
                    verdict, row.get('surprise_score'), ttr
                )

        rules_seen = set(global_buckets.keys()) | set(per_vault.keys())
        rows_written = 0

        async with self.metastore.session() as session:
            for rule, bucket in per_vault.items():
                await session.execute(
                    text(_UPSERT_SQL),
                    bucket.upsert_params(
                        rule_name=rule,
                        vault_id=str(vault_id) if vault_id is not None else None,
                        window_start=start,
                        window_end=end,
                    ),
                )
                rows_written += 1
            for rule, bucket in global_buckets.items():
                await session.execute(
                    text(_DELETE_GLOBAL_SQL),
                    {'rule_name': rule, 'window_start': start},
                )
                await session.execute(
                    text(_INSERT_GLOBAL_SQL),
                    bucket.upsert_params(
                        rule_name=rule,
                        vault_id=None,
                        window_start=start,
                        window_end=end,
                    ),
                )
                rows_written += 1
            await session.commit()

        return RefreshResult(
            rows_written=rows_written,
            rules_seen=len(rules_seen),
            proposals_aggregated=len(rows),
            window_start=start,
            window_end=end,
            vault_id=vault_id,
        )

    async def get_telemetry(
        self,
        *,
        rule_name: str | None = None,
        vault_id: UUID | None = None,
        include_global: bool = True,
    ) -> list[LintRuleTelemetryDTO]:
        """Read rollup rows.

        Args:
            rule_name: optional filter; when None, every rule is returned.
            vault_id: optional vault scope. When supplied, returns rows for
                that vault. When None and ``include_global=True``, returns
                only the global rollup. When None and ``include_global=False``,
                returns every per-vault row (across all vaults).
        """
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_SELECT_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': str(vault_id) if vault_id is not None else None,
                    'include_global': include_global,
                },
            )
            rows = result.mappings().all()
        return [_dto_from_row(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Layer 3 — Threshold calibration (methods below)
    # ------------------------------------------------------------------

    async def calibrate_thresholds(
        self,
        *,
        vault_id: UUID | None = None,
    ) -> CalibrationResult:
        """Read telemetry for every rule, compute new thresholds, write calibration rows.

        Idempotent per run: if telemetry hasn't changed since the last
        calibration, no new rows are written (the ``reason`` will say
        'unchanged').
        """
        telemetry_rows = await self.get_telemetry(
            vault_id=vault_id, include_global=vault_id is None
        )

        details: list[dict[str, Any]] = []
        calibrated = 0
        skipped_frozen = 0
        skipped_insufficient = 0
        unchanged = 0

        async with self.metastore.session() as session:
            for trow in telemetry_rows:
                rule = trow.rule_name
                v_id = str(trow.vault_id) if trow.vault_id else None

                # Load current calibration for this rule.
                result = await session.execute(
                    text(_GET_LATEST_CALIBRATION_SQL),
                    {'rule_name': rule, 'vault_id': v_id},
                )
                current_row = result.mappings().first()

                if current_row and current_row['frozen']:
                    skipped_frozen += 1
                    details.append({'rule': rule, 'status': 'frozen'})
                    continue

                current_threshold = (
                    float(current_row['surprise_threshold'])
                    if current_row and current_row['surprise_threshold'] is not None
                    else DEFAULT_SURPRISE_THRESHOLD
                )
                current_version = int(current_row['version']) if current_row else 0

                if trow.accept_rate is None:
                    skipped_insufficient += 1
                    details.append({'rule': rule, 'status': 'no_labelled_data'})
                    continue

                new_threshold, reason = _compute_new_threshold(
                    current_threshold, trow.accept_rate, trow.labelled_count
                )

                if new_threshold is None:
                    unchanged += 1
                    details.append({'rule': rule, 'status': 'unchanged', 'reason': reason})
                    continue

                new_version = current_version + 1
                rationale = {
                    'accept_rate': trow.accept_rate,
                    'labelled_count': trow.labelled_count,
                    'previous_threshold': current_threshold,
                    'new_threshold': new_threshold,
                    'reason': reason,
                }

                # Supersede all prior unsuperseded rows for this rule.
                await session.execute(
                    text(_SUPERSEDE_CALIBRATION_SQL),
                    {'rule_name': rule, 'vault_id': v_id, 'new_version': new_version},
                )

                # Insert the new calibration row.
                await session.execute(
                    text(_INSERT_CALIBRATION_SQL),
                    {
                        'rule_name': rule,
                        'vault_id': v_id,
                        'version': new_version,
                        'surprise_threshold': new_threshold,
                        'polarity_threshold': None,
                        'learned_from_window_start': trow.window_start,
                        'learned_from_window_end': trow.window_end,
                        'frozen': False,
                        'rationale': _json.dumps(rationale),
                    },
                )

                calibrated += 1
                details.append(
                    {
                        'rule': rule,
                        'status': 'calibrated',
                        'version': new_version,
                        'old_threshold': current_threshold,
                        'new_threshold': new_threshold,
                        'reason': reason,
                    }
                )

            await session.commit()

        return CalibrationResult(
            rules_calibrated=calibrated,
            rules_skipped_frozen=skipped_frozen,
            rules_skipped_insufficient_data=skipped_insufficient,
            rules_unchanged=unchanged,
            details=details,
        )

    async def get_calibrations(
        self,
        *,
        rule_name: str | None = None,
        vault_id: UUID | None = None,
    ) -> list[CalibrationDTO]:
        """Read calibration rows."""
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_LIST_CALIBRATIONS_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': str(vault_id) if vault_id else None,
                },
            )
            rows = result.mappings().all()
        return [_cal_dto(dict(r)) for r in rows]

    async def get_threshold(
        self,
        rule_name: str,
        vault_id: UUID | None = None,
    ) -> float:
        """Return the active surprise_threshold for a rule.

        Falls back to DEFAULT_SURPRISE_THRESHOLD when no calibration row
        exists. This is the method LLM checks call at emission time.
        """
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_GET_LATEST_CALIBRATION_SQL),
                {'rule_name': rule_name, 'vault_id': str(vault_id) if vault_id else None},
            )
            row = result.mappings().first()
        if row and row['surprise_threshold'] is not None:
            return float(row['surprise_threshold'])
        return DEFAULT_SURPRISE_THRESHOLD

    async def freeze_rule(
        self,
        rule_name: str,
        *,
        vault_id: UUID | None = None,
        frozen: bool = True,
    ) -> bool:
        """Set or clear the frozen flag on the active calibration row."""
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_FREEZE_CALIBRATION_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': str(vault_id) if vault_id else None,
                    'frozen': frozen,
                },
            )
            await session.commit()
        return bool(result.rowcount)

    async def rollback_calibration(
        self,
        rule_name: str,
        version: int,
        *,
        vault_id: UUID | None = None,
    ) -> bool:
        """Rollback to a specific calibration version.

        Marks all later versions as superseded and un-supersedes the target.
        """
        v_id = str(vault_id) if vault_id else None
        async with self.metastore.session() as session:
            # Mark everything after `version` as superseded.
            await session.execute(
                text(_SUPERSEDE_LATER_VERSIONS_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'version': version},
            )
            # Un-supersede the target version.
            result = await session.execute(
                text(_ROLLBACK_CALIBRATION_SQL),
                {'rule_name': rule_name, 'vault_id': v_id, 'version': version},
            )
            await session.commit()
        return bool(result.rowcount)


@dataclass
class _Bucket:
    """In-memory accumulator for one (rule, vault) rollup."""

    accept: int = 0
    no_op: int = 0
    dismiss: int = 0
    legacy: int = 0
    surprises: list[float] = field(default_factory=list)
    ttr_seconds: list[int] = field(default_factory=list)

    def observe(self, verdict: str, surprise: float | None, ttr: int | None) -> None:
        if verdict == 'accept':
            self.accept += 1
        elif verdict == 'no_op':
            self.no_op += 1
        elif verdict == 'dismiss':
            self.dismiss += 1
        else:
            self.legacy += 1
        if surprise is not None:
            self.surprises.append(float(surprise))
        if ttr is not None and ttr >= 0:
            self.ttr_seconds.append(ttr)

    def upsert_params(
        self,
        *,
        rule_name: str,
        vault_id: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        return {
            'rule_name': rule_name,
            'vault_id': vault_id,
            'window_start': window_start,
            'window_end': window_end,
            'accept_count': self.accept,
            'no_op_count': self.no_op,
            'dismiss_count': self.dismiss,
            'legacy_count': self.legacy,
            'median_surprise': _median(self.surprises),
            'median_time_to_resolve_seconds': _median_int(self.ttr_seconds),
        }


def _resolve_ttr(row: dict[str, Any]) -> int | None:
    created = row.get('created_at')
    resolved = row.get('resolved_at')
    if not created or not resolved:
        return None
    try:
        delta = resolved - created
    except TypeError:
        return None
    return int(delta.total_seconds())


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _median_int(values: list[int]) -> int | None:
    m = _median([float(v) for v in values])
    if m is None:
        return None
    return int(m)


def _dto_from_row(row: dict[str, Any]) -> LintRuleTelemetryDTO:
    vault_raw = row.get('vault_id')
    return LintRuleTelemetryDTO(
        rule_name=row['rule_name'],
        vault_id=UUID(vault_raw) if vault_raw else None,
        window_start=row['window_start'],
        window_end=row['window_end'],
        accept_count=int(row.get('accept_count') or 0),
        no_op_count=int(row.get('no_op_count') or 0),
        dismiss_count=int(row.get('dismiss_count') or 0),
        legacy_count=int(row.get('legacy_count') or 0),
        median_surprise=row.get('median_surprise'),
        median_time_to_resolve_seconds=row.get('median_time_to_resolve_seconds'),
        refreshed_at=row['refreshed_at'],
    )


# ---------------------------------------------------------------------------
# Layer 3 — Threshold calibration
# ---------------------------------------------------------------------------

# Default thresholds (used when no calibration row exists for a rule).
DEFAULT_SURPRISE_THRESHOLD = 0.7
DEFAULT_POLARITY_THRESHOLD = 0.5

# Calibration boundaries — never lower below floor or raise above ceiling.
THRESHOLD_FLOOR = 0.5
THRESHOLD_CEILING = 0.95
THRESHOLD_STEP = 0.05
THRESHOLD_MAX_STEP_PER_RUN = 0.1

# Minimum labelled verdicts before calibration kicks in.
MIN_LABELLED_FOR_CALIBRATION = 30

# Accept-rate boundaries that trigger threshold adjustment.
LOW_ACCEPT_RATE = 0.3
HIGH_ACCEPT_RATE = 0.8


@dataclass(frozen=True)
class CalibrationDTO:
    """Read-side projection of ``lint_rule_calibration``."""

    id: UUID
    rule_name: str
    vault_id: UUID | None
    version: int
    surprise_threshold: float | None
    polarity_threshold: float | None
    learned_at: datetime
    learned_from_window_start: datetime | None
    learned_from_window_end: datetime | None
    superseded_by_version: int | None
    frozen: bool
    rationale: dict[str, Any] | None


@dataclass(frozen=True)
class CalibrationResult:
    """Summary returned by ``calibrate_thresholds``."""

    rules_calibrated: int
    rules_skipped_frozen: int
    rules_skipped_insufficient_data: int
    rules_unchanged: int
    details: list[dict[str, Any]]


_GET_LATEST_CALIBRATION_SQL = """
    SELECT id::text, rule_name, vault_id::text, version,
           surprise_threshold, polarity_threshold,
           learned_at, learned_from_window_start, learned_from_window_end,
           superseded_by_version, frozen, rationale
    FROM lint_rule_calibration
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND superseded_by_version IS NULL
    ORDER BY version DESC
    LIMIT 1
"""

_LIST_CALIBRATIONS_SQL = """
    SELECT id::text, rule_name, vault_id::text, version,
           surprise_threshold, polarity_threshold,
           learned_at, learned_from_window_start, learned_from_window_end,
           superseded_by_version, frozen, rationale
    FROM lint_rule_calibration
    WHERE (CAST(:rule_name AS text) IS NULL OR rule_name = :rule_name)
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
    ORDER BY rule_name ASC, version DESC
"""

_INSERT_CALIBRATION_SQL = """
    INSERT INTO lint_rule_calibration (
        rule_name, vault_id, version,
        surprise_threshold, polarity_threshold,
        learned_from_window_start, learned_from_window_end,
        frozen, rationale
    )
    VALUES (
        :rule_name, CAST(:vault_id AS uuid), :version,
        :surprise_threshold, :polarity_threshold,
        :learned_from_window_start, :learned_from_window_end,
        :frozen, CAST(:rationale AS jsonb)
    )
"""

_SUPERSEDE_CALIBRATION_SQL = """
    UPDATE lint_rule_calibration
    SET superseded_by_version = :new_version
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND superseded_by_version IS NULL
      AND version < :new_version
"""

_FREEZE_CALIBRATION_SQL = """
    UPDATE lint_rule_calibration
    SET frozen = :frozen
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND superseded_by_version IS NULL
"""

_ROLLBACK_CALIBRATION_SQL = """
    UPDATE lint_rule_calibration
    SET superseded_by_version = NULL
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND version = :version
"""

# superseded_by_version semantics (three states):
#   NULL          — active (current calibration row)
#   positive int  — superseded by that version number
#   -1            — rolled back (explicitly reverted by operator)
_SUPERSEDE_LATER_VERSIONS_SQL = """
    UPDATE lint_rule_calibration
    SET superseded_by_version = -1
    WHERE rule_name = :rule_name
      AND (
        (CAST(:vault_id AS uuid) IS NULL AND vault_id IS NULL)
        OR vault_id = CAST(:vault_id AS uuid)
      )
      AND version > :version
      AND (superseded_by_version IS NULL OR superseded_by_version = -1)
"""


def _compute_new_threshold(
    current: float,
    accept_rate: float,
    n_labelled: int,
) -> tuple[float | None, str]:
    """Return (new_threshold, reason) or (None, reason) if no change is warranted.

    When ``n_labelled < MIN_LABELLED_FOR_CALIBRATION``, the function proceeds
    with the adjustment but annotates the reason with a ``low_sample_size``
    warning so callers can surface it.
    """
    low_sample_warning = ''
    if n_labelled < MIN_LABELLED_FOR_CALIBRATION:
        low_sample_warning = (
            f' [warning:low_sample_size n={n_labelled} < {MIN_LABELLED_FOR_CALIBRATION}]'
        )

    if accept_rate < LOW_ACCEPT_RATE:
        delta = min(THRESHOLD_STEP, THRESHOLD_MAX_STEP_PER_RUN)
        new = min(current + delta, THRESHOLD_CEILING)
        if new == current:
            return None, f'already at ceiling ({THRESHOLD_CEILING}){low_sample_warning}'
        return (
            new,
            f'accept_rate={accept_rate:.2f} < {LOW_ACCEPT_RATE}'
            f' → raised by {delta}{low_sample_warning}',
        )

    if accept_rate > HIGH_ACCEPT_RATE:
        delta = min(THRESHOLD_STEP, THRESHOLD_MAX_STEP_PER_RUN)
        new = max(current - delta, THRESHOLD_FLOOR)
        if new == current:
            return None, f'already at floor ({THRESHOLD_FLOOR}){low_sample_warning}'
        return (
            new,
            f'accept_rate={accept_rate:.2f} > {HIGH_ACCEPT_RATE}'
            f' → lowered by {delta}{low_sample_warning}',
        )

    return (
        None,
        f'accept_rate={accept_rate:.2f} is within'
        f' [{LOW_ACCEPT_RATE}, {HIGH_ACCEPT_RATE}]{low_sample_warning}',
    )


def _cal_dto(row: dict[str, Any]) -> CalibrationDTO:
    vault_raw = row.get('vault_id')
    rationale = row.get('rationale')
    if isinstance(rationale, str):
        try:
            rationale = _json.loads(rationale)
        except _json.JSONDecodeError:
            rationale = None
    return CalibrationDTO(
        id=UUID(row['id']) if isinstance(row['id'], str) else row['id'],
        rule_name=row['rule_name'],
        vault_id=UUID(vault_raw) if vault_raw else None,
        version=int(row['version']),
        surprise_threshold=row.get('surprise_threshold'),
        polarity_threshold=row.get('polarity_threshold'),
        learned_at=row['learned_at'],
        learned_from_window_start=row.get('learned_from_window_start'),
        learned_from_window_end=row.get('learned_from_window_end'),
        superseded_by_version=row.get('superseded_by_version'),
        frozen=bool(row.get('frozen', False)),
        rationale=rationale if isinstance(rationale, dict) else None,
    )
